# Analytics cookbook: the local store and how to ask it questions

How students use your tutor, answered offline. `python -m tutor.analytics`
copies the structured data into a SQLite file on your own machine, then
analyses that file; nothing touches production after the export.

The examples use the example course (`AST101`, `AST102`), pseudonyms like
`S12`, and class id `3`; substitute your own (`python -m tutor.analytics class`
with no id lists your classes).

## 1. Get the data

Keep the store outside every repository, for example in `~/tutor-data/`.

```bash
# From the database (DATABASE_URL in .env points at production; the export only reads)
python -m tutor.analytics export --out ~/tutor-data/tutor.sqlite

# Over HTTP instead, with the admin token (no database credentials on this machine)
TUTOR_ADMIN_TOKEN=... python -m tutor.analytics export --out ~/tutor-data/tutor.sqlite \
    --from-url https://tutor.example.edu

# Narrower or fuller copies
python -m tutor.analytics export --out ... --since 2026-09-20       # rows from that day on (first pull only)
python -m tutor.analytics export --out ... --class-id 3             # one class's roster, and that class
python -m tutor.analytics export --out ... --with-emails            # identifiable: emails and names
python -m tutor.analytics export --out ... --with-content           # message text
python -m tutor.analytics export --out ... --full                   # re-read everything
```

Without `--out` the file is `tutor-analytics.sqlite` in the current
directory, and every analysis command reads that name unless given `--db`.

A second run adds only new rows (by id, re-reading a short tail so a turn that
finished after the last export is refreshed) and replaces the small tables
that change in place (students, classes, progress, mastery attempts, the
curriculum, the lectures). `export_meta` records when and with what flags.

**The file is student data.** It is pseudonymous by default (`S12`, never an
address), but a roster can re-identify it. Keep it under the same rules as the
database ([retention-policy.md](retention-policy.md)): not in a repository,
not shared, a student deleted from it on a deletion request
(`DELETE FROM <every table> WHERE student_id = ?`, then
`DELETE FROM students WHERE id = ?`), and not kept identifiable past your
retention window.

## 2. The tables

| Table | Rows | Keys and joins |
|---|---|---|
| `students` | one per student: `id`, `pseudonym`, `created_at`, `last_seen`, `last_login`, `login_count`, `anonymized_at`, `is_instructor`; `email`, `name` only with `--with-emails` | everything joins on `student_id = students.id` |
| `classes`, `class_enrollments`, `class_instructors` | rosters, and who teaches each class (the lead is `classes.instructor_id` and has a row too) | `class_enrollments.student_id`, `class_enrollments.class_id = classes.id`, `class_instructors.class_id = classes.id` |
| `sessions` | one per sign-in: `created_at`, `expires_at`, `method` | `activity_events.login_session_id = sessions.id` |
| `messages` | one per question and reply: `role`, `course`, `lecture_num`, `slide_num`, `created_at`, `chars`; `content` only with `--with-content` | `activity_events.message_id = messages.id` (replies), `message_feedback.message_id` |
| `message_feedback` | the latest thumbs up or down on a reply | `message_id = messages.id` |
| `turn_events` | one per turn and per supporting model call: `ts`, `kind` (`chat`, `exam`, `evaluate`, `classify`, ...), `turn_id`, `intent`, `rung_used`, `move`, `goal_ids_json`, `grounding_hit`, `verdict`, tokens, `cost_usd`, `latency_ms`, `timings_json` | `activity_events.turn_id = turn_events.turn_id` (chat turns) |
| `activity_events` | one per thing a student did in the browser (section 3) | `visit_id` (a browser tab), `page_view_id` (a page load), `seq` (order within the page view; a gap is a lost batch) |
| `topics_accessed`, `path_events`, `graded_events`, `mastery_attempts`, `student_progress` | the learning state | `goal_id = learning_goals.id` |
| `learning_goals`, `tracks` | the curriculum | |
| `student_days` | one per student per local day with any activity: `day`, `course` (the day's main one), `questions`, `turns`, `cost_usd`, `active_min`, `deck_min`, `narration_min`, `visits`, `slides_seen`, `lectures_opened`, `error_outcomes`, `busy_outcomes`, `detector_cuts`, `blocks`, `slow_turns`, `thumbs_up`, `thumbs_down`, `logins`, `failed_logins`, `user_agents`, `request_errors`, `hours_mask` (24 bits, bit 0 the hour after local midnight); computed by the app from the raw tables (`tutor/student_days.py`), replaced whole on export | `student_id`; a missing day is a day of zeros |
| `login_attempts` | one per step of a sign-in: `created_at`, `student_id` (null before the account existed, or for a stranger), `method`, `outcome`, `agent` (`browser`, `bot` or `scanner`, never the browser string), `detail`; no address, no host | `student_id` |
| `request_errors` | one per error page a browser hit: `ts`, `status`, `method`, `path` (no query), `referrer_path`, `student_id`, `kind`, `agent`, `ref`, `detail` | `student_id` |
| `lectures` | one per rendered deck: `title`, `slides_total`, `narration_s_json` (`{"1": 30.5, ...}` seconds per slide), `narration_total_s` | `(course, lecture_num)` |

"Local day" means the class's time zone (`timezone` in `courses.yaml`).
Timestamps are ISO text in UTC (`2026-10-01 17:01:00`), which SQLite's
`datetime()`, `julianday()` and `strftime()` read directly. `ts` on an
activity event is server time corrected by the browser's clock offset;
`client_ts` is the browser's own reading (order events within one page view
by `seq`, not by time).

