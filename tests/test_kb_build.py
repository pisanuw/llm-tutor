"""Plan step 6.7 (findings E8, E9, E10): building the knowledge base.

One embedder for building and querying; every transcript corpus indexed by its
course prefix; a malformed file skipped and reported, never a half-written
run; transcript ids that cannot collide; ``index --rebuild``; and a manifest
of what the build read, inside the store.
"""
from __future__ import annotations

import hashlib
import json
import re
import types
from pathlib import Path
from unittest.mock import patch

from click.testing import CliRunner

from pipeline.knowledge_base import indexer, vectorstore
from tutor.kb import COLLECTIONS

ROOT = Path(__file__).resolve().parent.parent
# A small tree with a file for every collection and a course-prefixed corpus.
GOLDEN_KB = ROOT / "tests" / "golden" / "kb" / "knowledge-base"


class FakeClient:
    """Records what the build asks of the store."""

    def __init__(self, existing=()):
        self.existing = list(existing)
        self.deleted: list[str] = []

    def list_collections(self):
        return [types.SimpleNamespace(name=n) for n in self.existing]

    def delete_collection(self, name):
        self.deleted.append(name)

    def get_or_create_collection(self, name, metadata, embedding_function):
        return types.SimpleNamespace(name=name)


def _build(raw: Path, db: Path, client=None, **kw):
    written: dict[str, list[tuple]] = {}

    def upsert(collection, documents, metadatas, ids):
        written.setdefault(collection.name, []).extend(zip(ids, documents, metadatas))

    log = indexer.BuildLog()
    with patch.object(indexer, "get_client", return_value=client or FakeClient()), \
         patch.object(indexer, "upsert_documents", upsert):
        counts = indexer.build_index(raw_dir=raw, db_dir=db, verbose=False, log=log, **kw)
    return counts, written, log


def _tree(tmp: Path) -> Path:
    """A project with a raw folder and three transcript corpora, one lecture
    in each (named for its folder: each is a different recording)."""
    kb = tmp / "knowledge-base"
    seg = {"segments": [{"start": 1.0, "end": 2.0, "text": "a segment long enough to be indexed here"}]}
    for folder in ("AST101-2023Spring", "AST102-2025Autumn", "AST102-2026Winter", "notes"):
        (kb / "transcripts" / folder).mkdir(parents=True)
        (kb / "transcripts" / folder / f"{folder}.json").write_text(json.dumps(seg))
    (kb / "raw").mkdir()
    return kb / "raw"


def test_the_build_and_the_app_embed_the_same_way():
    assert type(vectorstore.embedding_function()).__name__ == "DefaultEmbeddingFunction"
    assert vectorstore.embedding_function() is vectorstore.embedding_function()
    requirements = (ROOT / "requirements.txt").read_text().lower()
    assert not re.search(r"^sentence-transformers\b", requirements, re.M)
    assert "sentence_transformers" not in (ROOT / "pipeline" / "knowledge_base" / "vectorstore.py").read_text()


def test_transcript_folders_go_to_their_course_by_prefix(tmp_path):
    """The transcripts collection reads the pipeline's own folder, then every
    corpus whose name starts with a course id from courses.yaml (in any case)."""
    raw = _tree(tmp_path)
    log = indexer.BuildLog()
    dirs = indexer.transcript_dirs(raw, log)
    assert list(dirs) == ["transcripts"]
    assert [d.name for d in dirs["transcripts"]] == [
        "transcripts", "AST101-2023Spring", "AST102-2025Autumn", "AST102-2026Winter"]
    assert len(log.notes) == 1 and "notes: the name does not start with a course id" in log.notes[0]
    (tmp_path / "knowledge-base" / "transcripts" / "ast101-2024Fall").mkdir()
    assert "ast101-2024Fall" in [d.name for d in indexer.transcript_dirs(raw)["transcripts"]]


def test_every_tracked_transcript_corpus_is_indexed():
    """Two of them were not (finding E9)."""
    raw = GOLDEN_KB / "raw"
    indexed = {d for folders in indexer.transcript_dirs(raw).values() for d in folders}
    corpora = {d for d in (GOLDEN_KB / "transcripts").iterdir() if d.is_dir()}
    assert corpora and corpora <= indexed
    assert {"AST102-2026Winter"} <= {d.name for d in corpora}


