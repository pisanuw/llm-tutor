"""Orchestrate all data ingestion for a course."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from pipeline.config import get_course_config, get_kb_config
from pipeline.ingest.docx_reader import ingest_documents
from pipeline.ingest.pdf_reader import ingest_pdfs
from pipeline.ingest.pptx_reader import ingest_lectures


def _find_assignment_docs(assignments_dir: Path) -> list[Path]:
    if not assignments_dir.exists():
        return []
    docs: list[Path] = []
    for ext in ("*.docx", "*.txt", "*.md"):
        docs.extend(assignments_dir.rglob(ext))
    return sorted(docs)


def _find_pdfs(path: Path) -> list[Path]:
    """One PDF, or every PDF under a folder."""
    if path.is_file() and path.suffix.lower() == ".pdf":
        return [path]
    if path.is_dir():
        return sorted(path.rglob("*.pdf"))
    return []


def _find_exam_pdfs(exams_dir: Path) -> list[Path]:
    if not exams_dir.exists():
        return []
    return list(exams_dir.rglob("*.pdf"))


def run_ingest(course: str, dry_run: bool = False, verbose: bool = True) -> dict:
    cc = get_course_config(course)
    kb = get_kb_config()
    raw_dir = kb["raw_dir"]

    slides_dir = Path(cc["source_slides"])
    assignments_dir = Path(cc["source_assignments"])
    exams_dir = Path(cc["source_exams_dir"])
    textbook_path = Path(cc["source_textbook"])
    documents_dir = Path(cc["source_documents"])

    manifest: dict = {
        "course": course,
        "timestamp": datetime.now().isoformat(),
        "dry_run": dry_run,
        "results": {},
    }

    # 1. Slides
    if slides_dir.exists():
        pptx_files = list(slides_dir.glob("*.pptx"))
        print(f"[ingest] Found {len(pptx_files)} lecture slides")
        if not dry_run:
            written = ingest_lectures(slides_dir, raw_dir / "slides", verbose=verbose)
            manifest["results"]["slides"] = [str(p) for p in written]
        else:
            manifest["results"]["slides"] = [p.name for p in pptx_files]
    else:
        print(f"[ingest] Slides dir not found: {slides_dir}")

    # 2. Textbook: one PDF or a folder of PDFs
    textbook_pdfs = _find_pdfs(textbook_path)
    if textbook_pdfs:
        print(f"[ingest] Textbook: {len(textbook_pdfs)} PDF(s)")
        if not dry_run:
            written = ingest_pdfs(textbook_pdfs, raw_dir / "textbook", verbose=verbose)
            manifest["results"]["textbook"] = [str(p) for p in written]
        else:
            manifest["results"]["textbook"] = [p.name for p in textbook_pdfs]
    else:
        print(f"[ingest] Textbook not found: {textbook_path}")

    # 3. Exam PDFs
    exam_pdfs = _find_exam_pdfs(exams_dir)
    print(f"[ingest] Found {len(exam_pdfs)} exam PDFs")
    if exam_pdfs and not dry_run:
        written = ingest_pdfs(exam_pdfs, raw_dir / "exams", verbose=verbose)
        manifest["results"]["exams"] = [str(p) for p in written]
    else:
        manifest["results"]["exams"] = [p.name for p in exam_pdfs]

    # 4. Assignment documents
    assignment_docs = _find_assignment_docs(assignments_dir)
    print(f"[ingest] Found {len(assignment_docs)} assignment documents")
    if assignment_docs and not dry_run:
        written = ingest_documents(assignment_docs, raw_dir / "assignments", verbose=verbose)
        manifest["results"]["assignments"] = [str(p) for p in written]
    else:
        manifest["results"]["assignments"] = [p.name for p in assignment_docs]

    # 5. Documents: notes, handouts, syllabi (PDF, .docx, .txt, .md)
    doc_pdfs = _find_pdfs(documents_dir)
    doc_texts = _find_assignment_docs(documents_dir)
    print(f"[ingest] Found {len(doc_pdfs) + len(doc_texts)} documents")
    if (doc_pdfs or doc_texts) and not dry_run:
        written = ingest_pdfs(doc_pdfs, raw_dir / "documents", verbose=verbose)
        written += ingest_documents(doc_texts, raw_dir / "documents", verbose=verbose)
        manifest["results"]["documents"] = [str(p) for p in written]
    else:
        manifest["results"]["documents"] = [p.name for p in doc_pdfs + doc_texts]

    # Write manifest
    if not dry_run:
        manifest_path = raw_dir / "manifest.json"
        manifest_path.parent.mkdir(parents=True, exist_ok=True)
        manifest_path.write_text(json.dumps(manifest, indent=2))
        print(f"[ingest] Manifest written to {manifest_path}")

    return manifest
