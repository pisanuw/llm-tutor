"""Deterministic C++ execution sandbox (Session 19; hardened in plan step 6.9).

``run_code`` compiles a student's C++ and runs it against fixed test cases,
returning **facts only**: did it compile, which tests passed, a truncated
stderr, and how the run was contained. Never a score, never an opinion.
``evaluate_code`` scores from these facts, so correctness comes from the
compiler and the tests, not from the model (design finding F6).

Trust posture: execution is **off by default** (``ENABLE_CODE_EXEC`` unset or
false). The subprocess backend also refuses to run where ``DATABASE_URL`` is
set, so it never runs beside production data; hosted execution waits for the
E2B backend (``CODE_EXEC_BACKEND=e2b``, not wired; decision 9).

Containment, subprocess backend (docs/sandbox.md has the reasoning):

  Namespaces, on Linux where unprivileged user namespaces work (probed once):
    the compiler and the program run in new user, mount, PID, network, IPC,
    and UTS namespaces, in a root that holds only /usr (read-only), the
    loader's configuration, a fresh /proc for the new PID namespace, three
    /dev nodes, a small /tmp, and the run's work directory. The old root is
    detached (pivot_root), and the command runs with no capabilities and no
    new privileges. It cannot read the app's files or another process's
    environment, reach the network, or outlive its run: every process in the
    namespace dies with it.
  Rlimits, set before exec: CPU seconds, processes, open files, and file size
    (0 for the program, so it cannot write a file).
  Memory: ASAN's max_allocation_size_mb and hard_rss_limit_mb; RLIMIT_AS is
    useless beside ASAN's shadow reservation, so it caps only a build without
    sanitizers.
  Output: each stream is read up to _OUTPUT_CAP bytes; more ends the run.

Without namespaces (macOS, or a host that forbids them) the compiler and the
program run as the app user under the rlimits alone, with the network dropped
by ``unshare -rn`` where that works. The facts say so (``isolation``,
``network_isolated``), and the app logs a boot warning (``boot_warnings``).

A run the sandbox could not judge (a refused backend, a compile that did not
finish, a toolchain failure, a sandbox that could not be built, a process
that outlived its run) carries ``infra_error``; evaluate_code never grades
it as the student's.
"""

from __future__ import annotations

import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any, Callable, Optional

from tutor.config import get_config
from tutor.paths import CURRICULUM_DIR

_HEAD = 2000             # max chars of any captured stream kept in the facts
_OUTPUT_CAP = 64 * 1024  # bytes read from each stream of a run; more ends it
_PIPE_GRACE = 5          # seconds a stream may stay open after the process is gone
_COMPILE_TIMEOUT = 30    # wall-clock seconds for the compiler
_COMPILE_FSIZE = 64 * 1024 * 1024

# Smoke run used when an exercise has no test cases: prove the program runs at
# all on empty stdin. Read-only at the call site (only spec.get(...)), so the
# shared module-level dict is safe to reference without copying.
_SMOKE_TEST_SPEC = {"name": "runs", "stdin": "", "expected": "", "match": "contains"}


# ---------------------------------------------------------------------------
# Capability + config
# ---------------------------------------------------------------------------

def code_exec_enabled() -> bool:
    return bool(get_config().get("enable_code_exec"))


def _backend() -> str:
    return get_config().get("code_exec_backend", "subprocess")


def _compiler() -> Optional[str]:
    for cc in ("g++", "clang++"):
        path = shutil.which(cc)
        if path:
            return path
    return None


def _refusal() -> str:
    """Why the subprocess backend will not run here, or "" (finding B6): it
    never runs beside a production database."""
    if _backend() == "subprocess" and os.getenv("DATABASE_URL", "").strip():
        return ("the subprocess backend does not run where DATABASE_URL is set; "
                "hosted execution waits for the E2B backend")
    return ""


# ---------------------------------------------------------------------------
# rlimit child setup (POSIX only)
# ---------------------------------------------------------------------------

_FORK_HEADROOM = 64   # processes a run may add beyond what its uid already owns


def _uid_task_count() -> int:
    """How many tasks (processes and their threads) the real uid owns now,
    read from /proc; 0 where there is no /proc. RLIMIT_NPROC is checked
    against this count, so a cap has to sit above it."""
    uid = str(os.getuid())
    try:
        pids = [p for p in os.listdir("/proc") if p.isdigit()]
    except OSError:
        return 0
    count = 0
    for pid in pids:
        try:
            with open(f"/proc/{pid}/status", encoding="ascii", errors="replace") as fh:
                mine, threads = False, 1
                for line in fh:
                    if line.startswith("Uid:"):
                        mine = line.split()[1] == uid
                    elif line.startswith("Threads:"):
                        threads = int(line.split()[1])
                        break
        except (OSError, ValueError, IndexError):
            continue
        if mine:
            count += threads
    return count


