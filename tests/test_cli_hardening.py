"""Plan step 6.8 (finding E14): every launch of the ``claude`` CLI keeps the
CLI's permission checks, denies its tools, and leaves the API key out.

Three pipeline tools (audit_goals, anchor_goals, extend_plan) ran the CLI with
its permission checks skipped and no tool denied, on prompts built from
LLM-written JSON; the app's USE_CLAUDE_CLI backend did the same with a
student's messages. The three now call script_generator._generate_via_cli, and
the backend denies every tool. The code is read for every launch, so a new one
has to be added here on purpose.
"""
from __future__ import annotations

import ast
import asyncio
import importlib
import json
import re
import subprocess
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
# Spelled in two parts so this file does not name it.
SKIP_FLAG = "--dangerously-" + "skip-permissions"
# The test that asserts its absence.
MAY_NAME_IT = {"tests/test_content_pipeline.py"}
LAUNCHES = {
    ("pipeline/generate/script_generator.py", "_generate_via_cli"),
    ("pipeline/extract_goals.py", "call_claude_goals"),
    ("pipeline/generate_mastery_exams.py", "_generate_one"),
    ("tutor/agent/cli_backend.py", "stream_cli"),
}
# A string that runs the CLI through a shell: "claude -p ...", "cd x && claude --model ...".
_COMMAND_LINE = re.compile(r"(?:^|[\s;&|`$(])claude\s+(?:-p\b|--[a-z])")
_ANSI = re.compile(r"\x1b\[[0-9;]*m")
_SPAWNS = {"Popen", "run", "call", "check_call", "check_output", "create_subprocess_exec",
           "create_subprocess_shell", "system", "popen", "spawnlp", "execvp"}


def _no_tools() -> str:
    from pipeline.generate.script_generator import _NO_TOOLS

    return _NO_TOOLS


# ---------------------------------------------------------------------------
# The whole tree
# ---------------------------------------------------------------------------

