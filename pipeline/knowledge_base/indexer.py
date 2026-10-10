"""Index all extracted raw JSON into ChromaDB.

Each input file is read whole before any of it is written, so a malformed file
is reported and skipped instead of stopping a run halfway through its upserts
(plan step 6.7, finding E10). ``build_index(rebuild=True)`` (``index
--rebuild``) drops the collections first, so chunks of a removed or re-chunked
file do not linger. Every build writes ``manifest.json`` into the store: the
inputs with their hashes, the counts, the embedder, and the commit, so a seed
says what it was built from (finding E9).
"""

from __future__ import annotations

import datetime
import hashlib
import json
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from pipeline.config import get_kb_config
from pipeline.knowledge_base.vectorstore import EMBEDDER, get_client, get_or_create_collection, upsert_documents
from tutor.courses import COURSES
from tutor.kb import COLLECTIONS

BATCH_SIZE = 64
MANIFEST = "manifest.json"

# Transcript folders under knowledge-base/transcripts/ are named
# <course>-<term> (AST101-2026Fall); the prefix must be a course id in
# courses.yaml, compared without regard to case.
_COURSE_PREFIXES = {c.lower(): c for c in COURSES}

Row = tuple[str, str, dict]   # (id, document, metadata)


@dataclass
class BuildLog:
    """What a build read, and what it could not."""
    inputs: list[Path] = field(default_factory=list)
    failures: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


def _make_id(source: str, idx) -> str:
    return hashlib.md5(f"{source}:{idx}".encode()).hexdigest()


# ---------------------------------------------------------------------------
# One parser per input format: a file's rows, or an exception
# ---------------------------------------------------------------------------

def _slide_rows(jf: Path, collection_name: str) -> list[Row]:
    data = json.loads(jf.read_text())
    filename = data.get("filename", "")
    rows = []
    for slide in data.get("slides", []):
        text_parts = []
        if slide.get("title"):
            text_parts.append(slide["title"])
        text_parts.extend(slide.get("body_text", []))
        if slide.get("speaker_notes"):
            text_parts.append(slide["speaker_notes"])
        text = " ".join(text_parts).strip()
        if not text or len(text.split()) < 3 or not filename:
            continue
        rows.append((_make_id(filename, slide["slide_number"]), text[:2000], {
            "source_file": filename,
            "lecture_title": data.get("lecture_title", ""),
            "slide_number": slide["slide_number"],
            "has_code": str(slide.get("has_code", False)),
            "collection": collection_name,
        }))
    return rows


def _transcript_rows(jf: Path, collection_name: str) -> list[Row]:
    """Two formats: the pipeline's ({source_file, full_text, segments: [{start,
    end, text}]}) and the bare whisper CLI's ({text, segments: [...]}). A
    segment's id is its position in the file, not its start time, which two
    segments can share. A bare file in a knowledge-base/transcripts/<course>-<term>
    folder is named for that folder too ("AST102-2026Winter/2026-01-08"): two
    courses' recordings may share a file name, and must not share ids."""
    data = json.loads(jf.read_text())
    folder = jf.parent.name
    corpus = jf.parent.parent.name == "transcripts" and folder.split("-", 1)[0].lower() in _COURSE_PREFIXES
    source_file = data.get("source_file") or (f"{folder}/{jf.stem}" if corpus else jf.stem)
    rows = []
    for i, seg in enumerate(data.get("segments", [])):
        text = seg.get("text", "").strip()
        if not text or len(text.split()) < 5:
            continue
        start = seg.get("start", 0.0)
        rows.append((_make_id(source_file, f"segment-{i}"), text[:1000], {
            "source_file": source_file,
            "start_seconds": str(start),
            "end_seconds": str(seg.get("end", 0.0)),
            "collection": collection_name,
        }))
    return rows


def _chunk_rows(jf: Path, collection_name: str) -> list[Row]:
    rows = []
    for i, chunk in enumerate(json.loads(jf.read_text())):
        text = chunk.get("text", "").strip()
        if not text or len(text.split()) < 5:
            continue
        meta = {"source_file": chunk.get("source_file", jf.name), "collection": collection_name}
        for key in ("page_number", "chunk_index", "doc_type", "section"):
            if key in chunk:
                meta[key] = str(chunk[key])
        rows.append((_make_id(jf.name, i), text[:2000], meta))
    return rows


