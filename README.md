# llm-tutor

A supervised AI tutor for your course, built on Claude. Students chat with it
in the browser; it answers from **your** slides, notes, transcripts and
textbook, in **your** voice, gives one hint at a time instead of handing over
answers, tracks what each student has mastered, and tells you how your class
is using it.

It ships configured for a small made-up example (two introductory astronomy
courses taught by a fictional "Dr. Morgan", with a tutor called "Astra") so it
runs the moment you clone it. **[ADDING-KNOWLEDGE.md](ADDING-KNOWLEDGE.md)**
walks you through replacing that example with your own course, step by step.

## What it does

**For students**

* A chat that searches your course material before it explains anything,
  cites it, and opens the matching lecture slide beside the conversation.
* Guidance, not answers: a hint ladder (H0 to H7) whose ceiling rises or falls
  with the student's mastery of the topic; a detector and a separate judge
  model check every draft before it is shown.
* Learning goals with prerequisites, a visual learning map, "what next"
  suggestions along a track, short mastery checks, and spaced review that
  brings topics back before they fade.
* Practice exercises from your bank, "Teach me" openers in your words,
  optional interactive explorables, an optional course book, and an optional
  read-aloud voice.
* Works on phones and desktops.

**For instructors and operators**

* Classes with rosters and co-instructors; an instructor dashboard and a
  weekly email (who is active, who went quiet, what the class struggles with).
* Admin pages: conversations, the supervision ledger (every turn's help
  ceiling, move, verdict and cost), thumbs-down feedback, sign-in problems,
  error reports, spend caps and per-student quotas.
* Offline analytics on a pseudonymous local copy of the data.
* Privacy defaults: sign-in whitelist, data retention and anonymization
  sweeps, exports that never leave your machine (see
  [docs/retention-policy.md](docs/retention-policy.md)).

## Quick start (the example course)

Requirements: Python 3.11, and an [Anthropic API key](https://console.anthropic.com/).

```bash
git clone https://github.com/<you>/llm-tutor.git && cd llm-tutor
python3.11 -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt

cp .env.example .env
#   set ANTHROPIC_API_KEY, SECRET_KEY (openssl rand -hex 32), ADMIN_TOKEN,
#   and ADMIN_EMAILS=<your email>

python -m pipeline check-content                 # validate the content
python -m pipeline index --rebuild               # build the knowledge base from knowledge-base/raw (local, free;
                                                 #   the first run downloads the ~80 MB embedding model)
python -m pipeline render-html --course AST101   # render the example slide decks (local, free)
python -m pipeline render-html --course AST102

.venv/bin/uvicorn tutor.main:app --reload        # open http://localhost:8000/auth/login
```

Sign in with the address you put in `ADMIN_EMAILS`. Without an email provider
configured (`RESEND_API_KEY`), the sign-in link is shown on the page. Pick a
course on the welcome page and ask "Why does the Moon have phases?".

`./run-locally.sh` does the same with a throwaway SQLite database, even if
your `.env` points at a production database.

## Make it yours

Everything course-specific lives in a handful of files you edit; no Python
changes are needed:

| File | What you specify |
|---|---|
| `courses.yaml` | The tutor's name, your name, the subject, your time zone, and your courses (id, title, number of lectures, where the source files are). |
| `persona/`, `instructor-profile/` | How you talk and how you teach; this becomes the system prompt. |
| `knowledge-base/raw/` | Your slides, transcripts, textbook, assignments and notes, as JSON the pipeline extracts from your files. |
| `curriculum/` | Lecture plans, learning goals and prerequisites, tracks, exercises, starter prompts. |
| `output/<course>/lecture-NN/content.md` | Slide decks (optional). |
| `explorables/`, `book/` | Optional interactives and a course book. |

**[ADDING-KNOWLEDGE.md](ADDING-KNOWLEDGE.md)** explains every field, every
command, what each step costs, and how to check it.
`python -m pipeline check-content` validates all of it.

## How it is built

Two parts share the course content:

1. **`pipeline/`**, an offline CLI (`python -m pipeline --help`) that ingests
   your source files, builds the ChromaDB knowledge base, drafts lecture plans,
   learning goals, exercises, mastery-check questions, slide decks and
   narration scripts (with Claude, mostly through the local `claude` CLI so
   your subscription is billed), and renders decks.
2. **`tutor/`**, the FastAPI web app. Every student message runs through one
   supervised pipeline (`tutor/agent/turn.py`): spend caps, a pending-exam
   intercept, an intent classifier, the help envelope from the student's
   mastery, a pedagogy strategist, the actor model with four tools
   (`find_topic`, `get_lecture_outline`, `get_exercise`, `evaluate_code`), a
   code-leak detector, a judge, and a telemetry ledger.

```
courses.yaml            who the tutor is and which courses it serves
persona/                your voice and teaching method (system prompt)
instructor-profile/     how your lectures go (system prompt)
curriculum/             lecture plans, goals, tracks, exercises, prompts
knowledge-base/raw/     extracted source text; knowledge-base/chromadb/ is built from it
output/                 slide decks (content.md tracked, index.html generated)
explorables/, book/     optional interactives and course book
tutor/                  the web app (agent/, routers/, web/templates, web/static, db/)
pipeline/               the content CLI
scripts/                reports, exports, maintenance
migrations/             Alembic schema migrations (run at boot)
tests/                  the test suite, on its own fixture course (tests/fixtures/content)
docs/                   runbook, sandbox, retention policy, analytics cookbook
```

[CLAUDE.md](CLAUDE.md) is a deeper tour of the architecture for contributors
(and for Claude Code).

## Deploying

The reference deployment is a Docker web service on Render with a Supabase
Postgres database; the vector store, rendered decks, narration and the
mastery-check bank travel as seed files downloaded at boot. See
[DEPLOY.md](DEPLOY.md). Any host that runs a Docker container with a
persistent disk and a Postgres database will do.

## Costs

The running tutor calls the Anthropic API on every turn (the actor plus small
supervisor calls), with prompt caching. Spend is bounded by
`DAILY_EXCHANGE_LIMIT`, `MONTHLY_COST_CAP_USD`, and per-student daily, weekly
and monthly quotas you set on the admin page. Building the knowledge base and
rendering decks is local and free; drafting content with Claude is billed to
your subscription (CLI) or API key, as listed in
[ADDING-KNOWLEDGE.md, section 13](ADDING-KNOWLEDGE.md#13-what-costs-money).

## Student data

The tutor stores conversations, progress and activity events. Before using it
with students, read [docs/retention-policy.md](docs/retention-policy.md),
decide what you will tell them (the sign-in page and help page carry a notice
you should review), and check your institution's rules on student records and
third-party AI services.

## Development

```bash
pip install -r requirements-dev.txt
pytest                      # network-free; runs against tests/fixtures/content, never your content
ruff check . && mypy        # lint and type check, as CI does
git config core.hooksPath .githooks   # Conventional Commits check on commit
```

Browser tests run only where Playwright and Chromium are installed.

## License

MIT. See [LICENSE](LICENSE).