def _make_preexec(cpu_sec: int, *, fsize: int = 0, nofile: int = 64, as_mb: int = 0):
    """Return a preexec_fn that drops the child into a new session and clamps
    rlimits: CPU seconds, file size (0: no file may be written), processes,
    open files, and, when ``as_mb`` is set, address space. None on platforms
    without os.setsid / resource."""
    try:
        import resource
    except ImportError:  # pragma: no cover - non-POSIX
        return None

    # Fork bombs, on Linux only. RLIMIT_NPROC is checked against every task
    # the real uid already owns, threads included (pytest's chromadb and
    # onnxruntime pools, a CI runner's agent, uvicorn's workers), so the cap
    # is that count, taken now in the parent, plus the headroom: a bomb still
    # gets at most 64 processes, and the compiler's first fork no longer fails
    # with EAGAIN on a busy host, which CI showed as "Cannot fork" whenever
    # the test order left enough threads alive before the sandbox tests. On
    # macOS there is no /proc and a desktop uid owns hundreds of processes,
    # so no cap (test_fork_bomb_contained is @linux_only for the same reason).
    nproc = getattr(resource, "RLIMIT_NPROC", None)
    nproc_cap = 0
    if nproc is not None and sys.platform.startswith("linux"):
        nproc_cap = _uid_task_count() + _FORK_HEADROOM

    def _clamp(res, cap: int) -> None:
        try:
            _soft, hard = resource.getrlimit(res)
            new_hard = cap if hard == resource.RLIM_INFINITY else min(cap, hard)
            resource.setrlimit(res, (new_hard, new_hard))
        except (ValueError, OSError):
            pass

    def _limit():
        os.setsid()  # own process group so a timeout can kill the whole tree
        # CPU seconds: hard stop even if wall-clock kill is delayed.
        resource.setrlimit(resource.RLIMIT_CPU, (cpu_sec, cpu_sec + 1))
        _clamp(resource.RLIMIT_FSIZE, fsize)
        _clamp(resource.RLIMIT_NOFILE, nofile)
        if nproc_cap:
            _clamp(nproc, nproc_cap)
        if as_mb:
            _clamp(resource.RLIMIT_AS, as_mb * 1024 * 1024)

    return _limit


# ---------------------------------------------------------------------------
# Namespaces
# ---------------------------------------------------------------------------

_UNSHARE = ("unshare", "--user", "--map-root-user", "--mount", "--pid", "--fork",
            "--kill-child", "--net", "--ipc", "--uts")

# Run by sh inside the new namespaces, as root of the new user namespace. $1 is
# the run's work directory, $2 an empty directory for the new root, and the
# rest the command. Once the root is built it writes _READY as the first line
# of stderr, then runs the command as a child (not as PID 1, so its signals
# behave normally) and exits with its status (128+S for a signal S). A command
# cannot forge the marker: it never runs when the setup fails.
_READY = "tutor-sandbox-ready"
_ENTER = r"""
set -eu
PATH=/usr/sbin:/usr/bin:/sbin:/bin
work=$1 root=$2
shift 2
mount -t tmpfs -o size=16m,mode=755 tmpfs "$root"
mkdir -p "$root/usr" "$root/etc" "$root/work" "$root/tmp" "$root/proc" "$root/dev" "$root/.old"
mount --rbind /usr "$root/usr"
mount -o remount,bind,ro "$root/usr"
for l in bin lib lib32 lib64 libx32 sbin; do
  if [ -L "/$l" ]; then
    ln -s "$(readlink "/$l")" "$root/$l"
  elif [ -d "/$l" ]; then
    mkdir -p "$root/$l"
    mount --rbind "/$l" "$root/$l"
    mount -o remount,bind,ro "$root/$l"
  fi
done
for f in ld.so.cache ld.so.conf; do
  if [ -f "/etc/$f" ]; then
    touch "$root/etc/$f"
    mount --bind "/etc/$f" "$root/etc/$f"
  fi
done
for d in ld.so.conf.d alternatives; do
  if [ -d "/etc/$d" ]; then
    mkdir -p "$root/etc/$d"
    mount --rbind "/etc/$d" "$root/etc/$d"
    mount -o remount,bind,ro "$root/etc/$d"
  fi
done
mount --bind "$work" "$root/work"
mount -t proc proc "$root/proc"
for n in null zero urandom; do
  touch "$root/dev/$n"
  mount --bind "/dev/$n" "$root/dev/$n"
done
mount -t tmpfs -o size=64m tmpfs "$root/tmp"
cd "$root"
pivot_root . .old
umount -l /.old
rmdir /.old
cd /work
PATH=/usr/bin:/bin
echo "__READY__" >&2
set +e
setpriv --no-new-privs --bounding-set=-all --inh-caps=-all -- "$@"
exit $?
""".replace("__READY__", _READY)