def _code_rows(jf: Path, collection_name: str) -> list[Row]:
    """Each code file is one document, truncated. The file is a JSON list of
    {repo, relative_path, language, content} (``pipeline github`` writes it)."""
    rows = []
    for i, f in enumerate(json.loads(jf.read_text())):
        content = f.get("content", "").strip()
        if not content or len(content.split()) < 5:
            continue
        rows.append((_make_id(jf.name, i), content[:3000], {
            "source_file": f.get("repo", jf.stem),
            "relative_path": f.get("relative_path", ""),
            "language": f.get("language", "unknown"),
            "collection": collection_name,
        }))
    return rows


# A file that is not the JSON its parser expects. Anything else (the store
# refusing a write) still stops the build.
_BAD_INPUT = (OSError, ValueError, KeyError, TypeError, AttributeError)


def _index_files(client, collection_name: str, files, parse: Callable[[Path, str], list[Row]],
                 log: BuildLog | None = None, verbose: bool = False, source: str = "") -> int:
    collection = get_or_create_collection(client, collection_name)
    log = log if log is not None else BuildLog()
    batch: list[Row] = []
    total = 0

    def flush(rows: list[Row]) -> int:
        if rows:
            ids, docs, metas = (list(x) for x in zip(*rows))
            upsert_documents(collection, docs, metas, ids)
        return len(rows)

    for jf in files:
        try:
            rows = parse(jf, collection_name)
        except _BAD_INPUT as exc:
            log.failures.append(f"{jf}: {exc.__class__.__name__}: {exc}")
            continue
        log.inputs.append(jf)
        batch.extend(rows)
        while len(batch) >= BATCH_SIZE:
            total += flush(batch[:BATCH_SIZE])
            batch = batch[BATCH_SIZE:]
    total += flush(batch)
    if verbose:
        print(f"  [index] {collection_name}{f' from {source}' if source else ''}: {total} documents indexed")
    return total


def _json_files(folder: Path, skip: frozenset = frozenset()) -> list[Path]:
    return [p for p in sorted(folder.glob("*.json")) if p.name not in skip] if folder.is_dir() else []


# The earlier entry points, kept for the CLI's index-transcripts and callers.
def _index_slides_from_dir(client, collection_name: str, slides_dir: Path, verbose: bool = False,
                           log: BuildLog | None = None) -> int:
    return _index_files(client, collection_name, _json_files(slides_dir), _slide_rows, log, verbose)


def _index_transcripts_from_dir(client, collection_name: str, transcripts_dir: Path, verbose: bool = False,
                                log: BuildLog | None = None, files: list[Path] | None = None) -> int:
    """The transcripts in ``transcripts_dir``, or just ``files`` from it."""
    if files is None:
        files = _json_files(transcripts_dir, _NOT_TRANSCRIPTS)
    return _index_files(client, collection_name, files, _transcript_rows, log, verbose,
                        source=f"{transcripts_dir.parent.name}/{transcripts_dir.name}")


def _index_collection_from_dir(client, collection_name: str, raw_dir: Path, verbose: bool = False,
                               log: BuildLog | None = None) -> int:
    return _index_files(client, collection_name, _json_files(raw_dir / collection_name), _chunk_rows, log, verbose)


def transcript_dirs(raw_dir: Path, log: BuildLog | None = None) -> dict[str, list[Path]]:
    """{collection: folders}. The transcripts collection reads the pipeline's
    own folder under raw/, then every knowledge-base/transcripts/<course>-<term>
    folder whose prefix is a course in courses.yaml. A folder without one is
    noted and skipped, never silently indexed under a guess (plan step 6.7,
    finding E9: corpora listed by hand were missed)."""
    out: dict[str, list[Path]] = {"transcripts": [raw_dir / "transcripts"]}
    corpora = raw_dir.parent / "transcripts"
    if corpora.is_dir():
        for folder in sorted(p for p in corpora.iterdir() if p.is_dir()):
            if folder.name.split("-", 1)[0].lower() not in _COURSE_PREFIXES:
                if log is not None:
                    log.notes.append(f"{folder}: the name does not start with a course id "
                                     f"from courses.yaml ({', '.join(COURSES)}) and a dash; not indexed")
                continue
            out["transcripts"].append(folder)
    return out


_NOT_TRANSCRIPTS = frozenset({"lecture-mapping.json"})


