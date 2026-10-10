"""Session 19 (handoff Task 6): the run_code execution sandbox.

Two layers:
- pure / disabled-path tests run everywhere (no compiler needed)
- containment tests compile and run real C++; they are skipped without a
  compiler, and the fork-bomb test is Linux-only (rlimits are reliably enforced
  there and the runner is disposable; we do not fork-bomb a dev Mac).

Exit gate: attempted escapes (infinite loop, file write, memory bomb, fork bomb)
are all contained, and the facts schema is stable.
"""
from __future__ import annotations

import os
import shutil
import sys
import time
from pathlib import Path

import pytest

import tutor.config as config
from tutor.agent import run_code as rc

GPP = shutil.which("g++") or shutil.which("clang++")
needs_cc = pytest.mark.skipif(not GPP, reason="no C++ compiler on PATH")
linux_only = pytest.mark.skipif(
    not sys.platform.startswith("linux"),
    reason="rlimit-enforced escape test; Linux runner only",
)
needs_ns = pytest.mark.skipif(
    not (GPP and rc._supports_namespaces()),
    reason="no compiler, or this host cannot build the namespace sandbox",
)

FACTS_KEYS = {
    "enabled", "backend", "compiled", "compile_error", "timed_out",
    "tests", "all_passed", "stderr_head", "note",
    "isolation", "network_isolated", "infra_error",          # plan step 6.9
}


@pytest.fixture()
def exec_on(monkeypatch):
    """Enable code execution for one test and reset the config cache after."""
    monkeypatch.setenv("ENABLE_CODE_EXEC", "true")
    config.get_config.cache_clear()
    yield
    config.get_config.cache_clear()


# ---------------------------------------------------------------------------
# Disabled-by-default + schema (no compiler required)
# ---------------------------------------------------------------------------

def test_disabled_by_default(monkeypatch):
    monkeypatch.delenv("ENABLE_CODE_EXEC", raising=False)
    config.get_config.cache_clear()
    try:
        facts = rc.run_code("int main(){return 0;}",
                            tests=[{"name": "x", "stdin": "", "expected": ""}])
        assert facts["enabled"] is False
        assert facts["backend"] == "disabled"
        assert facts["tests"] == []
        assert facts["all_passed"] is False
    finally:
        config.get_config.cache_clear()


def test_facts_schema_stable(monkeypatch):
    monkeypatch.delenv("ENABLE_CODE_EXEC", raising=False)
    config.get_config.cache_clear()
    try:
        facts = rc.run_code("int main(){}")
        assert set(facts.keys()) == FACTS_KEYS
    finally:
        config.get_config.cache_clear()


def test_load_exercise_tests():
    t = rc.load_exercise_tests("AST101-L04-inverse-square-law-code")
    assert [x["expected"] for x in t] == ["1", "9", "100"]
    assert rc.load_exercise_tests("nonexistent-id") == []
    # The _README key is not addressable as an exercise.
    assert rc.load_exercise_tests("_README") == []


def test_normalise_and_check():
    assert rc._check("5", "5\n", "exact")
    assert rc._check("a\nb", "a  \nb\n\n", "exact")
    assert not rc._check("5", "6", "exact")
    assert rc._check("lo wor", "hello world", "contains")
    assert not rc._check("zzz", "hello world", "contains")


# ---------------------------------------------------------------------------
# Real compilation + execution
# ---------------------------------------------------------------------------

@needs_cc
def test_correct_program_passes_its_tests(exec_on):
    code = (
        "#include <iostream>\n"
        "int main(){ long k; std::cin>>k; long sq=0; for(long i=0;i<k;i++) sq+=k;"
        " std::cout<<sq<<std::endl; return 0; }"
    )
    facts = rc.run_code(code, exercise_id="AST101-L04-inverse-square-law-code", timeout_sec=5)
    assert facts["compiled"] is True
    assert facts["all_passed"] is True
    assert len(facts["tests"]) == 3


