# Deploying the tutor

This guide takes a copy of this repository whose content you have already made
your own ([ADDING-KNOWLEDGE.md](ADDING-KNOWLEDGE.md), through its section 11)
to a live site your students can sign in to.

The reference deployment is:

* a **Docker web service on Render** (the web app is a long-running FastAPI
  server that streams replies, so it needs a real server, not a static or
  serverless host), with a **persistent disk** for the vector store, lecture
  narration and the voice cache;
* a **Postgres database**, here a **Supabase** project;
* **GitHub** for the code, the seed files (as Release assets) and the scheduled
  jobs (GitHub Actions).

Any host that runs a Docker container with a persistent volume, behind a TLS
proxy, next to any Postgres, will do; the notes in Step 3 say what changes.

Placeholders used below: `<owner>/<repo>` is your GitHub repository,
`https://tutor.example.edu` your public address, and
`your-service.onrender.com` the address Render gives the service.

---

## Overview

```
                 Render web service (Docker image built from this repo)
   ┌─────────────────────────────────────────────────────────────┐
   │  entrypoint.sh: seed the data, migrate the database, start  │
   │  uvicorn tutor.main:app                                     │
   │   ├─ ChromaDB store (read-only) on the persistent disk      │  <- CHROMA_SEED_URL (once)
   │   │    /app/knowledge-base/chromadb                         │
   │   │    ├─ lecture-audio/   narration clips                  │  <- AUDIO_SEED_URL (once)
   │   │    └─ tts-cache/       the chat's voice cache           │
   │   ├─ /app/output           rendered slide decks             │  <- OUTPUT_SEED_URL (every boot)
   │   ├─ curriculum/mastery_exams.json                          │  <- MASTERY_EXAMS_URL (every boot)
   │   ├─ SQLAlchemy  -> DATABASE_URL ───────────────────────────┼──> Postgres (Supabase)
   │   └─ Claude API  -> ANTHROPIC_API_KEY                       │
   └─────────────────────────────────────────────────────────────┘
```

**In the image** (copied from git by the [Dockerfile](Dockerfile)): `tutor/`,
`courses.yaml`, `persona/`, `instructor-profile/`, `curriculum/`,
`explorables/`, `migrations/`, the course book rendered from `book/*.qmd` in a
build stage, and the ONNX embedding model (fetched at build time, so the first
query needs no network). The knowledge base (`knowledge-base/`, raw or built)
and `output/` are kept out of the image by `.dockerignore`.

**Not in the image** (generated, large, or not for a public repository): the
vector store, the rendered decks, the narration audio and the mastery-check
bank. They travel as four **seed files** that `entrypoint.sh` downloads at
boot (Step 2).

The order of the work:

1. Create the database.
2. Build the seed files and publish them.
3. Create the Render service.
4. Fill in its environment variables.
5. Point your domain at it and set up email and sign-in.
6. Configure GitHub Actions (secrets, the example URLs in the workflows, schedules, deploys).
7. Check the first boot.
8. Add instructors, classes and students.

## Before you start

* **Content.** `python -m pipeline check-content` reports no ERROR, and
  `pytest -q` passes.
* **Your own repository.** Push your copy to a repository you own. A private
  repository is the safer default (see "Who can download the seeds" in Step 2).
  If you forked on GitHub, scheduled workflows stay off until you enable them
  in the Actions tab; in a new repository they run as soon as it exists.
