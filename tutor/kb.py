"""The knowledge base's ChromaDB collections, listed once (plan step 5.8,
finding E11).

The pipeline's indexer builds exactly these (``python -m pipeline index``),
and the tutor's find_topic searches all of them, in this order. The slide,
script, and Teach-me generators query subsets. A new collection is added
here, with a builder in pipeline/knowledge_base/indexer.py; a test holds the
two lists equal.

Each collection is built from one folder under ``knowledge-base/raw/`` of the
same name (transcripts also read ``knowledge-base/transcripts/<course>-<term>/``
folders). ADDING-KNOWLEDGE.md, step 3, says what belongs in each and the JSON
shape each expects.
"""

from __future__ import annotations

COLLECTIONS: tuple[str, ...] = (
    "slides",        # your lecture decks, one JSON per deck (pipeline ingest)
    "transcripts",   # lecture recordings, transcribed (pipeline transcribe)
    "textbook",      # the course text, chunked by page (pipeline ingest)
    "assignments",   # assignment specifications (pipeline ingest)
    "documents",     # anything else you wrote: notes, handouts, syllabi
    "code",          # example code (pipeline github, or your own JSON)
    "forum",         # class discussion posts (pipeline discord, or your own JSON)
)

# Collections whose text comes from students and other posters, not from the
# instructor's own material: the tutor is told such excerpts may be wrong.
COMMUNITY_COLLECTIONS: frozenset[str] = frozenset({"forum"})

# Collections whose rows are slides of a deck (they carry a slide number, and
# a hit can move the chat's slide panel).
SLIDE_COLLECTIONS: frozenset[str] = frozenset({"slides"})