@needs_cc
def test_wrong_program_fails_a_test(exec_on):
    code = (  # wrong: always prints 0 instead of k*k
        "#include <iostream>\nint main(){ long k; std::cin>>k; std::cout<<0<<std::endl; return 0; }"
    )
    facts = rc.run_code(code, exercise_id="AST101-L04-inverse-square-law-code", timeout_sec=5)
    assert facts["compiled"] is True
    assert facts["all_passed"] is False
    assert any(not t["passed"] for t in facts["tests"])


@needs_cc
def test_compile_error_reported(exec_on):
    facts = rc.run_code("int main(){ this is not valid c++ ",
                        tests=[{"name": "x", "stdin": "", "expected": ""}])
    assert facts["compiled"] is False
    assert facts["compile_error"]
    assert facts["all_passed"] is False


@needs_cc
def test_infinite_loop_times_out(exec_on):
    facts = rc.run_code("int main(){ while(true){} }",
                        tests=[{"name": "loop", "stdin": "", "expected": ""}],
                        timeout_sec=2)
    assert facts["timed_out"] is True
    assert facts["tests"][0]["passed"] is False


@needs_cc
def test_file_write_blocked(exec_on):
    import tempfile
    probe = os.path.join(tempfile.gettempdir(), f"tutor_probe_{os.getpid()}.txt")
    if os.path.exists(probe):
        os.remove(probe)
    code = (
        f'#include <fstream>\nint main(){{ std::ofstream o("{probe}");'
        ' o<<"pwned"; o.flush(); o.close(); return 0; }'
    )
    try:
        facts = rc.run_code(code, tests=[{"name": "fw", "stdin": "", "expected": "pwned"}],
                            timeout_sec=3)
        wrote = os.path.exists(probe) and os.path.getsize(probe) > 0
        assert not wrote, "FSIZE=0 should block the file write"
        assert facts["tests"][0]["passed"] is False
    finally:
        if os.path.exists(probe):
            os.remove(probe)


# macOS refuses setrlimit(RLIMIT_AS) outright ("current limit exceeds maximum
# limit") and _clamp swallows that, so the address-space cap is a silent no-op
# there and containment falls to ASAN's RSS poller alone, which lands either side
# of the 15 s budget from run to run. Linux enforces the limit, so assert it
# where it is real (as test_fork_bomb_contained below does).
@needs_cc
@linux_only
def test_memory_bomb_contained(exec_on):
    code = (
        "#include <string>\nint main(){ std::string s(8000000000ULL, 'x');"
        " return (int)s.size(); }"
    )
    t0 = time.time()
    facts = rc.run_code(code, tests=[{"name": "mem", "stdin": "", "expected": "ok"}],
                        timeout_sec=5, mem_mb=128)
    assert time.time() - t0 < 15, "memory bomb should not wedge the host"
    assert facts["tests"][0]["passed"] is False


@linux_only
def test_the_process_cap_sits_above_what_the_uid_already_owns():
    """RLIMIT_NPROC counts every task the uid owns, threads included, so a
    fixed cap of 64 failed the sandbox's first fork on a busy CI runner
    ("Cannot fork") depending on which tests had run before. The cap is now
    the current count plus the headroom."""
    import subprocess
    import sys
    import threading

    owned = rc._uid_task_count()
    assert owned >= threading.active_count()
    child = subprocess.run(
        [sys.executable, "-c", "import resource; print(resource.getrlimit(resource.RLIMIT_NPROC)[0])"],
        capture_output=True, text=True, timeout=20, preexec_fn=rc._make_preexec(5),
    )
    cap = int(child.stdout.strip())
    assert owned + rc._FORK_HEADROOM - 8 <= cap <= owned + rc._FORK_HEADROOM + 8, (owned, cap)