@lru_cache(maxsize=1)
def _supports_namespaces() -> bool:
    """Probe once: can this host build the sandbox? It needs unprivileged user
    namespaces, util-linux (unshare, mount, pivot_root, setpriv), and a fresh
    /proc (a container that masks parts of /proc refuses one)."""
    if not sys.platform.startswith("linux") or not shutil.which("unshare"):
        return False
    tmp = tempfile.mkdtemp(prefix="tutor-probe-")
    try:
        run = _execute(["true"], tmp, stdin="", timeout=10, preexec=_make_preexec(10),
                       env=_env("/work", 64), isolated=True)
        return run.ready and run.returncode == 0
    except OSError:
        return False
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


@lru_cache(maxsize=1)
def _supports_network_wrap() -> bool:
    """Probe once whether unshare -rn is usable: the network drop for a host
    without the full sandbox."""
    if sys.platform.startswith("linux") and shutil.which("unshare"):
        try:
            probe = subprocess.run(["unshare", "-rn", "true"], capture_output=True, timeout=10)
        except (OSError, subprocess.SubprocessError):
            return False
        return probe.returncode == 0
    return False


def _network_wrap(argv: list[str]) -> list[str]:
    """Wrap argv in `unshare -rn` to drop networking when available (Linux)."""
    if _supports_network_wrap():
        return ["unshare", "-rn", *argv]
    return argv


# ---------------------------------------------------------------------------
# One command, contained
# ---------------------------------------------------------------------------

@dataclass
class _Run:
    out: str = ""
    err: str = ""
    returncode: Optional[int] = None   # negative: killed by that signal
    timed_out: bool = False
    output_capped: bool = False        # a stream passed _OUTPUT_CAP and the run was ended
    contained: bool = True             # False: a stream stayed open after the kill
    ready: bool = True                 # False: the sandbox could not be built


def _kill_group(proc: "subprocess.Popen", own_session: bool) -> None:
    """Kill a runaway child and everything in its process group. In the
    sandbox that is unshare and the namespace's first process, whose death
    takes every other process in the namespace with it (--kill-child and the
    PID namespace), including one that left the group with setsid()."""
    try:
        if own_session and hasattr(os, "killpg"):
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        else:
            proc.kill()
    except (ProcessLookupError, OSError):
        pass  # already gone


def _drain(pipe, sink: bytearray, over: threading.Event) -> None:
    """Read a stream to its end, keeping at most _OUTPUT_CAP bytes."""
    try:
        while True:
            chunk = pipe.read1(65536)
            if not chunk:
                return
            room = _OUTPUT_CAP - len(sink)
            if room > 0:
                sink += chunk[:room]
            if len(chunk) > room:
                over.set()
    except (OSError, ValueError):
        return


def _feed(pipe, data: bytes) -> None:
    try:
        pipe.write(data)
    except (BrokenPipeError, OSError):
        pass
    finally:
        try:
            pipe.close()
        except OSError:
            pass