## 3. The events

The vocabulary is `EVENTS` in `tutor/activity_events.py`; an event or field
outside it is dropped on arrival. Durations are in `value_ms`; the rest of an
event's fields are in `detail_json`.

| Group | `name` | `value_ms` | `detail_json` |
|---|---|---|---|
| pages | `page_view`, `page_show`, `page_hide`, `page_heartbeat`, `page_leave`, `click`, `client_error` | on-screen time (hide, heartbeat, leave) | `ref`, `vw`, `vh` on a view; `target`/`label` columns and `href` (the same-origin path a link opens) on a click; `error` (the name only), `source`, `line` on a script failure |
| chat | `question_sent`, `reply_started`, `reply_first_token`, `reply_done`, `slide_cited`, `slide_chip_click`, `deck_missing` (course and lecture_num columns), `lecture_select`, `course_switch`, `panel_toggle`, `tts_play`/`tts_pause`/`tts_ended`, `feedback`, `code_submit`, `teachme_start`, `starter_click` | ms since the question was sent; voice played since the last tts event | `kind`, `chars`, `outcome`, `from`/`to`, `rating`, `predicted`, `index`, `position_s` |
| decks | `deck_open`, `slide_view`, `deck_heartbeat`, `narration_play`/`narration_pause`/`narration_seek`/`narration_ended`, `deck_close` | dwell on the previous slide (`slide_view`), on-screen time since the last beat, narration played since the last narration event, dwell on the last slide (`deck_close`) | `slides_total`, `context` (`chat`, `lecture`, `tab`), `has_audio`, `prev_slide`, `how` (`first`, `next`, `prev`, `jump`, `auto`), `narration_playing`, `active_ms`, `slides_seen`, `position_s` |
| other | `lecture_open`, `goal_click`, `review_start`, `review_answer`, `book_narration_play`/`book_narration_ended` | book narration played | `goal_id`, `modality`, `slug` |

## 4. Twelve questions

Open the file with `sqlite3 ~/tutor-data/tutor.sqlite`, DuckDB
(`ATTACH 'tutor.sqlite' AS t (TYPE SQLITE);`), or pandas
(`pd.read_sql(sql, sqlite3.connect(path))`). The analysis commands in
section 5 do the sessionizing and the thresholds for you; these are the
raw questions.