def test_the_fork_bomb_never_runs_outside_linux():
    """Outside Linux there is no /proc and no process cap, so the bomb
    fork-bombs the desktop: on 2026-09-28 a misplaced decorator ran it on a
    Mac and took the machine down. The markers are checked, not assumed."""
    marks = getattr(test_fork_bomb_contained, "pytestmark", [])
    assert linux_only.mark in marks and needs_cc.mark in marks


@needs_cc
@linux_only
def test_fork_bomb_contained(exec_on):
    code = (
        "#include <unistd.h>\nint main(){ for(;;){ if(fork()==0){ for(;;) fork(); } } }"
    )
    t0 = time.time()
    facts = rc.run_code(code, tests=[{"name": "fb", "stdin": "", "expected": ""}],
                        timeout_sec=2)
    assert time.time() - t0 < 15, "fork bomb should be killed, not wedge the host"
    assert facts["tests"][0]["passed"] is False


# ---------------------------------------------------------------------------
# Plan step 6.9 (findings B6, B7, B8, B9): what the program can reach
# ---------------------------------------------------------------------------

def _token() -> str:
    return "tutor-canary-" + os.urandom(6).hex()


def _one(code: str, expected: str = "", timeout_sec: int = 5) -> dict:
    facts = rc.run_code(code, tests=[{"name": "probe", "stdin": "", "expected": expected}],
                        timeout_sec=timeout_sec)
    assert facts["infra_error"] == "", facts["infra_error"]
    return facts


def _seen(facts: dict) -> str:
    return " ".join([facts["compile_error"], facts["stderr_head"], *(t["got"] for t in facts["tests"])])


@needs_ns
def test_the_file_read_probe_returns_nothing(exec_on, tmp_path):
    """The done-when of step 6.9: a file the app user can read is not there."""
    token = _token()
    secret = tmp_path / "secret.env"
    secret.write_text(f"ANTHROPIC_API_KEY={token}\n")
    code = (f'#include <fstream>\n#include <iostream>\n#include <string>\n'
            f'int main(){{ std::ifstream f("{secret}"); std::string s; std::getline(f, s);'
            f' std::cout << (f ? s : std::string("unreadable")) << std::endl; }}')
    facts = _one(code, expected="unreadable")
    assert token not in _seen(facts)
    assert facts["tests"][0]["passed"] and facts["isolation"] == "namespaces"


@needs_ns
def test_an_include_cannot_read_a_file(exec_on, tmp_path):
    """The compiler runs in the sandbox too: an #include quotes what it reads."""
    token = _token()
    secret = tmp_path / "secret.h"
    secret.write_text(f"{token} = 1;\n")
    facts = _one(f'#include "{secret}"\nint main(){{ return 0; }}')
    assert facts["compiled"] is False and "No such file" in facts["compile_error"]
    assert token not in _seen(facts)


@needs_ns
def test_the_apps_environment_is_out_of_reach(exec_on, monkeypatch):
    """/proc/<app>/environ held the API key; the sandbox has its own /proc."""
    token = _token()
    monkeypatch.setenv("TUTOR_CANARY", token)
    code = ('#include <dirent.h>\n#include <fstream>\n#include <iostream>\n#include <iterator>\n#include <string>\n'
            'int main(){ int seen = 0; DIR* d = opendir("/proc"); while (auto* e = readdir(d)) {'
            ' std::ifstream f(std::string("/proc/") + e->d_name + "/environ");'
            ' std::string env((std::istreambuf_iterator<char>(f)), {});'
            ' if (env.find("TUTOR_CANARY") != std::string::npos) std::cout << env; ++seen; }'
            ' std::cout << "entries " << seen << std::endl; }')
    facts = _one(code)
    assert token not in _seen(facts) and "TUTOR_CANARY" not in _seen(facts)


