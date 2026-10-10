"""The daily-use page: one chart per metric over the days of a
``daily.DailyTable`` (docs/analytics-cookbook.md, section 5).

One self-contained HTML file with no network dependencies: the table is
embedded as JSON and the page draws inline SVG columns, with a range row
(7/14/30/all days) that filters everything below it client-side, a tooltip
and keyboard focus on every day, a table twin behind every chart, and a
dark mode stepped from the same hues. The file holds student ids, so it
belongs with the store, not in the repository.
"""

from __future__ import annotations

import datetime
import html
import json
from typing import Sequence
from zoneinfo import ZoneInfo

from tutor.analytics.daily import DailyTable


def payload(table: DailyTable) -> dict:
    return {
        "days": table.days,
        "active_ids": table.active_ids,
        "metrics": [{"key": m.key, "title": m.title, "desc": m.desc, "unit": m.unit, "group": m.group,
                     "kind": m.kind, "series": [{"name": s.name, "values": s.values} for s in m.series]}
                    for m in table.metrics],
        "tz": table.tz,
        "students": table.students,
        "first_event": table.first_event,
    }


def render(table: DailyTable, title: str, context: Sequence[str], exported: str = "") -> str:
    if table.first_event:
        note = (f"Browser activity (decks, narration, on-screen time) has been recorded since "
                f"{table.first_event}; those charts are empty before that date.")
    else:
        note = ("The store has no browser activity events yet, so the on-screen, deck, narration, "
                "slide, and voice charts are empty.")
    made = datetime.datetime.now(ZoneInfo(table.tz)).strftime("%Y-%m-%d %H:%M %Z")
    subtitle = (context[0] if context else "") + (f" Store exported {exported}; page made {made}." if exported else "")
    note = " ".join(list(context[1:]) + [note])
    data = json.dumps(payload(table), separators=(",", ":")).replace("</", "<\\/")
    return (PAGE.replace("__TITLE__", html.escape(title))
                .replace("__SUBTITLE__", html.escape(subtitle))
                .replace("__NOTE__", html.escape(note))
                .replace("__DATA__", data))


PAGE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>__TITLE__</title>
<style>
:root {
  color-scheme: light;
  --surface: #fcfcfb; --card: #ffffff; --line: #e6e5e1; --grid: #ececea;
  --text: #0b0b0b; --text-2: #52514e; --text-3: #8a8984;
  --s1: #2a78d6; --s2: #eb6834; --tip: #0b0b0b; --tip-text: #ffffff; --focus: #2a78d6;
}
@media (prefers-color-scheme: dark) {
  :root:not([data-theme="light"]) {
    color-scheme: dark;
    --surface: #1a1a19; --card: #222221; --line: #33332f; --grid: #2c2c2a;
    --text: #ffffff; --text-2: #c3c2b7; --text-3: #8f8e86;
    --s1: #3987e5; --s2: #d95926; --tip: #f2f1ec; --tip-text: #0b0b0b; --focus: #3987e5;
  }
}
:root[data-theme="dark"] {
  color-scheme: dark;
  --surface: #1a1a19; --card: #222221; --line: #33332f; --grid: #2c2c2a;
  --text: #ffffff; --text-2: #c3c2b7; --text-3: #8f8e86;
  --s1: #3987e5; --s2: #d95926; --tip: #f2f1ec; --tip-text: #0b0b0b; --focus: #3987e5;
}
* { box-sizing: border-box; }
html, body { margin: 0; }
body { background: var(--surface); color: var(--text);
  font: 15px/1.45 -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif; }
main { max-width: 1200px; margin: 0 auto; padding: 20px 16px 48px; }
h1 { font-size: 22px; margin: 0 0 4px; }
h2 { font-size: 15px; font-weight: 600; color: var(--text-2); margin: 28px 0 10px; text-transform: uppercase; letter-spacing: .04em; }
.sub { color: var(--text-2); margin: 0 0 6px; font-size: 14px; }
.note { color: var(--text-3); margin: 0 0 16px; font-size: 13px; }
.filters { display: flex; flex-wrap: wrap; gap: 6px; align-items: center; margin: 12px 0 18px; }
.filters span { color: var(--text-2); font-size: 13px; margin-right: 4px; }
.filters button { font: inherit; font-size: 13px; padding: 5px 12px; border-radius: 999px; border: 1px solid var(--line);
  background: var(--card); color: var(--text); cursor: pointer; }
