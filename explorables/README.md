# explorables/

Small, self-contained interactive pages for **threshold concepts**: the few
learning goals that, once understood, change how a student sees the rest of
the course, and that students most often get stuck on. A guided interactive
is worth its authoring cost only there.

## How they are found

One file per goal, named by the goal's id:

```
explorables/<goal_id>.html
```

The app serves the folder at `/explorables/<goal_id>.html` (mounted in
`tutor/main.py`). [tutor/agent/explorables.py](../tutor/agent/explorables.py)
turns a goal id into that URL only when the file exists, so a goal without a
file never shows a dead link. The mastery map links an explorable for goals
marked `"threshold": true` in
[curriculum/learning_goals.json](../curriculum/learning_goals.json); flag the
goal there, then drop the file in. No code change is needed.
`python -m pipeline check-content` warns about a file whose name matches no
goal, or whose goal is not flagged.

## The contract

Every explorable must:

1. **Be fully self-contained and offline.** Inline all CSS and JavaScript and
   draw with inline SVG or canvas. No CDN, no web fonts, no images or scripts
   from anywhere else: the file must not contain an `http://` or `https://`
   reference at all. (Leave the `xmlns` attribute off inline SVG; HTML does
   not need it.)
2. **Ask for a prediction before the reveal.** The learner commits to an
   answer before the page shows what happens. Do not just animate.
3. **Offer at least one guided manipulable.** A slider, step buttons or a case
   picker that changes the model, with a few "try this" tasks, not a bare
   free-play sandbox.
4. **End with a blank-page reconstruction prompt.** Ask the learner to close
   the page and rebuild the idea from memory on paper, then check.

Make it accessible too: a visible label on every control, results announced
through an `aria-live` region, everything operable by keyboard, and a layout
that works on a 390-pixel-wide phone screen without sideways scrolling.

## The example

| Goal | File |
|------|------|
| `AST101-L02-explain-moon-phases` | Moon phases: predict the phase at a chosen orbit position, then move the Moon with a slider and watch the lit half and the view from Earth; ends with "draw the eight phases from memory". |

The example course flags five threshold goals; the other four
(`AST101-L03-axial-tilt-seasons`, `AST102-L01-parallax-distance`,
`AST102-L02-hr-diagram`, `AST102-L04-hubble-law`) have no explorable yet,
so the map simply shows no link for them.
