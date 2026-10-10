"""Extract structured text from PDF files (textbook, exams, study guides)."""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass
from pathlib import Path

import pdfplumber


@dataclass
class TextChunk:
    source_file: str
    page_number: int
    chunk_index: int
    text: str
    doc_type: str = "unknown"  # textbook, exam, study_guide, assignment


def _clean_text(text: str) -> str:
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def _split_into_chunks(text: str, chunk_size: int = 400, overlap: int = 50) -> list[str]:
    words = text.split()
    chunks: list[str] = []
    start = 0
    while start < len(words):
        end = min(start + chunk_size, len(words))
        chunks.append(" ".join(words[start:end]))
        start += chunk_size - overlap
    return chunks


def _guess_doc_type(pdf_path: Path) -> str:
    name = pdf_path.name.lower()
    if "study" in name or "guide" in name:
        return "study_guide"
    if "exam" in name or "midterm" in name or "final" in name:
        return "exam"
    if "assign" in name or "hw" in name or "homework" in name:
        return "assignment"
    return "textbook"


def extract_pdf_chunks(
    pdf_path: Path,
    chunk_size: int = 400,
    overlap: int = 50,
    doc_type: str | None = None,
) -> list[TextChunk]:
    if doc_type is None:
        doc_type = _guess_doc_type(pdf_path)

    chunks: list[TextChunk] = []
    chunk_idx = 0

    try:
        with pdfplumber.open(str(pdf_path)) as pdf:
            for page_num, page in enumerate(pdf.pages, start=1):
                try:
                    raw = page.extract_text() or ""
                except Exception:
                    continue
                text = _clean_text(raw)
                if not text:
                    continue
                page_chunks = _split_into_chunks(text, chunk_size, overlap)
                for chunk_text in page_chunks:
                    if len(chunk_text.split()) < 10:
                        continue
                    chunks.append(TextChunk(
                        source_file=pdf_path.name,
                        page_number=page_num,
                        chunk_index=chunk_idx,
                        text=chunk_text,
                        doc_type=doc_type,
                    ))
                    chunk_idx += 1
    except Exception:
        # Password-protected or corrupt PDF - skip silently
        return chunks

    return chunks


def save_pdf_chunks(chunks: list[TextChunk], output_dir: Path, stem: str) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    out_path = output_dir / f"{stem}.json"
    out_path.write_text(
        json.dumps([asdict(c) for c in chunks], indent=2, ensure_ascii=False)
    )
    return out_path


def ingest_pdfs(
    pdf_paths: list[Path],
    output_dir: Path,
    chunk_size: int = 400,
    skip_existing: bool = True,
    verbose: bool = False,
) -> list[Path]:
    written: list[Path] = []
    output_dir.mkdir(parents=True, exist_ok=True)
    for pdf_path in pdf_paths:
        out_path = output_dir / f"{pdf_path.stem}.json"
        if skip_existing and out_path.exists():
            written.append(out_path)
            continue
        try:
            chunks = extract_pdf_chunks(pdf_path, chunk_size=chunk_size)
        except Exception as e:
            if verbose:
                print(f"  SKIP {pdf_path.name}: {e}")
            continue
        if not chunks:
            if verbose:
                print(f"  SKIP {pdf_path.name}: no extractable text (may be encrypted or image-only)")
            continue
        out = save_pdf_chunks(chunks, output_dir, pdf_path.stem)
        written.append(out)
        if verbose:
            print(f"  {pdf_path.name}: {len(chunks)} chunks -> {out.name}")
    return written
