"""Reports as markdown, CSV, JSON, or one self-contained HTML page
(step 3.6).

A report is a title, a few lines of context (the window, the thresholds),
and sections, each a heading with a paragraph or a table (a list of dicts,
one per row). Markdown goes to the terminal; CSV carries the first table
with a row per line (and the other tables after a blank line, each headed
by its name); JSON is the whole report; HTML is one page with plain
styles, for sharing.
"""

from __future__ import annotations

import csv
import datetime
import html
import io
import json
from dataclasses import dataclass, field
from typing import Any, Optional, Sequence

FORMATS = ("md", "csv", "json", "html")


@dataclass
class Section:
    heading: str
    rows: list = field(default_factory=list)
    text: str = ""
    columns: Optional[Sequence[str]] = None


@dataclass
class Report:
    title: str
    context: list = field(default_factory=list)       # lines
    sections: list = field(default_factory=list)

    def add(self, heading: str, rows: Optional[list] = None, text: str = "",
            columns: Optional[Sequence[str]] = None) -> "Report":
        self.sections.append(Section(heading, list(rows or []), text, columns))
        return self


def _cell(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, datetime.datetime):
        return value.strftime("%Y-%m-%d %H:%M")
    if isinstance(value, datetime.date):
        return value.isoformat()
    if isinstance(value, float):
        return f"{value:g}"
    return str(value)


def _columns(rows: Sequence[dict], columns: Optional[Sequence[str]]) -> list[str]:
    if columns:
        return list(columns)
    seen: list[str] = []
    for r in rows:
        for k in r:
            if k not in seen:
                seen.append(k)
    return seen


# ---------------------------------------------------------------------------
# Markdown
# ---------------------------------------------------------------------------

def table_md(rows: Sequence[dict], columns: Optional[Sequence[str]] = None) -> str:
    if not rows:
        return "_(nothing in this window)_"
    cols = _columns(rows, columns)
    out = ["| " + " | ".join(cols) + " |", "|" + "|".join("---" for _ in cols) + "|"]
    for r in rows:
        out.append("| " + " | ".join(_cell(r.get(c)).replace("|", "\\|") for c in cols) + " |")
    return "\n".join(out)


def render_md(report: Report) -> str:
    out = [f"# {report.title}", ""]
    out += [line for line in report.context] + ([""] if report.context else [])
    for s in report.sections:
        out.append(f"## {s.heading}")
        out.append("")
        if s.text:
            out += [s.text, ""]
        if s.rows or not s.text:
            out += [table_md(s.rows, s.columns), ""]
    return "\n".join(out).rstrip() + "\n"


# ---------------------------------------------------------------------------
# CSV and JSON
# ---------------------------------------------------------------------------

def render_csv(report: Report) -> str:
    buf = io.StringIO()
    writer = csv.writer(buf)
    first = True
    for s in report.sections:
        if not s.rows:
            continue
        if not first:
            writer.writerow([])
        writer.writerow([f"# {s.heading}"])
        cols = _columns(s.rows, s.columns)
        writer.writerow(cols)
        for r in s.rows:
            writer.writerow([_cell(r.get(c)) for c in cols])
        first = False
    return buf.getvalue()


def _jsonable(value: Any) -> Any:
    if isinstance(value, (datetime.datetime, datetime.date)):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_jsonable(v) for v in value]
    return value


def render_json(report: Report) -> str:
    payload = {
        "title": report.title, "context": list(report.context),
        "sections": [{"heading": s.heading, "text": s.text, "rows": _jsonable(s.rows)} for s in report.sections],
    }
    return json.dumps(payload, indent=2, default=str) + "\n"


# ---------------------------------------------------------------------------
# HTML
# ---------------------------------------------------------------------------

_CSS = """
body { font: 15px/1.45 system-ui, -apple-system, sans-serif; margin: 2rem auto; max-width: 72rem; padding: 0 1rem; color: #1f2937; }
h1 { font-size: 1.5rem; } h2 { font-size: 1.15rem; margin-top: 2rem; }
p.ctx { color: #4b5563; margin: 0.2rem 0; }
table { border-collapse: collapse; margin: 0.5rem 0 1rem; font-size: 14px; }
th, td { border: 1px solid #d1d5db; padding: 0.3rem 0.6rem; text-align: left; vertical-align: top; }
th { background: #f3f4f6; } td.num { text-align: right; font-variant-numeric: tabular-nums; }
tr:nth-child(even) td { background: #fafafa; }
"""


def table_html(rows: Sequence[dict], columns: Optional[Sequence[str]] = None) -> str:
    if not rows:
        return "<p><em>(nothing in this window)</em></p>"
    cols = _columns(rows, columns)
    out = ["<table><thead><tr>" + "".join(f"<th>{html.escape(c)}</th>" for c in cols) + "</tr></thead><tbody>"]
    for r in rows:
        cells = []
        for c in cols:
            v = r.get(c)
            cls = ' class="num"' if isinstance(v, (int, float)) and not isinstance(v, bool) else ""
            cells.append(f"<td{cls}>{html.escape(_cell(v))}</td>")
        out.append("<tr>" + "".join(cells) + "</tr>")
    out.append("</tbody></table>")
    return "".join(out)


def render_html(report: Report) -> str:
    parts = ["<!DOCTYPE html><html lang=\"en\"><head><meta charset=\"utf-8\">",
             f"<title>{html.escape(report.title)}</title><style>{_CSS}</style></head><body>",
             f"<h1>{html.escape(report.title)}</h1>"]
    parts += [f"<p class=\"ctx\">{html.escape(line)}</p>" for line in report.context]
    for s in report.sections:
        parts.append(f"<h2>{html.escape(s.heading)}</h2>")
        if s.text:
            parts.append(f"<p>{html.escape(s.text)}</p>")
        if s.rows or not s.text:
            parts.append(table_html(s.rows, s.columns))
    parts.append("</body></html>\n")
    return "".join(parts)


def render(report: Report, fmt: str) -> str:
    if fmt == "md":
        return render_md(report)
    if fmt == "csv":
        return render_csv(report)
    if fmt == "json":
        return render_json(report)
    if fmt == "html":
        return render_html(report)
    raise ValueError(f"unknown format {fmt!r}; one of {', '.join(FORMATS)}")
