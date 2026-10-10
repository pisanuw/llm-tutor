# book/

An optional course book, built with [Quarto](https://quarto.org) and served
by the app at `/book`. The example is *Astra's Astronomy Notes*, eight short
chapters for the example astronomy courses, one per lecture. Replace them with
your own, or delete the chapters and leave the book out entirely: nothing else
depends on it.

## What is here

```
book/
├── _quarto.yml               # book settings and the chapter list
├── index.qmd                 # the preface
├── ast101-01-the-sky.qmd     # one chapter per lecture
├── ...
├── ast102-04-expanding-universe.qmd
├── styles.css                # callouts, quizzes, the narration player
├── quiz.js                   # the self-check quiz widget
├── narration.js              # the chapter narration player (signed-in students only)
├── narration-index.json      # the chapters that have narration ([] here)
└── build.sh                  # a small wrapper around quarto
```

## Build

Install Quarto (<https://quarto.org/docs/download/>), then:

```bash
cd book
quarto preview          # live-reload preview in your browser
quarto render           # the static site, in _book/
./build.sh check        # every chapter listed in _quarto.yml exists
```

The app serves `book/_book/` at `/book` when that folder exists; without it,
the Book links answer 404 and the app says so once at startup. You do not
have to build it yourself for production: the Dockerfile renders the book in a
build stage, so `_book/` is never committed.

## How chapters connect to the tutor

- **Chapters and lectures.** [curriculum/book-chapters.json](../curriculum/book-chapters.json)
  says which chapter (by file stem) teaches each lecture.
  `python -m pipeline propose-anchors` uses it to point each learning goal at
  a section of that chapter.
- **Sections and goals.** Each goal in
  [curriculum/learning_goals.json](../curriculum/learning_goals.json) can
  carry a `book_anchor`: `{"chapter": "ast101-02-moon", "section_id": "phases",
  "title": "What Causes the Phases?"}`. The section needs that id, which you
  give it explicitly in the heading:

  ```markdown
  ## What Causes the Phases? {#phases}
  ```

  The mastery map then links the goal to `/book/ast101-02-moon.html#phases`.
  Keep the ids stable: renaming a heading without its `{#id}` breaks the link.

## Writing a chapter

- Start with `# Title`, then sections with explicit `{#ids}`.
- Callouts are Quarto's own: `::: {.callout-tip title="Try it tonight"}`.
- A self-check question is a block of raw HTML that `quiz.js` wires up:

  ````markdown
  ```{=html}
  <div class="quiz" data-answer="b">
    <div class="question">1. Which phase is the Moon in during a solar eclipse?</div>
    <ul>
      <li data-choice="a"><label><strong>a.</strong> Full</label></li>
      <li data-choice="b"><label><strong>b.</strong> New</label></li>
    </ul>
    <div class="explain"><strong>b</strong>. The Moon must be between the Sun and Earth.</div>
  </div>
  ```
  ````

  Clicking a choice marks it right or wrong and reveals the explanation.

## Narration (optional)

`python -m pipeline render-book-audio` reads each chapter aloud in the
instructor's cloned voice (a dry run unless you pass `--spend`) and
writes `narration-index.json`, the list of chapters with a clip. The clips are
served only to signed-in students; `narration.js` shows a player on a chapter
page when the index lists it and the student's session can reach the clip.
With the index empty, as here, no player appears.
