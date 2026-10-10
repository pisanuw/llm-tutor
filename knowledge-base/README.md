# knowledge-base/

The course material the tutor searches before it answers. `find_topic`
looks through every collection listed in [tutor/kb.py](../tutor/kb.py) and
hands the best matches to the model, which is how the tutor quotes your
slides and your words instead of inventing its own.

```
knowledge-base/
├── raw/                     # what you commit: one folder per collection, JSON files
│   ├── slides/              # one file per deck
│   ├── transcripts/         # one file per recording
│   ├── textbook/            # chunks of your course text
│   ├── assignments/         # chunks of your assignment handouts
│   ├── documents/           # syllabus, handouts, notes
│   ├── forum/               # class discussion threads
│   └── code/                # example code (absent here: the example course has none)
├── transcripts/<COURSE>-<term>/   # optional: more transcript folders, e.g. AST101-2026Fall/
└── chromadb/                # the built vector store (generated, gitignored)
```

Everything under `raw/` here is the example astronomy course, invented for
this repository. Delete it and add your own.

## Building the store

```bash
python -m pipeline index --rebuild
```

reads every `raw/<collection>/*.json` (and every `transcripts/<COURSE>-<term>/`
folder), embeds the text, and writes `chromadb/` with a `manifest.json` that
lists each input file with its hash. `--rebuild` drops the old collections
first, so chunks from a file you deleted go too. A file that is not the JSON
its collection expects is skipped and listed, and the command exits 1.

Rebuild after any change under `raw/`. The app reads the store at startup;
restart it after a rebuild.

## The collections

Every chunk needs at least five words of text, or it is skipped. Keep chunks
to a paragraph or two (roughly 80 to 300 words): long chunks match vaguely,
tiny ones lose their context.

### slides

Your lecture decks, one JSON file per deck, one row per slide. A slide hit
can also move the chat's slide panel to the matching rendered deck, using
[curriculum/source-lecture-map.json](../curriculum/source-lecture-map.json)
to say which lecture each `filename` teaches.

Produced by `python -m pipeline ingest --course AST101` from the `.pptx`
files in the course's `sources.slides` folder (courses.yaml), or by
`python -m pipeline canvas-ingest` from downloaded course files.

```json
{"filename": "week2-moon.pptx", "lecture_title": "AST 101 Week 2: The Moon", "slide_count": 1,
 "slides": [{"slide_number": 1, "layout_name": "TITLE_AND_BODY", "title": "Half Lit, Always",
             "body_text": ["The Sun lights one half of the Moon at all times"],
             "speaker_notes": "Lamp-and-ball activity.", "has_code": false}]}
```

### transcripts

What you said in lecture: the tutor's best source for your voice and your
explanations. One file per recording, one row per segment of at least five
words. The example files are short excerpts.

Produced by `python -m pipeline transcribe` (local Whisper) from the
recordings folder in `config.yaml`, written to `raw/transcripts/`. Bare
Whisper CLI output (`{"text": ..., "segments": [...]}`, no `source_file`)
also works; put such files in `knowledge-base/transcripts/<COURSE>-<term>/`,
a folder whose name starts with a course id from courses.yaml, and
`index` (or `index-transcripts`) picks it up.

```json
{"source_file": "AST101-2026Fall-lecture-02.mp4", "full_text": "Okay, folks, let's get started.",
 "segments": [{"start": 4.0, "end": 9.5, "text": "Okay, folks, let's get started. Sky report first."}]}
```

### textbook

Your course text, as a JSON list of chunks. The example splits one free text
into one file per chapter to keep each file small; either way works.

Produced by `python -m pipeline ingest` from the PDF named in the course's
`sources.textbook` (one JSON file per PDF, chunked by page).

```json
[{"text": "The celestial sphere. To an observer on the ground, ...", "source_file": "open-astronomy-notes.pdf",
  "page_number": 4, "chunk_index": 1, "doc_type": "textbook"}]
```

### assignments

Assignment handouts, one chunk per question or section. Commit the questions,
never the answer keys: the tutor reads everything here.

Produced by `python -m pipeline ingest` from the `.docx`, `.txt` and `.md`
files in the course's `sources.assignments` folder.

```json
[{"text": "Draw a top-down view of the Sun, Earth and the Moon ...", "source_file": "AST101-problem-set-1.docx",
  "section": "Question 4: Phase Geometry", "doc_type": "document"}]
```

### documents

Anything else you wrote that students might ask about: the syllabus,
handouts, lab guides, FAQ pages. The example has a syllabus and an
observing-night handout.

Produced by `python -m pipeline canvas-ingest` from downloaded course files
(PDFs), or written by hand in the same shape as assignments.

```json
[{"text": "Each student has two late days per course ...", "source_file": "AST101-AST102-syllabus.pdf",
  "section": "Late Work", "doc_type": "document"}]
```

### forum

Class discussion threads. This is the one collection the tutor is told **may
be wrong** (`COMMUNITY_COLLECTIONS` in tutor/kb.py), because students wrote
much of it; the example includes a student's wrong answer that the instructor
corrects. Remove names, or keep first names only, before committing.

Produced by `python -m pipeline discord` (from DiscordChatExporter JSON) or
`python -m pipeline discord-fetch` (through a bot token), one row per
message; one row per thread, as in the example, works too.

```json
[{"text": "[Maya] Why is there a high tide on the far side?\n[Dr. Morgan] The Moon's pull gets weaker ...",
  "source_file": "AST101-class-forum.json", "section": "Why two tides a day and not one?", "doc_type": "forum"}]
```

### code

Example code for courses that use it. The example course has none, so
`raw/code/` is absent and the collection is simply empty.

Produced by `python -m pipeline github --repos <url>` (or `--org <name>`),
one row per source file.

```json
[{"repo": "example-starter-code", "relative_path": "src/main.cpp", "language": "C++",
  "content": "// prints the phase of the Moon for a given day ..."}]
```

## Do not commit

- **Copyrighted text you do not own**, such as a commercial textbook's full
  text. Index it locally if your license allows, and keep it out of git
  (add the file to `.gitignore`).
- **Student records**: graded work, names with grades, rosters, email
  addresses, exam answer keys. Forum exports especially need anonymizing.
- **Anything you would not show every student.** The tutor can quote any
  chunk to anyone who asks the right question.