@needs_ns
def test_the_network_is_unreachable(exec_on):
    code = ('#include <arpa/inet.h>\n#include <cstdio>\n#include <sys/socket.h>\n#include <unistd.h>\n'
            'int main(){ int s = socket(AF_INET, SOCK_STREAM, 0); sockaddr_in a{}; a.sin_family = AF_INET;'
            ' a.sin_port = htons(53); inet_pton(AF_INET, "1.1.1.1", &a.sin_addr);'
            ' std::puts(s >= 0 && connect(s, (sockaddr*)&a, sizeof a) == 0 ? "connected" : "no network"); }')
    facts = _one(code, expected="no network")
    assert facts["tests"][0]["passed"] and facts["network_isolated"] is True


def _named(name: str) -> list[str]:
    found = []
    for comm in Path("/proc").glob("[0-9]*/comm"):
        try:
            if comm.read_text().strip() == name:
                found.append(comm.parent.name)
        except OSError:
            continue
    return found


@needs_ns
def test_a_process_that_leaves_the_group_dies_with_the_run(exec_on):
    """Finding B7: a setsid() child escaped the group kill, and a background
    helper holding the pipe made a finished program read as a timeout."""
    code = ('#include <cstdio>\n#include <sys/prctl.h>\n#include <unistd.h>\n'
            'int main(){ if (fork() == 0) { setsid(); prctl(PR_SET_NAME, "tutor-escapee"); sleep(60); return 0; }'
            ' std::puts("done"); std::fflush(stdout); return 0; }')
    t0 = time.time()
    facts = _one(code, expected="done", timeout_sec=5)
    assert time.time() - t0 < 15
    assert facts["tests"][0]["passed"] and not facts["timed_out"]
    assert _named("tutor-escapee") == []


@needs_cc
def test_an_output_flood_is_stopped(exec_on):
    code = '#include <cstdio>\nint main(){ for (;;) std::fputs("xxxxxxxxxxxxxxxx", stdout); }'
    t0 = time.time()
    facts = rc.run_code(code, tests=[{"name": "flood", "stdin": "", "expected": "x"}], timeout_sec=20)
    assert time.time() - t0 < 15, "the flood should end the run, not the timeout"
    test = facts["tests"][0]
    assert test["output_capped"] and not test["passed"] and not test["timed_out"]
    assert len(test["got"]) <= rc._HEAD


def test_a_compile_failure_that_is_not_the_students(monkeypatch):
    """Finding B9: these were graded as the student's 0."""
    run = rc._Run
    assert rc._compile_infra_error(run(timed_out=True, returncode=None)).startswith("compilation did not finish")
    assert rc._compile_infra_error(run(err="/usr/bin/ld: cannot find -lasan: No such file", returncode=1)) \
        == "the toolchain failed: /usr/bin/ld: cannot find -lasan: No such file"
    assert rc._compile_infra_error(run(ready=False, err="mount: permission denied")) \
        == "the sandbox could not be built: mount: permission denied"
    assert rc._compile_infra_error(run(contained=False)) == "a compiler process outlived the compile"
    assert rc._compile_infra_error(run(err="main.cpp:1:5: error: expected ';'", returncode=1)) == ""


@needs_cc
def test_an_infrastructure_miss_is_reported_not_graded(exec_on, monkeypatch):
    real = rc._execute

    def compile_times_out(argv, *args, **kwargs):
        # Only the student's compile: the sanitizer probe (memoized for the
        # whole process) must still see the real toolchain.
        if argv[0] == GPP and argv[-1].endswith("/main.cpp"):
            return rc._Run(timed_out=True)
        return real(argv, *args, **kwargs)

    monkeypatch.setattr(rc, "_execute", compile_times_out)
    facts = rc.run_code("int main(){ return 0; }", tests=[{"name": "x", "stdin": "", "expected": ""}])
    assert facts["infra_error"].startswith("compilation did not finish") and facts["compiled"] is False
    assert facts["compile_error"] == "" and facts["tests"] == []


