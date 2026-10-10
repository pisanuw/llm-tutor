# Operations runbook

Day-to-day operation of a live deployment (the reference setup: Render and a
Supabase Postgres database, as set up in [DEPLOY.md](../DEPLOY.md)). The
companion documents are [retention-policy.md](retention-policy.md) (what is
kept and for how long) and [sandbox.md](sandbox.md) (running student code).

## Where to look first

| Place | What it tells you |
|---|---|
| `https://<your site>/healthz` | `{"status": "ok", "db": "ok", "kb": "ok", ...}` when the database answers `SELECT 1` and the store has slides. `"degraded"` with a 200: the database is fine, retrieval has nothing to search. 503 with `"db": "down"`: the database is unreachable (and whether a Supabase restore was requested). |
| Admin > **Today** | The day's errors, thumbs-down replies, sign-in failures, quota hits, and where students stand. |
| Admin > **Health** | The API key (one tiny billed call), the database, the store, which optional features are configured, and how student code would run. |
| Admin > **Supervision** | Every turn: intent, the help ceiling enforced, the move, the verdict, the detector, cost and latency; spend caps, quotas, restrictions, explain mode. |
| Admin > **Logins** (or `python -m scripts.login_report --days 3`) | Who signed in, who could not, and why. |
| Render logs | The boot (`[entrypoint]` lines), unhandled errors, and the structured `tutor.activity` events. |
| Alert emails | Sent to `ALERT_EMAILS` (default `ADMIN_EMAILS`): unhandled errors, the database going down and recovering, dead links students followed, and what a boot started without. |
| Actions tab | Failed scheduled jobs (each also emails `ADMIN_EMAILS` when Resend is configured). |

## Access

* Sign-in is whitelist-gated by default. Admin > **Access** turns the
  whitelist on and off and adds or removes entries: a full address
  (`ada@example.edu`) or a whole domain (`@example.edu`). Admins
  (`ADMIN_EMAILS`) always pass it.
* To admit a class: add their addresses or their domain on the Access tab and
  keep the whitelist **on**. Rostering a student in a class does not let them
  sign in; the whitelist does.
* To remove someone's access: delete their entry (or their domain's) on the
  Access tab. Existing sessions last until they expire (`JWT_EXPIRY_DAYS`,
  default 7); rotating `SECRET_KEY` ends every session at once.

## Turning the tutor on and off

* **On:** a valid `ANTHROPIC_API_KEY` on the service.
* **Off, gently:** set `MONTHLY_COST_CAP_USD` to a value already spent (for
  example `0.01`). Every turn is then refused with the tutor's "short rest"
  message, the rest of the site keeps working, and nothing more is billed.
* **Off, immediately:** replace `ANTHROPIC_API_KEY` with an invalid value
  (for example `off`). Every model call fails, so nothing is billed, and the
  site stays up. Do not leave it empty: the app refuses to start without the
  variable. Either change redeploys the service.

## Limits

* `DAILY_EXCHANGE_LIMIT` (chat turns per student per day) and
  `MONTHLY_COST_CAP_USD` (the whole class, per calendar month in UTC) are
  environment variables, both off (0) until you set them. `run_turn` checks
  them (`usage.over_caps`), so every way of reaching the tutor inherits them.
* `DAILY_REVIEW_LIMIT` (graded spaced-review answers per student per day,
  default 60, 0 off) and the same monthly cap guard `POST /review/grade`
  (`usage.over_review_caps`). Review grading is ledgered, so it counts toward
  the monthly cap. A review answer is accepted only for a goal that is due
  and has no pending mastery check.
* **Per-student dollar quotas** (`tutor/quotas.py`): a day, a week (from
  Monday) and a month of model spend per student, set on Admin > Supervision
  (defaults $1.50, $6, $15; 0 turns one off). The windows follow the class's
  time zone (`timezone` in `courses.yaml`), so the student's message says when
  the quota comes back. Every ledgered call under the student counts except
  the prompt-cache warm-up; admins are exempt. A hit is one zero-cost `quota`
  ledger row per student, window and period; the Today panel and the
  Supervision tab list them, and `quota-report.yml` emails the roll-ups the
  same tab switches on.
* `python -m pipeline cost-report` summarizes spend from the ledger.
* Running student code is **off** unless `ENABLE_CODE_EXEC=true`, and the
  subprocess backend refuses to run wherever `DATABASE_URL` is set, so on a
  production deployment it never runs ([sandbox.md](sandbox.md)). The Health
  tab's "Code execution sandbox" row says what would happen.

## Symptoms, checks, fixes

