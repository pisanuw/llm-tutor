"""find_topic embeds the query once, not once per collection
(latency review, B0).

A collection opened without an embedder rebuilds one from its stored config
on every ``query_texts`` call, a fresh ONNX session each time, so a search of
every collection loaded the model once per collection: the 15 to 25 s of a slow
production turn. These tests hold the search to one embedding and pin that
every collection is queried with it.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from tutor import kb
from tutor.agent import tools


def _fake_collection(_name):
    coll = MagicMock()
    coll.count.return_value = 3
    coll.query.return_value = {"documents": [["chunk"]], "metadatas": [[{}]], "distances": [[0.2]]}
    return coll


def test_the_query_is_embedded_once_and_passed_to_every_collection():
    colls = {name: _fake_collection(name) for name in kb.COLLECTIONS}
    with patch.object(tools, "_collection", side_effect=colls.__getitem__), \
         patch.object(tools, "_embed_query", return_value=[[0.1, 0.2]]) as embed:
        hits, failed = tools._search("what is a light-year?", 2)
    embed.assert_called_once_with("what is a light-year?")
    assert failed == 0 and len(hits) == len(kb.COLLECTIONS)
    for coll in colls.values():
        kwargs = coll.query.call_args.kwargs
        assert kwargs["query_embeddings"] == [[0.1, 0.2]]
        assert "query_texts" not in kwargs
        assert kwargs["n_results"] == 2
        coll.count.assert_called_once()


def test_empty_collections_cost_no_embedding():
    empty = MagicMock()
    empty.count.return_value = 0
    with patch.object(tools, "_collection", return_value=empty), \
         patch.object(tools, "_embed_query") as embed:
        hits, failed = tools._search("anything", 5)
    assert hits == [] and failed == 0
    embed.assert_not_called()


def test_the_embedder_is_built_once_per_process():
    tools._embedder.cache_clear()
    try:
        with patch("chromadb.utils.embedding_functions.DefaultEmbeddingFunction") as ef:
            ef.return_value = MagicMock(return_value=[[0.0]])
            tools._embed_query("a")
            tools._embed_query("b")
        ef.assert_called_once()
    finally:
        tools._embedder.cache_clear()


def test_warm_up_runs_one_search_and_never_raises(tutor_log):
    with patch.object(tools, "_search", return_value=([], 0)) as search:
        tools.warm_up()
    search.assert_called_once()
    with patch.object(tools, "_search", side_effect=RuntimeError("disk")):
        tools.warm_up()                              # logged, not raised
    assert "warm-up failed" in tutor_log.text
    with patch.object(tools, "_search", side_effect=tools.KnowledgeBaseMissing("none")):
        tools.warm_up()
