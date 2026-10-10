"""Ingest downloaded Canvas course files into the knowledge base.

Processes PDFs and PPTXs from source-materials/canvas/<offering>/ directories.
Run after canvas_reader.download_course_materials() has populated the directories.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from pipeline.ingest.pdf_reader import ingest_pdfs


def ingest_canvas_dir(
    canvas_dir: Path,
    raw_dir: Path,
    skip_existing: bool = True,
    verbose: bool = True,
) -> dict[str, int]:
    """Ingest all PDFs and PPTXs from one canvas offering directory: PDFs into
    the documents collection's folder, decks into the slides collection's."""
    pdf_out = raw_dir / "documents"
    slides_out = raw_dir / "slides"

    pdfs = [p for p in canvas_dir.iterdir() if p.suffix.lower() == ".pdf"]
    pptxs = [p for p in canvas_dir.iterdir() if p.suffix.lower() == ".pptx"]

    counts = {"pdfs": 0, "pptxs": 0}

    if pdfs:
        if verbose:
            print(f"  [canvas-ingest] {canvas_dir.name}: {len(pdfs)} PDFs")
        written = ingest_pdfs(pdfs, pdf_out, skip_existing=skip_existing, verbose=False)
        counts["pdfs"] = len(written)

    if pptxs:
        if verbose:
            print(f"  [canvas-ingest] {canvas_dir.name}: {len(pptxs)} PPTXs")
        for pptx in pptxs:
            out_path = slides_out / f"{canvas_dir.name}-{pptx.stem}.json"
            if skip_existing and out_path.exists():
                continue
            try:
                from pipeline.ingest.pptx_reader import extract_slide_content, save_lecture_json
                lecture = extract_slide_content(pptx)
                # Prefix stem to avoid name collisions across offerings
                lecture.filename = f"{canvas_dir.name}-{pptx.name}"
                save_lecture_json(lecture, slides_out)
                counts["pptxs"] += 1
            except Exception as e:
                if verbose:
                    print(f"    WARN: could not read {pptx.name}: {e}")

    return counts


def ingest_all_canvas(
    canvas_base: Path,
    raw_dir: Path,
    skip_existing: bool = True,
    verbose: bool = True,
) -> dict:
    if not canvas_base.exists():
        print(f"[canvas-ingest] Canvas directory not found: {canvas_base}")
        return {}

    offering_dirs = sorted(d for d in canvas_base.iterdir() if d.is_dir())
    if not offering_dirs:
        print("[canvas-ingest] No offering directories found.")
        return {}

    print(f"[canvas-ingest] Processing {len(offering_dirs)} course offering(s)...")
    totals: dict[str, int] = {"pdfs": 0, "pptxs": 0}
    results = {}

    for d in offering_dirs:
        counts = ingest_canvas_dir(d, raw_dir, skip_existing=skip_existing, verbose=verbose)
        results[d.name] = counts
        totals["pdfs"] += counts["pdfs"]
        totals["pptxs"] += counts["pptxs"]

    print(f"[canvas-ingest] Total: {totals['pdfs']} PDF files, {totals['pptxs']} PPTX files ingested")

    manifest_path = raw_dir / "canvas-manifest.json"
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(json.dumps({
        "timestamp": datetime.now().isoformat(),
        "results": results,
        "totals": totals,
    }, indent=2))

    return results
