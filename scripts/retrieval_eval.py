"""Retrieval regression eval: does find_topic still surface the right chunks?

Runs every question in evals/golden_retrieval.json (written for your own
courses: each item names a course id from courses.yaml, a question, and a
regex the right chunk matches) through the app's real find_topic (the exact prod retrieval path: same
collections, same embedder, same ranking) and checks that the expected
content appears in the top-N results. Expectations are regexes over
lecture_title/source/text, never chunk ids, so a re-index only fails the eval
when retrieval *quality* actually changed.

Usage:
    python scripts/retrieval_eval.py            # needs knowledge-base/chromadb
    python scripts/retrieval_eval.py --json     # machine-readable report

Exit code 0 when the pass rate meets the threshold in the golden file
(default 0.9), 1 otherwise, 2 when the chroma store is missing. Wired into
.github/workflows/retrieval-eval.yml (weekly + on demand); also runnable
locally wherever the seed is extracted. The required CI gate stays
network-free and does not run this.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
GOLDEN = ROOT / "evals" / "golden_retrieval.json"

# find_topic imports the app config, which requires these to exist; dummies
# are fine because retrieval never touches the Anthropic API.
import os  # noqa: E402

os.environ.setdefault("ANTHROPIC_API_KEY", "eval-dummy-key")
os.environ.setdefault("SECRET_KEY", "eval-dummy-secret")
os.environ.setdefault("ADMIN_TOKEN", "eval-dummy-admin-token")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    args = ap.parse_args()

    chroma = ROOT / "knowledge-base" / "chromadb" / "chroma.sqlite3"
    if not chroma.exists():
        print(f"chroma store missing at {chroma.parent}; extract the seed first", file=sys.stderr)
        return 2

    sys.path.insert(0, str(ROOT))
    from tutor.agent.tools import find_topic  # deferred: needs env + chroma

    spec = json.loads(GOLDEN.read_text())
    threshold = float(spec.get("pass_threshold", 0.9))
    rows = []
    for item in spec["questions"]:
        out = find_topic(item["q"], course=item["course"], top_k=5)
        top = out["results"][: int(item.get("within_top", 3))]
        rank = None
        for i, r in enumerate(top):
            blob = " ".join(str(r.get(f, "")) for f in ("lecture_title", "source", "text"))
            if re.search(item["regex"], blob):
                rank = i
                break
        rows.append({
            "id": item["id"],
            "q": item["q"],
            "passed": rank is not None,
            "rank": rank,
            "best": (top[0]["collection"], round(top[0]["score"], 3)) if top else None,
        })

    passed = sum(1 for r in rows if r["passed"])
    rate = passed / len(rows) if rows else 0.0

    if args.json:
        print(json.dumps({"passed": passed, "total": len(rows), "rate": rate,
                          "threshold": threshold, "rows": rows}, indent=1))
    else:
        for r in rows:
            mark = "ok " if r["passed"] else "FAIL"
            print(f"[{mark}] {r['id']} rank={r['rank']} {r['q']}")
        print(f"\n{passed}/{len(rows)} passed ({rate:.0%}); threshold {threshold:.0%}")

    return 0 if rate >= threshold else 1


if __name__ == "__main__":
    sys.exit(main())
