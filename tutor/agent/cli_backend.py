"""CLI backend: uses the `claude` executable (subscription) instead of the API key.

Provides stream_cli(system_prompt, messages, course) -> AsyncGenerator[dict, None],
the streaming entry point run_turn uses on the USE_CLAUDE_CLI path.

Tool phase runs locally (no API call): find_topic is always executed and its
results are injected as context into the user message before handing off to
`claude -p`.  get_exercise and evaluate_code are skipped in CLI mode.
"""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
from typing import AsyncGenerator, Optional

from tutor.agent.tools import dispatch_tool
from tutor.courses import DEFAULT_COURSE

# The tutor's text comes only from the model: every Claude Code tool is denied,
# and the permission checks stay on (plan step 6.8, finding E14). The CLI used
# to run with them skipped, so a student's message could have steered it into
# reading or running anything on the host. The list matches the pipeline's
# (pipeline/generate/script_generator._NO_TOOLS; a test holds them equal).
NO_TOOLS = "Bash,Edit,Write,Read,Glob,Grep,WebFetch,WebSearch,Task,TodoWrite,NotebookEdit"


def _cli_env() -> dict:
    """The CLI's environment without ANTHROPIC_API_KEY: this path exists to use
    the subscription, and with the key present the CLI can bill it instead."""
    env = dict(os.environ)
    env.pop("ANTHROPIC_API_KEY", None)
    return env


# ---------------------------------------------------------------------------
# Local tool phase
# ---------------------------------------------------------------------------

def _run_tools_local(
    messages: list[dict],
    course: str,
) -> tuple[list[dict], Optional[dict]]:
    """Run find_topic directly and inject results into the last user message.

    Returns (updated_messages, slide_ref).
    """
    # Extract the last user text
    user_text = ""
    for m in reversed(messages):
        if m["role"] == "user":
            c = m["content"]
            if isinstance(c, str):
                user_text = c
            elif isinstance(c, list):
                for block in c:
                    if isinstance(block, dict) and block.get("type") == "text":
                        user_text = block.get("text", "")
            break

    if not user_text:
        return messages, None

    try:
        result = dispatch_tool("find_topic", {"query": user_text, "course": course, "top_k": 3})
    except Exception:
        return messages, None

    slide_ref = result.get("slide_ref") if isinstance(result, dict) else None

    # Format retrieved context as plain text
    context_lines = ["[Retrieved course material]"]
    if isinstance(result, dict):
        for hit in result.get("results", []):
            src = hit.get("source", "")
            text = hit.get("text", "")
            if text:
                context_lines.append(f"Source: {src}\n{text[:600]}")
    context_block = "\n\n".join(context_lines)

    # Append context to the last user message
    updated = list(messages)
    for i in range(len(updated) - 1, -1, -1):
        if updated[i]["role"] == "user":
            original = updated[i]["content"]
            if isinstance(original, str):
                updated[i] = {**updated[i], "content": f"{original}\n\n{context_block}"}
            break

    return updated, slide_ref


# ---------------------------------------------------------------------------
# Build a plain-text conversation string for claude -p
# ---------------------------------------------------------------------------

def _build_input(messages: list[dict]) -> str:
    """Flatten messages into a plain-text string for claude -p.

    Includes up to the last 10 turns so context fits in a CLI arg.
    """
    tail = messages[-20:]  # 10 turns = 20 messages (user + assistant)
    parts = []
    for m in tail:
        role = "User" if m["role"] == "user" else "Assistant"
        c = m["content"]
        if isinstance(c, str):
            text = c
        elif isinstance(c, list):
            texts = [b.get("text", "") for b in c if isinstance(b, dict) and b.get("type") == "text"]
            text = " ".join(texts)
        else:
            continue
        parts.append(f"{role}: {text}")
    return "\n\n".join(parts)


# ---------------------------------------------------------------------------
# Async streaming
# ---------------------------------------------------------------------------

async def stream_cli(
    system_prompt: str,
    messages: list[dict],
    course: str = DEFAULT_COURSE,
) -> AsyncGenerator[dict, None]:
    """Async token stream via `claude -p --output-format stream-json`."""
    loop = asyncio.get_running_loop()
    messages, slide_ref = await loop.run_in_executor(
        None, _run_tools_local, messages, course
    )

    if slide_ref and slide_ref.get("slide_url"):
        yield {
            "type": "slide",
            "url": slide_ref["slide_url"],
            "title": slide_ref.get("lecture_title", ""),
            "num": slide_ref.get("slide_number", ""),
        }

    input_text = _build_input(messages)

    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".md", delete=False, encoding="utf-8"
    ) as f:
        f.write(system_prompt)
        prompt_file = f.name

    proc = None
    try:
        proc = await asyncio.create_subprocess_exec(
            "claude", "-p", input_text,
            "--system-prompt-file", prompt_file,
            "--output-format", "stream-json",
            "--verbose",
            "--include-partial-messages",
            "--no-session-persistence",
            "--tools", "",            # every built-in tool off, including any the CLI adds later
            "--disallowed-tools", NO_TOOLS,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            env=_cli_env(),
        )

        full_text: list[str] = []

        async for raw in proc.stdout:
            line = raw.decode(errors="replace").strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue

            etype = event.get("type")

            if etype == "stream_event":
                inner = event.get("event", {})
                if (inner.get("type") == "content_block_delta"
                        and inner.get("delta", {}).get("type") == "text_delta"):
                    chunk = inner["delta"]["text"]
                    full_text.append(chunk)
                    yield {"type": "token", "text": chunk}

            elif etype == "result":
                if event.get("is_error"):
                    yield {"type": "token", "text": f"\n[Error: {event.get('result', 'unknown')}]"}
                break

        await proc.wait()
        yield {"type": "done", "full_text": "".join(full_text)}

    finally:
        # If the consumer stopped iterating early (client disconnect ->
        # GeneratorExit) or anything raised mid-stream, the proc.wait() above
        # was never reached; kill and reap the claude -p child so it is not
        # left orphaned, then drop the temp prompt file.
        if proc is not None and proc.returncode is None:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
            try:
                await proc.wait()
            except Exception:
                pass
        try:
            os.unlink(prompt_file)
        except OSError:
            pass
