"""Plan Phase 7 (findings F7, F8, F13, E21, A20, C14): the numbers and shapes
the docs state are the code's.

CLAUDE.md is loaded as instructions by every Claude Code session, so a stale
count there misleads the next change. Each test reads a claim from a doc and
checks it against the code or the data it describes.
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CLAUDE = (ROOT / "CLAUDE.md").read_text()
GUIDE = (ROOT / "ADDING-KNOWLEDGE.md").read_text()
DEPLOY = (ROOT / "DEPLOY.md").read_text()
WORDS = {"four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10, "eleven": 11}


def test_the_collection_count_and_names():
    from tutor.kb import COLLECTIONS

    stated = re.search(r"The (\w+) collections \(", CLAUDE).group(1)
    assert WORDS[stated] == len(COLLECTIONS)
    # The adopter's guide documents every collection by name.
    for name in COLLECTIONS:
        assert f"| `{name}` |" in GUIDE, name


def test_the_deck_url_shape(tmp_path):
    from tutor.agent.tools import _slide_deck_url

    deck = tmp_path / "AST101" / "lecture-05" / "index.html"
    deck.parent.mkdir(parents=True)
    deck.write_text("<html></html>")
    assert _slide_deck_url(tmp_path, "AST101", 5) == "/slides/AST101/lecture-05/index.html"
    assert "`/slides/<course>/lecture-NN/index.html`" in CLAUDE
    assert "frames/slide-NN.png` shape" not in CLAUDE


def test_the_tools_are_named_in_the_preamble():
    from tutor.agent import prompt_builder
    from tutor.agent.tools import TOOL_DEFINITIONS

    names = {t["name"] for t in TOOL_DEFINITIONS}
    assert len(names) == 4 and "The four tools" in CLAUDE
    preamble = prompt_builder._preamble()
    assert "You have access to four tools:" in preamble
    assert all(f"- {name}:" in preamble for name in names)


def test_the_guide_names_every_persona_and_profile_file_the_prompt_reads():
    """ADDING-KNOWLEDGE.md step 2 lists exactly the files prompt_builder reads."""
    from tutor.agent import prompt_builder

    source = Path(prompt_builder.__file__).read_text()
    read = set(re.findall(r'(?:persona_dir|profile_dir) / "([\w-]+\.md)"', source))
    assert len(read) == 8
    for name in read:
        assert name in GUIDE, name


def test_every_pipeline_command_the_guide_uses_exists():
    from click.testing import CliRunner

    from pipeline.__main__ import cli

    used = set(re.findall(r"python -m pipeline ([a-z][\w-]+)", GUIDE))
    assert used, "the guide names no pipeline command"
    missing = sorted(c for c in used if c not in cli.commands)
    assert missing == []
    assert CliRunner().invoke(cli, ["--help"]).exit_code == 0


def test_every_workflow_is_in_the_deploy_table():
    table = DEPLOY.split("## Workflows", 1)[1].split("\n## ", 1)[0]
    listed = set(re.findall(r"^\| `([\w-]+\.yml)` \|", table, re.M))
    assert listed == {p.name for p in (ROOT / ".github" / "workflows").glob("*.yml")}


def test_every_table_is_in_the_retention_policy():
    from tutor.db.models import Base

    policy = (ROOT / "docs" / "retention-policy.md").read_text()
    missing = [t for t in Base.metadata.tables if not re.search(rf"`{t}(?:\.\w+)?`", policy)]
    assert missing == []


def test_the_minimum_env_and_the_ci_references():
    example = (ROOT / ".env.example").read_text()
    assert re.search(r"^ADMIN_EMAILS=", example, re.M)
    assert "ci.yml" not in (ROOT / "pyproject.toml").read_text()


def test_deploy_documents_every_seed_variable_render_declares():
    """The Step 4 table names each seed URL and the audio directory that
    render.yaml declares (the audio seed had no row, so an operator setting up
    a service from the table would have shipped it without narration)."""
    blueprint = (ROOT / "render.yaml").read_text()
    declared = {m.group(1) for m in re.finditer(r"^\s*- key: (\w+)", blueprint, re.M)}
    seeds = {k for k in declared if k.endswith("_URL") and k != "BASE_URL"} | {"LECTURE_AUDIO_DIR"}
    assert {"CHROMA_SEED_URL", "OUTPUT_SEED_URL", "AUDIO_SEED_URL", "MASTERY_EXAMS_URL"} <= seeds
    step4 = DEPLOY.split("## Step 4 — Environment variables", 1)[1].split("\n## ", 1)[0]
    missing = {k for k in seeds if f"| `{k}` |" not in step4}
    assert not missing, missing