def _execute(argv: list[str], tmpdir: str, *, stdin: str, timeout: float, preexec: Optional[Callable],
             env: dict, isolated: bool) -> _Run:
    """Run ``argv`` in the sandbox (``isolated``) or directly, with a wall-clock
    ``timeout`` and capped output; kill the whole group on a timeout or an
    output flood. The work directory is ``tmpdir/work``; the sandbox builds its
    root on ``tmpdir/root``."""
    work = os.path.join(tmpdir, "work")
    os.makedirs(work, exist_ok=True)
    if isolated:
        root = os.path.join(tmpdir, "root")
        os.makedirs(root, exist_ok=True)
        full = [*_UNSHARE, "sh", "-c", _ENTER, "sh", work, root, *argv]
    else:
        full = _network_wrap(list(argv))
    proc = subprocess.Popen(
        full, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        cwd=work, env=env, preexec_fn=preexec,
    )

    out_buf, err_buf = bytearray(), bytearray()
    over = threading.Event()
    readers = [threading.Thread(target=_drain, args=(proc.stdout, out_buf, over), daemon=True),
               threading.Thread(target=_drain, args=(proc.stderr, err_buf, over), daemon=True)]
    for t in readers:
        t.start()
    threading.Thread(target=_feed, args=(proc.stdin, stdin.encode()), daemon=True).start()

    deadline = time.monotonic() + timeout
    stopped = ""
    while True:
        try:
            proc.wait(timeout=0.05)
            break
        except subprocess.TimeoutExpired:
            if over.is_set():
                stopped = "output"
            elif time.monotonic() >= deadline:
                stopped = "timeout"
            else:
                continue
            _kill_group(proc, preexec is not None)
            try:
                proc.wait(timeout=_PIPE_GRACE)
            except subprocess.TimeoutExpired:
                pass
            break
    grace = time.monotonic() + _PIPE_GRACE
    for t in readers:
        t.join(timeout=max(0.0, grace - time.monotonic()))

    run = _Run(out=out_buf.decode(errors="replace"), err=err_buf.decode(errors="replace"),
               output_capped=over.is_set(), contained=not any(t.is_alive() for t in readers),
               returncode=proc.returncode)
    if isolated:
        head, sep, rest = run.err.partition("\n")
        run.ready = bool(sep) and head == _READY
        if run.ready:
            run.err = rest
        # The script's own exit status is the command's; 128+S was signal S.
        # Killed by us, unshare itself died by a signal (negative).
        if run.returncode is not None and run.returncode > 128:
            run.returncode = -(run.returncode - 128)
    # RLIMIT_CPU (SIGXCPU) can beat the wall-clock timer on a loaded machine:
    # the same outcome for the student, stopped for running too long.
    cpu_capped = run.returncode is not None and run.returncode < 0 and -run.returncode == signal.SIGXCPU
    run.timed_out = stopped == "timeout" or cpu_capped
    return run


# ---------------------------------------------------------------------------
# The toolchain
# ---------------------------------------------------------------------------

def _env(home: str, mem_mb: int) -> dict:
    """Minimal, hostile-friendly environment for the compiler and the program."""
    return {
        "PATH": "/usr/bin:/bin:/usr/local/bin",
        "HOME": home,
        "TMPDIR": "/tmp",
        "ASAN_OPTIONS": (
            f"max_allocation_size_mb={mem_mb}:hard_rss_limit_mb={mem_mb}:"
            "allocator_may_return_null=0:detect_leaks=0:abort_on_error=1:exitcode=134"
        ),
        "UBSAN_OPTIONS": "abort_on_error=1",
    }


def _compile_preexec() -> Optional[Callable]:
    return _make_preexec(_COMPILE_TIMEOUT * 2, fsize=_COMPILE_FSIZE, nofile=256)


@lru_cache(maxsize=4)
def _supports_asan(cc: str, isolated: bool) -> bool:
    """Whether a sanitized build compiles, links, and runs here, the way a
    real run does (finding B9: a syntax-only check passed where libasan was
    missing at link time, and every run then failed as the student's).
    Memoized: an enabled sandbox would otherwise pay this build on every grade."""
    tmp = tempfile.mkdtemp(prefix="tutor-probe-")
    try:
        work = Path(tmp) / "work"
        work.mkdir()
        (work / "probe.cpp").write_text("int main() { return 0; }\n")
        inside = "/work" if isolated else str(work)
        env = _env(inside, 256)
        build = _execute([cc, "-std=c++17", "-fsanitize=address,undefined", "-o", f"{inside}/probe",
                          f"{inside}/probe.cpp"], tmp, stdin="", timeout=_COMPILE_TIMEOUT,
                         preexec=_compile_preexec(), env=env, isolated=isolated)
        if not (build.ready and build.returncode == 0):
            return False
        run = _execute([f"{inside}/probe"], tmp, stdin="", timeout=10, preexec=_make_preexec(10),
                       env=env, isolated=isolated)
        return run.ready and run.returncode == 0
    except OSError:
        return False
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# A compile that failed for a reason that is not the student's code.
_TOOLCHAIN_FAILURE = re.compile(
    r"cannot find -l|cannot find '|cannot execute|failed to execute|error while loading shared libraries"
    r"|Killed signal terminated program|internal compiler error|No space left on device"
    r"|cannot open output file")


