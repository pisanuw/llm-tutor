# persona/

Who the tutor is when it speaks for you. These files describe how **you**
teach and how **you** talk, so the tutor sounds like you and not like a
generic assistant.

The files here describe the example instructor, the fictional Dr. Alex
Morgan, who teaches the example astronomy courses. Replace all three with
your own before students use the tutor.

## The three files the tutor reads

The prompt builder (`tutor/agent/prompt_builder.py`) reads exactly these
three files, in this order, and nothing else in this folder:

| File | What it holds | Aim for |
|------|---------------|---------|
| `persona-distilled.md` | Who you are in one paragraph, then 10 to 15 rules a tutor speaking for you must never break (never hand over a full solution, ask before telling, admit uncertainty, ...). | 30 to 50 lines |
| `communication.md` | Your voice: sentence length, humor, formatting habits, phrases you use and phrases you never use. | 40 to 70 lines |
| `teaching.md` | Your teaching method: what you believe about learning, how you handle "just give me the answer", how your hints escalate, with one short example exchange. | 80 to 130 lines |

`python -m pipeline generate-teachme` reads the same three files when it
writes the cached "Teach me" openers, so those openers sound like you too.

Any other file you put here (a CV, notes, old syllabi) is ignored by the
tutor. Keep such files elsewhere if you do not want them in the repository.

## Keep them tight

All three files are sent to the model on **every turn**, as part of the
system prompt. The prompt is cached, so a long persona does not cost much
per turn, but it does cost attention: a rule buried on line 300 is a rule
the model weighs less. Together the three files should stay under about
250 lines.

Some advice for writing your own:

- Write rules the model can act on. "Ask the student to predict before you
  explain" works. "Be a great teacher" does not.
- Describe how you actually teach, including your habits and your humor.
  Quote yourself if you have a phrase students would recognize.
- Say what you never do. The "avoid" lists change behavior more than the
  "do" lists.
- Leave out biography that does not change how you teach. Awards and
  publication lists are not instructions.
- Each file starts with an HTML comment saying what it is. Comments reach
  the model but do no harm; delete them once the file is yours.

How you sound in lecture, your recurring examples, and how you explain each
topic belong in `instructor-profile/`, not here (see its README).