def _tracked() -> list[str]:
    try:
        out = subprocess.run(["git", "ls-files", "-z"], cwd=ROOT, capture_output=True, check=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        pytest.skip("not a git checkout")
    return [p for p in out.stdout.decode().split("\0") if p]


def test_nothing_in_the_tree_skips_the_permission_checks():
    """The done-when of step 6.8. knowledge-base/ is course data, never run."""
    named = [rel for rel in _tracked()
             if rel not in MAY_NAME_IT and not rel.startswith("knowledge-base/")
             and (ROOT / rel).is_file() and SKIP_FLAG.encode() in (ROOT / rel).read_bytes()]
    assert named == []


def _docstrings(tree: ast.AST) -> set[int]:
    return {id(node.body[0].value) for node in ast.walk(tree)
            if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
            and node.body and isinstance(node.body[0], ast.Expr)
            and isinstance(node.body[0].value, ast.Constant) and isinstance(node.body[0].value.value, str)}


class _Launches(ast.NodeVisitor):
    """Each ``"claude", "-p", ...`` argument list, with its innermost function;
    and each other string that spells a ``claude`` command line (a shell launch)."""

    def __init__(self, docstrings: set[int]):
        self.docstrings = docstrings
        self.stack: list[ast.AST] = []
        self.found: list[tuple[ast.AST, list[ast.expr]]] = []
        self.shell: list[str] = []

    def visit_FunctionDef(self, node):
        self.stack.append(node)
        self.generic_visit(node)
        self.stack.pop()

    visit_AsyncFunctionDef = visit_FunctionDef

    def _check(self, parts: list[ast.expr]) -> None:
        head = [p.value if isinstance(p, ast.Constant) else None for p in parts[:2]]
        if head == ["claude", "-p"]:
            self.found.append((self.stack[-1] if self.stack else None, parts))

    def visit_List(self, node):
        self._check(node.elts)
        self.generic_visit(node)

    visit_Tuple = visit_List

    def visit_Call(self, node):
        self._check(node.args)
        self.generic_visit(node)

    def visit_Constant(self, node):
        if isinstance(node.value, str) and id(node) not in self.docstrings and _COMMAND_LINE.search(node.value):
            self.shell.append(node.value)


def _launches() -> tuple[list[tuple[str, ast.AST, list[ast.expr]]], list[str]]:
    found, shell = [], []
    for folder in ("pipeline", "tutor", "scripts"):
        for path in sorted((ROOT / folder).rglob("*.py")):
            tree = ast.parse(path.read_text())
            visitor = _Launches(_docstrings(tree))
            visitor.visit(tree)
            rel = path.relative_to(ROOT).as_posix()
            found += [(rel, fn, parts) for fn, parts in visitor.found]
            shell += [f"{rel}: {text!r}" for text in visitor.shell]
    return found, shell


def test_the_cli_is_launched_in_four_places_and_never_through_a_shell():
    found, shell = _launches()
    assert {(rel, getattr(fn, "name", None)) for rel, fn, _parts in found} == LAUNCHES
    assert shell == []


@pytest.mark.parametrize("rel, name", sorted(LAUNCHES))
def test_each_launch_denies_the_tools_and_passes_its_own_environment(rel, name):
    (fn, parts), = [(fn, parts) for r, fn, parts in _launches()[0] if (r, getattr(fn, "name", None)) == (rel, name)]
    words = [p.value if isinstance(p, ast.Constant) else getattr(p, "id", None) for p in parts]
    assert SKIP_FLAG not in words
    assert "--disallowed-tools" in words
    deny = words[words.index("--disallowed-tools") + 1]
    assert deny is not None and deny.endswith("NO_TOOLS"), deny
    # `--tools ""` empties the built-in set, so a tool a later CLI adds is off
    # without anyone naming it; the deny list stays as the second layer.
    assert "--tools" in words, f"{rel}:{name} does not pass --tools"
    assert words[words.index("--tools") + 1] == "", words[words.index("--tools") + 1]
    spawns = [node for node in ast.walk(fn) if isinstance(node, ast.Call)
              and getattr(node.func, "attr", getattr(node.func, "id", None)) in _SPAWNS]
    assert spawns and all(any(k.arg == "env" for k in call.keywords) for call in spawns)


def test_every_launch_denies_the_same_tools():
    from pipeline import extract_goals, generate_mastery_exams
    from tutor.agent import cli_backend

    lists = {_no_tools(), cli_backend.NO_TOOLS, extract_goals.NO_TOOLS, generate_mastery_exams.NO_TOOLS}
    assert len(lists) == 1
    assert {"Bash", "Read", "Write", "Edit", "Glob", "Grep", "WebFetch", "WebSearch"} <= set(_no_tools().split(","))


# ---------------------------------------------------------------------------
# The three pipeline tools, through the helper
# ---------------------------------------------------------------------------

class FakeCLI:
    """subprocess.Popen in script_generator: records each launch and answers."""

    def __init__(self, reply: str = "", returncode: int = 0, stderr: str = "", hang: bool = False):
        self.reply, self.returncode, self.stderr, self.hang = reply, returncode, stderr, hang
        self.launches: list[types.SimpleNamespace] = []

    def __call__(self, cmd, **kwargs):
        launch = types.SimpleNamespace(cmd=cmd, kwargs=kwargs, input=None, timeout=None)
        self.launches.append(launch)
        cli = self

        class Proc:
            returncode, pid = cli.returncode, 4242

            def communicate(self, input, timeout):
                launch.input, launch.timeout = input, timeout
                if cli.hang:
                    raise subprocess.TimeoutExpired(cmd, timeout)
                return cli.reply, cli.stderr

            def wait(self):
                return self.returncode

        return Proc()


def assert_hardened(launch) -> None:
    cmd = launch.cmd
    assert cmd[:2] == ["claude", "-p"] and SKIP_FLAG not in cmd
    assert cmd[cmd.index("--disallowed-tools") + 1] == _no_tools()
    assert "ANTHROPIC_API_KEY" not in launch.kwargs["env"]
    assert launch.kwargs["start_new_session"] is True       # the whole group is killed on a timeout


@pytest.fixture
def cli(monkeypatch):
    """The CLI, faked, with an API key in the environment it must not see."""
    from pipeline.generate import script_generator

    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-must-not-reach-the-cli")
    fake = FakeCLI()
    monkeypatch.setattr(script_generator.subprocess, "Popen", fake)
    return fake


@pytest.mark.parametrize("module", ["pipeline.audit_goals", "pipeline.anchor_goals"])
def test_the_goal_tools_ask_haiku_through_the_helper(module, cli):
    cli.reply = '  {"proposed_edges": []}\n'
    assert importlib.import_module(module)._call_claude("the prompt", timeout=7) == '{"proposed_edges": []}'
    (launch,) = cli.launches
    assert_hardened(launch)
    assert launch.cmd[launch.cmd.index("--model") + 1] == "haiku"
    assert launch.input == "the prompt" and launch.timeout == 7              # on stdin, not argv


@pytest.mark.parametrize("module", ["pipeline.audit_goals", "pipeline.anchor_goals"])
def test_a_failed_call_is_printed_and_returns_none(module, cli, monkeypatch, capsys):
    from pipeline.generate import script_generator

    call = importlib.import_module(module)._call_claude
    cli.returncode, cli.stderr = 1, "usage limit [/reached]"
    assert call("p") is None
    printed = _ANSI.sub("", capsys.readouterr().out)
    assert "claude call failed: claude CLI exited 1: usage limit [/reached]" in printed    # markup-safe
    assert "[red]" not in printed

    cli.returncode, cli.hang = 0, True
    killed = []
    monkeypatch.setattr(script_generator.os, "getpgid", lambda pid: pid)
    monkeypatch.setattr(script_generator.os, "killpg", lambda pgid, sig: killed.append(pgid))
    assert call("p", timeout=3) is None
    assert killed == [4242] and "timed out after 3s" in _ANSI.sub("", capsys.readouterr().out)

    def missing(cmd, **kwargs):
        raise FileNotFoundError(2, "No such file or directory", "claude")

    monkeypatch.setattr(script_generator.subprocess, "Popen", missing)
    assert call("p") is None
    assert "CLI not found" in _ANSI.sub("", capsys.readouterr().out)


def test_the_plan_extender_asks_through_the_helper(tmp_path, monkeypatch, cli):
    from pipeline.utils import extend_plan as ext

    (tmp_path / "curriculum").mkdir()
    # The fixture's AST102 has 4 lectures (courses.yaml); the plan stops at 3.
    (tmp_path / "curriculum" / "AST102-lecture-plan.md").write_text(
        "\n".join(f"## Lecture {n:02d}: Topic {n}\n\n---\n" for n in range(1, 4)))
    monkeypatch.setattr(ext, "lecture_plan_path", lambda course: tmp_path / "curriculum" / f"{course}-lecture-plan.md")
    cli.reply = "## Lecture 04: Wrap-up\n"
    ext.extend_plan("AST102")
    (launch,) = cli.launches
    assert_hardened(launch)
    assert "missing lecture(s) 4" in launch.input and launch.timeout == 300
    (tmp_path / "curriculum" / "AST102-lecture-plan.md").write_text("## Lecture 01: One\n")
    cli.returncode, cli.stderr = 1, "boom"
    with pytest.raises(RuntimeError, match="claude CLI exited 1: boom"):
        ext.extend_plan("AST102")


# ---------------------------------------------------------------------------
# The app's local CLI backend
# ---------------------------------------------------------------------------

def test_the_tutor_cli_denies_every_tool_and_drops_the_key(monkeypatch):
    from tutor.agent import cli_backend

    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-must-not-reach-the-cli")
    monkeypatch.setattr(cli_backend, "_run_tools_local", lambda messages, course: (messages, None))
    launches = []

    async def lines(*events):
        for event in events:
            yield (json.dumps(event) + "\n").encode()

    class Proc:
        returncode = None

        def __init__(self):
            delta = {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "Hi"}}
            self.stdout = lines({"type": "stream_event", "event": delta}, {"type": "result"})

        async def wait(self):
            self.returncode = 0
            return 0

    async def spawn(*args, **kwargs):
        launches.append((args, kwargs))
        return Proc()

    monkeypatch.setattr(cli_backend.asyncio, "create_subprocess_exec", spawn)

    async def collect():
        return [e async for e in cli_backend.stream_cli("system", [{"role": "user", "content": "--help"}])]

    events = asyncio.run(collect())
    assert events[-1] == {"type": "done", "full_text": "Hi"}
    (args, kwargs), = launches
    assert args[:2] == ("claude", "-p") and SKIP_FLAG not in args
    assert args[2] == "User: --help"                        # a student's leading dashes are not an option
    assert args[args.index("--disallowed-tools") + 1] == cli_backend.NO_TOOLS == _no_tools()
    assert "ANTHROPIC_API_KEY" not in kwargs["env"]
