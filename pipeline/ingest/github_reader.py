"""Clone and extract content from GitHub repositories (course code, example programs).

Uses the GitHub API (no token needed for public repos; GITHUB_TOKEN in .env for private).
The extracted files go to the folder the caller names (``pipeline github``
passes knowledge-base/raw/code, the code collection's folder), one JSON list
per repository.
"""

from __future__ import annotations

import json
import os
import subprocess
from dataclasses import asdict, dataclass
from pathlib import Path


GITHUB_API = "https://api.github.com"

# Source files by extension, with the language each is labeled as.
CODE_LANGUAGES = {
    ".cpp": "cpp", ".cc": "cpp", ".cxx": "cpp", ".hpp": "cpp", ".h": "cpp",
    ".c": "c", ".cs": "csharp", ".java": "java", ".kt": "kotlin", ".scala": "scala",
    ".py": "python", ".r": "r", ".jl": "julia", ".m": "matlab",
    ".js": "javascript", ".ts": "typescript", ".go": "go", ".rs": "rust",
    ".rb": "ruby", ".php": "php", ".swift": "swift", ".hs": "haskell",
    ".sql": "sql", ".sh": "bash",
}
# Files kept as text beside the code (READMEs, notes, build files).
TEXT_EXTS = {".txt", ".md", ".cmake"}
SUPPORTED_CODE_EXTS = set(CODE_LANGUAGES) | TEXT_EXTS


@dataclass
class CodeFile:
    repo: str
    relative_path: str
    content: str
    language: str = "text"


def _github_token() -> str | None:
    return os.environ.get("GITHUB_TOKEN")


def _api_headers() -> dict:
    headers = {"Accept": "application/vnd.github+json"}
    token = _github_token()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def _api_get(endpoint: str) -> dict | list:
    import urllib.request
    url = f"{GITHUB_API}{endpoint}"
    req = urllib.request.Request(url, headers=_api_headers())
    with urllib.request.urlopen(req) as resp:
        return json.loads(resp.read().decode())


def list_org_repos(org: str) -> list[dict]:
    return _api_get(f"/orgs/{org}/repos?per_page=100&type=all")


def clone_repo(repo_url: str, dest_dir: Path, shallow: bool = True) -> Path:
    dest_dir.mkdir(parents=True, exist_ok=True)
    repo_name = repo_url.rstrip("/").split("/")[-1].removesuffix(".git")
    clone_dir = dest_dir / repo_name

    if clone_dir.exists():
        subprocess.run(["git", "-C", str(clone_dir), "pull", "--quiet"], check=False)
        return clone_dir

    cmd = ["git", "clone", "--quiet"]
    if shallow:
        cmd += ["--depth", "1"]
    cmd += [repo_url, str(clone_dir)]

    token = _github_token()
    env = None
    if token and "github.com" in repo_url:
        # Pass credentials via environment so the token never appears in the
        # clone URL (process table, shell history, or cloned repo's .git/config).
        env = {
            **os.environ,
            "GIT_CONFIG_COUNT": "1",
            "GIT_CONFIG_KEY_0": "http.extraHeader",
            "GIT_CONFIG_VALUE_0": f"Authorization: Bearer {token}",
            "GIT_TERMINAL_PROMPT": "0",
        }

    subprocess.run(cmd, check=True, env=env)
    return clone_dir


def extract_code_files(repo_dir: Path, repo_name: str, max_files: int = 200) -> list[CodeFile]:
    files: list[CodeFile] = []
    for path in repo_dir.rglob("*"):
        if not path.is_file():
            continue
        if path.suffix.lower() not in SUPPORTED_CODE_EXTS:
            continue
        if any(part.startswith(".") for part in path.parts):
            continue
        try:
            content = path.read_text(errors="replace")
        except Exception:
            continue
        if len(content) > 50000:
            content = content[:50000] + "\n[... truncated ...]"
        files.append(CodeFile(
            repo=repo_name,
            relative_path=str(path.relative_to(repo_dir)),
            content=content,
            language=CODE_LANGUAGES.get(path.suffix.lower(), "text"),
        ))
        if len(files) >= max_files:
            break
    return files


def save_repo_files(files: list[CodeFile], output_dir: Path, repo_name: str) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    out_path = output_dir / f"github_{repo_name}.json"
    out_path.write_text(
        json.dumps([asdict(f) for f in files], indent=2, ensure_ascii=False)
    )
    return out_path


def ingest_github_repos(
    repo_urls: list[str],
    clone_dir: Path,
    output_dir: Path,
    verbose: bool = False,
) -> list[Path]:
    written: list[Path] = []
    for url in repo_urls:
        repo_name = url.rstrip("/").split("/")[-1].removesuffix(".git")
        if verbose:
            print(f"  Cloning: {url}")
        try:
            repo_dir = clone_repo(url, clone_dir)
            files = extract_code_files(repo_dir, repo_name)
            out = save_repo_files(files, output_dir, repo_name)
            written.append(out)
            if verbose:
                print(f"    {repo_name}: {len(files)} code files -> {out.name}")
        except Exception as e:
            print(f"  ERROR cloning {url}: {e}")
    return written