.filters button[aria-pressed="true"] { background: var(--text); color: var(--surface); border-color: var(--text); }
.tiles { display: grid; grid-template-columns: repeat(auto-fill, minmax(150px, 1fr)); gap: 10px; }
.tile { background: var(--card); border: 1px solid var(--line); border-radius: 10px; padding: 12px 14px; min-width: 0; }
.tile .l { font-size: 13px; color: var(--text-2); }
.tile .v { font-size: 26px; font-weight: 600; margin-top: 2px; }
.tile.hero .v { font-size: 48px; line-height: 1.05; }
.grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(320px, 1fr)); gap: 12px; }
figure { margin: 0; background: var(--card); border: 1px solid var(--line); border-radius: 10px; padding: 12px 14px 8px; min-width: 0; }
figure header { display: flex; justify-content: space-between; align-items: baseline; gap: 8px; }
figure header .t { font-weight: 600; }
figure header .r { font-size: 13px; color: var(--text-2); white-space: nowrap; }
figure .d { font-size: 12.5px; color: var(--text-3); margin: 0 0 6px; }
figure svg { display: block; width: 100%; height: auto; overflow: visible; }
.legend { display: flex; flex-wrap: wrap; gap: 4px 14px; font-size: 12.5px; color: var(--text-2); margin: 4px 0 2px; }
.legend i { display: inline-block; width: 10px; height: 10px; border-radius: 2px; margin-right: 6px; vertical-align: -1px; }
.foot { display: flex; justify-content: space-between; align-items: center; margin-top: 4px; }
.foot button { font: inherit; font-size: 12px; color: var(--text-2); background: none; border: 0; padding: 4px 0; cursor: pointer; text-decoration: underline; }
.empty { font-size: 13px; color: var(--text-3); padding: 48px 0; text-align: center; }
table { border-collapse: collapse; width: 100%; font-size: 12.5px; margin-top: 6px; }
th, td { text-align: right; padding: 3px 6px; border-top: 1px solid var(--line); font-variant-numeric: tabular-nums; }
th:first-child, td:first-child { text-align: left; }
th { color: var(--text-2); font-weight: 500; }
.tbl-wrap { max-height: 260px; overflow: auto; }
.bar { transition: opacity .1s; }
.hit { fill: transparent; cursor: default; outline: none; }
.hit:focus-visible + .barg .bar, .slot:hover .bar { opacity: .75; }
.gridline { stroke: var(--grid); stroke-width: 1; shape-rendering: crispEdges; }
.axis { stroke: var(--line); stroke-width: 1; shape-rendering: crispEdges; }
.tick { font-size: 11px; fill: var(--text-3); }
.dlabel { font-size: 11px; fill: var(--text-2); font-weight: 600; }
#tip { position: fixed; pointer-events: none; background: var(--tip); color: var(--tip-text); padding: 6px 9px; border-radius: 6px;
  font-size: 12.5px; line-height: 1.35; z-index: 10; display: none; max-width: 240px; }
