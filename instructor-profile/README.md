# instructor-profile/

How the instructor sounds in a lecture hall and how they explain each topic.
The files here describe the example instructor, the fictional Dr. Alex
Morgan, and the example astronomy courses. Replace them with your own.

## persona/ versus instructor-profile/

- `persona/` says **who you are and the rules** a tutor speaking for you
  follows on every turn: never hand over a graded answer, ask before telling,
  your voice in writing.
- `instructor-profile/` says **how you actually teach the material**: how you
  sound when you lecture, the arc of a class session, the examples you reach
  for, and how you explain each topic and which misconceptions you go after.

Both are sent to the model as part of the cached system prompt, persona
first. Everything here is optional: a missing file is noted in the prompt as
missing and the tutor still runs.

## The files the tutor reads

`tutor/agent/prompt_builder.py` reads these five, in this order:

| File | What it holds |
|------|---------------|
| `transcripts-voice.md` | How you sound when you teach out loud: phrases, rhythm, humor, how you handle a wrong answer. Short real (or, here, invented) quotes work best. |
| `lecture-flow.md` | The arc of a typical session, from the first minute to the exit question. |
| `teaching-patterns.md` | Your pedagogical signatures, and your grading stance (what earns credit, what you forgive). |
| `examples-library.md` | The analogies, demonstrations and worked examples you return to, by topic. |
| `topic-explanations.md` | For each lecture: how you explain the key ideas, in what order, and the misconceptions you target. |

Keep the five together under about 400 lines. They are cached, but every
line competes for the model's attention with every other line.

## Drafting them from your own material

You do not have to write these from scratch. After you ingest your slides
and transcribe your lecture recordings (see `knowledge-base/README.md`),

```bash
python -m pipeline analyze --course AST101
```

drafts `style-guide.md` (your style, read from slides and transcripts) and
`examples-library.md` (your recurring examples) into this folder, and a topic
map into `curriculum/`. The slide and script generators read both drafts and
the plan generator reads `style-guide.md`; the tutor's prompt reads only the
five files above, `examples-library.md` among them. Treat every draft as a first
pass: read it, cut what is wrong, and add what only you know.

The other four files are yours to write. Reading two or three of your own
lecture transcripts with a highlighter is the fastest way to fill in
`transcripts-voice.md` and `topic-explanations.md`.
