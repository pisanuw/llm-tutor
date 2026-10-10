"""Plan step 6.2c (findings F1, F18): the repository guards CI runs first.

Each test builds a scratch git repository. Done when: a bulk sync commit
cannot land green (test_a_bulk_sync_commit_fails_three_ways).
"""
from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

import pytest

from scripts import repo_guard

ROOT = Path(__file__).resolve().parent.parent

ZERO = "0" * 40


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-c", "commit.gpgsign=false", "-c", "user.name=T", "-c", "user.email=t@x.test",
                           *args], cwd=repo, check=True, capture_output=True, text=True).stdout.strip()


def _commit(repo: Path, message: str, files: dict | None = None, delete: tuple = ()) -> str:
    for rel, content in (files or {}).items():
        path = repo / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        (path.write_bytes if isinstance(content, bytes) else path.write_text)(content)
    for rel in delete:
        _git(repo, "rm", "-q", rel)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "--allow-empty", "-m", message)
    return _git(repo, "rev-parse", "HEAD")


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q", "-b", "main")
    _commit(root, "docs: start", {"README.md": "hi\n"})
    return root


def _check(repo, base, head, **kw):
    return repo_guard.check(base, head, cwd=repo, **kw)


def test_a_clean_range_passes(repo):
    base = _git(repo, "rev-parse", "HEAD")
    head = _commit(repo, "feat(ui): add a page", {"tutor/page.html": "<p>x</p>"})
    report = _check(repo, base, head)
    assert report.failures == [] and report.checked == 1


def test_a_sync_subject_fails_even_with_a_conforming_line_below(repo):
    base = _git(repo, "rev-parse", "HEAD")
    _commit(repo, "sync laptop")
    head = _commit(repo, "flyer\n\nfix: a conforming line in the body")   # the hook's grep passed this
    failures = _check(repo, base, head).failures
    assert len(failures) == 2
    assert "'sync laptop'" in failures[0] + failures[1] and "'flyer'" in failures[0] + failures[1]


def test_merges_and_git_reverts_are_exempt(repo):
    base = _git(repo, "rev-parse", "HEAD")
    _commit(repo, "Merge branch 'feature'")
    head = _commit(repo, 'Revert "feat(ui): add a page"')
    assert _check(repo, base, head).failures == []


def test_the_subject_rule_is_the_hook_rule():
    hook = (ROOT / ".githooks" / "commit-msg").read_text()
    assert repo_guard.SUBJECT.pattern == re.search(r"^pattern='([^']+)'", hook, re.M).group(1)
    assert repo_guard.SUBJECT.match("fix(agent): keep the turn")
    assert not repo_guard.SUBJECT.match("Fix: keep the turn")


def test_a_file_over_the_limit_fails(repo):
    base = _git(repo, "rev-parse", "HEAD")
    head = _commit(repo, "chore: add assets", {"a.bin": b"x" * 100, "b.bin": b"x" * 101})
    failures = _check(repo, base, head, max_bytes=100).failures
    assert len(failures) == 1 and failures[0].startswith("b.bin: ")


def test_a_jump_in_file_count_fails_unless_a_trailer_names_why(repo):
    base = _git(repo, "rev-parse", "HEAD")
    four = {f"data/{i}.json": "{}" for i in range(4)}
    head = _commit(repo, "feat: add data", four)
    assert any("grows by 4 (limit 3)" in f for f in _check(repo, base, head, max_new=3).failures)
    assert _check(repo, base, head, max_new=4).failures == []
    _git(repo, "reset", "-q", "--hard", base)
    head = _commit(repo, "feat: add data\n\nAllow-Bulk-Add:", four)                   # no reason given
    assert _check(repo, base, head, max_new=3).failures != []
    _git(repo, "reset", "-q", "--hard", base)
    head = _commit(repo, "feat: add data\n\nAllow-Bulk-Add: regenerated golden files", four)
    assert _check(repo, base, head, max_new=3).failures == []


def test_student_record_paths_fail_and_are_never_printed(repo):
    base = _git(repo, "rev-parse", "HEAD")
    head = _commit(repo, "chore: import", {
        "course/2019/submissions/alicesmith/hw1.cpp": "int main(){}",
        "resumes/bobjones.pdf": "%PDF",
        "old/GradePage AST 102 A.pdf": "%PDF",
        "old/Gradebook Export-20190830.csv": "x",
        "old/grading_spreadsheet.xlsx": "x",
        "source-materials2/notes.txt": "x",
    })
    report = _check(repo, base, head)
    assert len(report.failures) == 1
    message = report.failures[0]
    assert message.startswith("6 path(s) look like student records")
    assert "(1 submissions/, 1 resumes/, 1 GradePage, 1 gradebook, 1 grading_spreadsheet, 1 source-materials/)" in message
    assert "alicesmith" not in message and "bobjones" not in message
    assert len(report.flagged) == 6


def test_a_record_added_and_removed_in_the_range_still_fails(repo):
    """History keeps what a push adds, even when a later commit deletes it."""
    base = _git(repo, "rev-parse", "HEAD")
    _commit(repo, "chore: import", {"submissions/alicesmith/a.cpp": "x"})
    head = _commit(repo, "chore: remove it again", delete=("submissions/alicesmith/a.cpp",))
    assert any("1 submissions/" in f for f in _check(repo, base, head).failures)


