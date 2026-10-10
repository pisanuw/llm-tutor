"""Golden retrieval regression (capability item 8): your knowledge base
against your questions in evals/golden_retrieval.json.

Opt-in, because it measures YOUR content, not the code: it runs only with
RUN_RETRIEVAL_EVAL=1 and a built store at knowledge-base/chromadb
(python -m pipeline index --rebuild), e.g.

    RUN_RETRIEVAL_EVAL=1 pytest tests/test_retrieval_golden.py

The network-free suite never runs it; .github/workflows/retrieval-eval.yml
runs the same evaluator weekly against the deploy seed."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
CHROMA = ROOT / "knowledge-base" / "chromadb" / "chroma.sqlite3"


@pytest.mark.skipif(os.environ.get("RUN_RETRIEVAL_EVAL") != "1" or not CHROMA.exists(),
                    reason="opt-in: RUN_RETRIEVAL_EVAL=1 and a built knowledge base")
def test_golden_retrieval_meets_threshold():
    env = {k: v for k, v in os.environ.items() if k not in ("TUTOR_CONTENT_DIR", "TUTOR_CHROMA_DIR")}
    proc = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "retrieval_eval.py")],
        capture_output=True, text=True, timeout=600, env=env,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
