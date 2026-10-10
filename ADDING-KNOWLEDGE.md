# Adding your course knowledge

This guide takes you from a fresh clone, which teaches a small made-up
astronomy course, to a tutor that teaches **your** course in **your** voice.
It says, for every step, what you write, where it goes, which fields you must
fill in and what each one does, which command turns it into something the
tutor uses, what that command costs, and how to check the result.

Read the [overview](#0-how-the-tutor-uses-your-knowledge) once, then work
through the steps in order. Steps 1, 2, 4, 5 and 6 are the minimum for a
useful tutor; the rest make it better.

```
1  courses.yaml                      who the tutor is, which courses        (required)
2  persona/, instructor-profile/     how you talk and teach                 (strongly recommended)
3  knowledge-base/                   your slides, notes, transcripts, text  (strongly recommended)
4  curriculum/<COURSE>-lecture-plan.md   what each lecture covers           (required)
5  curriculum/learning_goals.json    the skills, and what depends on what   (required)
6  curriculum/tracks.json            the order goals are suggested in       (required)
7  curriculum/exercises.json         practice problems                      (recommended)
8  curriculum/mastery_exams.json     mastery checks (generated)             (recommended)
9  output/<COURSE>/lecture-NN/       slide decks shown beside the chat      (optional)
10 extras                            starter prompts, Teach-me openers,     (optional)
                                     explorables, a course book, narration
11 check, run, and deploy
```

After every step, run:

```bash
python -m pipeline check-content
```

It reads every file this guide mentions and reports ERRORs (something will
break), WARNINGs (worth fixing) and NOTEs (things not generated yet). It
makes no model calls and costs nothing.

---

## Contents

0. [How the tutor uses your knowledge](#0-how-the-tutor-uses-your-knowledge)
1. [Name the tutor and list your courses (`courses.yaml`)](#1-name-the-tutor-and-list-your-courses-coursesyaml)
2. [Describe how you talk and teach (`persona/`, `instructor-profile/`)](#2-describe-how-you-talk-and-teach)
3. [Load your source material into the knowledge base](#3-load-your-source-material-into-the-knowledge-base)
4. [Write a lecture plan for each course](#4-write-a-lecture-plan-for-each-course)
5. [Define the learning goals and their prerequisites](#5-define-the-learning-goals-and-their-prerequisites)
6. [Define tracks](#6-define-tracks)
7. [Write practice exercises](#7-write-practice-exercises)
8. [Generate the mastery-check question bank](#8-generate-the-mastery-check-question-bank)
9. [Add slide decks](#9-add-slide-decks)
10. [Extras: starters, Teach-me, explorables, the book, narration](#10-extras)
11. [Check, run, and deploy](#11-check-run-and-deploy)
12. [Changing content later](#12-changing-content-later)
13. [What costs money](#13-what-costs-money)
14. [Troubleshooting](#14-troubleshooting)
15. [Checklist](#15-checklist)
16. [Appendix: a worked example, from syllabus to tutor](#appendix-a-worked-example-from-syllabus-to-tutor)

---

## 0. How the tutor uses your knowledge

Every student message goes through one supervised pipeline
(`tutor/agent/turn.py`). Your content enters it in five places:

| What you provide | Where it lives | What the tutor does with it |
|---|---|---|
| Identity: tutor name, instructor, subject, courses | `courses.yaml` | Page titles, greeting, emails, the first lines of the system prompt, the course picker, every grader's and classifier's idea of the subject. |
| Voice and teaching method | `persona/`, `instructor-profile/` | Pasted into the system prompt (cached), so every reply sounds like you and follows your method. |
| Source material | `knowledge-base/raw/` turned into `knowledge-base/chromadb/` | Searched by the `find_topic` tool before the tutor explains a concept; the tutor cites what it finds and moves the slide panel to the matching deck. |
| Curriculum: lectures, goals, prerequisites, tracks | `curriculum/` | Decides what the question is about (the classifier maps a question to goals), how much help is allowed (the hint ceiling depends on the student's mastery of those goals), what to suggest next (the navigator walks the track and the prerequisite graph), the learning map, and the progress panel. |
| Practice and assessment | `curriculum/exercises.json`, `mastery_exams.json`, `teachme/` | `get_exercise` hands out your exercises; mastery checks gate the next goal; spaced review brings goals back; Teach-me openers start a topic in your words. |

Two kinds of files are involved:

* **Content you write and commit**: `courses.yaml`, `persona/`,
  `instructor-profile/`, `curriculum/` (except `mastery_exams.json`),
  `explorables/`, `book/`, and the slide sources `output/*/lecture-*/content.md`.
* **Artifacts the pipeline generates** (gitignored, rebuilt or seeded at
  deploy): the vector store `knowledge-base/chromadb/`, rendered decks
  `output/*/lecture-*/index.html`, narration audio, `curriculum/mastery_exams.json`.

All content you write lives under one **content root**, the repository root by
default. Setting `TUTOR_CONTENT_DIR=/path/to/folder` moves `courses.yaml`,
`curriculum/`, `persona/`, `instructor-profile/` and `explorables/` there,
which is handy if you keep your course in a separate private repository. The
test suite uses this to run against its own frozen copy
(`tests/fixtures/content/`), so editing your content never breaks a test.

### Before you start

```bash
python3.11 -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt     # the web app, the tests, most pipeline commands
pip install -r requirements.txt         # document readers for `ingest`, Whisper for `transcribe` (large)
cp .env.example .env                    # then set ANTHROPIC_API_KEY, SECRET_KEY, ADMIN_TOKEN, ADMIN_EMAILS
```

Many pipeline steps draft content with Claude through the local `claude` CLI
(Claude Code), which bills your Claude subscription instead of an API key.
Install it and sign in once (`claude` in a terminal) if you want those steps.
[Section 13](#13-what-costs-money) lists which steps call a model and how.

Keep raw course files (decks, PDFs, recordings, anything with student work or
grades) in `source-materials/`, which is gitignored. Never commit student
records or copyrighted books.

---

## 1. Name the tutor and list your courses (`courses.yaml`)

`courses.yaml` at the content root is the one file that names your courses.
The web app reads it at startup (`tutor/courses.py`) and the pipeline reads it
for every command (`pipeline/config.py`). A malformed file stops the app at
boot with a message naming the field.

### What to specify

```yaml
tutor:
  name: Astra
  instructor: Dr. Alex Morgan
  instructor_short: Dr. Morgan
  instructor_email: instructor@example.edu
  institution: Example College
  subject: introductory astronomy
  audience: first-year undergraduates with no physics background
  timezone: America/Los_Angeles
  code_language: ""
  site_url: ""
  avatar: ""

courses:
  - id: AST101
    label: AST 101
    title: The Night Sky
    lectures: 4
    topics: the celestial sphere, phases of the Moon and eclipses, seasons, light and telescopes
    note: ""
    sources:
      slides: source-materials/AST101/slides
      assignments: source-materials/AST101/assignments
      exams: source-materials/AST101/exams
      textbook: source-materials/AST101/textbook.pdf
      documents: source-materials/AST101/documents
      topic_list: source-materials/AST101/topics.md
      description: source-materials/AST101/description.txt
```

**`tutor:` block**

| Field | Required | What it is | Where it shows up |
|---|---|---|---|
| `name` | yes | What students call the tutor. Short, friendly, not a real person's name. | Every page title and header, the greeting, sign-in emails, report subjects, "You are <name>" in the system prompt. |
| `instructor` | yes | Your full name as students know it. | The system prompt ("the digital teaching assistant for <instructor>'s ... courses"), sign-in emails. |
| `instructor_short` | no (defaults to `instructor`) | How students address you: "Dr. Morgan", "Professor Lee", "Sam". | "You teach as <instructor_short>", the "Tell <instructor_short>" button on error pages, "Play in <instructor_short>'s voice". |
| `instructor_email` | no | Where "email your instructor" links point. Empty hides the links and says "ask <instructor_short>" instead. | Help page, sign-in page notice, instructor dashboard. |
| `institution` | no | Your school, or empty. | Sign-in page, help footer, system prompt, grader prompts. |
| `subject` | yes | A noun phrase completing "a tutor for ...": "introductory astronomy", "organic chemistry", "Spanish 2", "intro to Python". Be specific; the graders use it to judge answers. | System prompt, mastery and review graders, the code evaluator, the intent classifier, the judge, the conversation summarizer, goal extraction, exam generation. |
| `audience` | no (defaults to "students") | Who your students are, in a few words. | Grader prompts ("for first-year undergraduates ...") so feedback is pitched right. |
| `timezone` | no (defaults to UTC) | Your class's IANA time zone (`America/New_York`, `Europe/Berlin`, `Asia/Tokyo`). | What "today" means: daily quotas and caps, daily and weekly reports, activity rollups, report labels. Validated at startup. |
| `code_language` | no | Leave empty unless students submit code. When set (e.g. `C++`), code prompts, the code box, the code evaluator and photo transcription name that language. The built-in sandbox that compiles and runs student code supports **C++ only** (and is off unless `ENABLE_CODE_EXEC=true`); with another language, code is still evaluated by the model, just not executed. | Tool descriptions, evaluator prompt, chat code box, slide and exam generation. |
| `site_url` | no | Your public address once deployed (`https://tutor.example.edu`). | Help page footer, emails. Leave empty while developing. |
| `avatar` | no | An image file in `tutor/web/static/` (a square photo or logo; 512x512 works). Empty shows the tutor's initial in a circle. | Sign-in, welcome, and next to every tutor reply. |

**`courses:` list** (one entry per course; order = order in the course
picker; the first course is the default for a student who has not picked)

| Field | Required | Rules | What it is for |
|---|---|---|---|
| `id` | yes | 1 to 8 letters or digits, no spaces or dashes. Unique. | It becomes a folder (`output/AST101/`), a URL segment (`/slides/AST101/...`), a file prefix (`curriculum/AST101-lecture-plan.md`), the prefix of every goal id (`AST101-L02-explain-moon-phases`), and the value stored with every message and progress row. **Choose it once**: renaming it later orphans every student's saved progress (see [section 12](#12-changing-content-later)). |
| `label` | no (defaults to `id`) | Any text. | How pages name the course: "AST 101", "Chem 2A". |
| `title` | no | Any text. | Shown under the label: "The Night Sky". |
| `lectures` | yes | 1 to 99. | Lectures are numbered 1..N everywhere: the plan, goal ids, decks, the lecture picker, the deck completeness check at deploy. |
| `topics` | no | One line. | The course card on the welcome page. |
| `note` | no | One line. | An extra line on the course card ("Taking the evening section? Pick this one."). |
| `sources` | no | Paths relative to the repo root (absolute and `~` work). Missing folders are skipped. | Where `python -m pipeline ingest` and friends look for your raw files ([step 3](#3-load-your-source-material-into-the-knowledge-base)). Defaults to `source-materials/<id>/<kind>`. |

`sources` keys: `slides` (a folder of `.pptx`), `assignments` (a folder of
`.docx`, `.txt`, `.md`), `exams` (a folder of `.pdf`; extracted but never
indexed, so the tutor cannot quote answer keys), `textbook` (a `.pdf` or a
folder of `.pdf`), `documents` (a folder of notes, handouts and syllabi:
`.pdf`, `.docx`, `.txt`, `.md`), `topic_list` (a Markdown list of the
course's topics, used by `pipeline map`/`analyze`), `description` (a text file
with the catalog description, used by `pipeline plan`).

### Check

```bash
python -m pipeline check-content
.venv/bin/uvicorn tutor.main:app --reload    # the sign-in page shows your tutor's name and subject
```

---

## 2. Describe how you talk and teach

The system prompt is assembled once at startup (`tutor/agent/prompt_builder.py`)
from `courses.yaml` plus up to eight Markdown files. They are sent to the
model with every turn (from a 1-hour prompt cache), so they shape every
reply. Missing files are allowed; the prompt then says the file is missing.

### `persona/` (who the tutor is)

| File | What to write | Length |
|---|---|---|
| `persona/persona-distilled.md` | The core rules: one paragraph on who you are as a teacher, then 10 to 15 bullet rules ("ask before you tell", "never hand over a finished solution", "admit when you are not sure", "short replies, one question at a time"). | 20 to 40 lines |
| `persona/communication.md` | Your voice: sentence length, humor, formality, phrases you use and avoid, how you format (lists? bold? emoji?), how you greet and close. | 30 to 60 lines |
| `persona/teaching.md` | Your method: how you introduce a concept, how hints escalate, how you handle "just give me the answer", how you correct a misconception, what you praise. One short example exchange helps a lot. | 60 to 150 lines |

Write in the second person to the model or in the first person as yourself;
both work. Be concrete: "I ask students to predict what happens before I show
it" beats "I use active learning".

### `instructor-profile/` (how your lectures go)

| File | What to write |
|---|---|
| `transcripts-voice.md` | How you sound in lecture, ideally with 6 to 10 real short quotes from your recordings. |
| `lecture-flow.md` | The arc of a typical session (warm-up, demo, explanation, check, wrap-up). |
| `teaching-patterns.md` | Your signatures (a demo you always do, how you run a check for understanding) and your grading stance. |
| `examples-library.md` | The examples and analogies you reuse, by topic, so the tutor uses yours instead of inventing new ones. |
| `topic-explanations.md` | For each lecture: how you explain the key ideas and the misconceptions you go after. This file has the most effect on correctness and consistency. |

`python -m pipeline analyze --course <ID>` can draft `examples-library.md`
and a `style-guide.md` from the slides and transcripts already in the
knowledge base ([step 3](#3-load-your-source-material-into-the-knowledge-base)),
and writes a topic map for planning. It **overwrites**
`instructor-profile/examples-library.md` and `style-guide.md`, so run it
before you hand-edit them (or commit first). Treat drafts as drafts: edit them. (The
prompt builder does not read `style-guide.md`; fold anything good from it into
the files above.)

### Rules that are not yours to change here

The hint ladder (H0 = least help, H7 = full solution), the per-turn help
ceiling, exam lockdown, and the "never paste a finished solution below H7"
rule are appended after your files by code, and they win when your persona
conflicts with them. Your files decide *how* the tutor helps, not *how much*.

### Size

Together the eight files should stay under roughly 60 KB. Everything is
cached, but a huge prompt is slower and costlier on the first turn of each
hour, and buries the important rules.

### Check

Restart the server (the prompt is built once), ask the tutor a question, and
read the reply. `python -m pipeline check-content` warns about missing or
empty files.

---

## 3. Load your source material into the knowledge base

The tutor's `find_topic` tool searches a ChromaDB vector store at
`knowledge-base/chromadb/` before it explains anything, and cites what it
finds. The store is built from JSON files in `knowledge-base/raw/<collection>/`,
which you produce from your original files with the pipeline (or write by
hand). The flow is always:

```
source-materials/ (your files, gitignored)
   └─ python -m pipeline ingest / transcribe / canvas-ingest / discord / github
knowledge-base/raw/<collection>/*.json     (reviewable text, can be committed)
   └─ python -m pipeline index --rebuild
knowledge-base/chromadb/                   (the store the tutor searches; gitignored)
```

### The collections

The list is fixed in `tutor/kb.py`; each collection has a builder in
`pipeline/knowledge_base/indexer.py` and reads the folder of the same name.

| Collection | Put here | Produced by | JSON shape |
|---|---|---|---|
| `slides` | Your lecture decks. A slide hit can move the chat's slide panel to the matching rendered deck (via `source-lecture-map.json`, below). | `pipeline ingest` from `.pptx` in `sources.slides`; `pipeline canvas-ingest` | One file per deck: `{"filename": "week2-moon.pptx", "lecture_title": "...", "slides": [{"slide_number": 1, "title": "...", "body_text": ["..."], "speaker_notes": "...", "has_code": false}]}` |
| `transcripts` | What you said in lecture. The richest source of your voice and your explanations. | `pipeline transcribe` (local Whisper) into `raw/transcripts/`, or drop Whisper JSON into `knowledge-base/transcripts/<COURSE>-<term>/` (e.g. `AST101-2026Fall/`; the prefix must be a course id) | `{"source_file": "AST101-2026Fall-lecture-02.mp4", "segments": [{"start": 0.0, "end": 9.5, "text": "..."}]}` (bare `whisper` CLI output without `source_file` also works) |
| `textbook` | The course text, chunked by page. Only text you have the right to use (your own notes, an open textbook). | `pipeline ingest` from `sources.textbook` | A JSON list of chunks: `[{"text": "...", "source_file": "notes.pdf", "page_number": 12, "chunk_index": 0, "doc_type": "textbook"}]` |
| `assignments` | Assignment and project specifications. | `pipeline ingest` from `sources.assignments` (`.docx`, `.txt`, `.md`) | Chunk list: `[{"text": "...", "source_file": "ps1.docx", "section": "Part 2", "doc_type": "document"}]` |
| `documents` | Anything else you wrote: syllabus, handouts, lab sheets, FAQs, lecture notes. | `pipeline ingest` from `sources.documents`; `pipeline canvas-ingest` (PDFs from a Canvas course) | Chunk list, as above |
| `code` | Example code, if your course has any. | `pipeline github --repos <url>` or `--org <org>` | `[{"repo": "...", "relative_path": "...", "language": "python", "content": "..."}]` |
| `forum` | Class discussion threads. **Marked as community content**: the tutor is told these excerpts may be wrong. Strip names first. | `pipeline discord` (DiscordChatExporter JSON) or `pipeline discord-fetch` (bot token) | Chunk list, as above |

Rules the indexer applies: a chunk with fewer than 5 words is skipped (3 for
slides); text is truncated to 2,000 characters (1,000 for a transcript
segment, 3,000 for a code file); a file that is not the JSON its collection
expects is skipped and listed, and `index` then exits 1.

`knowledge-base/README.md` has a minimal example of each shape, and the
example course ships a few files of each in `knowledge-base/raw/`.

### Commands

```bash
# Decks, textbook, exams, assignments and documents named in courses.yaml sources:
python -m pipeline ingest --course AST101 --dry-run     # what would be read
python -m pipeline ingest --course AST101

# Lecture recordings (local Whisper; nothing leaves your machine):
python -m pipeline transcribe --recordings-dir source-materials/recordings/AST101
#   or transcribe elsewhere and copy the JSON into knowledge-base/transcripts/AST101-2026Fall/

# Optional sources:
python -m pipeline canvas --course AST101 --list-courses      # Canvas LMS: find the course id
python -m pipeline canvas --course AST101 --course-id 12345   # download files into source-materials/AST101/canvas/
python -m pipeline canvas-ingest --course AST101
python -m pipeline github --repos https://github.com/you/examples
python -m pipeline discord --exports-dir source-materials/discord

# Build the store (always after any change under knowledge-base/):
python -m pipeline index --rebuild
```

`index --rebuild` drops and rebuilds every collection, so a removed file's
chunks disappear too, and writes `knowledge-base/chromadb/manifest.json`
(inputs with hashes, counts, embedder, commit). It embeds locally with
Chroma's bundled ONNX all-MiniLM-L6-v2 model: no API calls. A few thousand
chunks take a minute or two.

Canvas needs `canvas.base_url` and a token file in `config.yaml`
(`canvas.token_file`); Discord fetching needs `DISCORD_TOKEN` in `.env`;
GitHub org listing uses `GITHUB_TOKEN` if set.

### Map your decks to lectures: `curriculum/source-lecture-map.json`

Your original decks may not be numbered like your lecture plan
(`week3-light.pptx` may be lecture 4). This file tells the tutor which
lecture each indexed deck teaches, so a slide hit opens the right rendered
deck. Without it the tutor still answers; the slide panel just does not move.

```json
{
  "AST101": {
    "map": {"week1-sky.pptx": 1, "week2-moon.pptx": 2},
    "unmapped": ["midterm-review.pptx"]
  }
}
```

Keys are the `filename` values in `raw/slides/*.json`. Matching is exact,
then by case-insensitive stem. `unmapped` documents decks that deliberately
have no lecture (review sessions); it is not required.

### Measure retrieval: `evals/golden_retrieval.json`

A list of questions with a regex the top results must match:

```json
{"pass_threshold": 0.9,
 "questions": [{"id": "g01", "q": "Why does the Moon have phases?", "course": "AST101",
                "regex": "(?i)phase|sunlit|half", "within_top": 3}]}
```

Write 15 to 40 questions students really ask, then run
`python scripts/retrieval_eval.py`. Re-run it whenever you change the
knowledge base. Low scores usually mean the material is missing, chunked
badly (scanned PDFs have no text: OCR them first), or phrased very
differently from students' questions.

---

## 4. Write a lecture plan for each course

One Markdown file per course: `curriculum/<COURSE>-lecture-plan.md`. The
`get_lecture_outline` tool returns a lecture's section verbatim when a
student asks what a lecture covers; the chat's lecture picker and the
lectures page take their titles from it; goal extraction reads its objectives
and prerequisites; slide and script generation follow it.

### Format (parsed by code: follow it exactly)

```markdown
# AST 101: The Night Sky

> **Curriculum Notes**
> - 4 lectures, 75 minutes each

---

## Lecture 01: The Sky Above Us

**Week:** 1
**Duration:** 75 min

**Topics:**
- The celestial sphere
- Daily motion

**Learning Objectives:** By the end of this lecture, students will be able to:
1. Describe the celestial sphere and name its reference points
2. Explain why the stars rise and set
3. Estimate angles on the sky with an outstretched hand

**Time Allocation:**
| Segment | Minutes |
|---------|---------|
| Warm-up question | 10 |

**Key Examples:**
- The fist-at-arm's-length angle ruler

**Demonstrations:**
1. ...

**Assignment Connection:** Problem Set 1, questions 1-3.

**Prerequisite Lectures:** None

---

## Lecture 02: Phases of the Moon and Eclipses
...
**Prerequisite Lectures:** 01
```

What the parsers rely on:

* `## Lecture NN: Title` headings, numbered 01 to the course's `lectures`
  with none skipped (`check-content` verifies this).
* `**Learning Objectives:**` followed by a numbered list `1. ...`; each item
  becomes about one learning goal in step 5. Write them as things a student
  can do ("Compute a star's distance from its parallax"), 3 or 4 per lecture.
* `**Prerequisite Lectures:**` with `None` or lecture numbers (`01`, `01, 03`,
  `02-04`); goal extraction turns these into prerequisite edges.

Everything else (Week, Time Allocation, Key Examples, ...) is free-form and
only read by people and by the outline tool. Review lectures ("Midterm
Review", "Final Review", "Wrap-Up") are fine; goal extraction skips them.

### Drafting it

Write it yourself from your syllabus (best), or draft one:

```bash
python -m pipeline analyze --course AST101   # needs ingested slides; writes curriculum/AST101-topic-map.json
python -m pipeline plan --course AST101      # drafts curriculum/AST101-lecture-plan.md (API key; see section 13)
```

`plan` uses the topic map, `sources.topic_list`, `sources.description` and the
style guide, plans exactly `lectures` lectures, and **replaces** an existing
plan file. Edit the draft.

---

## 5. Define the learning goals and their prerequisites

`curriculum/learning_goals.json` is the backbone of everything adaptive: the
classifier maps each question to goals, the help ceiling comes from the
student's mastery of those goals, mastery checks and spaced review are per
goal, the map draws one node per goal, and the navigator suggests the next
goal whose prerequisites are met.

### Format

```json
{
  "goals": [
    {
      "id": "AST101-L02-explain-moon-phases",
      "title": "Explain the cause of lunar phases",
      "description": "Student can explain the Moon's phases from the changing angle between the Sun, the Moon and Earth, and rule out Earth's shadow as the cause.",
      "course": "AST101",
      "lecture": 2,
      "hook": "Replace the most common wrong idea about the Moon with the right one.",
      "threshold": true,
      "slide_anchor": {"lecture": 2, "slide": 4, "title": "What Causes the Phases?"},
      "book_anchor": {"chapter": "ast101-02-moon", "section_id": "phases", "title": "Phases"}
    }
  ],
  "edges": [
    {"from": "AST101-L01-daily-motion", "to": "AST101-L02-explain-moon-phases"}
  ],
  "graph_version": 1
}
```

| Field | Required | Rules and meaning |
|---|---|---|
| `id` | yes | `<COURSE>-L<NN>-<slug>`: the course id, the two-digit lecture, and 2 to 4 lowercase hyphenated words. Unique. **Permanent** once students use the tutor: progress, mastery, exams and reviews are stored against it. |
| `title` | yes | An imperative phrase, at most about 8 words ("Compute a star's distance from parallax"). Shown on the map, the progress panel, chips and exam intros. |
| `description` | yes | One sentence starting "Student can ...": precisely what a student who has mastered it can do. Exam questions, exercises and grading rubrics are generated from it, so make it testable. |
| `course` | yes | A course id from `courses.yaml`; must match the id prefix. |
| `lecture` | yes | The lecture that teaches it; must match `L<NN>` in the id and be within 1..`lectures`. |
| `hook` | no | One line on why it matters, shown with the goal. |
| `threshold` | no | `true` for the few concepts that unlock everything after them (and that students most often get wrong). These get an interactive explorable if one exists ([step 10](#10-extras)). |
| `slide_anchor` | no | `{lecture, slide, title}`: the slide of the rendered deck that teaches the goal. The map's "Slides" link opens the deck there. |
| `book_anchor` | no | `{chapter, section_id, title}`: the course-book section (`book/<chapter>.qmd`, `{#section_id}`). The map's "Read" link uses it. |

`edges` are prerequisites: `from` must be understood before `to`. Rules: both
ends must be real goals; no cycles (`check-content` reports one if you
create it). Edges may cross courses (a second course building on the first).
By default a prerequisite in **another** course is advisory: it shows on the
map but does not block or trigger a check, because a student may have taken
the earlier course elsewhere (`CROSS_COURSE_PREREQS_BIND` in
`tutor/agent/navigator.py` changes that).

How many goals: 3 or 4 per lecture works well (the example has 12 per
4-lecture course). Too many and mastery evidence is spread thin; too few and
the help ceiling is coarse.

### Drafting it

```bash
python -m pipeline extract-goals --dry-run          # parse the plans, call nothing
python -m pipeline extract-goals                    # every course (claude CLI, subscription)
python -m pipeline extract-goals --course AST102    # one course; other courses' goals are kept
```

Extraction makes one Claude call per course for the goals, then derives
edges from each lecture's "Prerequisite Lectures" line (last goal of the
earlier lecture to first goal of the later one). It never invents edges
between courses: add those by hand. Then improve the graph:

```bash
python -m pipeline audit-goals                # report orphans, cycles and gaps in the graph (free)
python -m pipeline audit-goals --propose      # also propose missing edges, within and across courses (claude CLI)
python -m pipeline apply-goals-diff --dry-run # show what the reviewed proposals would change
python -m pipeline apply-goals-diff           # apply them, bump graph_version
python -m pipeline propose-anchors      # proposes slide_anchor, book_anchor and hook for every goal
python -m pipeline apply-anchors        # applies the reviewed anchors
```

The `propose-*`/`audit-*` commands write a `*_proposed.json` file for you to
read and edit before the `apply-*` command. Book anchors need
`curriculum/book-chapters.json` (`{"AST101": {"1": "ast101-01-the-sky", ...}}`).

### Check

`python -m pipeline check-content`, then open `/map` in the app: every goal
should appear, with arrows from prerequisites.

---

## 6. Define tracks

`curriculum/tracks.json` lists, per course, one or more orders in which goals
are suggested. Every course **must** have a `<COURSE>-lecture-order` track:
it is the default a student starts on. Students can switch tracks on the
"Order of suggestions" page.

```json
{
  "tracks": [
    {"id": "AST101-lecture-order", "course": "AST101", "title": "Lecture Order",
     "description": "Goals in the order the lectures teach them.",
     "goal_ids": ["AST101-L01-celestial-sphere", "AST101-L01-daily-motion", "..."]},
    {"id": "AST101-big-ideas-first", "course": "AST101", "title": "Big Ideas First",
     "description": "The concepts that unlock the rest come first.",
     "goal_ids": ["..."]}
  ],
  "graph_version": 1
}
```

| Field | Rules |
|---|---|
| `id` | At most 64 characters; `<COURSE>-<slug>` by convention. |
| `course` | A course id. |
| `title`, `description` | Shown on the track picker. |
| `goal_ids` | Goals of that course only. Include every goal of the course (a goal missing from the student's track is never suggested, though it stays on the map). The navigator never suggests a goal before its prerequisites, whatever the order. |

```bash
python -m pipeline init-tracks          # writes <COURSE>-lecture-order for every course from the goals
python -m pipeline order-tracks --check # exit 1 when a track puts a goal before a same-lecture prerequisite
python -m pipeline order-tracks         # fixes that, keeping each track's design otherwise
```

Tracks are re-read into the database at every app start, so a change needs
only a restart or redeploy.

---

## 7. Write practice exercises

`curriculum/exercises.json` is the only source the `get_exercise` tool uses.
When a student asks for practice, the tutor picks an exercise for the goal in
question; when none matches, it improvises one, so curated ones always win.

```json
{
  "graph_version": 1,
  "exercises": [
    {"id": "AST102-L01-parallax-distance-trace", "course": "AST102", "lecture_num": 1,
     "topic": "Compute a star's distance from parallax", "difficulty": "trace",
     "prompt": "A star has a parallax of 0.25 arcseconds. Compute its distance in parsecs and light-years. Show each step.",
     "hints": [],
     "expected_behavior": "4 parsecs (1 / 0.25), about 13 light-years, with d = 1/p shown.",
     "starter_code": "",
     "goal_ids": ["AST102-L01-parallax-distance"]}
  ]
}
```

| Field | Required | Meaning |
|---|---|---|
| `id` | yes | Unique. Convention: `<goal id>-<difficulty>`. Attempts are attributed to the goal through it. |
| `course`, `lecture_num` | yes | Must match the goal. |
| `topic` | yes | Usually the goal's title. |
| `difficulty` | yes | `concept` (explain in your own words), `trace` (work an example by hand and show steps), `code` (write code; only if `code_language` is set), `debug` (find the error in given work). |
| `prompt` | yes | What the student sees. |
| `expected_behavior` | strongly recommended | What a correct answer contains. The evaluator and the tutor grade against it; it is never shown to the student. |
| `hints` | no | Ordered hints; may be empty (the tutor writes hints at the allowed level). |
| `starter_code` | no | For `code` exercises. |
| `goal_ids` | yes | The goal(s) the exercise gives evidence for. |

```bash
python -m pipeline build-exercises      # drafts a concept exercise for every goal, plus trace/code ones by the title's verb
```

The drafts are templated from each goal's title and description (no model
call). Rewrite the important ones by hand: specific numbers, a real
scenario, a precise `expected_behavior`.

**Code exercises (only for courses with code).** Set `code_language` in
`courses.yaml`. To have student code compiled and run against tests (C++ only,
`ENABLE_CODE_EXEC=true`, never beside a production database; see
`docs/sandbox.md`), add cases to `curriculum/exercise_tests.json`:

```json
{"AST101-L01-sum-code": {"contract": "Read two integers; print their sum.",
                          "tests": [{"name": "small", "stdin": "2 3", "expected": "5"}]}}
```

`match` is `"exact"` (whitespace-normalized, default) or `"contains"`.
`python -m pipeline build-exercise-tests` scaffolds entries.

---

## 8. Generate the mastery-check question bank

When a student moves past a goal, a short mastery check (two or three
questions) confirms they own it before the next goal unlocks; spaced review
later re-asks questions to keep goals fresh. The questions come from
`curriculum/mastery_exams.json`, a pool of about 8 per goal, generated from
each goal's title and description and graded against per-question rubrics.

```bash
python -m pipeline generate-mastery-exams                  # every goal (claude CLI, haiku; about 4 s per goal)
python -m pipeline generate-mastery-exams --resume         # continue after timeouts
python -m pipeline generate-mastery-exams --course AST101
python -m pipeline generate-mastery-exams --goal-id AST101-L02-explain-moon-phases
```

With `code_language` set, about a third of each pool are small code
questions; otherwise all are conceptual or worked problems.

**Review the bank**: skim it for questions that are wrong, ambiguous, or test
something the goal's description does not promise. Fix the description and
regenerate that goal rather than editing many questions by hand.

The file is gitignored (it is large and regenerable, and you may not want
answers in a public repository). Without it mastery checks are simply off
and every goal stays reachable. In production it is downloaded at boot from
`MASTERY_EXAMS_URL` (see DEPLOY.md and `python -m pipeline upload-mastery-exams`).

---

## 9. Add slide decks

The chat page shows your lecture decks beside the conversation and jumps to
the slide the tutor cites. Decks live in `output/<COURSE>/lecture-NN/`:
`content.md` is the slide source (committed), `index.html` is the rendered
Reveal.js deck the app serves (generated).

`content.md` format (one block per slide, separated by a line `---`):

```markdown
## Slide 1: The Night Sky
type: title
title: The Sky Above Us
notes: Welcome. Ask: who has seen the Milky Way with their own eyes?

---

## Slide 4: What Causes the Phases?
type: content
title: What Causes the Phases?
bullets:
  - Half of the Moon is always lit by the Sun
  - We see different amounts of that lit half
body: |
  Earth's shadow has nothing to do with it (except during a lunar eclipse).
notes: Lamp-and-ball demo.
```

`type` is one of `title`, `agenda`, `content`, `code`, `diagram-placeholder`,
`quiz`, `summary`. Slides are numbered by position (1..N), which is what
`slide_anchor.slide` refers to.

```bash
python -m pipeline render-html --course AST101              # content.md -> index.html, every lecture (local, free)
python -m pipeline generate-all --course AST101 --resume    # or: draft content.md and script.md for lectures that lack them
python -m pipeline status --verbose                         # which lectures are complete
```

`generate-all` drafts slides from the plan, your profile and the knowledge
base, renders the deck, and writes a narration script (`script.md`). Edit
`content.md` and re-run `render-html --force`. Theme colors are in
`pipeline/render/html_renderer.py`.

---

## 10. Extras

### Starter prompts: `curriculum/starter_prompts.json`

Chips offered to a student who does not know what to ask.

```json
{"meta": [{"label": "What can you help me with?", "message": "What can you help me with?"}],
 "AST101": [{"label": "Why does the Moon have phases?", "message": "Why does the Moon go through phases? Is it Earth's shadow?"}]}
```

`label` is the chip (short), `message` is what gets sent. `meta` chips apply
to every course; each course id has its own list (5 to 8 is plenty). Use the
questions your students really ask, including their misconceptions.

### Teach-me openers: `curriculum/teachme/<COURSE>/<goal id>.json`

"Teach me" on the map starts a goal with a cached opening explanation in your
voice, at three depths per track. Generated once, served verbatim (and free).

```bash
python -m pipeline generate-teachme --course AST101          # claude CLI, grounded in the knowledge base
python -m pipeline generate-teachme --goal-id AST101-L02-explain-moon-phases --force
```

Format: `{"goal_id", "title", "course", "variants": {"<track id>": {"brief", "standard", "thorough"}}}`.
A goal without a file falls back to a live answer. Read them: they are the
first thing a student sees on a topic.

### Explorables: `explorables/<goal id>.html`

A small interactive page for a threshold goal (`"threshold": true`): predict,
manipulate, then reconstruct from memory. Self-contained HTML only (no
network resources; `check-content` and the tests enforce it). Drop the file in;
the map links it. See `explorables/README.md` and the example
`explorables/AST101-L02-explain-moon-phases.html`.

### The course book: `book/`

An optional Quarto book served publicly at `/book`, built in the Docker image
(`quarto render`). Chapters are `book/*.qmd`; `book/_quarto.yml` lists them;
goals link to sections through `book_anchor`. Delete the chapters you do not
want, or leave the example out of your deployment. See `book/README.md`.

### Narration and voice

With `ELEVENLABS_API_KEY` and `ELEVENLABS_VOICE_ID` set (a cloned voice of
yours, with your consent), every tutor reply gets a speaker button, and
`python -m pipeline render-audio --course AST101 --lecture 1` narrates a deck
from its `script.md`. Audio is cached; `python -m tutor.prewarm_tts --spend`
pre-renders the Teach-me openers. This bills ElevenLabs.

---

## 11. Check, run, and deploy

```bash
python -m pipeline check-content             # no ERRORs
python -m pipeline index --rebuild           # if you changed knowledge-base/raw
python -m pipeline render-html --course <ID> # for each course with decks
pytest -q                                    # the suite runs on its own fixture, so it should stay green
.venv/bin/uvicorn tutor.main:app --reload    # http://localhost:8000/auth/login
```

Sign in with an address in `ADMIN_EMAILS` (dev mode prints the magic link on
the page), then try, as a student would: pick a course on `/welcome`, ask a
question from your starter prompts, check that the reply cites your material
and moves the slide panel, ask for an exercise, open `/map`.

Then read [DEPLOY.md](DEPLOY.md). The generated artifacts travel as "seed"
tarballs downloaded at boot: the vector store (`CHROMA_SEED_URL`), the
rendered decks (`OUTPUT_SEED_URL`), narration (`AUDIO_SEED_URL`) and the
exam bank (`MASTERY_EXAMS_URL`). Your committed content (`courses.yaml`,
`curriculum/`, `persona/`, ...) is copied into the image.

---

## 12. Changing content later

| You changed | Do this |
|---|---|
| `courses.yaml` (names, subject, time zone) | Restart. |
| `persona/`, `instructor-profile/` | Restart (the prompt is built once). |
| `knowledge-base/raw/` | `python -m pipeline index --rebuild`, then redeploy the chroma seed. |
| Lecture plan | Restart (outline tool and titles read it live; titles are cached per process). Re-run `extract-goals` only if you want new goals. |
| `learning_goals.json` | Goals are seeded into the database only when its goals table is empty (first boot). On a running deployment, run `python -m pipeline.seed_goals --allow-prod` with `DATABASE_URL` pointing at it: it upserts goals (titles, descriptions, anchors, hooks, threshold flags) and replaces the edges. |
| `tracks.json` | Restart (re-read at every boot). |
| `exercises.json`, `starter_prompts.json`, `teachme/` | Restart or redeploy. |
| `mastery_exams.json` | Regenerate, upload, update `MASTERY_EXAMS_URL`, redeploy. |
| Decks | `render-html`, then rebuild and upload the output seed. |

**Never rename or delete a goal id or a course id students have used.**
Their progress, mastery, exam attempts and reviews point at it. To retire a
goal, leave it in the file and drop it from the tracks; to fix its wording,
change `title` and `description`, not `id`. A new term with new students is
the safe moment for a restructure (or start a fresh database).

---

## 13. What costs money

| Step | Calls | Billed to |
|---|---|---|
| `check-content`, `ingest`, `index`, `render-html`, `build-exercises`, `init-tracks`, `order-tracks`, `status` | nothing | free |
| `transcribe` | local Whisper | free (CPU or GPU time) |
| `extract-goals`, `audit-goals`, `propose-anchors`, `generate-mastery-exams`, `generate-teachme`, `script` / `generate-all` scripts | the local `claude` CLI | your Claude subscription (the CLI is launched with every tool disabled and `ANTHROPIC_API_KEY` removed from its environment) |
| `analyze`, `plan`, `slides` / `generate-all` slides, `script --use-api` | the Anthropic API | `ANTHROPIC_API_KEY`, per token |
| `render-audio`, `render-book-audio --spend`, `tutor.prewarm_tts --spend`, the speaker button | ElevenLabs | your ElevenLabs plan |
| The running tutor | the Anthropic API (actor, classifier, judge, strategist, graders) | `ANTHROPIC_API_KEY`; capped by `DAILY_EXCHANGE_LIMIT`, `MONTHLY_COST_CAP_USD` and the per-student quotas on Admin, Supervision |

`python -m pipeline cost-report` summarizes the running tutor's spend from
the database ledger.

---

## 14. Troubleshooting

| Symptom | Likely cause |
|---|---|
| App will not start: `courses.yaml ...` | The message names the field. Course ids: letters and digits only, at most 8. `timezone` must be an IANA name. |
| "The course materials are not available right now" in replies | No vector store: run `python -m pipeline index --rebuild`. |
| Replies never cite slides / the panel never moves | Decks not rendered (`render-html`), or `source-lecture-map.json` does not name the indexed deck filenames, or the deck's lecture has no `index.html`. |
| The map is empty | `learning_goals.json` missing or invalid at first boot (seeding is skipped when the goals table already has rows; see section 12). |
| Every turn runs at the highest help level | Expected for new students: a goal with no graded evidence allows the most help. Mastery evidence lowers it. `scripts/set_mastery.py` simulates a strong student for a demo. |
| Mastery checks never appear | `curriculum/mastery_exams.json` missing (or `MASTERY_EXAMS_URL` unset in production). |
| Exercises feel generic | They are the `build-exercises` templates: rewrite them with specifics. |
| `index` exits 1 | A file in `knowledge-base/raw/` is not the JSON its collection expects; the output lists it. |
| Transcripts folder "not indexed" | The folder name must start with a course id and a dash (`AST101-2026Fall`). |

---

## 15. Checklist

- [ ] `courses.yaml`: tutor name, instructor, subject, audience, time zone; every course with id, label, title, lecture count.
- [ ] `persona/` three files and `instructor-profile/` five files describe you.
- [ ] Source files in `source-materials/`; `ingest`, `transcribe` as needed; `index --rebuild`; `source-lecture-map.json`; a golden retrieval set that passes.
- [ ] `curriculum/<COURSE>-lecture-plan.md` for every course, lectures 01..N with objectives and prerequisites.
- [ ] `learning_goals.json`: 3 or 4 goals per lecture, testable descriptions, prerequisite edges (including across courses), threshold flags, anchors.
- [ ] `tracks.json`: a `<COURSE>-lecture-order` track for every course.
- [ ] `exercises.json`: at least one good exercise per goal.
- [ ] `mastery_exams.json` generated and reviewed.
- [ ] Decks rendered; starter prompts written; optional Teach-me openers, explorables, book.
- [ ] Removed the example: `explorables/AST101-*`, `book/ast10*.qmd`, `knowledge-base/raw/*` example files, `output/AST10*/`, `curriculum/teachme/AST10*`, and the AST entries anywhere you did not overwrite.
- [ ] `python -m pipeline check-content` reports no ERROR; `pytest -q` passes; you tried it as a student.

---

## Appendix: a worked example, from syllabus to tutor

A hypothetical instructor, Prof. Rivera, teaches **BIO 110: Cells and Genes**
(12 lectures) and wants a tutor called "Mendel". This is the order of work,
with what each step produced.

**1. Identity.** `courses.yaml`:

```yaml
tutor:
  name: Mendel
  instructor: Prof. Dana Rivera
  instructor_short: Prof. Rivera
  instructor_email: drivera@college.example
  institution: Riverside Community College
  subject: introductory cell biology and genetics
  audience: first-year students, many pre-nursing
  timezone: America/Chicago
  code_language: ""
courses:
  - id: BIO110
    label: BIO 110
    title: Cells and Genes
    lectures: 12
    topics: cell structure, membranes, metabolism, mitosis and meiosis, Mendelian genetics, DNA
    sources:
      slides: source-materials/BIO110/slides
      textbook: source-materials/BIO110/openstax-biology-ch3-12.pdf
      assignments: source-materials/BIO110/homework
      documents: source-materials/BIO110/handouts
```

She deleted the example's `AST101`/`AST102` entries, `curriculum/*AST*`,
`curriculum/teachme/AST*`, `output/AST*`, `explorables/AST*`, the example
files in `knowledge-base/raw/*/`, and the `book/ast*.qmd` chapters (keeping
`book/` for later).

**2. Voice.** She wrote `persona/persona-distilled.md` (14 rules, starting
"Ask what the student already thinks before explaining"),
`persona/communication.md` (short sentences, no exclamation marks, one
analogy per answer), and `persona/teaching.md` (how she runs a "predict the
offspring" exercise, how she responds to "just tell me the answer"). Then she
transcribed two recorded lectures and pasted ten real quotes into
`instructor-profile/transcripts-voice.md`.

**3. Sources.** She copied her decks, the open textbook PDF, homework and
handouts into `source-materials/BIO110/` (gitignored), then:

```bash
pip install -r requirements.txt
python -m pipeline ingest --course BIO110
python -m pipeline transcribe --recordings-dir source-materials/recordings/BIO110
python -m pipeline index --rebuild
```

She wrote `curriculum/source-lecture-map.json` mapping her twelve deck file
names to lectures 1 to 12, and fifteen real student questions into
`evals/golden_retrieval.json`; `python scripts/retrieval_eval.py` scored 14/15.

**4. Lecture plan.** She wrote `curriculum/BIO110-lecture-plan.md` from her
syllabus: twelve `## Lecture NN:` sections, each with three or four
"students will be able to" objectives and a "Prerequisite Lectures" line
(lecture 9, Mendelian genetics, lists `07, 08`: meiosis and probability).

**5. Goals.** `python -m pipeline extract-goals` drafted 41 goals. She merged
two near-duplicates, rewrote six descriptions to be testable ("Student can
predict the genotype and phenotype ratios of a monohybrid cross" instead of
"Student understands Punnett squares"), marked three threshold concepts
(diffusion vs osmosis, mitosis vs meiosis, dominance), and ran
`python -m pipeline audit-goals --propose`, accepting 9 of 14 proposed edges
before `apply-goals-diff`.

**6. Tracks.** `python -m pipeline init-tracks` wrote `BIO110-lecture-order`.
She added a second track, "Exam 1 review", by hand, and checked it with
`python -m pipeline order-tracks --check`.

**7. Exercises.** `python -m pipeline build-exercises` drafted one concept
exercise per goal and trace exercises for the "Predict" and "Calculate"
goals; she rewrote the twelve most important with real organisms and numbers.

**8. Mastery checks.** `python -m pipeline generate-mastery-exams` (about
three minutes); she skimmed the bank and regenerated two goals after
sharpening their descriptions.

**9. Decks.** `python -m pipeline generate-all --course BIO110 --resume`
drafted `content.md` for all twelve lectures from her plan, profile and
knowledge base; she edited three and ran `render-html --force`.

**10. Extras.** Eight starter prompts in `curriculum/starter_prompts.json`
taken from her office-hours notes, including the misconception "Is osmosis
just diffusion of salt?".

**11. Check and run.** `python -m pipeline check-content` reported 0 errors
and 2 warnings (two goals without exercises, which she accepted);
`pytest -q` passed; she tried it as a student, then followed DEPLOY.md.
