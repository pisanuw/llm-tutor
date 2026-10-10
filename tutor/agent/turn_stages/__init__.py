"""The stages of one supervised turn, in the order ``run_turn`` calls them.

``tutor.agent.turn.run_turn`` is the only orchestrator. Nothing else imports
these modules (tests/test_single_actor.py enforces it), so no endpoint can
call a stage directly and skip the supervision (plan step 5.2, finding A15).

    request.py         the turn's request, as the stages read and rewrite it
    dbstep.py          each database step is its own transaction, off the loop
    errors.py          what the student and the history see when a turn fails
    exam_intercept.py  a pending mastery exam: grade, clarify, or defer
    context.py         the student block, the history, the rolling summary
    cli_turn.py        the local no-key path through the ``claude`` CLI
    preflight.py       classify, conduct, envelope, prerequisite gate, strategist
    actor_client.py    the only place the actor model is called, and its cost
    actor_loop.py      one streaming call with the bounded native tool loop
    supervision.py     detector always, judge on risky turns, one revise
    persist.py         the provisional event, the reply, learning state, telemetry

Each stage reaches its collaborators through their modules at call time
(``state_mod.record_topic_access``, never a name bound at import), so a test
that patches ``tutor.agent.<module>.<name>`` reaches every stage.
"""
