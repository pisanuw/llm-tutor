"""Anthropic client factory for the supervisor tier (plan step 1.3).

Two kinds of callers talk to the model. The actor (``turn.py``) streams long
answers and keeps its own client and timeout. Everything else is a short,
bounded call: the intent classifier, the judge, the strategist, the session
summarizer, the exam grader, intent labeller, and clarifier, the code rubric
evaluator, photo transcription, misconception mining, and the admin health
ping. Each of those used to build ``anthropic.Anthropic(api_key=...)`` with
the SDK defaults, a 600-second read timeout and two retries, so a single hung
call could hold a student's turn for half an hour (the "thinking forever"
hang). They all build their client here instead.

The read timeout bounds a whole non-streaming call, because the API sends the
body only when generation ends. 30 seconds covers a Haiku reply of a few
hundred tokens with a wide margin; a caller that asks for more output passes
a longer ``timeout``.
"""

from __future__ import annotations

from typing import Optional, Union

import anthropic
import httpx

SUPERVISOR_TIMEOUT = httpx.Timeout(30.0, connect=5.0)
SUPERVISOR_MAX_RETRIES = 1


def supervisor_client(
    *,
    api_key: Optional[str] = None,
    timeout: Union[httpx.Timeout, float, None] = None,
) -> anthropic.Anthropic:
    """A synchronous client with a bounded timeout and one retry.

    ``api_key`` defaults to the configured key (callers that fall back when
    it is missing check it first). ``timeout`` overrides the read timeout
    (seconds) or the whole ``httpx.Timeout``; the connect timeout stays 5 s.
    """
    if api_key is None:
        from tutor.config import get_config

        api_key = get_config().get("anthropic_api_key", "")
    if timeout is None:
        bound = SUPERVISOR_TIMEOUT
    elif isinstance(timeout, httpx.Timeout):
        bound = timeout
    else:
        bound = httpx.Timeout(float(timeout), connect=5.0)
    return anthropic.Anthropic(
        api_key=api_key,
        timeout=bound,
        max_retries=SUPERVISOR_MAX_RETRIES,
    )
