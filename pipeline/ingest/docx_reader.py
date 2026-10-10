"""Extract text from Word documents (.docx) and plain text grading files."""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass
from pathlib import Path

import docx


@dataclass
class DocChunk:
    source_file: str
    section: str
    text: str
    doc_type: str = "assignment"


def _clean(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def _guess_doc_type(path: Path) -> str:
    name = path.name.lower()
    if "grading" in name:
        return "grading_rubric"
    if "assign" in name or "p1" in name or "p2" in name or "p3" in name or "p4" in name or "p5" in name:
        return "assignment"
    if "exam" in name or "midterm" in name or "final" in name:
        return "exam"
    return "document"


def extract_docx(docx_path: Path) -> list[DocChunk]:
    doc_type = _guess_doc_type(docx_path)
    document = docx.Document(str(docx_path))
    chunks: list[DocChunk] = []
    current_section = "Introduction"
    buffer: list[str] = []

    def flush():
        text = _clean(" ".join(buffer))
        if len(text.split()) >= 5:
            chunks.append(DocChunk(
                source_file=docx_path.name,
                section=current_section,
                text=text,
                doc_type=doc_type,
            ))
        buffer.clear()

    for para in document.paragraphs:
        text = _clean(para.text)
        if not text:
            continue
        # Treat heading-style paragraphs as section boundaries
        if para.style and "heading" in para.style.name.lower():
            flush()
            current_section = text
        else:
            buffer.append(text)
            if len(buffer) >= 10:
                flush()

    flush()
    return chunks


def extract_txt(txt_path: Path) -> list[DocChunk]:
    doc_type = _guess_doc_type(txt_path)
    lines = txt_path.read_text(errors="replace").splitlines()
    chunks: list[DocChunk] = []
    current_section = "Document"
    buffer: list[str] = []

    def flush():
        text = _clean(" ".join(buffer))
        if len(text.split()) >= 5:
            chunks.append(DocChunk(
                source_file=txt_path.name,
                section=current_section,
                text=text,
                doc_type=doc_type,
            ))
        buffer.clear()

    for line in lines:
        line = line.strip()
        if not line:
            if buffer:
                flush()
            continue
        # Detect section headers: short all-caps or ending with ":"
        if len(line) < 80 and (line.isupper() or line.endswith(":")):
            flush()
            current_section = line.rstrip(":")
        else:
            buffer.append(line)

    flush()
    return chunks


def save_doc_chunks(chunks: list[DocChunk], output_dir: Path, stem: str) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    out_path = output_dir / f"{stem}.json"
    out_path.write_text(
        json.dumps([asdict(c) for c in chunks], indent=2, ensure_ascii=False)
    )
    return out_path


def ingest_documents(paths: list[Path], output_dir: Path, verbose: bool = False) -> list[Path]:
    written: list[Path] = []
    for path in paths:
        if path.suffix.lower() == ".docx":
            chunks = extract_docx(path)
        elif path.suffix.lower() in (".txt", ".md"):
            chunks = extract_txt(path)
        else:
            continue
        out = save_doc_chunks(chunks, output_dir, path.stem)
        written.append(out)
        if verbose:
            print(f"  {path.name}: {len(chunks)} chunks -> {out.name}")
    return written
