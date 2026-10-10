# curriculum/teachme/

Cached "Teach me" openers. When a student clicks **Teach me** on a goal in
the mastery map, the app looks here first. If the goal has a file, its opener
is shown instantly as the tutor's first message, verbatim, with no model call.
A goal without a file falls back to a normal live answer, so this folder is an
optimization, never a requirement.

## Layout

One file per goal, in a folder per course:

```
curriculum/teachme/<COURSE>/<goal_id>.json
```

```json
{
  "goal_id": "AST101-L02-explain-moon-phases",
  "title": "Explain the cause of lunar phases",
  "course": "AST101",
  "variants": {
    "AST101-lecture-order":   {"brief": "...", "standard": "...", "thorough": "..."},
    "AST101-big-ideas-first": {"brief": "...", "standard": "...", "thorough": "..."}
  }
}
```

- `variants` has one entry per track in `curriculum/tracks.json` for that
  course, so the opener can match the order the student is learning in.
- Each track has three depths: `brief` (a few sentences), `standard` (the
  default, one example), and `thorough` (from first principles, with the
  common misconceptions).
- Markdown is allowed. End each opener with a question back to the student.

The app falls back gracefully: a missing track uses the course's
`-lecture-order` variant, and a missing depth uses `standard`.

## Generating them

```bash
python -m pipeline generate-teachme --course AST101
```

writes a file for every goal that does not have one yet, in the instructor's
voice (the three `persona/` files and courses.yaml), grounded in the
knowledge base. It runs offline through the `claude` command-line tool, so it
bills your Claude subscription, not an API key. Read the results before you
commit them: students see them exactly as written.

The two files here are hand-written examples for the example course. Since
`/chat/teachme` stores openers verbatim, `python -m tutor.prewarm_tts` can
render them to speech ahead of time if you use the voice feature.