* **Accounts.** An [Anthropic API key](https://console.anthropic.com/),
  [Render](https://render.com), [Supabase](https://supabase.com) (or another
  Postgres), [Resend](https://resend.com) with a domain you can add DNS
  records to (needed for real use: see Step 5). Optional: a Google Cloud
  project (Google sign-in), ElevenLabs (voice), Sentry (error reports).
* **Tools on your machine.** Python 3.11 with `pip install -r requirements-dev.txt`,
  the [GitHub CLI](https://cli.github.com/) signed in (`gh auth login`), `tar`,
  and, for backups, [age](https://age-encryption.org).

> **Scheduled workflows are safe on a fresh clone.** The ones that need your
> site address or your seed release read the repository variables `SITE_URL`
> and `SEED_RELEASE_TAG` and skip while those are unset; the ones that need
> the database skip without `DATABASE_URL`, except the backup and the restore
> drill, which wait for the `BACKUP_AGE_RECIPIENT` variable (see [Workflows](#workflows)). Disable any you will not use in the
> Actions tab.

---

## Step 1: Create the database

### Supabase

1. Create a project. A project of its own is the simplest choice: the
   backups and the restore drill dump the whole `public` schema, so another
   application's tables in the same project would travel with yours (and its
   row-level security policies can need extra steps on a restore, see
   [Nightly database backups](#nightly-database-backups)). Pick a region near
   the Render region you will use.
2. Open **Connect** (or Project Settings > Database) and copy the **Session
   pooler** connection string (port 5432). Do not use the transaction pooler
   (port 6543): `pg_dump` in the backup workflow needs a session. Supabase's
   direct connection is IPv6-only unless you buy its IPv4 add-on, and GitHub's
   hosted runners have no IPv6, so the session pooler is the one that works
   everywhere.
3. Put your database password in it and append `?sslmode=require`:
   `postgresql://postgres.<ref>:<password>@aws-0-<region>.pooler.supabase.com:5432/postgres?sslmode=require`.
4. That string is `DATABASE_URL`, both on Render (Step 4) and as a GitHub
   secret (Step 6). The app rewrites the scheme to `postgresql+psycopg://`
   itself and drops ORM-only query parameters (`get_engine` in
   [tutor/db/models.py](tutor/db/models.py)).
5. Note the project's **reference id** (`<ref>` in the pooler user name
   `postgres.<ref>`, and in the dashboard URL) and create a **personal access token** (Account settings >
   Access Tokens; not the project's `service_role` key). They become
   `SUPABASE_PROJECT_REF` and `SUPABASE_ACCESS_TOKEN`, used only to un-pause a
   paused project.

**Pausing.** Supabase pauses a free project after about a week without
database activity, and a paused database takes the site down for the next
visitor. Three things guard against it: `db-keepalive.yml` probes the site
every third day (the probe's `SELECT 1` counts as activity) and un-pauses the
project if needed; `deploy.yml` un-pauses it before every deploy; and the app
itself asks Supabase to restore the project when `/healthz` or an unhandled
database error finds it down ([tutor/selfheal.py](tutor/selfheal.py)). The
last two need the `SUPABASE_*` pair; without it they report "unconfigured" and
do nothing. On a paid plan none of this matters.

**Row-level security.** Supabase exposes every table in `public` through its
REST API to anyone holding the project's anon key. The migrations create every
table with row-level security already enabled and no policies, so that API
reads nothing; the app connects as the database owner and is not affected.
`tests/test_migrations.py` fails a migration that creates a table without
declaring it RLS-enabled.

### Any other Postgres

Any recent Postgres works (the restore drill restores into Postgres 17). Set
`DATABASE_URL` to its connection string, with `?sslmode=require` when the
server is remote, and leave `SUPABASE_ACCESS_TOKEN` and `SUPABASE_PROJECT_REF`
unset everywhere: the un-pause steps then skip themselves.

Without `DATABASE_URL` the app uses a local SQLite file (`TUTOR_DB`, inside
the container at `/app/data/tutor.db`, which a redeploy wipes). That is for
local work only.

### Connection budget

Poolers and small servers cap client connections (on Supabase, see the pool
size under the project's database settings; the smallest compute allows about
15). The app keeps one shared pool per process: `DB_POOL_SIZE` connections
kept open plus `DB_MAX_OVERFLOW` more on demand (defaults 3 and 4, so at most
7), waiting `DB_POOL_TIMEOUT` seconds (10) for a free one and retiring
connections after `DB_POOL_RECYCLE` seconds (300). That leaves room for the
backup workflow and scripts. A chat turn holds a connection only for each
database step, never while it waits on the model; grading a mastery-check
answer is the one exception. Raise the pool only if the logs show pool
timeouts, and keep it well under the cap.

### Schema migrations (Alembic)

The schema is versioned with Alembic (`migrations/`). **Migrations run at
boot**: before uvicorn starts, `entrypoint.sh` runs `python -m tutor.migrate`,
which handles three states:

* an empty database: built by the migrations, every table born with
  row-level security;
* a versioned database (`alembic_version` has a row): upgraded to head;
* a database the app built itself at boot (tables but no `alembic_version`
  row): stamped at the baseline revision, then upgraded. Every revision after
  the baseline is guarded, so it only adds what is missing.

A failed migration logs a warning (sent in the one boot-warnings email) and
the app starts anyway on its own boot-time schema setup
(`startup.ensure_schema`, a safety net). `RUN_MIGRATIONS=0` skips the step,
for example while a fix for a bad revision ships.

To run the same thing by hand, with `DATABASE_URL` pointing at the target:

```bash
python -m tutor.migrate   # stamp if needed, then upgrade to head
alembic current           # confirm the database is at head
```

To change the schema, add a revision instead of editing `tutor/startup.py`,
guard each step the way the existing revisions do, and read the generated
SQL by hand:

```bash
alembic revision --autogenerate -m "add X"
```

On the first boot against an empty database the app also seeds the learning
goals from `curriculum/learning_goals.json` (only while the goals table is
empty) and upserts the tracks from `curriculum/tracks.json` (every boot).

---

## Step 2: Build the seed files and publish them

The container downloads four seed files at boot:

| Seed | Built from | Environment variable | Lands in | Downloaded |
|------|------------|----------------------|----------|------------|
| `chroma.tgz` | `knowledge-base/chromadb/` | `CHROMA_SEED_URL` | the persistent disk, `/app/knowledge-base/chromadb` | until one seed succeeds (a `.seed-ok` marker) |
| `slides.tgz` | `output/<COURSE>/lecture-NN/` decks: `index.html`, `content.md`, `timing.json` | `OUTPUT_SEED_URL` | `/app/output` (container filesystem) | every boot |
| `lecture-audio.tgz` (optional) | `output/<COURSE>/lecture-NN/audio/slide-NN.mp3`, and `output/book/*.mp3` | `AUDIO_SEED_URL` | the persistent disk, `<mount>/lecture-audio` | until one seed succeeds (a `.seed-ok` marker) |
| `mastery_exams.json` (optional) | `curriculum/mastery_exams.json` | `MASTERY_EXAMS_URL` | `/app/curriculum/mastery_exams.json` (container filesystem) | every boot |

A seed never stops the boot. `entrypoint.sh` downloads each file with retries
(`SEED_RETRIES`, default 5), unpacks it into a scratch directory, checks it
(`python -m tutor.seed_check`), and only then swaps it into place and writes
its `.seed-ok` marker. A failed or truncated download leaves the previous copy
(or nothing) and no marker, so the next boot tries again; the app starts
without that data and emails one "started without some of its data" message.
What each missing seed costs:

* no store: the tutor answers without retrieval (no citations, no slide
  links), and `/healthz` reports `"kb"` as not ok;
* no decks: the slide panel and `/slides/...` answer 404;
* no audio: decks play no narration, `/lecture-audio/...` answers 404;
* no exam bank: mastery checks are off and every goal stays reachable.

### Build them

Work from the repository root with your virtual environment active, and keep
the files outside the working copy (`*.tgz` is gitignored, but a folder of its
own is tidier). On macOS run `export COPYFILE_DISABLE=1` first, so Finder
metadata files stay out of the archives.

```bash
mkdir -p ~/tutor-seed
```

**The vector store.** Build it from scratch with the app's own embedder;
the build also writes `manifest.json` (inputs, hashes, counts, embedder) into
the store, so the seed records what it was built from.

```bash
python -m pipeline index --rebuild
tar --exclude='.DS_Store' -czf ~/tutor-seed/chroma.tgz -C knowledge-base/chromadb .
tar -tzf ~/tutor-seed/chroma.tgz | head      # ./chroma.sqlite3 and ./manifest.json at the top level
```

The archive's top level must be the store itself (`chroma.sqlite3` and the
collection folders), not a `chromadb/` folder. The boot check opens the store,
runs a query on every non-empty collection, and **rejects a store with no
documents in any collection** (or one a truncated download left unreadable),
so index something first (ADDING-KNOWLEDGE.md section 3).

**The decks.** If you have narration, render it before the decks: a deck
rendered while `audio/slide-01.mp3` is absent has no audio player.

```bash
python -m pipeline render-audio --course AST101 --lecture 1    # optional; ElevenLabs, one lecture at a time
python -m pipeline render-html --course AST101 --force          # repeat for every course in courses.yaml
python -m pipeline status --verbose                             # every lecture should be complete

tar --exclude='.DS_Store' --exclude='audio' --exclude='frames' --exclude='*.mp3' \
    --exclude='*.mp4' --exclude='*.pptx' --exclude='script.md' --exclude='_*' \
    -czf ~/tutor-seed/slides.tgz -C output .
tar -tzf ~/tutor-seed/slides.tgz | head      # ./AST101/lecture-01/index.html, ...
```

The layout is `<COURSE>/lecture-NN/index.html` at the top level, with
`content.md` (the `/lectures` catalog reads each deck's title from it) and
`timing.json` (its minutes) beside it. Narration scripts (`script.md`) are
not for students: leave them out (the boot deletes any copy a seed carries).
The boot checks that **every lecture 1..N of every course** in `courses.yaml`
has a non-empty `index.html`; a partial tree is installed but warned about,
and the missing lectures answer 404.

**The narration** (optional). Per-slide clips, in the path the decks ask for
(`/lecture-audio/<COURSE>/lecture-NN/audio/slide-NN.mp3`), plus the course
book's chapter narration if you made it (`python -m pipeline render-book-audio --spend`,
one MP3 per chapter in `output/book/`, served at `/lecture-audio/book/`):

```bash
(cd output && find . -path '*/lecture-*/audio/slide-*.mp3' -o -path './book/*.mp3' \
   | tar -czf ~/tutor-seed/lecture-audio.tgz -T -)
```

Clips re-encoded to 48 kbps mono are a fraction of the size and fine for
speech: `ffmpeg -i in.mp3 -ac 1 -b:a 48k out.mp3`.

**The mastery-check bank** (optional; ADDING-KNOWLEDGE.md section 8):

```bash
python -m pipeline generate-mastery-exams --resume
cp curriculum/mastery_exams.json ~/tutor-seed/
```

The bank is gitignored, so an image Render builds from git never contains it
and always downloads it. (An image you build yourself from a working copy
that has the file carries it, and then ignores `MASTERY_EXAMS_URL`.)

**Files over 2 GiB.** A GitHub Release asset must be smaller than 2 GiB.
`CHROMA_SEED_URL` and `AUDIO_SEED_URL` accept a space-separated list of URLs
that are **pieces of one archive**, joined in order before unpacking:

```bash
split -b 1900m ~/tutor-seed/chroma.tgz ~/tutor-seed/chroma.tgz.part-
```

`OUTPUT_SEED_URL` also accepts a space-separated list, but each URL there is a
**complete archive of its own** (for example one per course), each unpacked
into `/app/output`.

### Publish them as a GitHub Release

GitHub does not bill bandwidth for Release assets, which matters because the
decks and the exam bank are downloaded on every boot.

```bash
TAG=deploy-assets-$(date +%Y-%m-%d)
gh release create "$TAG" --repo <owner>/<repo> \
  --title "Deploy seed files $TAG" --notes "Seed files for the tutor deployment" \
  --prerelease --latest=false \
  ~/tutor-seed/chroma.tgz ~/tutor-seed/slides.tgz \
  ~/tutor-seed/lecture-audio.tgz ~/tutor-seed/mastery_exams.json   # leave out the optional files you do not have

# Each asset's API URL: these are the *_SEED_URL / MASTERY_EXAMS_URL values.
gh api "repos/<owner>/<repo>/releases/tags/$TAG" --jq '.assets[] | "\(.name)\t\(.url)"'
```

The URLs have the form
`https://api.github.com/repos/<owner>/<repo>/releases/assets/<id>`. For such
a URL `entrypoint.sh` asks for the file itself (`Accept: application/octet-stream`)
and, when `GITHUB_TOKEN` is set, sends it as a bearer token. Any other
`https://` URL (object storage, a public download link) is fetched plainly.

The exam bank can also be uploaded on its own; the command prints the value
to set:

```bash
python -m pipeline upload-mastery-exams --tag "$TAG" --repo <owner>/<repo>
```

**Private repository:** set `GITHUB_TOKEN` on Render to a token that can read
the repository's contents: a fine-grained personal access token limited to
that one repository with **Contents: read-only** is enough. Without it every
seed download fails with 404.

**Who can download the seeds.** On a public repository, Release assets are
public. The store holds the text extracted from your slides, notes,
transcripts and textbook, and the exam bank holds every question with its
answer and rubric. Unless you are happy for anyone to download those, publish
the seeds on a private repository (this one, or a private repository used only
for seeds, with `GITHUB_TOKEN` scoped to it). `retrieval-eval.yml` looks for
the store among this repository's own Releases.

### Replacing a seed later

1. Rebuild the file, then `gh release upload "$TAG" <file> --clobber --repo <owner>/<repo>`
   (or publish a new release). **A re-upload gets a new asset id**, so read the
   new URL with the `gh api` command above and update the variable on Render.
   Changing a variable redeploys the service.
2. The decks and the exam bank are fetched again on every boot, so that is all
   they need.
3. The store and the audio are seeded **once per disk**: a new URL alone does
   not replace them, and neither does deleting the `.seed-ok` marker, because
   the boot adopts a readable store, or a non-empty audio folder, that it
   finds without a marker. Move the old copy aside in the service's Shell (the
   Render dashboard), then redeploy:

   ```bash
   cd /app/knowledge-base/chromadb
   rm -f .seed-ok && mv chroma.sqlite3 chroma.sqlite3.old   # the store
   mv lecture-audio lecture-audio.old                       # the narration
   ```

   A successful store seed removes the old store's files (it never touches
   `lecture-audio/` or `tts-cache/`). After a successful audio seed, delete
   `lecture-audio.old`. If a seed fails, move the old copy back and redeploy.
   Until the redeploy finishes, the moved data is unavailable.

---

## Step 3: Create the Render service

1. In Render: **New > Blueprint**, connect your repository. Render reads
   [render.yaml](render.yaml): one Docker web service named `tutor` (rename it
   in the file first if you like), on the `standard` plan (2 GB of memory;
   the embedding model and the app can run out of memory on the 512 MB
   `starter` plan), with a 2 GB persistent disk named `chroma` mounted at
   `/app/knowledge-base/chromadb`, and `/auth/login` as its health check.
   Size the disk to your store, plus narration, plus a voice cache that grows
   as students press the speaker button; Render bills disks per GB.
2. Render asks for every variable marked `sync: false`. Fill them in from
   [Step 4](#step-4--environment-variables); optional ones can stay empty and
   be added later.
3. The first deploy builds the image (the course book, the Python packages,
   the embedding model; a failed model fetch fails the build) and boots it.
   The boot seeds the data as root, hands the disk and `/app/data` to the
   unprivileged `app` user (uid 10001), and runs everything after that as
   `app`: it waits up to two minutes for the database, runs the migrations,
   and starts uvicorn.
4. **Turn auto-deploy off**: the service's Settings > Build & Deploy >
   Auto-Deploy: **Off**. `render.yaml` asks for that (`autoDeployTrigger: "off"`),
   but the dashboard setting is the one that counts for a service that is not
   kept in sync with the Blueprint. From then on deploys go through the manual
   `deploy.yml` workflow, which refuses a commit that has not passed `tests`
   (Step 6). With auto-deploy on, every push would go live before its tests
   finished.
5. Copy the **deploy hook** URL (Settings > Deploy Hook). It becomes the
   `RENDER_DEPLOY_HOOK_URL` repository secret; anyone holding it can deploy, so
   treat it as a secret.

Changing an environment variable in the dashboard also redeploys the service,
outside the workflow. Deploys stop the old instance before the new one starts
(a persistent disk allows one instance), so expect a short outage; uvicorn
gives open replies 15 seconds to finish, and a reply cut off by the restart is
recorded as interrupted.

**Another host.** Run the image with a persistent volume at
`/app/knowledge-base/chromadb`, the variables from Step 4, and the port in
`PORT` (default 8000) behind a TLS proxy. Set `FORWARDED_ALLOW_IPS` to the
address range your proxy connects from, and list every public hostname in
`BASE_URL` or `ALLOWED_HOSTS` (Render's own hostname is trusted automatically
through `RENDER_EXTERNAL_HOSTNAME`; elsewhere nothing is). Replace `deploy.yml`
with your host's deploy mechanism.

---

## Step 4 — Environment variables

Set these on the Render service (Environment). "Preset" means `render.yaml`
already gives the value; everything else marked required must be filled in.

| Variable | Required | What it does |
|---|---|---|
| `ANTHROPIC_API_KEY` | yes | The Claude API key for every model call. The app refuses to start without it, so to stop all model spend at once replace it with an invalid value (the site keeps running; the tutor stops answering). |
| `SECRET_KEY` | preset (generated) | Signs session cookies. Keep it stable: changing it signs everyone out. Off Render, generate one with `openssl rand -hex 32`. |
| `ADMIN_TOKEN` | yes | A long random string. Signs an operator in at `/admin/login` and authorizes scripts through the `X-Admin-Token` header (`tutor.analytics export --from-url`, the deck count in `link-check.yml`). Changing it ends token sessions. |
| `ADMIN_EMAILS` | yes | Comma-separated addresses of the people who run the site. They open `/admin` and always pass the sign-in whitelist. No default: left empty, nobody can open `/admin` (the boot warns). |
| `DATABASE_URL` | yes | The Postgres connection string from Step 1. |
| `BASE_URL` | yes | The public address, no trailing slash: `https://your-service.onrender.com` at first, `https://tutor.example.edu` once your domain points at the service (Step 5). Sign-in links fall back to it, so a wrong value breaks sign-in. |
| `ALLOWED_HOSTS` | optional | Extra comma-separated hostnames that sign-in may build its links on. The `BASE_URL` host, Render's own hostname and `localhost` are always trusted; a request for any other host gets links on `BASE_URL`. |
| `CHROMA_SEED_URL` | yes | The `chroma.tgz` asset URL (or the URLs of its pieces, space-separated, in order). |
| `OUTPUT_SEED_URL` | yes, if you have decks | The `slides.tgz` asset URL (or several complete archives, space-separated). Unset: the slide panel answers 404. |
| `AUDIO_SEED_URL` | optional | The `lecture-audio.tgz` asset URL (or its pieces). Seeded once onto the disk under `<mount>/lecture-audio`. Unset: no narration. |
| `MASTERY_EXAMS_URL` | optional | The `mastery_exams.json` asset URL. Unset: mastery checks are off. |
| `LECTURE_AUDIO_DIR` | preset | `/app/knowledge-base/chromadb/lecture-audio`: where the audio seed lands, and what the `/lecture-audio` mount and the `/lectures` catalog read. |
| `TTS_CACHE_DIR` | preset | `/app/knowledge-base/chromadb/tts-cache`: the voice cache on the disk, so clips survive deploys. |
| `GITHUB_TOKEN` | if the seeds are in a private repository | A read-only token for the seed downloads (Step 2). Leave unset for public assets. |
| `FORWARDED_ALLOW_IPS` | preset | `10.0.0.0/8,172.16.0.0/12,192.168.0.0/16`, the private ranges Render's proxy connects from. uvicorn then takes the client address from `X-Forwarded-For`, so the per-address limits (sign-in emails, code guesses, the `/healthz` throttle) apply per visitor instead of to the whole class at once. Never `*`: uvicorn would then trust an address the caller wrote. |
| `RESEND_API_KEY` / `FROM_EMAIL` | yes, for real use | Sends the sign-in emails, the alerts and the reports. `FROM_EMAIL` must be on a domain verified in Resend. Read the warning in Step 5 before running without them. |
| `GOOGLE_CLIENT_ID` / `GOOGLE_CLIENT_SECRET` | optional | Google sign-in (Step 5). Unset: email sign-in only. |
| `ELEVENLABS_API_KEY` / `ELEVENLABS_VOICE_ID` | optional | Both set: a speaker button on every saved reply, in the cloned voice. Either unset: no voice. |
| `SENTRY_DSN` | optional | Error reports to Sentry: errors only, with bodies, cookies, query strings, auth headers, sign-in tokens and SQL parameters removed. `SENTRY_ENV` names the environment (default `production`). |
| `SUPABASE_ACCESS_TOKEN` / `SUPABASE_PROJECT_REF` | recommended on Supabase's free plan | Lets the app un-pause its own database (Step 1). The same values go into the GitHub secrets of the same names. |
| `ALERT_EMAILS` / `ALERT_COOLDOWN_MINUTES` | optional | Who gets system-error emails (default: `ADMIN_EMAILS`), and how long the same problem stays quiet after an email (default 60 minutes). |
| `DAILY_EXCHANGE_LIMIT` | recommended | Chat turns per student per day (the class's time zone). Default 0: no limit. See [Spend caps and quotas](#spend-caps-and-quotas). |
| `MONTHLY_COST_CAP_USD` | recommended | Model spend for everyone in the calendar month (UTC), in dollars. Default 0: no cap. |
| `DAILY_REVIEW_LIMIT` | optional | Graded spaced-review answers per student per day. Default 60; 0 turns it off. |
| `DB_POOL_SIZE` / `DB_MAX_OVERFLOW` / `DB_POOL_TIMEOUT` / `DB_POOL_RECYCLE` | optional | The connection pool (Step 1): 3, 4, 10 s, 300 s. |
| `CLAUDE_MODEL` | optional | Overrides the tutor's model (the default is the actor entry in `tutor/agent/models.py`). |
| `CACHE_PREWARM` | optional | Default `true`: opening the chat warms the prompt cache with one small billed call, at most once per 50 minutes. `false` turns it off. |
| `LOG_LEVEL` / `ACTIVITY_LEVEL` | optional | The app's log level (default `INFO`) and the detail of the structured `tutor.activity` events (`minimal`, `standard`, `verbose`). |
| `LOG_CONVERSATION_CONTENT` | optional | Default `false`. `true` adds student messages and tutor replies to the activity log, which then holds student text in your host's log store, out of reach of the retention tools. |
| `SLOW_TURN_MS` | optional | A turn slower than this (default 25000) is flagged slow in the reports. |
| `ACTIVITY_EVENTS` / `ACTIVITY_HEARTBEAT_S` / `ACTIVITY_EVENTS_DAYS` | optional | Browser activity events: `off` leaves the tracking scripts out of every page; the heartbeat interval (default 60 s, at least 10); how many days the retention sweep keeps raw events (unset: they age out with the one-year anonymization; see the note on `retention-sweep.yml` in [Workflows](#workflows)). |
| `JWT_EXPIRY_DAYS` / `MAGIC_LINK_EXPIRY_MINUTES` | optional | How long a sign-in lasts (default 7 days) and how long an emailed link or code works (default 30 minutes). |
| `RUN_MIGRATIONS` / `SEED_RETRIES` | optional | `RUN_MIGRATIONS=0` skips the boot migrations; `SEED_RETRIES` is the download retry count (default 5). |
| `ENABLE_CODE_EXEC` | leave unset | Running student code is off by default and refuses to run beside `DATABASE_URL`; the image has no compiler. See [docs/sandbox.md](docs/sandbox.md). |
| `API_DOCS` | leave unset | `true` publishes the API docs at `/docs`, admin routes included. For local work only. |

---

## Step 5: Domain, email and sign-in

### Your domain

1. In the service's Settings > Custom Domains, add `tutor.example.edu`, and at
   your DNS provider create the CNAME record Render shows (pointing at
   `your-service.onrender.com`). Render issues the TLS certificate.
2. Set `BASE_URL=https://tutor.example.edu`.
3. Set `site_url: https://tutor.example.edu` in `courses.yaml` (the emails and
   the help page use it), commit, and deploy.

The `onrender.com` address keeps working: sign-in started there comes back
there.

### Email (Resend)

1. In Resend, add and verify a sending domain (it gives you DNS records to
   add), and create an API key.
2. Set `RESEND_API_KEY` and `FROM_EMAIL=tutor@<your verified domain>`.

> **Never open a deployment to the internet without `RESEND_API_KEY`.**
> Without it the sign-in page runs in development mode: it shows the sign-in
> link and code on the page to whoever typed the address. Anyone who knows an
> admin's address (admins always pass the whitelist) can then sign in as that
> admin.

Email also carries the operator alerts (unhandled errors, database outages,
the boot warnings) and the reports. A sign-in email that Resend refuses is
shown to the student as a failure with your support address, and logged.

### Google sign-in (optional)

1. In the Google Cloud console, configure the OAuth consent screen, then
   create an **OAuth client ID** of type **Web application**.
2. Add the authorized redirect URI `https://tutor.example.edu/auth/google/callback`.
   The app builds the callback from the host the visitor arrived on (when that
   host is trusted), so if people also use the Render address, add
   `https://your-service.onrender.com/auth/google/callback` too.
3. Set `GOOGLE_CLIENT_ID` and `GOOGLE_CLIENT_SECRET`.

Google sign-in goes through the same whitelist as email sign-in.

---

## Step 6: GitHub Actions

### Repository secrets and variables

Settings > Secrets and variables > Actions. Each workflow skips or fails as
listed in [Workflows](#workflows) when its secrets are missing.

| Name | Kind | Used by | Notes |
|---|---|---|---|
| `RENDER_DEPLOY_HOOK_URL` | secret | `deploy.yml` | The deploy hook from Step 3. |
| `DATABASE_URL` | secret | the reports, the sweep, the auditor, mining, the backup, the drill | The same string as on Render. Workflows that write (`audit.yml`, `misconception-mining.yml`, `retention-sweep.yml`) pass `--allow-prod`; the rest only read. |
| `ANTHROPIC_API_KEY` | secret | `audit.yml`, `misconception-mining.yml` | Their model calls are billed to this key. |
| `SECRET_KEY` / `ADMIN_TOKEN` | secret | `audit.yml` (both), `link-check.yml` (`ADMIN_TOKEN`) | The same values as on Render. |
| `RESEND_API_KEY` / `FROM_EMAIL` / `ADMIN_EMAILS` | secret | every workflow's failure email, and the reports | Without them a failed run is the only signal. |
| `REPORT_EMAIL` | secret, optional | `daily-report.yml`, `quota-report.yml` | Where the digests go; default: the first `ADMIN_EMAILS` address. |
| `SLOW_TURN_MS` | secret, optional | `daily-report.yml` | As on Render. |
| `SUPABASE_ACCESS_TOKEN` / `SUPABASE_PROJECT_REF` | secret, optional | `deploy.yml`, `db-keepalive.yml` | Un-pausing a free Supabase project. |
| `BACKUP_AGE_RECIPIENT` | **variable** (not a secret) | `db-backup.yml` | The public key backups are encrypted to ([Nightly database backups](#nightly-database-backups)). |
| `SITE_URL` | **variable** | `uptime.yml`, `link-check.yml`, `db-keepalive.yml`, the report emails' links | Your public address, e.g. `https://tutor.example.edu`. Unset: those probes skip. |
| `SEED_RELEASE_TAG` | **variable** | `retrieval-eval.yml` | The tag of the release that holds `chroma.tgz` (Step 2). Unset: the eval skips. |
| `ACTIVITY_EVENTS_DAYS` | **variable**, optional | `retention-sweep.yml` | Prune raw activity events older than this many days, as on Render. |

Anyone who can push a workflow to the repository can read these secrets, so
keep write access to people you would trust with the database.

### Schedules

Every `cron:` line is in **UTC**. The times shipped were chosen to run
overnight in US Pacific time (roughly midnight to 4:30 a.m.); shift them to
your own night. Keep their order when you do: the daily report, then the
auditor and the backup, then the retention sweep, which runs after the
backup so the night's dump still holds the rows it removes. The quota
roll-up sends its weekly email on Monday and its monthly one on the 1st in
the class's time zone (`timezone` in `courses.yaml`), so schedule it shortly
after your local midnight.

### Deploying

The only deploy path is the manual **Deploy to Render** workflow:

```bash
gh workflow run deploy.yml                          # the current tip of main
gh workflow run deploy.yml -f ref=<sha>             # a specific commit
gh workflow run deploy.yml -f allow_untested=true   # emergency: skip the tests check (it says so in the log)
```

or Actions > Deploy to Render > Run workflow. It checks that `tests.yml`
passed on that commit, un-pauses the Supabase project when the `SUPABASE_*`
secrets are set (waiting up to five minutes), and calls the deploy hook with
`ref=<sha>` so Render builds exactly the tested commit. **Pushing to `main`
does not deploy.**

Recommended: protect `main` and require the `tests` check. Note that
`tests.yml` runs `scripts/repo_guard.py` on every commit a push adds: commit
subjects must be [Conventional Commits](https://www.conventionalcommits.org/)
(`docs: ...`, `feat(ui): ...`; `git config core.hooksPath .githooks` checks
them locally), no file may be over 10 MB, and paths that look like student
records are refused.

---

## Step 7: Check the first boot

1. **The deploy log.** Look for `ChromaDB seed complete.`, `Slide seed complete.`,
   `Running the app as app`, the migrations, and uvicorn starting. A line
   starting `[entrypoint] WARNING` names what is missing; the same list
   arrives by email once the app is up.
2. **`/healthz`.** `curl https://tutor.example.edu/healthz` should return
   `{"status": "ok", "db": "ok", "kb": "ok", ...}`. `"degraded"` with a 200
   means the database is fine but the store has nothing to search; a 503 with
   `"db": "down"` means the database is unreachable (and, with the
   `SUPABASE_*` pair, a restore was requested).
3. **Sign in** at `/auth/login` with an address from `ADMIN_EMAILS`. The page
   must say it emailed you a code and a link; if it shows the link itself,
   `RESEND_API_KEY` is not set.
4. **Admin > Health** checks the API key with one tiny call, the database,
   the store, and which optional features are configured.
5. **Admin > Access.** The sign-in whitelist is **on** by default: only admins
   and whitelisted addresses can sign in. Add students one address at a time,
   or a whole domain as `@example.edu`. Turning the whitelist off lets anyone
   with an email address sign in and spend your API budget.
6. **Try it as a student**: pick a course on the welcome page, ask one of
   your starter questions, check that the reply cites your material and moves
   the slide panel, ask for an exercise, open `/map`.
7. **Set the limits** (Admin > Supervision, and the two variables in
   [Spend caps and quotas](#spend-caps-and-quotas)).
8. **Read what students are told.** The sign-in page and `/help` carry a
   notice about what is recorded, who can read it and for how long. Adapt it
   and [docs/retention-policy.md](docs/retention-policy.md) to your
   institution before the first student signs in.
9. **Run the uptime workflow once** by hand (Actions > uptime > Run workflow)
   to confirm the edited address answers.

## Step 8: Instructors, classes and students

1. The instructor signs in once (their address must pass the whitelist), so
   they have an account.
2. Admin > **Classes** > *Make instructor* with their address.
3. *Create a class*: name, course, term, the instructor, and the term's first
   and last day (the form calls them quarter dates; the weekly email goes out
   only on days inside them).
   Open the class and paste the student addresses, the whole roster at once.
   An address with no account yet is pre-registered and fills itself in on
   that student's first sign-in; only unusable addresses and duplicates are
   skipped.
4. Being on a roster does not let a student sign in: the **Access** whitelist
   still decides, so make sure their addresses or their domain are on it.
5. The instructor now sees the Instructor page, limited to their own classes
   (rosters, activity, and the conversations of their students in the class's
   course), and gets the weekly email if you send it (`instructor-report.yml`).
   Any instructor of a class, or an admin, can add co-instructors from the
   class's Instructors block. Each class has a **Weekly email** switch in
   Admin > Classes.

---

## Updating content later

[ADDING-KNOWLEDGE.md, section 12](ADDING-KNOWLEDGE.md#12-changing-content-later)
lists what each kind of change needs. In deployment terms:

* **Committed content** (`courses.yaml`, `persona/`, `instructor-profile/`,
  `curriculum/` except the exam bank, `explorables/`, `book/`): commit, wait
  for `tests`, run the deploy workflow.
* **Learning goals** are copied into the database only on a first boot. On a
  running site, run `python -m pipeline.seed_goals --allow-prod` with
  `DATABASE_URL` pointing at it.
* **Generated content** (the store, the decks, the narration, the exam bank):
  rebuild the seed and replace it as in [Step 2](#replacing-a-seed-later).

Never rename or delete a course id or a goal id that students have used: their
progress points at it.

---

## Nightly database backups

`db-backup.yml` dumps the database every night (`pg_dump -Fc`, with a client
of the server's major version or newer) and keeps the dump as an Actions
artifact for 21 days. The dump holds every student's conversations, so it is
encrypted with [age](https://age-encryption.org) before upload: anyone with
read access to the repository can download artifacts, but only the holder of
the private key can read one. Until the public key is configured the job
fails (and emails) rather than store a readable dump.

One-time setup, on your own machine:

```bash
age-keygen -o ~/tutor-backup-key.txt   # keep this file offline and backed up;
                                       # it is the only way to read a backup
# Copy the printed "Public key: age1..." into a repository variable:
#   Settings > Secrets and variables > Actions > Variables >
#   New repository variable  BACKUP_AGE_RECIPIENT = age1...
# (several keys, space-separated, encrypt to each of them)
```

Restore:

```bash
# Download the tutor-db-<run id> artifact from a db-backup run, unzip it, then:
age -d -i ~/tutor-backup-key.txt -o tutor.dump tutor.dump.age
pg_restore -d "postgresql://...?sslmode=require" --clean --if-exists --no-owner tutor.dump
```

Use a plain `postgresql://` URL that `pg_restore` understands. Restoring into
a Supabase project needs nothing more. Restoring into a plain Postgres can
need one step first: if your Supabase `public` schema holds policies that
call `auth.uid()` (another application sharing the project, for example),
`pg_restore` stops at `schema "auth" does not exist`. Create the small `auth`
shims in the target first; the step *Give the scratch database the Supabase
auth shims* in `db-restore-drill.yml` has the SQL.

### Monthly restore drill

`db-restore-drill.yml` runs on the 3rd of each month and proves that a dump
restores. It takes a fresh read-only dump of production, encrypts and
decrypts it with a throwaway key (it never holds your private key), restores
the `public` schema into an empty Postgres 17, and checks it with
`scripts/restore_drill.py`: every app table present, one Alembic revision,
students present, row-level security still on. It uploads nothing and logs
counts, never rows; a failure emails `ADMIN_EMAILS`. A push or pull request
that changes the drill or the migrations runs it on a synthetic source (the
migrations and made-up rows), so production is read only on the schedule or
when you start it by hand.

The drill does not test your private key: once a term, download the latest
backup artifact and decrypt it as above.

---

## Monitoring and alerts

* **`/healthz`** runs `SELECT 1` and reports the store; `uptime.yml` asks it
  every two hours and `db-keepalive.yml` every third day. Render's own health
  check is `/auth/login`, which answers even when the database is down.
* **Alert emails** go to `ALERT_EMAILS` (default `ADMIN_EMAILS`) through
  Resend: unhandled errors, the database going down and recovering, a
  signed-in student following a link to a missing page, and, once per boot,
  what the boot started without. The same problem emails at most once per
  `ALERT_COOLDOWN_MINUTES`, and no more than 20 alerts go out in an hour.
* **Admin > Today** lists the day's errors, thumbs-down replies, sign-in
  failures and quota hits; **Admin > Supervision** the cost and the ledger of
  every turn.
* **Sentry** (`SENTRY_DSN`) collects errors with their stack traces.

[docs/runbook.md](docs/runbook.md) turns symptoms into checks and fixes.

## Spend caps and quotas

Every model call is recorded with its cost in the `turn_events` ledger
(`python -m pipeline cost-report` summarizes it). Four limits bound it, all
checked inside every turn (`usage.over_caps` in `run_turn`), so every way of
reaching the tutor inherits them:

| Limit | Where it is set | Default |
|---|---|---|
| Chat turns per student per day | `DAILY_EXCHANGE_LIMIT` on Render | 0 (off) |
| Spend for the whole class this calendar month (UTC) | `MONTHLY_COST_CAP_USD` on Render | 0 (off) |
| Graded review answers per student per day | `DAILY_REVIEW_LIMIT` on Render | 60 |
| Dollars per student per day, week (from Monday) and month | Admin > Supervision (stored in the database) | $1.50, $6, $15; 0 turns one off |

Days and weeks follow the class's time zone. Admins are exempt from the
per-student quotas, and the prompt-cache warm-up never counts against a
student. A student who hits a limit reads a message saying when it lifts; the
chat shows the remaining budget from 80 percent of a quota, and each hit is
one zero-cost `quota` row in the ledger, which the Today panel, the
Supervision tab and `quota-report.yml` read.

The two class-wide caps are off until you set them. Set
`MONTHLY_COST_CAP_USD` before students arrive: it is the one limit that bounds
the whole bill. Setting it to a value already spent (`0.01`) pauses the tutor
for everyone with a friendly message; replacing `ANTHROPIC_API_KEY` with an
invalid value stops every model call (an empty value stops the app from
starting).

## One instance only

Do not scale the service to two or more instances. Several limits and
guards keep their state in the process's memory: the sign-in email and code
limiters, the `/healthz` per-address limit, alert de-duplication and the
hourly cap, the self-heal cooldown, the one-turn-at-a-time claim per student,
the review grader's claim, the reply buffers a reloaded page reattaches to,
the daily photo allowance, and the per-student cap on activity events. On two
instances every limit doubles and de-duplication silently breaks. The
persistent disk already pins a Render service to one instance; on another
host, keep it that way, or move that state into Postgres first.

---

## Workflows

Every workflow in `.github/workflows/`. Times are UTC; every scheduled
workflow runs one at a time (a concurrency group), and all but `tests.yml` can
also be started by hand (Actions > the workflow > Run workflow). "Skips" means the run ends green without doing
anything; "fails" means a red run (and a failure email when the Resend
secrets are set).

Two **repository variables** (Settings > Secrets and variables > Actions >
Variables, not secrets) tell the workflows where your site and your seeds are:
`SITE_URL` (your public address, e.g. `https://tutor.example.edu`, used by
`uptime`, `link-check` and `db-keepalive`, and for links in the report emails)
and `SEED_RELEASE_TAG` (the release holding `chroma.tgz`, used by
`retrieval-eval`). A job that needs one of them is skipped while it is unset.

| Workflow | Runs (UTC) | Needs | Without it | What it does |
|----------|------------|-------|------------|--------------|
| `tests.yml` | every push and pull request | nothing (dummy keys) | | The `tests` check: repository guards, ruff, Tailwind CSS freshness, mypy, an import gate, and the network-free suite. `deploy.yml` requires it green. |
| `deploy.yml` | by hand only | `RENDER_DEPLOY_HOOK_URL`; optionally the `SUPABASE_*` pair | fails without the hook; skips the un-pause step without the pair | Un-pauses Supabase, then deploys a tested commit through the hook. |
| `docker-image.yml` | a push or pull request that changes the `Dockerfile`, `.dockerignore`, `entrypoint.sh`, `requirements-web.txt` or itself | nothing | | Builds the image and checks that the embedder works offline, the server runs as a non-root user that cannot write its code, `/healthz` answers, and the sign-in page is served. |
| `daily-report.yml` | daily 08:45 | `DATABASE_URL`, `RESEND_API_KEY`, `FROM_EMAIL`, `ADMIN_EMAILS`; optionally `REPORT_EMAIL`, `SLOW_TURN_MS` | skips without `DATABASE_URL`; prints instead of emailing without Resend | Emails the day's activity digest, then a thumbs-down report when a reply was flagged. Read-only. Links use the `SITE_URL` variable. |
| `quota-report.yml` | daily 09:05 | as `daily-report.yml` | skips without `DATABASE_URL` | Emails the quota roll-ups switched on in Admin > Supervision (daily; weekly on Monday; monthly on the 1st), only when someone hit a quota. Read-only. |
| `instructor-report.yml` | by hand only (add a `schedule:` to send it weekly) | `DATABASE_URL`, `RESEND_API_KEY`, `FROM_EMAIL`, `ADMIN_EMAILS` | skips without `DATABASE_URL`; prints without Resend | One email per instructor about their own classes, with a blind copy to the first `ADMIN_EMAILS` address. `python -m scripts.instructor_report --no-email` previews it; `--to <address>` sends every report to one inbox. Read-only. |
| `retention-sweep.yml` | daily 09:30 | `DATABASE_URL` | skips without `DATABASE_URL` | `scripts/retention_sweep.py --confirm --allow-prod`: prunes used and expired sign-in links, sign-in attempts and error-page rows older than 90 days, and anonymizes students inactive for more than a year ([docs/retention-policy.md](docs/retention-policy.md)). To prune raw activity events too, set the repository variable `ACTIVITY_EVENTS_DAYS` (the web service's value never reaches the workflow). |
| `audit.yml` | daily 09:17 | `ANTHROPIC_API_KEY`, `DATABASE_URL`, `SECRET_KEY`, `ADMIN_TOKEN` | skips without the API key or `DATABASE_URL` | The nightly auditor ([docs/audit/README.md](docs/audit/README.md)): re-judges the last day's risky turns and a sample of the rest with a stronger model (billed), writes its verdicts to the database, and keeps a report as an artifact for 14 days. |
| `misconception-mining.yml` | Sundays 10:10 | `ANTHROPIC_API_KEY`, `DATABASE_URL` | skips without either | Clusters the week's per-student misconception notes into candidates for goals that at least four students struggled with (billed). Candidates reach prompts only after `python -m scripts.mine_misconceptions --approve <id>`. |
| `db-backup.yml` | daily 09:20 | `DATABASE_URL`; variable `BACKUP_AGE_RECIPIENT` | skips without the variable; with it, fails without `DATABASE_URL` | An age-encrypted dump kept 21 days as an artifact ([Nightly database backups](#nightly-database-backups)). |
| `db-keepalive.yml` | every third day at 07:23 | optionally the `SUPABASE_*` pair; variable `SITE_URL` | skips the un-pause without the pair; skips the probe without `SITE_URL` | Un-pauses the Supabase project if needed, then asks `/healthz` for a 200. |
| `db-restore-drill.yml` | the 3rd of each month at 11:33; synthetic on a push or pull request that changes the drill or the migrations | `DATABASE_URL` (production mode only); variable `BACKUP_AGE_RECIPIENT` for the scheduled run | the scheduled run skips without the variable and fails without `DATABASE_URL` | Proves a production dump restores into an empty Postgres ([Monthly restore drill](#monthly-restore-drill)). |
| `link-check.yml` | Tuesdays 10:20 | variable `SITE_URL`; optionally `ADMIN_TOKEN` | skips without `SITE_URL`; without the token it skips the deck count | Crawls the public pages, the course book and the external links in the Teach-me openers, and, with the token, checks that every lecture has its deck on the instance. Fails on a dead link or an unreachable site. |
| `uptime.yml` | every 2 hours at :11 | variable `SITE_URL` | skips | Asks `/healthz` for a 200 three times, half a minute apart. |
| `retrieval-eval.yml` | Mondays 10:40 | variable `SEED_RELEASE_TAG`, `evals/golden_retrieval.json`; the built-in token reads the Release | skips without the variable | Downloads `chroma.tgz` from that release and runs your golden retrieval questions through the app's real retrieval; fails below the threshold in the golden file. Not part of `tests`. |

---

## Running the image locally

```bash
docker build -t tutor .
docker run --rm -p 8000:8000 --env-file .env tutor
# then open http://localhost:8000/auth/login
```

Without `DATABASE_URL` it uses a SQLite file inside the container, and without
the seed variables it starts with no store, decks or exam bank (each one
warned about in the log). To try the seeds, set the `*_SEED_URL` variables in
`.env`, or mount your local builds:
`-v "$PWD/knowledge-base/chromadb:/app/knowledge-base/chromadb" -v "$PWD/output:/app/output"`.
