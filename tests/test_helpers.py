"""The shared test machinery itself (plan step 6.2a, finding F17).

Each helper here exists because its absence broke the suite in some order,
so each gets a test that fails in every order instead.
"""
from __future__ import annotations

import threading
import types
from unittest.mock import patch

import pytest
from sqlalchemy.orm import sessionmaker

from tests import conftest
from tests.helpers import applied, make_session
from tutor.db.models import Student


def test_applied_restores_the_original_when_two_patches_share_a_target():
    ns = types.SimpleNamespace(f="original")
    with applied([patch.object(ns, "f", "first"), patch.object(ns, "f", "second")]):
        assert ns.f == "second"
    assert ns.f == "original"      # stopped in start order, it would be "first"


def test_applied_stops_every_patch_when_the_body_raises():
    ns = types.SimpleNamespace(f="original", g="original")
    with pytest.raises(RuntimeError):
        with applied([patch.object(ns, "f", "x"), patch.object(ns, "g", "y")]):
            raise RuntimeError("boom")
    assert (ns.f, ns.g) == ("original", "original")


def test_a_second_thread_sees_the_same_database():
    """The request thread of a TestClient uses the session a fixture made.
    The per-thread in-memory recipe gave that thread an empty database."""
    s = make_session(Student(id=7, email="t@example.edu", name="T"))
    seen: list = []

    def other_thread():
        seen.append(s.query(Student).filter_by(id=7).count())
        seen.append(sessionmaker(bind=s.get_bind())().query(Student).count())

    t = threading.Thread(target=other_thread)
    t.start()
    t.join()
    assert seen == [1, 1]


class _Item:
    def __init__(self, nodeid: str, browser: bool = False):
        self.nodeid = nodeid
        self.fixturenames = ["db", "browser"] if browser else ["db"]


def test_each_module_runs_its_browser_tests_last():
    items = [_Item("tests/test_b.py::test_2", browser=True), _Item("tests/test_b.py::test_1"),
             _Item("tests/test_a.py::test_3"), _Item("tests/test_a.py::test_1", browser=True),
             _Item("tests/test_a.py::test_2")]
    conftest.pytest_collection_modifyitems(None, items)
    assert [i.nodeid for i in items] == [
        "tests/test_b.py::test_1", "tests/test_b.py::test_2",
        "tests/test_a.py::test_3", "tests/test_a.py::test_2", "tests/test_a.py::test_1",
    ]