def test_every_collection_is_built_from_its_raw_folder(tmp_path):
    """One builder per collection, each reading knowledge-base/raw/<collection>/
    (the transcripts builder the corpora too); every file is read."""
    counts, written, log = _build(GOLDEN_KB / "raw", tmp_path / "db")
    assert list(counts) == list(COLLECTIONS) and all(counts.values()), counts
    assert {name: len(rows) for name, rows in written.items()} == counts
    assert log.failures == []
    read = {p.relative_to(GOLDEN_KB).as_posix() for p in log.inputs}
    assert read == {p.relative_to(GOLDEN_KB).as_posix() for p in GOLDEN_KB.rglob("*.json")}
    assert {p.split("/")[1] for p in read if p.startswith("raw/")} == set(COLLECTIONS)


def test_a_malformed_file_is_skipped_and_reported(tmp_path):
    raw = _tree(tmp_path)
    (raw / "assignments").mkdir()
    (raw / "assignments" / "bad.json").write_text("{ not json")
    (raw / "assignments" / "good.json").write_text(json.dumps([{"text": "a chunk with enough words in it"}]))
    counts, written, log = _build(raw, tmp_path / "db")
    assert counts["assignments"] == 1 and len(written["assignments"]) == 1
    assert len(log.failures) == 1 and "bad.json: JSONDecodeError" in log.failures[0]
    assert counts["transcripts"] == 3                                 # the corpora, indexed


def test_transcript_segments_with_the_same_start_get_their_own_ids(tmp_path):
    same = {"source_file": "lec.mp4", "segments": [
        {"start": 5.0, "end": 6.0, "text": "the first segment has plenty of words"},
        {"start": 5.0, "end": 7.0, "text": "the second segment starts at the same time"}]}
    path = tmp_path / "lec.json"
    path.write_text(json.dumps(same))
    rows = indexer._transcript_rows(path, "transcripts")
    assert len({row[0] for row in rows}) == 2


def test_a_recording_in_two_folders_is_indexed_once(tmp_path):
    """The pipeline's transcript of a lecture wins over a bare Whisper copy of
    it; a lecture that was in two folders used to be indexed twice."""
    raw = _tree(tmp_path)
    corpora = tmp_path / "knowledge-base" / "transcripts"
    bare = {"segments": [{"start": 0.0, "end": 1.0, "text": "a bare Whisper copy of the same lecture"}]}
    for folder in ("AST101-2023Spring", "AST102-2025Autumn", "AST102-2026Winter"):
        (corpora / folder / "Monday lecture.json").write_text(json.dumps(bare))
    (raw / "transcripts").mkdir()
    ours = {"source_file": "Monday lecture.mp4", "segments": [
        {"start": 0.0, "end": 1.0, "text": "the pipeline's own transcript of this lecture"}]}
    (raw / "transcripts" / "Monday lecture.json").write_text(json.dumps(ours))
    counts, written, log = _build(raw, tmp_path / "db")
    texts = [row[1] for row in written["transcripts"]]
    # Ours, and each corpus's own lecture; none of the bare copies.
    assert counts["transcripts"] == 4 and texts.count("the pipeline's own transcript of this lecture") == 1
    assert "a bare Whisper copy of the same lecture" not in texts
    dupes = [n for n in log.notes if "the same recording as" in n]
    assert len(dupes) == 3 and all("indexed once" in n for n in dupes)
    assert {Path(n.split(": the same")[0]).parent.name for n in dupes} == {
        "AST101-2023Spring", "AST102-2025Autumn", "AST102-2026Winter"}


