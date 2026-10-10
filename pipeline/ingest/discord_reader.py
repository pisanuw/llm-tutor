"""Parse Discord channel exports for course-related conversations.

Supports:
1. DiscordChatExporter JSON format (most common self-export tool)
2. Plain text/CSV exports

Discord bot token: set DISCORD_TOKEN in .env or pass token_file argument.
Canvas token is not used here; Discord requires its own bot token.

To export: Use DiscordChatExporter (github.com/Tyrrrz/DiscordChatExporter)
  DiscordChatExporter.Cli export -t <TOKEN> -c <CHANNEL_ID> --format Json

What counts as course-relevant and who counts as the instructor come from
courses.yaml: each course's id, label, title and topics, a few words every
course uses (lecture, assignment, exam, ...), and the instructor's names.
The output goes to the forum collection's folder (knowledge-base/raw/forum,
which ``pipeline discord`` passes), one JSON list per export; each message
carries a ``text`` field, the shape the forum collection is indexed from.
"""

from __future__ import annotations

import datetime
import json
import re
from dataclasses import asdict, dataclass
from pathlib import Path

from tutor.courses import COURSE_LIST, IDENTITY

# Words any course's discussion uses, whatever the subject.
GENERIC_KEYWORDS = (
    "lecture", "assignment", "homework", "exam", "midterm", "final exam", "quiz",
    "project", "office hours", "syllabus", "textbook",
)

_TITLES = re.compile(r"^(?:dr|prof|professor|mr|mrs|ms|mx)\.?\s+", re.I)


def course_keywords() -> list[str]:
    """Lowercased words and phrases that mark a message as about the courses:
    each course's id, label and title, the phrases of its ``topics`` line
    (split at commas and "and"), the generic course words, and, when the
    course uses code, the language's name."""
    words: list[str] = list(GENERIC_KEYWORDS)
    for c in COURSE_LIST:
        words += [c.id, c.label, c.title]
        for part in re.split(r",|\band\b", c.topics):
            part = re.sub(r"^(?:the|a|an)\s+", "", part.strip(), flags=re.I)
            if len(part) >= 4:
                words.append(part)
    if IDENTITY.code_language:
        words += [IDENTITY.code_language, "compile", "error"]
    seen: list[str] = []
    for w in (w.strip().lower() for w in words):
        if w and w not in seen:
            seen.append(w)
    return seen


def relevance_pattern(keywords: list[str] | None = None) -> re.Pattern:
    """One case-insensitive pattern matching any keyword as whole words
    (``course_keywords()`` when none are given)."""
    words = keywords if keywords is not None else course_keywords()
    if not words:
        return re.compile(r"(?!)")   # matches nothing
    return re.compile(r"(?<!\w)(?:" + "|".join(re.escape(k) for k in words) + r")(?!\w)",
                      re.IGNORECASE)


def instructor_name_hints() -> list[str]:
    """Lowercased name fragments that mark a poster as the instructor: the
    instructor's names from courses.yaml without the title ("alex morgan",
    "morgan"), plus the generic "instructor" and "prof"."""
    names: list[str] = []
    for full in (IDENTITY.instructor, IDENTITY.instructor_short):
        bare = _TITLES.sub("", full.strip()).strip()
        if bare:
            names.append(bare)
            names.append(bare.split()[-1])
    names += ["instructor", "prof"]
    out: list[str] = []
    for n in (n.lower().strip(".,") for n in names):
        if n and n not in out:
            out.append(n)
    return out


@dataclass
class DiscordMessage:
    message_id: str
    author: str
    timestamp: str
    content: str
    channel: str
    is_instructor: bool = False


@dataclass
class ConversationThread:
    channel: str
    topic: str
    messages: list[DiscordMessage]

    @property
    def text(self) -> str:
        parts = [f"[{m.author}] {m.content}" for m in self.messages if m.content.strip()]
        return "\n".join(parts)


def _is_instructor(author: str, instructor_names: list[str]) -> bool:
    author_lower = author.lower()
    return any(name.lower() in author_lower for name in instructor_names)


