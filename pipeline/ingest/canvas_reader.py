"""Download course materials from Canvas LMS via REST API."""

from __future__ import annotations

import re

import json
import time
from pathlib import Path
from typing import Iterator
from urllib.parse import urlencode

import urllib.request
import urllib.error

from pipeline.config import get_canvas_config

# config.yaml's canvas section (plan step 5.8, finding E20: it used to be
# ignored in favor of these two constants).
CANVAS_BASE_URL = get_canvas_config()["base_url"].rstrip("/")
TOKEN_FILE = get_canvas_config()["token_file"]
SUPPORTED_DOWNLOAD_TYPES = {".pptx", ".pdf", ".docx", ".txt", ".png", ".jpg"}


def _load_token(token_file: Path | None = None) -> str:
    path = token_file or TOKEN_FILE
    token = path.read_text().strip()
    if not token:
        raise ValueError(f"Canvas token is empty at {path}")
    return token


def _api_get(endpoint: str, token: str, params: dict | None = None) -> list | dict:
    url = f"{CANVAS_BASE_URL}/api/v1{endpoint}"
    if params:
        url += "?" + urlencode(params)
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode())


def _paginate(endpoint: str, token: str, per_page: int = 100) -> Iterator[dict]:
    page = 1
    while True:
        results = _api_get(endpoint, token, {"per_page": per_page, "page": page})
        if not results:
            break
        if isinstance(results, dict):
            yield results
            break
        for item in results:
            yield item
        if len(results) < per_page:
            break
        page += 1


def list_courses(token_file: Path | None = None) -> list[dict]:
    token = _load_token(token_file)
    courses = list(_paginate("/courses", token))
    return [
        {
            "id": c["id"],
            "name": c.get("name", ""),
            "course_code": c.get("course_code", ""),
            "sis_course_id": c.get("sis_course_id", ""),
        }
        for c in courses
        if "name" in c
    ]


def list_course_files(course_id: int, token_file: Path | None = None) -> list[dict]:
    token = _load_token(token_file)
    files: list[dict] = []
    for f in _paginate(f"/courses/{course_id}/files", token):
        files.append({
            "id": f["id"],
            "filename": f.get("filename", ""),
            "display_name": f.get("display_name", ""),
            "url": f.get("url", ""),
            "size": f.get("size", 0),
            "content_type": f.get("content_type", ""),
            "folder_id": f.get("folder_id"),
        })
    return files


def download_file(url: str, dest_path: Path, token: str) -> None:
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        dest_path.write_bytes(resp.read())


def download_course_materials(
    course_id: int,
    output_dir: Path,
    extensions: set[str] | None = None,
    token_file: Path | None = None,
    verbose: bool = False,
) -> list[Path]:
    if extensions is None:
        extensions = SUPPORTED_DOWNLOAD_TYPES
    token = _load_token(token_file)
    output_dir.mkdir(parents=True, exist_ok=True)

    files = list_course_files(course_id, token_file)
    downloaded: list[Path] = []

    for f in files:
        filename = f["filename"] or f["display_name"]
        suffix = Path(filename).suffix.lower()
        if suffix not in extensions:
            continue
        dest = output_dir / filename
        if dest.exists():
            if verbose:
                print(f"  Skipping (exists): {filename}")
            downloaded.append(dest)
            continue
        try:
            if verbose:
                print(f"  Downloading: {filename} ({f['size']:,} bytes)")
            download_file(f["url"], dest, token)
            downloaded.append(dest)
            time.sleep(0.2)  # be polite to Canvas API
        except urllib.error.URLError as e:
            print(f"  ERROR downloading {filename}: {e}")

    return downloaded


def find_course(match: str, token_file: Path | None = None) -> dict | None:
    """The first Canvas course whose name or course code contains ``match``
    (case and spaces ignored): pass your course's label from courses.yaml
    ("AST 101") or its catalog number ("101")."""
    needle = re.sub(r"\s+", "", match).lower()
    if not needle:
        return None
    for c in list_courses(token_file):
        name = re.sub(r"\s+", "", c.get("name") or "").lower()
        code = re.sub(r"\s+", "", c.get("course_code") or "").lower()
        if needle in name or needle in code:
            return c
    return None