```sql
-- 1. Who used the tutor this week, and how much?
SELECT s.pseudonym, COUNT(DISTINCT date(m.created_at)) AS days, COUNT(*) AS questions
FROM messages m JOIN students s ON s.id = m.student_id
WHERE m.role = 'user' AND m.created_at >= datetime('now', '-7 days')
GROUP BY s.pseudonym ORDER BY questions DESC;

-- 2. Seconds between a student's consecutive questions in one course
SELECT student_id, course, created_at,
       (julianday(created_at) - julianday(LAG(created_at) OVER (PARTITION BY student_id, course ORDER BY created_at))) * 86400 AS gap_s
FROM messages WHERE role = 'user' ORDER BY student_id, created_at;

-- 3. Think time: from the reply's end to the next question (client-measured)
SELECT q.student_id, q.ts AS asked,
       (julianday(q.ts) - julianday(MAX(d.ts))) * 86400 AS think_s
FROM activity_events q JOIN activity_events d
  ON d.student_id = q.student_id AND d.name = 'reply_done' AND d.ts < q.ts
WHERE q.name = 'question_sent' GROUP BY q.id ORDER BY q.ts;

-- 4. Which slides of AST101 lecture 2 did student 12 see, and for how long?
SELECT json_extract(detail_json, '$.prev_slide') AS slide, COUNT(*) AS views, SUM(value_ms) / 1000.0 AS seconds
FROM activity_events
WHERE student_id = 12 AND course = 'AST101' AND lecture_num = 2 AND name IN ('slide_view', 'deck_close')
GROUP BY 1 ORDER BY 1;
-- (slide_view carries the dwell on the slide BEFORE it, in prev_slide; deck_close carries the last slide's)

-- 5. Narration seconds played per lecture, against the lecture's length
SELECT e.student_id, e.course, e.lecture_num,
       SUM(e.value_ms) / 1000.0 AS played_s, l.narration_total_s,
       ROUND(SUM(e.value_ms) / 1000.0 / l.narration_total_s, 2) AS fraction
FROM activity_events e JOIN lectures l ON l.course = e.course AND l.lecture_num = e.lecture_num
WHERE e.name IN ('narration_pause', 'narration_ended', 'narration_seek', 'deck_heartbeat')
  AND (e.name <> 'deck_heartbeat' OR json_extract(e.detail_json, '$.narration_playing') = 1)
GROUP BY 1, 2, 3 ORDER BY 1, 2, 3;

-- 6. Where did a reply get its slide, and did the student look?
SELECT c.turn_id, c.lecture_num, c.slide_num,
       EXISTS (SELECT 1 FROM activity_events v WHERE v.student_id = c.student_id AND v.name = 'slide_view'
               AND v.lecture_num = c.lecture_num AND v.ts BETWEEN c.ts AND datetime(c.ts, '+10 minutes')) AS looked
FROM activity_events c WHERE c.name = 'slide_cited';

-- 7. The last thing before a student left for more than an hour
WITH ordered AS (
  SELECT *, LEAD(ts) OVER (PARTITION BY student_id ORDER BY ts, seq) AS next_ts FROM activity_events)
SELECT student_id, ts, name, course, lecture_num, turn_id
FROM ordered WHERE next_ts IS NULL OR (julianday(next_ts) - julianday(ts)) * 24 > 1 ORDER BY student_id, ts;

-- 8. Replies the student answered within a minute (follow-ups) vs walked away from
SELECT d.student_id,
       SUM(CASE WHEN (julianday(q.ts) - julianday(d.ts)) * 86400 < 60 THEN 1 ELSE 0 END) AS follow_ups,
       SUM(CASE WHEN q.ts IS NULL OR (julianday(q.ts) - julianday(d.ts)) * 86400 > 600 THEN 1 ELSE 0 END) AS walked
FROM activity_events d LEFT JOIN activity_events q
  ON q.student_id = d.student_id AND q.name = 'question_sent'
  AND q.id = (SELECT MIN(id) FROM activity_events x WHERE x.student_id = d.student_id AND x.name = 'question_sent' AND x.ts > d.ts)
WHERE d.name = 'reply_done' GROUP BY d.student_id;

-- 9. Lost batches: a gap in the sequence within a page view
SELECT page_view_id, COUNT(*) AS have, MAX(seq) AS expect
FROM activity_events GROUP BY page_view_id HAVING have < expect;

-- 10. Class against class: questions per enrolled student
SELECT c.name, COUNT(DISTINCT ce.student_id) AS enrolled,
       COUNT(m.id) * 1.0 / COUNT(DISTINCT ce.student_id) AS questions_per_student
FROM classes c JOIN class_enrollments ce ON ce.class_id = c.id
LEFT JOIN messages m ON m.student_id = ce.student_id AND m.role = 'user' AND m.course = c.course
GROUP BY c.id;

-- 11. Topic switches: consecutive questions of one student on different lectures
WITH turns AS (
  SELECT student_id, created_at, lecture_num,
         LAG(lecture_num) OVER (PARTITION BY student_id ORDER BY created_at) AS prev
  FROM messages WHERE role = 'user' AND course = 'AST101')
SELECT student_id, created_at, prev AS from_lecture, lecture_num AS to_lecture
FROM turns WHERE prev IS NOT NULL AND prev <> lecture_num;

-- 12. The time a reply took, as the student saw it, by stage
SELECT turn_id, latency_ms, json_extract(timings_json, '$.classify_ms') AS classify_ms,
       json_extract(timings_json, '$.first_token_at_ms') AS first_token_at_ms
FROM turn_events WHERE kind = 'chat' AND turn_id IS NOT NULL ORDER BY ts DESC LIMIT 50;
```