def transcript_files(folders: list[Path], log: BuildLog | None = None) -> list[tuple[Path, list[Path]]]:
    """Each folder with the transcripts to index from it. The first folder is
    the pipeline's own (raw/transcripts, named after each video); a bare
    Whisper copy of one of its recordings (the same file stem) in a later
    folder is the same lecture and is indexed once, from the pipeline's copy
    (it used to be indexed twice). Files with the same stem in two different
    corpus folders are different recordings (two courses, or two terms) and
    are both kept."""
    pipeline_own: dict[str, Path] = {}
    out = []
    for i, folder in enumerate(folders):
        keep = []
        for jf in _json_files(folder, _NOT_TRANSCRIPTS):
            if i == 0:
                pipeline_own[jf.stem] = jf
                keep.append(jf)
            elif jf.stem in pipeline_own:
                if log is not None:
                    log.notes.append(f"{jf}: the same recording as {pipeline_own[jf.stem]}, indexed once")
            else:
                keep.append(jf)
        out.append((folder, keep))
    return out


def transcript_folders(raw_dir: Path) -> list[Path]:
    """The folders the transcripts collection is built from; ``index`` and
    ``index-transcripts`` read the same list (plan step 5.8, finding E17)."""
    return transcript_dirs(raw_dir)["transcripts"]


def _index_transcript_collection(collection_name: str):
    def build(client, raw_dir: Path, verbose: bool, log: BuildLog) -> int:
        folders = transcript_dirs(raw_dir, log)[collection_name]
        return sum(_index_transcripts_from_dir(client, collection_name, folder, verbose, log, files=files)
                   for folder, files in transcript_files(folders, log))
    return build


def _chunks(collection_name: str):
    def build(client, raw_dir: Path, verbose: bool, log: BuildLog) -> int:
        return _index_collection_from_dir(client, collection_name, raw_dir, verbose, log)
    return build


# One builder per collection in tutor.kb.COLLECTIONS (plan step 5.8, E11).
# Each reads knowledge-base/raw/<collection>/*.json in the shape its parser
# expects (ADDING-KNOWLEDGE.md, step 3).
Builder = Callable[[object, Path, bool, BuildLog], int]
BUILDERS: dict[str, Builder] = {
    "slides": lambda client, raw_dir, verbose, log: _index_slides_from_dir(
        client, "slides", raw_dir / "slides", verbose, log),
    "transcripts": _index_transcript_collection("transcripts"),
    "textbook": _chunks("textbook"),
    "assignments": _chunks("assignments"),
    "documents": _chunks("documents"),
    "code": lambda client, raw_dir, verbose, log: _index_files(
        client, "code", _json_files(raw_dir / "code"), _code_rows, log, verbose),
    "forum": _chunks("forum"),
}


def _git_commit(root: Path) -> str | None:
    try:
        out = subprocess.run(["git", "rev-parse", "HEAD"], cwd=root, capture_output=True, text=True, timeout=10)
    except OSError:
        return None
    if out.returncode != 0:
        return None
    return out.stdout.strip() or None


def write_manifest(db_dir: Path, raw_dir: Path, counts: dict[str, int], log: BuildLog, rebuild: bool) -> Path:
    """manifest.json in the store: what this build read and wrote. The seed
    (a tarball of the store) carries it."""
    root = raw_dir.parent.parent

    def shown(path: Path) -> str:
        try:
            return path.resolve().relative_to(root.resolve()).as_posix()
        except ValueError:
            return str(path)

    manifest = {
        "built_at": datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0).isoformat(),
        "git_commit": _git_commit(root),
        "embedder": EMBEDDER,
        "rebuild": rebuild,
        "collections": counts,
        "inputs": [{"path": shown(p), "bytes": p.stat().st_size,
                    "sha256": hashlib.sha256(p.read_bytes()).hexdigest()} for p in log.inputs],
        "failures": log.failures,
        "notes": log.notes,
    }
    path = Path(db_dir) / MANIFEST
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(manifest, indent=1) + "\n")
    return path


def build_index(raw_dir: Path | None = None, db_dir: Path | None = None, verbose: bool = True,
                rebuild: bool = False, log: BuildLog | None = None) -> dict[str, int]:
    kb = get_kb_config()
    if raw_dir is None:
        raw_dir = kb["raw_dir"]
    if db_dir is None:
        db_dir = kb["db_dir"]
    log = log if log is not None else BuildLog()

    client = get_client(db_dir)
    if rebuild:
        existing = {getattr(c, "name", c) for c in client.list_collections()}
        for name in COLLECTIONS:
            if name in existing:
                client.delete_collection(name)
    counts = {name: BUILDERS[name](client, raw_dir, verbose, log) for name in COLLECTIONS}
    write_manifest(db_dir, raw_dir, counts, log, rebuild)

    total = sum(counts.values())
    if verbose:
        print(f"[index] Total: {total} documents across {len(counts)} collections")
    return counts
