"""Shared pipeline utilities."""


def text_from(message) -> str:
    """Return the concatenated text of every text block in a Messages response.

    Tolerates an empty content list or a leading non-text block, returning ""
    only when no text is present at all — the same guard as tutor/agent/llm_response.py.
    """
    content = getattr(message, "content", None) or []
    return "".join(
        getattr(block, "text", "")
        for block in content
        if isinstance(getattr(block, "text", None), str)
    )