## 5. The analysis commands

```bash
python -m tutor.analytics visits --db ... --student S12 --days 14
python -m tutor.analytics lectures --db ... --class 3 [--course AST101 --lecture 2]
python -m tutor.analytics questions --db ... --student S12
python -m tutor.analytics disengagement --db ... --class 3
python -m tutor.analytics timeline --db ... --student S12 --day 2026-10-03
python -m tutor.analytics student --db ... S12
python -m tutor.analytics class --db ... 3                # no id: list the classes
python -m tutor.analytics compare --db ... 3 4 [--format html > classes.html]
python -m tutor.analytics daily --db ... --format html --out ~/tutor-data/daily.html --open
python -m tutor.analytics daily --db ... --class-id 3 --days 30 [--tz UTC] [--instructors]
```

Every command takes `--db` (default `tutor-analytics.sqlite`), a window
(`--days N`, or `--since`/`--until`), and `--format md|csv|json|html`. A
student is an id, a pseudonym, or, in a store made with `--with-emails`, an
address.

`daily` is one value per calendar day for every metric (students active,
sign-ins, visits, questions by course, model cost, reply latency, voice plays,
feedback, on-screen, deck and narration minutes, slides viewed, lectures
touched, goals mastered, mastery checks, probes), on the class's calendar
(`--tz` changes it). `--days` is the span (default 90). As Markdown, CSV or
JSON it is a totals table and a day-per-row table; as HTML it is a
self-contained page of charts, one per metric, with range buttons (7, 14, 30
days, all) that filter the embedded days in the browser, a tooltip on every
day, and a table behind every chart. The page holds student ids, so keep it
beside the store (`--out`), never in a repository. Staff accounts are left out
unless `--instructors`.

Each report prints the thresholds it used, and each is a flag:

| Flag | Default | Meaning |
|---|---|---|
| `--idle-gap` | 30 min | a longer silence starts a new visit |
| `--follow-up` | 60 s | think time under this is a follow-up question |
| `--reading-max` | 10 min | think time up to this is reading; longer is away |
| `--read-slide` | 3 s | a median dwell at or over this is reading a slide, under it flipping |
| `--coverage` | 50 % | slides seen over slides in the deck, to count a lecture covered |
| `--listened` | 80 % | narration played over the lecture's narration, to count it listened |
| `--left-after` | 10 min | no on-screen signal this long after a reply: the student left |
| `--heartbeat` | 60 s | how long one on-screen signal counts as active (match `ACTIVITY_HEARTBEAT_S`) |

## 6. Refreshing

Run the export again; it continues from the last id. After a deletion
request, delete the student from the local file too (section 1). A new term
does not need a new file: filter by class or by window.