def test_a_rename_into_a_record_path_counts_as_adding_it(repo):
    base = _git(repo, "rev-parse", "HEAD")
    _commit(repo, "chore: notes", {"notes/a.txt": "the same text\n" * 20})
    (repo / "submissions").mkdir()
    _git(repo, "mv", "notes/a.txt", "submissions/a.txt")
    _commit(repo, "chore: move")
    head = _commit(repo, "chore: drop", delete=("submissions/a.txt",))
    assert any("1 submissions/" in f for f in _check(repo, base, head).failures)


def test_lookalikes_pass(repo):
    base = _git(repo, "rev-parse", "HEAD")
    head = _commit(repo, "feat: views", {"tutor/routers/submission_view.py": "x", "docs/resume-notes.md": "x",
                                         "docs/grade-book-notes.md": "x", "tests/data/grading.json": "x"})
    assert _check(repo, base, head).failures == []


def test_a_bulk_sync_commit_fails_three_ways(repo):
    """The shape of an accidental bulk import: a bare subject, hundreds of
    files, student-named submission folders."""
    base = _git(repo, "rev-parse", "HEAD")
    files = {f"source-materials2/AST102/2019AU/submissions/student{i}/ass1.cpp": "int main(){}" for i in range(250)}
    files["source-materials2/AST102/2019AU/gradebook.csv"] = "x"
    head = _commit(repo, "partial", files)
    failures = _check(repo, base, head).failures
    assert len(failures) == 3
    assert "not a Conventional Commit: 'partial'" in failures[0]
    assert "grows by 251 (limit 200)" in failures[1]
    assert "251 path(s)" in failures[2] and "student1" not in failures[2]


# ---------------------------------------------------------------------------
# Which commits an event adds
# ---------------------------------------------------------------------------

@pytest.fixture
def cloned(repo, tmp_path):
    """``repo`` with an ``origin`` whose main is the first commit."""
    origin = tmp_path / "origin.git"
    _git(tmp_path, "clone", "-q", "--bare", str(repo), str(origin))
    _git(repo, "remote", "add", "origin", str(origin))
    _git(repo, "fetch", "-q", "origin")
    return repo


def test_a_push_checks_what_it_adds(cloned):
    before = _git(cloned, "rev-parse", "HEAD")
    after = _commit(cloned, "feat: one")
    assert repo_guard.resolve_range("push", {"before": before, "after": after}, cloned) == (before, after)


def test_a_new_branch_is_checked_from_the_default_branch(cloned):
    main = _git(cloned, "rev-parse", "HEAD")
    _git(cloned, "checkout", "-q", "-b", "feature")
    _commit(cloned, "feat: one")
    after = _commit(cloned, "feat: two")
    event = {"before": ZERO, "after": after, "repository": {"default_branch": "main"}}
    base, head = repo_guard.resolve_range("push", event, cloned)
    assert (base, head) == (main, after)
    assert len(repo_guard.commits(base, head, cloned)) == 2


def test_a_force_push_is_checked_from_the_shared_commit(cloned):
    main = _git(cloned, "rev-parse", "HEAD")
    before = _commit(cloned, "feat: first try")
    _git(cloned, "reset", "-q", "--hard", main)
    after = _commit(cloned, "feat: second try")
    assert repo_guard.resolve_range("push", {"before": before, "after": after}, cloned) == (main, after)


def test_a_pull_request_is_checked_from_its_merge_base(cloned):
    main = _git(cloned, "rev-parse", "HEAD")
    _git(cloned, "checkout", "-q", "-b", "feature")
    head = _commit(cloned, "feat: one")
    event = {"pull_request": {"base": {"sha": main}, "head": {"sha": head}}}
    assert repo_guard.resolve_range("pull_request", event, cloned) == (main, head)


def test_a_branch_deletion_checks_nothing(cloned):
    assert repo_guard.resolve_range("push", {"before": "a" * 40, "after": ZERO}, cloned) is None


def test_main_reads_the_event_and_annotates_in_actions(cloned, tmp_path, monkeypatch, capsys):
    before = _git(cloned, "rev-parse", "HEAD")
    after = _commit(cloned, "sync", {"resumes/bobjones.pdf": "%PDF"})
    event = tmp_path / "event.json"
    event.write_text(json.dumps({"before": before, "after": after}))
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    code = repo_guard.main(["--event-path", str(event), "--event-name", "push", "--repo", str(cloned),
                            "--show-paths"])
    out = capsys.readouterr().out
    assert code == 1
    assert out.count("::error::") == 2 and "bobjones" not in out     # --show-paths is ignored in CI
    monkeypatch.delenv("GITHUB_ACTIONS")
    assert repo_guard.main(["--base", before, "--head", after, "--repo", str(cloned), "--show-paths"]) == 1
    assert "  resumes/bobjones.pdf" in capsys.readouterr().out            # on the owner's machine


def _has_head() -> bool:
    return subprocess.run(["git", "rev-parse", "--verify", "-q", "HEAD^{commit}"], cwd=ROOT,
                          capture_output=True).returncode == 0


def test_this_repository_passes_at_head():
    """The tree CI checks out today has no flagged path and no file over the
    limit (history is decision 1's to rewrite)."""
    if not (ROOT / ".git").exists():
        pytest.skip("not a git checkout")
    if _has_head():
        assert repo_guard.risky(repo_guard.tracked("HEAD")) == []
        assert repo_guard.large("HEAD") == []
        return
    # A repository before its first commit: check what that commit would hold
    # (every file git would add, ignored files left out).
    files = repo_guard.git("ls-files", "-z", "--cached", "--others", "--exclude-standard").split("\0")[:-1]
    assert files
    assert repo_guard.risky(files) == []
    assert [p for p in files if (ROOT / p).is_file()
            and (ROOT / p).stat().st_size > repo_guard.MAX_FILE_BYTES] == []
