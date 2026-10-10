# Data retention and consent policy (template)

**This is a template.** It describes what this software stores, who can see
it, and what it deletes or anonymizes by default, so that whoever runs a
deployment can turn it into their own policy. Replace the placeholders in
angle brackets, settle every item under
[Decide before you launch](#decide-before-you-launch), and have the result
reviewed by whoever is responsible for student records at your institution.

It is not legal advice. The rules that apply depend on where you are and who
your students are: for example FERPA for education records in the United
States, the GDPR in the European Union and the United Kingdom, and your
institution's own policies on student data and on third-party AI services.
Check them before any student signs in.

Placeholders: `<Institution>`, `<Course or program>`, `<Operator>` (who runs
the deployment, usually the instructor), `<contact address>` (where requests
go; the sign-in notice uses `instructor_email` from `courses.yaml`).

---

## Decide before you launch

- [ ] **Who is responsible** for the data (`<Operator>`, `<Institution>`), and
      who handles requests at `<contact address>`.
- [ ] **The service providers.** Each turn sends the student's message and
      recent conversation to Anthropic; the site runs on your host (Render in
      the reference setup) and database (Supabase); email goes through Resend;
      optional features add Google (sign-in), ElevenLabs (voice), Sentry
      (errors) and GitHub (scheduled jobs and backups). Confirm your
      institution allows each one, whether an agreement with the provider is
      needed, where the data is stored, and read each provider's terms (for
      example, whether API data is used for training and how long it is kept).
- [ ] **Operational use or research.** The defaults assume the data is used to
      run and improve the tutor only. Analysing transcripts for research or
      publication usually needs an ethics (IRB) review before any data is
      collected.
