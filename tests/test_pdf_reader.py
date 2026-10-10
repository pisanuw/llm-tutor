"""Tests for PDF ingestion."""

from __future__ import annotations

import json
from pathlib import Path


from pipeline.ingest.pdf_reader import (
    TextChunk,
    _clean_text,
    _split_into_chunks,
    _guess_doc_type,
    save_pdf_chunks,
)


def test_clean_text_collapses_whitespace():
    assert _clean_text("hello   world\n\t foo") == "hello world foo"


def test_split_into_chunks_basic():
    words = " ".join(f"word{i}" for i in range(100))
    chunks = _split_into_chunks(words, chunk_size=20, overlap=5)
    assert len(chunks) > 1
    # First chunk has correct number of words
    assert len(chunks[0].split()) == 20


def test_split_into_chunks_overlap():
    words = " ".join(str(i) for i in range(10))
    chunks = _split_into_chunks(words, chunk_size=6, overlap=2)
    # Chunk 0: words 0-5; Chunk 1: words 4-9 (overlap of 2)
    assert chunks[0].startswith("0")
    assert "4" in chunks[1]


def test_guess_doc_type():
    assert _guess_doc_type(Path("midterm_exam.pdf")) == "exam"
    assert _guess_doc_type(Path("final_study_guide.pdf")) == "study_guide"
    assert _guess_doc_type(Path("the-cosmic-perspective.pdf")) == "textbook"
    assert _guess_doc_type(Path("assignment1.pdf")) == "assignment"


def test_save_pdf_chunks(tmp_path):
    chunks = [
        TextChunk(source_file="test.pdf", page_number=1, chunk_index=0, text="hello world data", doc_type="textbook"),
        TextChunk(source_file="test.pdf", page_number=1, chunk_index=1, text="more content here", doc_type="textbook"),
    ]
    out = save_pdf_chunks(chunks, tmp_path, "test")
    assert out.exists()
    data = json.loads(out.read_text())
    assert len(data) == 2
    assert data[0]["text"] == "hello world data"
    assert data[0]["doc_type"] == "textbook"
