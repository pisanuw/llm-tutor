"""Boot-time checks on seeded data (plan step 2.7, finding E5).

entrypoint.sh runs these before it trusts a seed:

    python -m tutor.seed_check chroma DIR   # a readable Chroma store
    python -m tutor.seed_check json FILE    # a file that parses as JSON
    python -m tutor.seed_check slides DIR   # every lecture of every course has a deck

Exit status 0 means usable, 1 means not. The Chroma check opens the store,
needs documents in at least one of the knowledge base's collections
(tutor/kb.py; a course need not have slides), and runs one nearest-neighbour
query per non-empty collection with one of its own stored vectors. A query
reads the vector index files that a truncated download leaves short, which a
row count alone does not.

Kept free of tutor.config, so it runs before the app's settings are loaded.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from tutor.courses import COURSE_LIST, COURSES
from tutor.kb import COLLECTIONS

def check_chroma(path) -> tuple[bool, str]:
    """(usable, detail) for the Chroma store at ``path``. Never raises."""
    if not (Path(path) / "chroma.sqlite3").is_file():
        return False, "no chroma.sqlite3"
    try:
        import chromadb

        client = chromadb.PersistentClient(path=str(path))
        names, documents = [], 0
        for collection in client.list_collections():
            names.append(collection.name)
            count = collection.count()
            if count:
                stored = collection.get(limit=1, include=["embeddings"])["embeddings"]
                collection.query(query_embeddings=[list(stored[0])], n_results=1)
                if collection.name in COLLECTIONS:
                    documents += count
        if not documents:
            return False, "no documents in any knowledge-base collection"
        return True, f"{len(names)} collections, {documents} documents"
    except Exception as exc:  # noqa: BLE001 - any failure means "not usable"
        return False, f"{type(exc).__name__}: {str(exc)[:200]}"


def check_json(path) -> tuple[bool, str]:
    """(usable, detail) for the JSON file at ``path``. Never raises."""
    try:
        with open(path, encoding="utf-8") as fh:
            json.load(fh)
        return True, "valid JSON"
    except Exception as exc:  # noqa: BLE001
        return False, f"{type(exc).__name__}: {str(exc)[:200]}"


# Every course in courses.yaml has ``lectures`` decks (pipeline/lectures.py
# renders output/<course>/lecture-NN/index.html for each). tutor.courses
# reads only courses.yaml, so the check runs before the app's settings.
SLIDES_COURSES = tuple(COURSES)
SLIDES_PER_COURSE = {c.id: c.lectures for c in COURSE_LIST}


def check_slides(path) -> tuple[bool, str]:
    """(usable, detail) for a slide seed: every lecture of every course has a
    non-empty ``index.html`` (F8: a partial tree used to boot clean and 404
    for a quarter). Never raises."""
    try:
        missing = []
        for course in SLIDES_COURSES:
            for n in range(1, SLIDES_PER_COURSE[course] + 1):
                deck = Path(path) / course / f"lecture-{n:02d}" / "index.html"
                if not deck.is_file() or deck.stat().st_size == 0:
                    missing.append(f"{course}/lecture-{n:02d}")
        if missing:
            shown = ", ".join(missing[:6]) + (" ..." if len(missing) > 6 else "")
            return False, f"{len(missing)} deck(s) missing: {shown}"
        return True, f"{sum(SLIDES_PER_COURSE.values())} decks"
    except Exception as exc:  # noqa: BLE001
        return False, f"{type(exc).__name__}: {str(exc)[:200]}"


_CHECKS = {"chroma": check_chroma, "json": check_json, "slides": check_slides}


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 2 or args[0] not in _CHECKS:
        print("usage: python -m tutor.seed_check chroma DIR | json FILE | slides DIR", file=sys.stderr)
        return 2
    ok, detail = _CHECKS[args[0]](args[1])
    print(f"[seed_check] {args[0]} {args[1]}: {'ok' if ok else 'FAILED'} ({detail})")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