def _compile_infra_error(comp: _Run) -> str:
    """Why a compile says nothing about the student's code, or ""."""
    if not comp.ready:
        return "the sandbox could not be built: " + (comp.err.strip().splitlines() or ["no detail"])[-1][:200]
    if not comp.contained:
        return "a compiler process outlived the compile"
    if comp.timed_out:
        return f"compilation did not finish in {_COMPILE_TIMEOUT} s"
    found = _TOOLCHAIN_FAILURE.search(comp.err or comp.out or "")
    if comp.returncode != 0 and found:
        line = next((ln for ln in (comp.err or comp.out).splitlines() if found.group(0) in ln), found.group(0))
        return "the toolchain failed: " + line.strip()[:200]
    return ""


# ---------------------------------------------------------------------------
# Facts schema
# ---------------------------------------------------------------------------

def _facts(
    *,
    enabled: bool,
    backend: str,
    compiled: bool = False,
    compile_error: str = "",
    timed_out: bool = False,
    tests: Optional[list[dict]] = None,
    stderr_head: str = "",
    note: str = "",
    isolation: str = "none",
    network_isolated: bool = False,
    infra_error: str = "",
) -> dict[str, Any]:
    tests = tests or []
    return {
        "enabled": enabled,
        "backend": backend,
        "compiled": compiled,
        "compile_error": compile_error[:_HEAD],
        "timed_out": timed_out,
        "tests": tests,
        "all_passed": bool(compiled and tests and all(t["passed"] for t in tests)),
        "stderr_head": stderr_head[:_HEAD],
        "note": note,
        # How the run was contained (findings B6, B8): "namespaces" or "none".
        "isolation": isolation,
        "network_isolated": network_isolated,
        # Why nothing here is the student's result (finding B9), or "".
        "infra_error": infra_error,
    }


def boot_warnings() -> list[str]:
    """Boot warnings when code execution is on with less containment than
    docs/sandbox.md describes (finding B8), or refused (finding B6)."""
    if not code_exec_enabled() or _backend() != "subprocess":
        return []
    refusal = _refusal()
    if refusal:
        return [f"ENABLE_CODE_EXEC is on, but {refusal}: no student code will run."]
    if not _supports_namespaces():
        network = "dropped" if _supports_network_wrap() else "NOT dropped"
        return ["ENABLE_CODE_EXEC is on without namespaces: the compiler and student programs can "
                f"read any file the app user can, and the network is {network} (docs/sandbox.md)."]
    return []


def status_line() -> str:
    """The admin health page's line for the sandbox."""
    if not code_exec_enabled():
        return "off (no student code runs)"
    backend = _backend()
    if backend != "subprocess":
        return f"on ({backend})"
    if _refusal():
        return "on, but refused: DATABASE_URL is set"
    if _supports_namespaces():
        return "on (subprocess, namespaces)"
    return "on (subprocess, NO namespaces: programs can read the app's files)"


# ---------------------------------------------------------------------------
# Test-case loading
# ---------------------------------------------------------------------------

def _tests_path() -> Path:
    return CURRICULUM_DIR / "exercise_tests.json"


def load_exercise_tests(exercise_id: str) -> list[dict]:
    """Return the test cases for an exercise id, or [] if none are defined.

    Each test is ``{"name", "stdin", "expected", "match"?}`` where match is
    "exact" (default, whitespace-normalised) or "contains".
    """
    import json
    path = _tests_path()
    if not path.exists():
        return []
    data = json.loads(path.read_text())
    entry = data.get(exercise_id)
    if not entry:
        return []
    return entry.get("tests", [])


def _normalise(text: str) -> str:
    """Strip trailing whitespace on every line and trailing blank lines, so a
    test's expected output is not defeated by a stray newline."""
    lines = [ln.rstrip() for ln in text.replace("\r\n", "\n").split("\n")]
    while lines and lines[-1] == "":
        lines.pop()
    return "\n".join(lines)


def _check(expected: str, got: str, match: str) -> bool:
    if match == "contains":
        return _normalise(expected) in _normalise(got)
    return _normalise(expected) == _normalise(got)


# ---------------------------------------------------------------------------
# run_code
# ---------------------------------------------------------------------------