def test_index_transcripts_indexes_a_recording_once_and_exits_1_on_a_bad_file(tmp_path):
    from pipeline import __main__ as cli_mod

    one, two = tmp_path / "a", tmp_path / "b"
    for folder in (one, two):
        folder.mkdir()
        (folder / "lecture.json").write_text(json.dumps(
            {"segments": [{"start": 0.0, "end": 1.0, "text": f"a segment from folder {folder.name} here"}]}))
    written = []

    def upsert(collection, documents, metadatas, ids):
        written.extend(documents)

    with patch("pipeline.knowledge_base.vectorstore.get_client", return_value=FakeClient()), \
         patch.object(indexer, "get_or_create_collection", lambda client, name: types.SimpleNamespace(name=name)), \
         patch.object(indexer, "upsert_documents", upsert):
        result = CliRunner().invoke(cli_mod.cli, ["index-transcripts", "--quiet",
                                                  "--transcripts-dir", str(one), "--transcripts-dir", str(two)])
        assert result.exit_code == 0, result.output
        assert written == ["a segment from folder a here"] and "indexed once" in result.output
        (two / "broken.json").write_text("{")
        result = CliRunner().invoke(cli_mod.cli, ["index-transcripts", "--quiet", "--transcripts-dir", str(two)])
    assert result.exit_code == 1 and "broken.json: JSONDecodeError" in result.output


def test_rebuild_drops_the_app_collections_first(tmp_path):
    raw = _tree(tmp_path)
    client = FakeClient(existing=["slides", "transcripts", "someone-elses"])
    _build(raw, tmp_path / "db", client=client, rebuild=True)
    assert client.deleted == ["slides", "transcripts"]
    client = FakeClient(existing=["slides"])
    _build(raw, tmp_path / "db", client=client)
    assert client.deleted == []


def test_the_manifest_says_what_the_build_read(tmp_path):
    raw = _tree(tmp_path)
    (raw / "code").mkdir()
    (raw / "code" / "bad.json").write_text("[1, 2")
    counts, _written, log = _build(raw, tmp_path / "db", rebuild=True, client=FakeClient())
    manifest = json.loads((tmp_path / "db" / indexer.MANIFEST).read_text())
    assert manifest["collections"] == counts and list(manifest["collections"]) == list(COLLECTIONS)
    assert manifest["embedder"] == vectorstore.EMBEDDER and manifest["rebuild"] is True
    inputs = {i["path"]: i for i in manifest["inputs"]}
    assert "knowledge-base/transcripts/AST102-2025Autumn/AST102-2025Autumn.json" in inputs
    one = tmp_path / "knowledge-base" / "transcripts" / "AST101-2023Spring" / "AST101-2023Spring.json"
    assert inputs["knowledge-base/transcripts/AST101-2023Spring/AST101-2023Spring.json"]["sha256"] == \
        hashlib.sha256(one.read_bytes()).hexdigest()
    assert len(manifest["failures"]) == 1 and "code/bad.json" in manifest["failures"][0]
    assert manifest["notes"] and "built_at" in manifest


def test_the_index_command_exits_1_when_a_file_was_skipped():
    from pipeline import __main__ as cli_mod

    def fake_build(verbose, rebuild, log):
        log.failures.append("knowledge-base/raw/code/bad.json: JSONDecodeError: boom")
        return {name: 0 for name in COLLECTIONS}

    with patch.object(indexer, "build_index", side_effect=fake_build) as build:
        result = CliRunner().invoke(cli_mod.cli, ["index", "--rebuild", "--quiet"])
    assert result.exit_code == 1
    assert "bad.json: JSONDecodeError" in result.output
    assert build.call_args.kwargs["rebuild"] is True


def test_two_courses_recordings_with_the_same_name_are_both_indexed(tmp_path):
    """One transcripts collection serves every course, so two courses' Whisper
    files with the same name (a date) are different recordings: both are
    indexed, under different ids, with the folder in their source name."""
    raw = tmp_path / "knowledge-base" / "raw"
    raw.mkdir(parents=True)
    corpora = tmp_path / "knowledge-base" / "transcripts"
    for folder, text in (("AST101-2026Fall", "the AST 101 lecture recorded that day"),
                         ("AST102-2026Fall", "the AST 102 lecture recorded that day")):
        (corpora / folder).mkdir(parents=True)
        (corpora / folder / "2026-10-01.json").write_text(json.dumps(
            {"segments": [{"start": 0.0, "end": 1.0, "text": text}]}))
    counts, written, log = _build(raw, tmp_path / "db")
    rows = written["transcripts"]
    assert counts["transcripts"] == 2 and len({r[0] for r in rows}) == 2
    assert {r[2]["source_file"] for r in rows} == {"AST101-2026Fall/2026-10-01", "AST102-2026Fall/2026-10-01"}
    assert not [n for n in log.notes if "the same recording as" in n]
