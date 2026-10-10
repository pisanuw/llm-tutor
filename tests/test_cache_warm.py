"""The actor's prompt cache is kept warm (latency plan, B1).

The persona prefix (~44K tokens) is cached for an hour instead of five
minutes, and opening the chat page writes it with a ``max_tokens=0`` call,
throttled to one per 50 minutes and skipped while an actor call has touched
the cache more recently. Each warm-up is a ``cache_warm`` ledger row, priced
at the 1-hour write rate.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from tutor.agent import llm_response, turn as agent, usage as usage_mod
from tutor.agent.turn_stages import actor_client, cache_warm
from tutor.db.models import Student, TurnEvent
from tests.helpers import make_session


@pytest.fixture(autouse=True)
def _fresh(monkeypatch):
    cache_warm._reset_for_tests()
    from tutor.config import get_config

    monkeypatch.setitem(get_config(), "cache_prewarm", True)
    yield
    cache_warm._reset_for_tests()


@pytest.fixture
def db():
    s = make_session()
    s.add(Student(id=1, email="stu@example.edu", name="Stu"))
    s.commit()
    yield s
    s.close()


def _usage(write=44303, write_1h=44303):
    return SimpleNamespace(input_tokens=5, output_tokens=0, cache_read_input_tokens=0,
                           cache_creation_input_tokens=write,
                           cache_creation=SimpleNamespace(ephemeral_1h_input_tokens=write_1h,
                                                          ephemeral_5m_input_tokens=0))


def test_the_persona_prefix_is_cached_for_an_hour():
    with patch("tutor.agent.prompt_builder.build_system_prompt", return_value="PERSONA"):
        blocks = actor_client.system_blocks("per request")
    assert blocks[0]["cache_control"] == {"type": "ephemeral", "ttl": "1h"}
    assert "cache_control" not in blocks[1]


def test_prewarm_writes_the_same_prefix_as_a_first_round_and_generates_nothing():
    fake = MagicMock()
    fake.messages.create.return_value = SimpleNamespace(usage=_usage(), model="claude-sonnet-4-6",
                                                        content=[])
    with patch.object(actor_client, "client", return_value=fake), \
         patch("tutor.agent.prompt_builder.build_system_prompt", return_value="PERSONA"):
        actor_client.prewarm()
        first_round_system = actor_client.system_blocks()
    kw = fake.messages.create.call_args.kwargs
    assert kw["max_tokens"] == 0
    assert kw["system"] == first_round_system
    assert kw["tools"] == actor_client.TOOL_DEFINITIONS
    assert kw["tool_choice"] == {"type": "auto"}
    assert kw["model"] == actor_client.actor_model()


def test_a_warm_up_is_ledgered_at_the_one_hour_write_price(db):
    served = actor_client.Served(_usage(), "claude-sonnet-4-6")
    with patch.object(actor_client, "prewarm", return_value=served) as prewarm:
        assert cache_warm.warm(db, 1) is True
    prewarm.assert_called_once()
    ev = db.query(TurnEvent).filter_by(kind="cache_warm").one()
    assert ev.student_id == 1 and ev.cache_write_tokens == 44303
    # 44,303 tokens at 2 x $3 per million, plus 5 input tokens.
    assert ev.cost_usd == pytest.approx(44303 * 6e-6 + 5 * 3e-6)


def test_warm_ups_are_throttled_and_skipped_while_the_cache_is_alive(db):
    served = actor_client.Served(_usage(), None)
    with patch.object(actor_client, "prewarm", return_value=served) as prewarm:
        assert cache_warm.warm(db, 1) is True
        assert cache_warm.warm(db, 1) is False          # a second page load
    assert prewarm.call_count == 1

    cache_warm._reset_for_tests()
    actor_client.note_cache_touch()                     # a real turn just ran
    with patch.object(actor_client, "prewarm") as prewarm:
        assert cache_warm.warm(db, 1) is False
    prewarm.assert_not_called()


def test_off_in_cli_mode_and_when_switched_off(db, monkeypatch):
    from tutor.config import get_config

    with patch.object(actor_client, "prewarm") as prewarm:
        monkeypatch.setitem(get_config(), "cache_prewarm", False)
        assert cache_warm.warm(db, 1) is False
        monkeypatch.setitem(get_config(), "cache_prewarm", True)
        monkeypatch.setitem(get_config(), "use_claude_cli", True)
        assert cache_warm.warm(db, 1) is False
    prewarm.assert_not_called()


def test_a_failed_warm_up_is_logged_and_never_raises(db, caplog):
    with patch.object(actor_client, "prewarm", side_effect=RuntimeError("overloaded")):
        assert cache_warm.warm(db, 1) is False
    assert db.query(TurnEvent).count() == 0


def test_opening_the_chat_schedules_a_warm_up():
    from tests.browser_support import app_client, student_db

    s = student_db()
    with patch.object(agent, "warm_prompt_cache") as warm, app_client(s) as client:
        assert client.get("/chat?course=AST101").status_code == 200
    warm.assert_called_once_with(1)
    s.close()


def test_one_hour_writes_are_priced_at_twice_input():
    counts = llm_response.usage_counts(_usage(write=1000, write_1h=1000))
    assert counts["cache_write_1h_tokens"] == 1000
    fields = usage_mod.cost_fields("claude-sonnet-4-6", counts)
    assert fields["cost_usd"] == pytest.approx(1000 * 6e-6 + 5 * 3e-6)
    # A 5-minute write keeps the 1.25x price.
    five = usage_mod.cost_fields("claude-sonnet-4-6",
                                 llm_response.usage_counts(_usage(write=1000, write_1h=0)))
    assert five["cost_usd"] == pytest.approx(1000 * 3.75e-6 + 5 * 3e-6)
