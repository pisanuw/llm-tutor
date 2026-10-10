"""The local no-key path (USE_CLAUDE_CLI): the tutor through the ``claude``
CLI on the owner's subscription, for local work only.

No tool phase and no supervision: tutor.config refuses to start it with a
DATABASE_URL, so it can never serve real students. Plan step 5.1 kept it
(decision 11's deviation: the owner prefers the CLI to a billed key).
"""

from __future__ import annotations

from typing import AsyncGenerator, Optional

from sqlalchemy.orm import Session as DBSession

from tutor.agent import envelope as envelope_mod
from tutor.agent import prompt_builder
from tutor.agent import state as state_mod
from tutor.agent.turn_stages.dbstep import db_step
from tutor.agent.turn_stages.request import TurnRequest
from tutor.db.models import Message


async def run(
    db: DBSession, req: TurnRequest, per_request_ctx: str, messages: list[dict],
) -> AsyncGenerator[dict, None]:
    """Stream the CLI's reply; its "done" goes out only after the reply is
    committed. The envelope is assembled from the request lecture and
    appended to the string prompt."""
    from tutor.agent import cli_backend   # the CLI path only

    bundle = await db_step(db, envelope_mod.assemble_envelope, req.sid, req.course, db,
                           lecture_num=req.lecture_num)
    env_block = prompt_builder.render_envelope(bundle.envelope, bundle.goal_titles, bundle.calibration_note)
    system_prompt = prompt_builder.build_system_prompt() + per_request_ctx + "\n\n" + env_block
    full_parts: list[str] = []
    cli_done: Optional[dict] = None
    async for event in cli_backend.stream_cli(system_prompt, messages, req.course):
        if event["type"] == "done":
            cli_done = event   # held until the reply is committed below
            continue
        if event["type"] == "token":
            full_parts.append(event["text"])
        yield event
    full_text = "".join(full_parts)

    def _save_cli_reply():
        db.add(Message(
            student_id=req.sid,
            role="assistant",
            content=full_text,
            lecture_num=req.lecture_num,
            slide_num=req.slide_num,
            course=req.course,
        ))
        if req.lecture_num:
            state_mod.record_topic_access(db, req.sid, req.lecture_num, req.slide_num, req.user_message)

    await db_step(db, _save_cli_reply)
    if cli_done is not None:
        yield cli_done