| Symptom | Check | Fix |
|---|---|---|
| **The site does not load** | `/healthz`; Render's service status and logs; the last deploy | A failing deploy: roll back in Render (Events > a previous deploy > Rollback) or redeploy the last good commit with `gh workflow run deploy.yml -f ref=<sha>`. A crash loop at boot: the log names it (a bad `courses.yaml` stops the app with the field it dislikes). |
| **Every page is slow or 500s; `/healthz` says `"db": "down"`** | Supabase dashboard: is the project paused or over a limit? Admin > Health | A paused free project: with the `SUPABASE_*` pair set, `/healthz` already requested a restore (wait a few minutes); otherwise restore it in the Supabase dashboard, or run `db-keepalive.yml` by hand. Pool timeouts in the log: something holds connections; see the pool settings in DEPLOY.md Step 1 before raising them. |
| **Students cannot sign in** | Admin > Logins (`python -m scripts.login_report --failed`): the outcome column says denied, expired, wrong code, or email failed | Denied: add them (or their domain) on Admin > Access. Email failed: check `RESEND_API_KEY`, the verified domain and `FROM_EMAIL` in Resend's dashboard. Links landing on the wrong host: `BASE_URL` and `ALLOWED_HOSTS`. Google sign-in failing: the redirect URI in the Google console must match `https://<host>/auth/google/callback` for the host they used. |
| **The sign-in page shows the link and code itself** | `RESEND_API_KEY` | Set it at once: in this mode anyone who types an admin's address can sign in as that admin. |
| **Replies never cite the material; `"kb"` is not ok** | The boot log's ChromaDB lines; `/healthz` | The store seed failed or was rejected (no documents in any collection, or a truncated download). Fix the seed and redeploy; to replace a store that is already on the disk, move it aside first (DEPLOY.md Step 2, Replacing a seed later). |
| **The slide panel shows 404** | The boot log's slide lines (`seed_check slides` names the missing lectures) | Render the missing decks, rebuild `slides.tgz`, update `OUTPUT_SEED_URL`, redeploy. |
| **No narration** | `AUDIO_SEED_URL`; the boot log; was the deck rendered after its audio? | Seed the audio (to replace audio already on the disk, move `<mount>/lecture-audio` aside first, as DEPLOY.md Step 2 describes); re-render decks with `render-html --force` after `render-audio`. |
| **Mastery checks never appear** | `MASTERY_EXAMS_URL`; the boot log's exam-bank line | Generate, upload and point the variable at the new asset (a re-upload changes its id). |
| **A mastery check misbehaves** | The student's attempts in Admin > Supervision | Grading fails closed by design: a grader error leaves the attempt pending rather than passing or failing it. Attempts are capped per goal per day. The database is the lever for anything else. |
| **The auditor flags under-blocks (the live tutor too lenient)** | The disagreement queue on Admin > Supervision; the nightly audit report ([audit/README.md](audit/README.md)) | Inspect the turns. A real leak: tighten the detector or the judge and add the case to `tests/sim/corpus.py` ([sim/README.md](sim/README.md)); consider turning explain mode off. |
| **A student is abusive or games the help gate** | Their turns on the Supervision tab | Supervision > Restrict caps their help ceiling at H1; Clear when resolved. `python -m scripts.clear_restriction <email> [--set] --allow-prod` does the same from a terminal. Never ban an enrolled student automatically. |
| **Cost spikes** | Supervision: the caps panel and the costliest turns; `python -m pipeline cost-report`; the cache share in the audit report | Lower `DAILY_EXCHANGE_LIMIT`, `MONTHLY_COST_CAP_USD` or the quotas; worst case, switch the tutor off (above). A falling prompt-cache share (under about 60 percent of input read from cache) roughly doubles cost: look for a change to the system prompt or to `CACHE_PREWARM`. |
| **A scheduled workflow fails** | Its run log in the Actions tab | Usually a missing secret or the example URL still in the file (DEPLOY.md Step 6). The [Workflows](../DEPLOY.md#workflows) table says what each needs. |
| **A bad deploy** | Render's deploy log; `/healthz` | Roll back in Render, or `git revert` the commit on a branch, merge once `tests` is green, and run the deploy workflow. |
| **A deletion request** | `python -m scripts.delete_student_data <email>` (a dry run: counts only) | Within 30 days (or your policy's window): `--confirm --allow-prod`. Then remove the student from your local exports and downloads, and their whitelist entry if they should lose access ([retention-policy.md](retention-policy.md)). |

## Rollback

* **Code:** `git revert <sha>` on a branch, open a pull request, merge once
  `tests` passes, then deploy with the workflow (pushing to `main` does not
  deploy). For an emergency, Render's dashboard can roll back to any earlier
  deploy at once.
* **Schema:** migrations are additive and guarded, and run at boot
  (`python -m tutor.migrate`). A bad revision is fixed by a new revision, not
  a downgrade. `RUN_MIGRATIONS=0` on the service skips the step while the fix
  ships.
* **Data:** restore a nightly backup (DEPLOY.md, Nightly database backups).

## Routine checks

* **Daily:** the morning digest email; Admin > Today (errors, thumbs-down,
  sign-in failures, quota hits).
* **Weekly:** the auditor's disagreement queue and cache share; cost against
  your budget (`python -m pipeline cost-report`); the link-check and
  retrieval-eval results; candidate misconceptions waiting for approval
  (`python -m scripts.mine_misconceptions --list`).
* **Monthly:** the restore drill (the 3rd) restores a fresh dump into an empty
  Postgres; it emails on failure.
* **Each term:** decrypt the latest backup artifact with your offline key
  (the drill cannot test the key); review the whitelist and archive finished
  classes; confirm the retention sweep is running (its log lists what it
  anonymized); purge your local exports past their window.
* **Before each term, and after any change to the supervision code:** run
  `python -m tests.sim.run_sim` (offline, free). If it exits 2 (UNMEASURED:
  the supervision code changed since the last live run), run
  `python -m tests.sim.run_sim --live --write` with a real `ANTHROPIC_API_KEY`
  (billed: a dollar or two and 15 to 25 minutes) and keep the
  `docs/sim/report.md` it writes ([sim/README.md](sim/README.md)).