def test_the_subprocess_backend_refuses_beside_a_database(exec_on, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://u:p@db.example.com:5432/postgres")
    monkeypatch.setattr(rc, "_execute", lambda *a, **k: pytest.fail("nothing may run"))
    facts = rc.run_code("int main(){ return 0; }", tests=[{"name": "x", "stdin": "", "expected": ""}])
    assert "DATABASE_URL" in facts["infra_error"] and facts["enabled"] is True
    assert any("no student code will run" in w for w in rc.boot_warnings())


def test_boot_warnings_say_what_is_missing(exec_on, monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setattr(rc, "_supports_namespaces", lambda: False)
    monkeypatch.setattr(rc, "_supports_network_wrap", lambda: False)
    (warning,) = rc.boot_warnings()
    assert "without namespaces" in warning and "NOT dropped" in warning
    monkeypatch.setattr(rc, "_supports_namespaces", lambda: True)
    assert rc.boot_warnings() == []
    monkeypatch.setenv("ENABLE_CODE_EXEC", "false")
    config.get_config.cache_clear()
    monkeypatch.setattr(rc, "_supports_namespaces", lambda: False)
    assert rc.boot_warnings() == []


@pytest.mark.skipif(not os.getenv("GITHUB_ACTIONS"), reason="CI only")
def test_ci_runs_the_real_sandbox():
    """The tests workflow allows unprivileged user namespaces, so every
    containment test above runs in CI instead of skipping."""
    assert GPP and rc._supports_namespaces()
    assert rc._supports_asan(GPP, True)


def test_the_sandbox_doc_matches_the_code():
    doc = (Path(__file__).resolve().parent.parent / "docs" / "sandbox.md").read_text()
    for key in FACTS_KEYS:
        assert f"`{key}`" in doc, key
    for flag in rc._UNSHARE[1:]:
        assert f"`{flag}`" in doc, flag
    for knob in ("ENABLE_CODE_EXEC", "CODE_EXEC_BACKEND", "CODE_EXEC_TIMEOUT_SEC", "CODE_EXEC_MEM_MB"):
        assert f"`{knob}`" in doc, knob
    assert "hard_rss_limit_mb" in doc and "DATABASE_URL" in doc and "pivot_root" in doc
    assert "no filesystem namespace" not in doc.lower()           # the gap 6.9 closed


def test_the_health_page_says_how_code_runs(monkeypatch):
    monkeypatch.delenv("ENABLE_CODE_EXEC", raising=False)
    assert rc.status_line() == "off (no student code runs)"
    monkeypatch.setenv("ENABLE_CODE_EXEC", "true")
    config.get_config.cache_clear()
    monkeypatch.setattr(rc, "_supports_namespaces", lambda: True)
    assert rc.status_line() == "on (subprocess, namespaces)"
    monkeypatch.setattr(rc, "_supports_namespaces", lambda: False)
    assert "NO namespaces" in rc.status_line()
    monkeypatch.setenv("DATABASE_URL", "postgresql://u:p@db.example.com:5432/postgres")
    assert "refused" in rc.status_line()
    from tutor.health import check_features

    row = next(r for r in check_features() if r.name == "Code execution sandbox")
    assert row.on and "refused" in row.detail


_ROOT_ENTRIES = {".", "..", "bin", "dev", "etc", "lib", "lib32", "lib64", "libx32", "proc", "sbin", "tmp", "usr", "work"}


@needs_ns
def test_the_root_holds_only_the_sandbox(exec_on):
    code = ('#include <dirent.h>\n#include <iostream>\n'
            'int main(){ DIR* d = opendir("/"); while (auto* e = readdir(d)) std::cout << e->d_name << "\\n"; }')
    entries = set(_one(code)["tests"][0]["got"].split())
    assert "work" in entries and "usr" in entries
    assert entries <= _ROOT_ENTRIES, entries - _ROOT_ENTRIES       # no /.old, /home, /app, /root


@needs_ns
def test_the_program_cannot_undo_the_sandbox(exec_on):
    code = ('#include <cstdio>\n#include <sys/mount.h>\n#include <unistd.h>\n'
            'int main(){ std::printf("%d %d %d\\n", mount("none", "/tmp", "tmpfs", 0, nullptr),'
            ' umount2("/usr", MNT_DETACH), chroot("/tmp")); }')
    assert _one(code)["tests"][0]["got"].split() == ["-1", "-1", "-1"]     # no capabilities left


def _fake_compiler(tmp_path, body: str) -> str:
    cc = tmp_path / "fake-g++"
    cc.write_text("#!/bin/sh\n" + body)
    cc.chmod(0o755)
    return str(cc)


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX shell")
def test_the_asan_probe_builds_links_and_runs(tmp_path):
    """Finding B9: -fsyntax-only passed where libasan was missing at link time."""
    probe = rc._supports_asan.__wrapped__
    syntax_only = _fake_compiler(tmp_path, 'case "$*" in *-fsyntax-only*) exit 0;; esac\n'
                                           'echo "/usr/bin/ld: cannot find -lasan" >&2; exit 1\n')
    assert probe(syntax_only, False) is False
    links_but_fails = _fake_compiler(tmp_path, 'while [ "$1" != "-o" ]; do shift; done\n'
                                               'printf "#!/bin/sh\\nexit 1\\n" > "$2"; chmod +x "$2"\n')
    assert probe(links_but_fails, False) is False
    links_and_runs = _fake_compiler(tmp_path, 'while [ "$1" != "-o" ]; do shift; done\n'
                                              'printf "#!/bin/sh\\nexit 0\\n" > "$2"; chmod +x "$2"\n')
    assert probe(links_and_runs, False) is True


def test_asan_caps_resident_memory():
    options = rc._env("/work", 256)["ASAN_OPTIONS"]
    assert "hard_rss_limit_mb=256" in options and "max_allocation_size_mb=256" in options


@needs_cc
@linux_only
def test_without_namespaces_an_escaped_process_is_a_containment_error(exec_on, monkeypatch):
    """Finding B7 on a host without the sandbox: a child that leaves the group
    and holds the output pipe is reported, not graded as a timeout."""
    monkeypatch.setattr(rc, "_supports_namespaces", lambda: False)
    monkeypatch.setattr(rc, "_PIPE_GRACE", 1)
    code = ('#include <cstdio>\n#include <sys/prctl.h>\n#include <unistd.h>\n'
            'int main(){ if (fork() == 0) { setsid(); prctl(PR_SET_NAME, "tutor-straggler"); sleep(20); return 0; }'
            ' std::puts("done"); return 0; }')
    try:
        facts = rc.run_code(code, tests=[{"name": "bg", "stdin": "", "expected": "done"}], timeout_sec=5)
        assert facts["infra_error"] == "a process from the program outlived its run"
        assert facts["isolation"] == "none" and facts["tests"] == []
    finally:
        for pid in _named("tutor-straggler"):
            try:
                os.kill(int(pid), 9)
            except OSError:
                pass


def _host_allows_namespaces() -> bool:
    """util-linux's own check, independent of run_code's script."""
    if not sys.platform.startswith("linux") or not shutil.which("unshare"):
        return False
    import subprocess

    try:
        return subprocess.run(["unshare", "--user", "--map-root-user", "--mount", "--pid", "--fork", "--net",
                               "--mount-proc", "true"], capture_output=True, timeout=20).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


@pytest.mark.skipif(not GPP or not _host_allows_namespaces(), reason="this host forbids the namespaces")
def test_the_sandbox_is_used_wherever_the_host_allows_it():
    """A regression in the sandbox script must fail here, not skip every
    containment test above by making the probe say 'no namespaces'."""
    assert rc._supports_namespaces()
    assert rc._supports_asan(GPP, True)