#tip .dt { opacity: .75; margin-bottom: 2px; }
#tip .row { display: flex; align-items: center; gap: 6px; }
#tip .row b { font-weight: 600; min-width: 3ch; }
#tip .k { display: inline-block; width: 10px; height: 3px; border-radius: 2px; }
</style>
</head>
<body>
<main>
<h1>__TITLE__</h1>
<p class="sub">__SUBTITLE__</p>
<p class="note">__NOTE__</p>
<div class="filters" id="filters" role="group" aria-label="Range"><span>Range</span></div>
<div class="tiles" id="tiles"></div>
<div id="charts"></div>
</main>
<div id="tip" role="tooltip"></div>
<script id="data" type="application/json">__DATA__</script>
<script>
(function () {
  const DATA = JSON.parse(document.getElementById('data').textContent);
  const N = DATA.days.length;
  const PRESETS = [7, 14, 30, 90].filter(p => p < N).concat([N]);
  let range = PRESETS.includes(30) ? 30 : PRESETS[PRESETS.length - 1];
  const tip = document.getElementById('tip');
  const colors = ['var(--s1)', 'var(--s2)'];

  const fmt = {
    int: v => Math.round(v).toLocaleString(),
    min: v => (v >= 10 ? Math.round(v).toLocaleString() : v.toFixed(1)),
    usd: v => '$' + v.toFixed(2),
    s: v => v.toFixed(1) + ' s',
  };
  const unitWord = { int: '', min: ' min', usd: '', s: '' };
  const f = (unit, v) => (v == null ? '–' : fmt[unit](v));

  function dayLabel(iso, long) {
    const d = new Date(iso + 'T12:00:00');
    const opts = long ? { weekday: 'short', month: 'short', day: 'numeric' } : { month: 'numeric', day: 'numeric' };
    return d.toLocaleDateString(undefined, opts);
  }
  function nice(max) {
    if (max <= 0) return 1;
    const p = Math.pow(10, Math.floor(Math.log10(max)));
    for (const m of [1, 2, 2.5, 5, 10]) if (m * p >= max) return m * p;
    return 10 * p;
  }
  const esc = s => String(s);

  function sliceMetric(m) {
    return { ...m, series: m.series.map(s => ({ ...s, values: s.values.slice(N - range) })) };
  }
  const days = () => DATA.days.slice(N - range);

  // ---- filters ----
  const filters = document.getElementById('filters');
  for (const p of PRESETS) {
    const b = document.createElement('button');
    b.textContent = p === N ? `All ${N} days` : `${p} days`;
    b.addEventListener('click', () => { range = p; renderAll(); });
    b.dataset.p = p;
    filters.appendChild(b);
  }

  // ---- tiles ----
  function renderTiles() {
    const el = document.getElementById('tiles');
    el.textContent = '';
    const ids = new Set();
    DATA.active_ids.slice(N - range).forEach(l => l.forEach(x => ids.add(x)));
    const sum = key => {
      const m = DATA.metrics.find(x => x.key === key);
      return m.series.reduce((a, s) => a + s.values.slice(N - range).reduce((b, v) => b + (v || 0), 0), 0);
    };
    const tiles = [
      ['Students active', ids.size.toLocaleString(), true],
      ['Questions', fmt.int(sum('questions'))],
      ['Model cost', fmt.usd(sum('cost_usd'))],
      ['On-screen minutes', fmt.int(sum('active_min'))],
      ['Narration minutes', fmt.int(sum('narration_min'))],
      ['Slides viewed', fmt.int(sum('slides_viewed'))],
    ];
    for (const [l, v, hero] of tiles) {
      const t = document.createElement('div');
      t.className = 'tile' + (hero ? ' hero' : '');
      const a = document.createElement('div'); a.className = 'l'; a.textContent = l;
      const b = document.createElement('div'); b.className = 'v'; b.textContent = v;
      t.append(a, b); el.appendChild(t);
    }
  }

  // ---- charts ----
  const SVG = 'http://www.w3.org/2000/svg';
  const mk = (tag, attrs) => { const e = document.createElementNS(SVG, tag); for (const k in attrs) e.setAttribute(k, attrs[k]); return e; };

  function chart(fig, m, width) {
    const ds = days();
    const n = ds.length;
    const plotH = 150, padT = 16, padB = 22, padL = 40, padR = 8;
    const H = padT + plotH + padB;
    const svg = mk('svg', { viewBox: `0 0 ${width} ${H}`, width, height: H, role: 'img',
                            'aria-label': `${m.title}, daily, last ${n} days` });
    const totals = ds.map((_, i) => m.series.reduce((a, s) => a + (s.values[i] || 0), 0));
    const maxV = Math.max(...totals, 0);
    if (maxV <= 0) {
      const t = mk('text', { x: width / 2, y: padT + plotH / 2, 'text-anchor': 'middle', class: 'tick' });
      t.textContent = 'nothing recorded in this range';
      svg.appendChild(t);
      return svg;
    }
    const intAxis = m.unit === 'int';
    let yMax = nice(maxV);
    if (intAxis && yMax < 2) yMax = 2;
    const plotW = width - padL - padR;
    const y = v => padT + plotH - (v / yMax) * plotH;
    const slot = plotW / n;
    const bw = Math.max(2, Math.min(24, slot - 2));

    // grid + y ticks (0, half, max)
    const ticks = intAxis && yMax % 2 ? [0, yMax] : [0, yMax / 2, yMax];
    for (const v of ticks) {
      svg.appendChild(mk(v === 0 ? 'line' : 'line', { x1: padL, x2: width - padR, y1: y(v), y2: y(v), class: v === 0 ? 'axis' : 'gridline' }));
      const t = mk('text', { x: padL - 6, y: y(v) + 4, 'text-anchor': 'end', class: 'tick' });
      t.textContent = m.unit === 'usd' ? '$' + (Number.isInteger(v) ? v : v.toFixed(2)) : v.toLocaleString();
      svg.appendChild(t);
    }

    // x labels: every day up to 14, else Mondays
    const everyDay = n <= 14;
    ds.forEach((iso, i) => {
      const d = new Date(iso + 'T12:00:00');
      if (everyDay || d.getDay() === 1) {
        const t = mk('text', { x: padL + slot * (i + 0.5), y: H - 6, 'text-anchor': 'middle', class: 'tick' });
        t.textContent = dayLabel(iso, false);
        svg.appendChild(t);
      }
    });

    // bars
    let maxI = totals.indexOf(maxV);
    ds.forEach((iso, i) => {
      const g = mk('g', { class: 'slot' });
      const hit = mk('rect', { x: padL + slot * i, y: padT, width: slot, height: plotH, class: 'hit', tabindex: 0,
                               'aria-label': `${dayLabel(iso, true)}: ` + m.series.map(s => `${s.name} ${f(m.unit, s.values[i])}`).join(', ') });
      const barg = mk('g', { class: 'barg' });
      const x = padL + slot * (i + 0.5) - bw / 2;
      let base = 0;
      m.series.forEach((s, k) => {
        const v = s.values[i];
        if (v == null || v <= 0) return;
        const top = y(base + v), bottom = y(base) - (base > 0 ? 2 : 0);
        const h = Math.max(0, bottom - top);
        const r = Math.min(4, h, bw / 2);
        const isTop = k === m.series.length - 1 || m.series.slice(k + 1).every(t => !(t.values[i] > 0));
        const d = isTop
          ? `M${x},${bottom} V${top + r} a${r},${r} 0 0 1 ${r},-${r} H${x + bw - r} a${r},${r} 0 0 1 ${r},${r} V${bottom} Z`
          : `M${x},${bottom} V${top} H${x + bw} V${bottom} Z`;
        barg.appendChild(mk('path', { d, fill: colors[k % colors.length], class: 'bar' }));
        base += v;
      });
      if (i === maxI && totals[i] > 0) {
        const t = mk('text', { x: padL + slot * (i + 0.5), y: y(totals[i]) - 4, 'text-anchor': 'middle', class: 'dlabel' });
        t.textContent = f(m.unit, totals[i]);
        barg.appendChild(t);
      }
      const show = (ev) => {
        tip.textContent = '';
        const dt = document.createElement('div'); dt.className = 'dt'; dt.textContent = dayLabel(iso, true);
        tip.appendChild(dt);
        m.series.forEach((s, k) => {
          const row = document.createElement('div'); row.className = 'row';
          const key = document.createElement('i'); key.className = 'k'; key.style.background = colors[k % colors.length];
          const b = document.createElement('b'); b.textContent = f(m.unit, s.values[i]) + (m.kind === 'sum' ? unitWord[m.unit] : '');
          const l = document.createElement('span'); l.textContent = s.name;
          row.append(key, b, l); tip.appendChild(row);
        });
        tip.style.display = 'block';
        const rect = hit.getBoundingClientRect();
        let left = rect.left + rect.width / 2 - tip.offsetWidth / 2;
        left = Math.max(8, Math.min(window.innerWidth - tip.offsetWidth - 8, left));
        tip.style.left = left + 'px';
        tip.style.top = Math.max(8, rect.top - tip.offsetHeight - 8) + 'px';
      };
      const hide = () => { tip.style.display = 'none'; };
      hit.addEventListener('pointerenter', show); hit.addEventListener('pointerleave', hide);
      hit.addEventListener('focus', show); hit.addEventListener('blur', hide);
      g.append(hit, barg);
      svg.appendChild(g);
    });
    return svg;
  }

  function table(m) {
    const ds = days();
    const wrap = document.createElement('div'); wrap.className = 'tbl-wrap';
    const t = document.createElement('table');
    const thead = document.createElement('thead'); const hr = document.createElement('tr');
    for (const h of ['day', ...m.series.map(s => s.name)]) { const th = document.createElement('th'); th.textContent = h; hr.appendChild(th); }
    thead.appendChild(hr); t.appendChild(thead);
    const tb = document.createElement('tbody');
    ds.forEach((iso, i) => {
      const tr = document.createElement('tr');
      const td = document.createElement('td'); td.textContent = iso; tr.appendChild(td);
      for (const s of m.series) { const c = document.createElement('td'); c.textContent = f(m.unit, s.values[i]); tr.appendChild(c); }
      tb.appendChild(tr);
    });
    t.appendChild(tb); wrap.appendChild(t);
    return wrap;
  }

  function figure(m) {
    const fig = document.createElement('figure');
    const head = document.createElement('header');
    const tt = document.createElement('span'); tt.className = 't'; tt.textContent = m.title;
    const rr = document.createElement('span'); rr.className = 'r';
    const vals = m.series.flatMap(s => s.values).filter(v => v != null);
    const total = vals.reduce((a, b) => a + b, 0);
    if (m.key === 'active_students') rr.textContent = fmt.int(total) + ' student-days';
    else if (m.kind === 'sum') rr.textContent = 'total ' + (m.unit === 'min' ? fmt.int(total) + ' min' : f(m.unit, total));
    else rr.textContent = vals.length ? 'median ' + f(m.unit, vals.slice().sort((a, b) => a - b)[Math.floor(vals.length / 2)]) : '';
    head.append(tt, rr);
    const d = document.createElement('p'); d.className = 'd'; d.textContent = m.desc;
    fig.append(head, d);
    const holder = document.createElement('div'); fig.appendChild(holder);
    if (m.series.length > 1) {
      const lg = document.createElement('div'); lg.className = 'legend';
      m.series.forEach((s, k) => { const e = document.createElement('span'); const i = document.createElement('i');
        i.style.background = colors[k % colors.length]; e.append(i, document.createTextNode(s.name)); lg.appendChild(e); });
      fig.appendChild(lg);
    }
    const foot = document.createElement('div'); foot.className = 'foot';
    const tb = document.createElement('button'); tb.type = 'button'; tb.textContent = 'Show table';
    let tbl = null;
    tb.addEventListener('click', () => {
      if (tbl) { tbl.remove(); tbl = null; tb.textContent = 'Show table'; }
      else { tbl = table(m); fig.appendChild(tbl); tb.textContent = 'Hide table'; }
    });
    foot.appendChild(tb); fig.appendChild(foot);
    const draw = () => { holder.textContent = ''; holder.appendChild(chart(fig, m, Math.max(240, fig.clientWidth - 28))); };
    fig._draw = draw;
    return fig;
  }

  function renderCharts() {
    const root = document.getElementById('charts');
    root.textContent = '';
    const groups = [];
    for (const m of DATA.metrics) if (!groups.includes(m.group)) groups.push(m.group);
    for (const g of groups) {
      const h = document.createElement('h2'); h.textContent = g; root.appendChild(h);
      const grid = document.createElement('div'); grid.className = 'grid'; root.appendChild(grid);
      for (const m of DATA.metrics.filter(x => x.group === g)) grid.appendChild(figure(sliceMetric(m)));
    }
    requestAnimationFrame(() => document.querySelectorAll('figure').forEach(fg => fg._draw()));
  }

  function renderAll() {
    filters.querySelectorAll('button').forEach(b => b.setAttribute('aria-pressed', String(Number(b.dataset.p) === range)));
    renderTiles();
    renderCharts();
  }
  let rt;
  window.addEventListener('resize', () => { clearTimeout(rt); rt = setTimeout(() => document.querySelectorAll('figure').forEach(fg => fg._draw()), 120); });
  renderAll();
})();
</script>
</body>
</html>
"""