def parse_discordchatexporter_json(
    export_path: Path,
    instructor_names: list[str] | None = None,
) -> list[DiscordMessage]:
    if instructor_names is None:
        instructor_names = instructor_name_hints()

    raw = json.loads(export_path.read_text(encoding="utf-8"))
    channel = raw.get("channel", {}).get("name", export_path.stem)

    messages: list[DiscordMessage] = []
    for msg in raw.get("messages", []):
        content = msg.get("content", "").strip()
        if not content:
            continue
        author = msg.get("author", {}).get("name", "unknown")
        messages.append(DiscordMessage(
            message_id=str(msg.get("id", "")),
            author=author,
            timestamp=msg.get("timestamp", ""),
            content=content,
            channel=channel,
            is_instructor=_is_instructor(author, instructor_names),
        ))
    return messages


def parse_plain_text_export(export_path: Path) -> list[DiscordMessage]:
    """Parse a simple line-by-line Discord export: [timestamp] Author: message"""
    messages: list[DiscordMessage] = []
    pattern = re.compile(r"^\[?(.+?)\]?\s+([\w\s]+?):\s+(.+)$")
    channel = export_path.stem

    for line in export_path.read_text(errors="replace").splitlines():
        m = pattern.match(line.strip())
        if not m:
            continue
        timestamp, author, content = m.groups()
        messages.append(DiscordMessage(
            message_id="",
            author=author.strip(),
            timestamp=timestamp.strip(),
            content=content.strip(),
            channel=channel,
        ))
    return messages


def filter_course_relevant(
    messages: list[DiscordMessage],
    keywords: list[str] | None = None,
) -> list[DiscordMessage]:
    pattern = relevance_pattern(keywords)
    return [m for m in messages if pattern.search(m.content)]


def group_into_threads(
    messages: list[DiscordMessage],
    window_minutes: int = 30,
) -> list[ConversationThread]:
    if not messages:
        return []

    threads: list[ConversationThread] = []
    current: list[DiscordMessage] = [messages[0]]
    window = datetime.timedelta(minutes=window_minutes)

    for msg in messages[1:]:
        gap_exceeded = False
        try:
            prev_ts = datetime.datetime.fromisoformat(current[-1].timestamp.replace("Z", "+00:00"))
            cur_ts = datetime.datetime.fromisoformat(msg.timestamp.replace("Z", "+00:00"))
            gap_exceeded = (cur_ts - prev_ts) > window
        except (ValueError, AttributeError):
            pass

        if gap_exceeded:
            threads.append(ConversationThread(
                channel=current[0].channel,
                topic=current[0].content[:80],
                messages=current,
            ))
            current = [msg]
        else:
            current.append(msg)

    if current:
        threads.append(ConversationThread(
            channel=current[0].channel,
            topic=current[0].content[:80],
            messages=current,
        ))

    return threads


def forum_rows(messages: list[DiscordMessage], source: str) -> list[dict]:
    """Each message as a forum-collection row: its own fields plus ``text``
    ("[author] content", what is embedded) and ``source_file``."""
    return [{**asdict(m), "text": f"[{m.author}] {m.content}", "source_file": source}
            for m in messages]


def save_discord_chunks(messages: list[DiscordMessage], output_dir: Path, stem: str) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    out_path = output_dir / f"{stem}.json"
    payload = forum_rows(messages, out_path.name)
    out_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False))
    return out_path


def ingest_discord_exports(
    exports_dir: Path,
    output_dir: Path,
    instructor_names: list[str] | None = None,
    filter_relevant: bool = True,
    verbose: bool = False,
) -> list[Path]:
    written: list[Path] = []
    if not exports_dir.exists():
        print(f"[discord] Exports directory not found: {exports_dir}")
        return written

    json_files = list(exports_dir.glob("*.json"))
    txt_files = list(exports_dir.glob("*.txt"))

    for f in sorted(json_files + txt_files):
        if f.suffix == ".json":
            messages = parse_discordchatexporter_json(f, instructor_names)
        else:
            messages = parse_plain_text_export(f)

        if filter_relevant:
            messages = filter_course_relevant(messages)

        if not messages:
            continue

        out = save_discord_chunks(messages, output_dir, f"discord_{f.stem}")
        written.append(out)
        if verbose:
            print(f"  {f.name}: {len(messages)} relevant messages -> {out.name}")

    return written
