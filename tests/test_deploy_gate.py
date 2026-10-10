"""Plan step 0.5 (E2, E18): nothing ships because a commit was pushed, and
nothing ships that the `tests` workflow has not passed on.

Render's own auto-deploy shipped every push to main before CI finished, and
deploy.yml's workflow_run filter matched on the branch name alone, so a fork
pull request whose branch was called main could deploy. Since d9e6965b the
only trigger is `workflow_dispatch`: the operator chooses *when*, and the job
itself asks GitHub for a green `tests` run on the commit it is about to ship,
so "manual" never means "untested". These checks read the workflow and
Blueprint files; they cannot see the Render dashboard setting, which DEPLOY.md
(Step 3) says to turn off by hand.
"""
from __future__ import annotations

from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent


def _deploy_workflow() -> dict:
    return yaml.safe_load((ROOT / ".github" / "workflows" / "deploy.yml").read_text())


def test_deploy_is_triggered_by_hand_and_nothing_else():
    """A push, a schedule or a workflow_run would put production back on a
    timer that nobody asked for: a docs-only commit used to restart it."""
    wf = _deploy_workflow()
    on = wf.get("on", wf.get(True))  # PyYAML reads the bare key `on` as True
    assert list(on) == ["workflow_dispatch"], f"deploy.yml also triggers on {sorted(on)}"
    inputs = on["workflow_dispatch"]["inputs"]
    assert inputs["ref"]["default"] == "", "blank must mean the current tip of main"
    assert inputs["allow_untested"]["default"] is False, "shipping untested code must be opt-in"
    assert wf["concurrency"]["group"] == "render-deploy", "two deploys must not overlap"


def test_deploy_refuses_a_commit_with_no_green_tests_run():
    """What replaced the workflow_run gate: the first step asks GitHub for a
    successful run of the canonical `tests` workflow on that exact commit and
    fails the job when there is none, so choosing *when* to deploy is not
    choosing *whether* it was tested."""
    check = _deploy_workflow()["jobs"]["deploy"]["steps"][0]["run"]
    assert "actions/workflows/tests.yml/runs?head_sha=$sha&status=success" in check
    assert "::error::" in check and "exit 1" in check, "no green run must fail the job"
    # The override is the only way past the check, and the outputs that let the
    # later steps act are written after it, never before.
    assert check.index("ALLOW_UNTESTED") < check.index("exit 1") < check.index("deploy=true")


def test_deploy_pins_the_tested_commit_and_skips_a_moved_tip():
    job = _deploy_workflow()["jobs"]["deploy"]
    # actions: read is what the green-run query needs; nothing here may write.
    assert job["permissions"] == {"contents": "read", "actions": "read"}
    steps = job["steps"]
    assert steps[0].get("id") == "tip", "the tip check must run before anything else"
    assert "::warning::$sha is not the current tip of main." in steps[0]["run"]
    assert len(steps) > 1, "a workflow that only checks deploys nothing"
    for step in steps[1:]:
        assert step["if"] == "steps.tip.outputs.deploy == 'true'", f"ungated step: {step.get('name')}"
    trigger = next(s for s in steps if s.get("name") == "Trigger Render deploy")
    assert trigger["env"]["TESTED_SHA"] == "${{ steps.tip.outputs.sha }}", "the deploy must pin the tested commit"
    assert "ref=${TESTED_SHA}" in trigger["run"]
    # Every secret reaches the shell through env, never inline in a script.
    for step in steps:
        assert "secrets." not in step.get("run", ""), f"inline secret in: {step.get('name')}"


def test_render_blueprint_turns_auto_deploy_off():
    blueprint = yaml.safe_load((ROOT / "render.yaml").read_text())
    web = next(s for s in blueprint["services"] if s.get("type") == "web")
    assert web.get("autoDeployTrigger") == "off"
    assert "autoDeploy" not in web, "deprecated autoDeploy would be ignored or conflict"


def test_render_blueprint_trusts_the_proxy_for_the_client_address():
    """Every per-address limit (tutor/routers/auth.py, tutor/routers/status.py)
    keys on request.client.host; uvicorn honours X-Forwarded-For only from
    FORWARDED_ALLOW_IPS (default 127.0.0.1), and Render's proxy is a private
    peer, so without this the limits are per class (student-facing review,
    finding F32)."""
    blueprint = yaml.safe_load((ROOT / "render.yaml").read_text())
    web = next(s for s in blueprint["services"] if s.get("type") == "web")
    env = {e["key"]: e for e in web["envVars"]}
    value = env["FORWARDED_ALLOW_IPS"].get("value")
    # Never "*": uvicorn would then take the leftmost forwarded entry, which a
    # caller writes themselves, and every limit would key on their choice.
    assert value != "*" and "10.0.0.0/8" in value and "172.16.0.0/12" in value and "192.168.0.0/16" in value
    entrypoint = (ROOT / "entrypoint.sh").read_text()
    assert "forwarded-allow-ips" not in entrypoint, "the flag would override the environment"