def run_code(
    student_code: str,
    exercise_id: str = "",
    tests: Optional[list[dict]] = None,
    *,
    timeout_sec: Optional[int] = None,
    mem_mb: Optional[int] = None,
) -> dict[str, Any]:
    """Compile and run ``student_code`` against ``tests``; return facts only.

    ``tests`` defaults to the exercise's entry in exercise_tests.json. When code
    execution is disabled (the default), returns immediately with
    ``enabled=False`` and runs nothing.
    """
    cfg = get_config()
    backend = _backend()

    if not code_exec_enabled():
        return _facts(enabled=False, backend="disabled",
                      note="code execution disabled (ENABLE_CODE_EXEC unset)")

    if backend == "e2b":  # pragma: no cover - not wired until before pilot
        return _facts(enabled=True, backend="e2b", note="E2B backend not configured",
                      infra_error="E2B backend not configured")

    refusal = _refusal()
    if refusal:
        return _facts(enabled=True, backend=backend, note=refusal, infra_error=refusal)

    cc = _compiler()
    if not cc:
        return _facts(enabled=True, backend=backend, note="no C++ compiler found on PATH",
                      infra_error="no C++ compiler found on PATH")

    if tests is None:
        tests = load_exercise_tests(exercise_id)

    timeout_sec = int(timeout_sec or cfg.get("code_exec_timeout_sec", 5))
    mem_mb = int(mem_mb or cfg.get("code_exec_mem_mb", 256))
    isolated = _supports_namespaces()
    sanitized = _supports_asan(cc, isolated)
    contained = {"isolation": "namespaces" if isolated else "none",
                 "network_isolated": isolated or _supports_network_wrap()}

    tmpdir = tempfile.mkdtemp(prefix="tutor-run-")
    try:
        work = Path(tmpdir) / "work"
        work.mkdir()
        (work / "main.cpp").write_text(student_code)
        inside = "/work" if isolated else str(work)
        src, exe = f"{inside}/main.cpp", f"{inside}/prog"
        env = _env(inside, mem_mb)

        flags = ["-std=c++17", "-O0"]
        if sanitized:
            flags += ["-fsanitize=address,undefined", "-fno-omit-frame-pointer"]
        comp = _execute([cc, *flags, "-o", exe, src], tmpdir, stdin="", timeout=_COMPILE_TIMEOUT,
                        preexec=_compile_preexec(), env=env, isolated=isolated)
        infra = _compile_infra_error(comp)
        if infra:
            return _facts(enabled=True, backend=backend, note=infra, infra_error=infra, **contained)
        if comp.returncode != 0:
            # The student's own file is "main.cpp" in what they are shown.
            error = (comp.err or comp.out).replace(src, "main.cpp")
            return _facts(enabled=True, backend=backend, compiled=False, compile_error=error, **contained)

        # Without sanitizers nothing else bounds memory, so cap the address space.
        preexec = _make_preexec(timeout_sec, as_mb=0 if sanitized else mem_mb + 256)

        # No test cases: a "smoke" run with empty stdin proves it runs at all.
        run_specs = tests if tests else [_SMOKE_TEST_SPEC]

        results: list[dict] = []
        any_timeout = False
        last_stderr = ""
        for spec in run_specs:
            name = spec.get("name", "test")
            expected = spec.get("expected", "")
            run = _execute([exe], tmpdir, stdin=spec.get("stdin", ""), timeout=timeout_sec,
                           preexec=preexec, env=env, isolated=isolated)
            if not run.ready or not run.contained:
                infra = ("the sandbox could not be built" if not run.ready
                         else "a process from the program outlived its run")
                return _facts(enabled=True, backend=backend, compiled=True, note=infra,
                              infra_error=infra, **contained)
            last_stderr = run.err or last_stderr
            if run.timed_out:
                any_timeout = True
                passed = False
            elif run.output_capped:
                passed = False
            elif not tests:
                # An empty-contract smoke test passes iff the program exited 0.
                passed = run.returncode == 0
            else:
                passed = run.returncode == 0 and _check(expected, run.out, spec.get("match", "exact"))
            results.append({
                "name": name,
                "passed": bool(passed),
                "expected": (expected or "")[:_HEAD],
                "got": (run.out or "")[:_HEAD],
                "timed_out": run.timed_out,
                "output_capped": run.output_capped,
            })

        return _facts(
            enabled=True, backend=backend, compiled=True,
            timed_out=any_timeout,
            tests=results if tests else [],
            stderr_head=last_stderr,
            note="" if tests else "no test cases; smoke run only",
            **contained,
        )
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)
