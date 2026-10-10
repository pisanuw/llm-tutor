# Code execution sandbox (`run_code`)

For a course whose students write code, the tutor can compile and run a
submission against fixed test cases. `tutor/agent/run_code.py` does it and
returns **facts only**: did it compile, which tests passed, a truncated
stderr, and how the run was contained. `evaluate_code` scores from those
facts, so correctness comes from the compiler and the tests, not from the
model.

The sandbox compiles **C++ only** (with `g++`, else `clang++`, found on
`PATH`; with neither, every run is an infrastructure miss). Set `code_language: C++` in
`courses.yaml` for a course that uses it; any other value labels the code box
but nothing runs the code, and `evaluate_code` grades from its rubric alone.

## Off by default, and never beside production data

Execution is gated on `ENABLE_CODE_EXEC=true`. Unset (the default, and the
right setting for a hosted deployment), `run_code` returns
`{"enabled": false, ...}` and runs nothing.

The subprocess backend also **refuses to run where `DATABASE_URL` is set**,
so it never runs next to production data: with the switch on beside a
database, every run is reported as an infrastructure miss and the app logs a
boot warning. The production image has no compiler either. Hosted execution
is reserved for a separate backend (`CODE_EXEC_BACKEND=e2b`, a remote sandbox
service), which is **not wired**: selecting it reports "not configured" and
runs nothing. In practice the sandbox is for local use: a single machine,
SQLite, and a trusted operator trying exercises.

The admin Health tab's "Code execution sandbox" row says what would happen
(off; on with namespaces; on without them; or refused beside a database).

Settings (`tutor/config.py`):

| env | default | meaning |
|-----|---------|---------|
| `ENABLE_CODE_EXEC` | `false` | master switch; nothing runs unless `true` |
| `CODE_EXEC_BACKEND` | `subprocess` | `subprocess` (local) or `e2b` (reserved, not wired) |
| `CODE_EXEC_TIMEOUT_SEC` | `5` | wall-clock and CPU limit per test run |
| `CODE_EXEC_MEM_MB` | `256` | ASAN's allocation and resident-memory cap (address space without ASAN) |

## What the subprocess backend contains

### Namespaces (Linux)

Where unprivileged user namespaces work (probed once per process), both the
**compiler and the program** run under `unshare` with `--user`,
`--map-root-user`, `--mount`, `--pid`, `--fork`, `--kill-child`, `--net`,
`--ipc`, and `--uts`. A short shell script then builds a new root on a tmpfs:

- `/usr` bind-mounted read-only (with `/bin`, `/lib`, `/lib64`, `/sbin` as the
  symlinks or read-only binds the host has), plus the loader's
  `/etc/ld.so.cache`, `ld.so.conf`, and `/etc/alternatives`;
- the run's work directory at `/work` (the source and the binary);
- a fresh `/proc` for the new PID namespace, `/dev/null`, `/dev/zero`, and
  `/dev/urandom`, and a 64 MB tmpfs `/tmp`.

It switches root with `pivot_root` and detaches the old one, so nothing else
of the host is reachable, and runs the command through
`setpriv --no-new-privs --bounding-set=-all --inh-caps=-all`: no
capabilities, so it cannot mount, `chroot`, or undo any of this. What that
closes:

| what a program might reach | how it is stopped |
|----------------------------|-------------------|
| the app's files (`.env`, a SQLite database, the store), by reading them or through the compiler's `#include` | they are not in the new root |
| another process's environment (`/proc/<app>/environ` holds the API key) | the sandbox's `/proc` shows only its own processes |
| the network | no interfaces (`--net`) |
| a child that calls `setsid()` to escape a process-group kill | it dies with the PID namespace |
| a background helper that holds the output pipe open | it is killed when the program exits |

The script reports `ready`, then the command's exit status, on a descriptor
the command never inherits, so a program cannot fake either. The command runs
as a child of the script, not as PID 1, so its signals (an ASAN abort,
`SIGXCPU`) behave normally.

### Rlimits

Set before `exec`, for the compiler and the program:

| escape | mechanism |
|--------|-----------|
| infinite loop / CPU burn | `RLIMIT_CPU` plus a wall-clock kill (a `SIGXCPU` exit counts as a timeout) |
| fork bomb | `RLIMIT_NPROC`, Linux only: the number of tasks the user already owns plus 64, because the kernel counts every task of the user against the limit |
| file write / disk bomb | `RLIMIT_FSIZE = 0` for the program; 64 MB for the compiler's output |
| fd exhaustion | `RLIMIT_NOFILE` (64; 256 for the compiler) |

### Memory

ASAN reserves a very large shadow region (terabytes of address space), so
`RLIMIT_AS` cannot cap a sanitized program. Instead `ASAN_OPTIONS` sets
`max_allocation_size_mb=<cap>` (an oversized allocation aborts) and
`hard_rss_limit_mb=<cap>` (a program whose resident memory passes the cap is
stopped). A build without sanitizers gets `RLIMIT_AS` of the cap plus 256 MB
instead.

### Output

Each stream is read up to 64 KiB; a program that prints more is stopped at
once rather than at the timeout, and its test fails with `output_capped`.
Only the first 2,000 characters of any stream reach the facts.

### Sanitizers

Whether to build with `-fsanitize=address,undefined` is decided by a probe
that compiles, links, and runs a program the way a real run does. (A probe
that only checked the syntax would pass on a host where `libasan` is missing
at link time, and every real run would then fail as if it were the
student's.)

## Without namespaces

On macOS, or a Linux host that forbids unprivileged user namespaces (Ubuntu
24.04 does by default through AppArmor; the tests workflow lifts it with
`sysctl` on its throwaway runner), the compiler and the program run as the
app user under the rlimits alone, with the network dropped by `unshare -rn`
where that works. They can read any file the app user can. The facts say so
and the app logs a boot warning.

macOS is weaker still: it has no `/proc`, so there is no process cap, and it
refuses `RLIMIT_AS`, so the address-space cap is silently skipped. Do not read
a local pass on a Mac as proof of containment.

## The facts

| key | meaning |
|-----|---------|
| `enabled`, `backend` | whether execution is on, and which backend ran |
| `compiled`, `compile_error` | the compile's outcome; the student's file is named `main.cpp` |
| `tests`, `all_passed`, `timed_out` | each test's `name`, `passed`, `expected`, `got`, `timed_out`, `output_capped` |
| `stderr_head`, `note` | the last non-empty stderr, and a short remark |
| `isolation` | `namespaces` or `none` |
| `network_isolated` | whether the run had no network |
| `infra_error` | why nothing here is the student's result, or empty |

An `infra_error` (a refused backend, a compile that did not finish in 30 s, a
toolchain failure, a sandbox that could not be built, a process that
outlived its run) is never graded: `evaluate_code` falls back to the rubric.

The student is never shown what the program printed (a program could print a
secret it found, and an API key is short), and a compile error is quoted only
when it is about `main.cpp`: an `#include` of another file makes the compiler
quote that file.

## Test cases

`curriculum/exercise_tests.json` maps an exercise id to
`{contract, tests: [{name, stdin, expected, match?}]}`. `match` is `exact`
(whitespace-normalized, the default) or `contains`. With no tests for an id,
`run_code` does a smoke run (compile and run on empty stdin; it passes if the
exit status is 0). Scaffold entries for your code exercises with
`python -m pipeline build-exercise-tests` (ADDING-KNOWLEDGE.md, section 7).