- [ ] **The retention window.** By default identifiable data is anonymized one
      year after a student's last activity (`retention-sweep.yml` runs
      `scripts/retention_sweep.py`, whose `--days` defaults to 365). If you
      choose another window, change the workflow's command and the sign-in
      notice together, and check that the scheduled sweep actually runs (the
      [Workflows](../DEPLOY.md#workflows) table in DEPLOY.md lists what its
      step needs).
- [ ] **Browser activity events.** On by default (`ACTIVITY_EVENTS`). Decide
      whether to keep them, and for how long (`ACTIVITY_EVENTS_DAYS`, which
      must reach the sweep workflow's environment to take effect).
- [ ] **Instructor access.** A class's instructors can read the conversations
      of the students on that class's roster. If that is not acceptable in
      your setting, do not give instructors the instructor role.
- [ ] **The notice students read.** The sign-in page
      (`tutor/web/templates/login.html`) and the help page
      (`tutor/web/templates/_help_content.html`) say what is recorded, who can
      read it, that the tutor is not used for grading, the one-year window,
      and how to ask for deletion. Make the text match your decisions. The
      sign-in notice links to the course site (`/book/index.html`) for "the
      full retention and consent notice": publish this policy there, or change
      the link.
- [ ] **Grading.** The notice says the tutor's assessments are formative and
      not part of the course grade. Keep it true, or change the notice.
- [ ] **Deletion requests**: who carries them out and within what time (this
      template says 30 days).
- [ ] **Backups**: how long the encrypted nightly backups are kept (21 days,
      `retention-days` in `db-backup.yml`) and who holds the private key.
- [ ] **Logs**: keep `LOG_CONVERSATION_CONTENT` off, and know how long your
      host keeps application logs.
- [ ] **The sign-in whitelist** keeps the addresses you add; neither deletion
      nor anonymization removes an exact-address entry. Plan to prune it each
      term.
- [ ] **Local copies**: who may export or download data, where the copies live,
      and when they are deleted.
- [ ] **Minors**: if any students may be under the age of digital consent
      where you are, additional rules may apply.

---

## What the tutor stores

Everything below lives in the deployment's Postgres database. "Student"
includes instructors and admins, who also have accounts.

| Data | Table | Why |
|------|-------|-----|
| Account: email, name, Google account id, created, last seen, last sign-in, sign-in count, the instructor and restriction flags, the weekly-email switch, when it was anonymized | `students` | sign-in, rosters |
| Sign-in links and codes (the address they were sent to); pruned once used or expired | `magic_links` | email sign-in |
| Signed-in sessions: when, how, when they expire | `sessions` | sign-in |
| Sign-in attempts: address, method, outcome, host, browser string (never the link or the code) | `login_attempts` | the admin Logins tab: why a student could not sign in |
| Addresses and domains allowed to sign in | `whitelist` | access control |
| Conversations: every student message and tutor reply, with course, lecture and slide | `messages` | running the tutor, continuity, instructor review |
| Rolling summaries of each student's conversation | `session_summaries` | continuity across long conversations |
| Code submissions and the feedback on them | `exercise_attempts` | feedback, mastery |
| Thumbs up or down on replies, the optional "what was wrong" text, and when staff marked a flag seen | `message_feedback` | finding bad replies |
| Every rating a student gave, in order, with its text | `feedback_events` | a later thumbs-up does not erase an earlier complaint |
| Telemetry for every turn and every supporting model call: intent, the help ceiling enforced, the teaching move, the verdict, the detector, grounding, tokens, cost, latency, the turn id | `turn_events` | supervision, cost, audit |
| Judge and auditor verdicts on turns, with their reasons | `judge_samples` | calibrating the supervisors |
| Mastery per learning goal | `student_progress` | the help ceiling, the progress panel, the map |
| Graded evidence: exercise and mastery-check scores | `graded_events` | mastery |
| Mastery-check attempts: the questions asked, the outcome, when (the student's answers are in `messages`) | `mastery_attempts` | gating the next goal |
| The student's chosen learning track, and their path choices | `student_paths`, `path_events` | navigation, suggestions |
| Predicted score before a code evaluation, with the first 200 characters of the submission | `score_predictions` | calibration |
| Per-student misconception notes | `misconceptions` | targeted help |
| The cohort misconception library, with up to three students' notes copied in as examples | `goal_misconceptions` (`goal_misconceptions.examples_json`) | teaching what many students get wrong |
| Topics a student touched | `topics_accessed` | the progress panel |
| Browser activity: page views, clicks on controls (with the control's visible `label`), visibility and heartbeats, questions and replies by id, slides shown and how long, narration played; ids only, never an email or a message's text | `activity_events` | how the site is used: lectures listened to, slides read, time between questions |
| One row per student per local day with any activity: counts and minutes only, folded from the tables above | `student_days` | comparing a student with the class without reading raw rows |
| Error pages hit: status, method, path (no query; sign-in tokens removed), same-site referrer, the browser's class, the reference shown, and a student's "report this" click | `request_errors` | finding dead links and broken pages |
| Classes, their rosters, and who teaches each | `classes`, `class_enrollments`, `class_instructors` | the instructor pages and email |

Tables with no student data: `settings` (operator switches such as the
quotas and the whitelist flag), and the curriculum: `learning_goals`,
`goal_prerequisites` and `tracks`.

## Copies outside the database

- **The local analytics store** (`python -m tutor.analytics export`,
  [analytics-cookbook.md](analytics-cookbook.md)): a SQLite copy of the
  structured tables on the operator's machine, pseudonymous unless exported
  with `--with-emails`, and without message text unless `--with-content`.
- **Downloaded conversations** (`python -m scripts.download_conversations`):
  Markdown transcripts, under `~/tutor-data/conversations` by default. The
  default tree is pseudonymous (`S<id>`, with roster names and addresses
  scrubbed from the message text too); `--with-names` writes an identifiable
  tree beside it. The script refuses to write inside a git working copy and
  makes its files readable by their owner only.
- **Report emails**: the daily digest, the thumbs-down report, the quota
  roll-ups and the weekly instructor email name students and summarize their
  activity. They live in the recipients' mailboxes. (The daily report run by
  hand also writes per-student Markdown files under `reports/`, which git
  ignores.)
- **Backups**: the nightly dump, encrypted to the operator's key, kept 21 days
  as a GitHub Actions artifact.
- **Audit reports**: the nightly auditor's report names students only as
  `s<id>` and is kept 14 days as an Actions artifact.
- **Application logs** (the host's log store): the structured `tutor.activity`
  events carry ids and metadata (student id, course, intent, verdict, cost,
  latency), never an email, and carry conversation text only if
  `LOG_CONVERSATION_CONTENT=true`. The `tutor.eval_details` log records the
  code evaluator's server-only notes (they can contain a full solution).
  Logs age out on the host's schedule, and the tools below cannot reach them;
  `LOG_LEVEL=WARNING` keeps both out of the log altogether.
- **The voice cache** (`TTS_CACHE_DIR`, on the persistent disk): audio of
  tutor replies students asked to hear, named by a hash of voice and text,
  not by student. Nothing prunes it; delete the folder if your policy needs
  it gone.
- **Error reports** (Sentry, only when `SENTRY_DSN` is set): errors with
  request bodies, cookies, query strings, auth headers, sign-in tokens and
  SQL parameters removed before an event leaves the app.

Every local copy is student data under the same rules as the database: never
in a repository or shared, a student's rows or files deleted on a deletion
request, and not kept identifiable past the retention window.

The tutor writes nothing to a learning management system or a gradebook.

## Who can see what

- **The student**: their own conversations, progress and map.
- **A class's instructors**: for the students on that class's roster, in that
  class's course: roster status, activity, progress, and conversations (the
  Instructor page, and the weekly email, which names students). No costs, and
  nothing about other classes.
- **Admins** (`ADMIN_EMAILS`, or anyone holding `ADMIN_TOKEN`): everything in
  the admin pages: every conversation, the turn ledger with costs, sign-ins,
  timelines, activity, error pages, feedback.
- **Operators**: anyone with the database connection string, the hosting
  accounts, or write access to the repository (which can read its Actions
  secrets).
- **Service providers**, as processors: Anthropic receives each turn's prompt
  (the system prompt, the retrieved course material, the recent conversation
  and the rolling summary); the host and the database provider store
  everything; Resend sees addresses and the emails' contents; Google sees a
  Google sign-in; ElevenLabs receives the text of replies a student asks to
  hear; Sentry receives scrubbed error events; GitHub holds the encrypted
  backups, the audit reports and the workflow logs.

No one else. The data is not sold or shared outside the course.

## Retention windows (the defaults in the code)

| Data | Kept | How |
|------|------|-----|
| Identifiable data (everything in the table above that names or belongs to a student) | until **one year after the student's last activity**, then anonymized | the daily retention sweep |
| Used or expired sign-in links | until the next sweep | the sweep, every run |
| `login_attempts` | 90 days | the sweep, every run |
| `request_errors` | 90 days | the sweep, every run |
| Raw `activity_events` | with the rest (anonymized at one year), or `ACTIVITY_EVENTS_DAYS` days when that is set where the sweep runs | the sweep |
| Encrypted backups | 21 days | Actions artifact expiry |
| Audit reports | 14 days | Actions artifact expiry |
| Application logs | the host's log retention | the host |
| On request | deleted within 30 days | the deletion path below |

"Last activity" is the latest of: last seen, last sign-in, newest message,
newest turn, newest browser event. A student who keeps using the tutor is
never anonymized; one who stops is anonymized a year later.

### Anonymization

At one year the sweep removes every direct identifier and all free text, and
keeps only de-identified structured data, under the student's row turned into
a pseudonym. That data is used only to improve the tutor for future classes.
The per-table rule is `state.ANONYMIZE_POLICY`; `tests/test_erasure.py` fails
if a table that can hold a student's rows has no rule, so a new table cannot
be forgotten.

| Removed | Kept, de-identified |
|---|---|
| Email, name and Google id on `students` (the email becomes `anon-<id>@anonymized.invalid`) | the `students` row as a pseudonym, with its counters and flags |
| `sessions`, `magic_links`, `login_attempts` | `turn_events` metadata: intent, ceiling, move, verdict, rule id, grounding, tokens, cost, latency |
| `messages`, `message_feedback`, `feedback_events`, `session_summaries` | `graded_events` and `mastery_attempts` scores, `student_progress` mastery, `student_paths` and `path_events` |
| Free text blanked: `exercise_attempts.submission` and `.feedback`, `topics_accessed.topic_tag`, `misconceptions.description`, `score_predictions.context`, `judge_samples.reason`, `activity_events.label`, and the judge reason and error strings inside `turn_events.verdicts_json` | `score_predictions` predicted against actual; `activity_events` (event, time, dwell, slide, ids) |
| The student's notes copied into `goal_misconceptions.examples_json` | `student_days`, `request_errors`, and the class rosters (`classes`, `class_enrollments`, `class_instructors`) under the pseudonym |

The sweep is `scripts/retention_sweep.py`, a dry run unless `--confirm`;
against a production database it also needs `--allow-prod`:

```bash
python -m scripts.retention_sweep                          # dry run: who is eligible, what would be pruned
python -m scripts.retention_sweep --confirm --allow-prod   # prune and anonymize (what the daily workflow runs)
python -m scripts.retention_sweep --email ada@example.edu --confirm --allow-prod   # anonymize one student now
python -m scripts.retention_sweep --list                   # every student's last activity and status
```

`state.anonymize_student_data` does the work for one student and is
idempotent.

## Consent and disclosure

Before signing in, a student reads a notice on the sign-in page (and the same
points on `/help`): the tutor is an educational AI assistant in
`<Instructor>`'s voice that can be wrong and will not give full solutions;
their questions, code, progress and use of the site are recorded to run and
improve it; their class's instructor can see how they use it and read their
conversations for that class; it is not used to grade them; the data is not
sold or shared outside the course; identifiable records are kept up to one
year and then anonymized; and they can ask for deletion at any time at
`<contact address>`. Decide with your institution whether signing in after
that notice is enough, or whether you need explicit consent.

## Deletion on request

`state.delete_student_data` removes every row that belongs to the student
and the `students` row itself. The rows are found by `state.erasure_scope`,
which follows foreign keys from the student's row, so feedback on the
student's replies, judge samples on their turns and their activity go too,
and their notes are removed from the cohort misconception library. Run it
with:

```bash
python -m scripts.delete_student_data ada@example.edu                         # dry run: counts only
python -m scripts.delete_student_data ada@example.edu --confirm --allow-prod  # delete
```

Before and after:

- **An instructor's deletion removes the classes they lead, with those
  classes' rosters.** Hand the class to another instructor first (Admin >
  Classes) if it must survive.
- It does not touch an exact-address entry in the `whitelist` (remove it on
  Admin > Access if the student should lose access), the application logs,
  report emails already sent, or the encrypted backups (gone within 21 days).
- Delete the student from every local copy: the analytics store
  (`DELETE FROM <each table> WHERE student_id = ?`, then the `students` row)
  and the downloaded conversations.
- `--messages-only` is not an erasure: it clears a student's conversation,
  its feedback and the rolling summary and keeps everything else (meant for a
  clean demo account).

Honour a request within 30 days (or your policy's window).
