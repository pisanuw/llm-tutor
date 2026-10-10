/* The map page's script (plan step 5.5, finding D20: it was inline in
   map.html). Needs d3, loaded in the page head; served as
   /static/map.js?v=<content hash>. The page hands it the courses in order
   as window.TUTOR_COURSES ([{id, label, color}], color from course_color). */
// The graph arrives as JSON data in the page (|tojson: no raw "<").
const GRAPH = JSON.parse(document.getElementById("graph-data").textContent);

// The courses in order; without the page's list, the ones the graph names.
const COURSES = (Array.isArray(window.TUTOR_COURSES) && window.TUTOR_COURSES.length)
  ? window.TUTOR_COURSES
  : [...new Set(GRAPH.nodes.map(n => String(n.course)))].map(id => ({id, label: id, color: null}));
const COURSE_INDEX = new Map(COURSES.map((c, i) => [String(c.id), i]));
// A node colored for a course the list does not name keeps the default blue.
const DEFAULT_COLOR = {mid: "#3b82f6", light: "#93c5fd"};

function courseIndex(course) {
  const i = COURSE_INDEX.get(String(course));
  return i === undefined ? COURSES.length : i;
}
function courseLabel(course) {
  const c = COURSES[COURSE_INDEX.get(String(course))];
  return (c && c.label) || String(course);
}
function courseColor(course) {
  const c = COURSES[COURSE_INDEX.get(String(course))];
  return (c && c.color) || DEFAULT_COLOR;
}
// Each course gets its own column across the width: x = W*(i+1)/(n+1).
function courseX(course) {
  const n = Math.max(COURSES.length, 1);
  const i = Math.min(courseIndex(course), n - 1);
  return W() * (i + 1) / (n + 1);
}
// Declared before the nodes are built: their names read both.
const RECOMMENDED_IDS = new Set();
// The words students see for each status (plan step 3.6, finding D24).
const STATUS_WORD = {locked: "Not started", seen: "Started", unlocked: "Practiced", mastered: "Solid"};

// -----------------------------------------------------------------------
// D3 Force simulation
// -----------------------------------------------------------------------
const svg = d3.select("#graph-svg");
const W = () => window.innerWidth;
const H = () => window.innerHeight - 52;

// Arrow marker
svg.append("defs").append("marker")
  .attr("id", "arrow")
  .attr("viewBox", "0 -4 8 8")
  .attr("refX", 18)   // offset past node radius
  .attr("refY", 0)
  .attr("markerWidth", 5)
  .attr("markerHeight", 5)
  .attr("orient", "auto")
  .append("path")
    .attr("d", "M0,-4L8,0L0,4")
    .attr("fill", "#94a3b8");

const g = svg.append("g");  // zoomable group

// Zoom + pan (touch-action: none on the svg hands pinch/pan to these handlers)
const zoomBehavior = d3.zoom()
  .scaleExtent([0.15, 4])
  .on("zoom", e => g.attr("transform", e.transform));
svg.call(zoomBehavior);

const REDUCED_MOTION = window.matchMedia("(prefers-reduced-motion: reduce)").matches;

function fitView() {
  svg.transition().duration(REDUCED_MOTION ? 0 : 250).call(zoomBehavior.transform, d3.zoomIdentity);
}

// Build node map for fast lookup
const nodeById = new Map(GRAPH.nodes.map(n => [n.id, n]));

// Force layout: an x-force pulls each course into its own column.
const sim = d3.forceSimulation(GRAPH.nodes)
  .force("link", d3.forceLink(GRAPH.links)
    .id(d => d.id)
    .distance(60)
    .strength(0.4)
  )
  .force("charge", d3.forceManyBody().strength(-180))
  .force("collide", d3.forceCollide(14))
  .force("x", d3.forceX(d => courseX(d.course)).strength(0.08))
  .force("y", d3.forceY(H() / 2).strength(0.04))
  .alphaDecay(0.025);

// Edges
const link = g.append("g").attr("class", "links")
  .selectAll("line")
  .data(GRAPH.links)
  .join("line")
    .attr("class", "link")
    .attr("marker-end", "url(#arrow)");

// Nodes
const node = g.append("g").attr("class", "nodes")
  .selectAll("g.node")
  .data(GRAPH.nodes)
  .join("g")
    .attr("class", d => `node ${d.status}`)
    .call(d3.drag()
      .on("start", dragStart)
      .on("drag",  dragging)
      .on("end",   dragEnd)
    )
    .on("click", (event, d) => { event.stopPropagation(); openPanel(d, event.currentTarget, false); })
    // Keyboard (plan step 3.4, finding D12): each node is a named button.
    .attr("tabindex", 0)
    .attr("role", "button")
    .attr("aria-label", d => nodeLabel(d))
    .on("keydown", (event, d) => {
      if (event.key === "Enter" || event.key === " ") {
        event.preventDefault();
        openPanel(d, event.currentTarget, true);
      }
    })
    .on("focus", (event, d) => keepInView(d, event.currentTarget));

// Circle — radius by status
node.append("circle")
  .attr("class", "viz")
  .attr("r", d => d.status === "mastered" ? 9 : d.status === "unlocked" ? 8 : 7);

// Course-color ring for unlocked/mastered (replace fill with course tint when locked)
node.selectAll("circle.viz")
  .attr("fill", d => {
    if (d.status === "locked")    return "#d1d5db";
    if (d.status === "seen")      return "#d8b4fe";
    if (d.status === "mastered")  return "#fbbf24";
    // unlocked: course tint
    return courseColor(d.course).light;
  })
  .attr("stroke", d => {
    if (d.status === "mastered")  return "#d97706";
    if (d.status === "seen")      return "#9333ea";
    if (d.status === "unlocked")  return courseColor(d.course).mid;
    return "#9ca3af";
  });

// Invisible touch target over each node: 32px diameter hit area so a finger
// can tap a 14-18px circle. Transparent fill still receives pointer events;
// they bubble to the group's click/drag handlers. Kept out of "circle.viz"
// styling above so CSS never paints it.
node.append("circle")
  .attr("class", "hit")
  .attr("r", 16)
  .attr("fill", "transparent")
  .attr("stroke", "none");

// Close panel when clicking canvas background
svg.on("click", closePanel);

// Tick
function render() {
  link
    .attr("x1", d => d.source.x)
    .attr("y1", d => d.source.y)
    .attr("x2", d => d.target.x)
    .attr("y2", d => d.target.y);

  node.attr("transform", d => `translate(${d.x},${d.y})`);
}
sim.on("tick", render);
// Reduced motion: lay the graph out before showing it instead of letting
// the nodes drift into place.
if (REDUCED_MOTION) {
  sim.stop();
  sim.tick(300);
  render();
}

// -----------------------------------------------------------------------
// Drag
// -----------------------------------------------------------------------
function dragStart(event, d) {
  if (!event.active) sim.alphaTarget(0.3).restart();
  d.fx = d.x; d.fy = d.y;
}
function dragging(event, d) { d.fx = event.x; d.fy = event.y; }
function dragEnd(event, d) {
  if (!event.active) sim.alphaTarget(0);
  d.fx = null; d.fy = null;
}

// -----------------------------------------------------------------------
// Side panel
// -----------------------------------------------------------------------
let _panelGoal = null;
let _panelOpener = null;   // the node or list item to refocus on close

function nodeLabel(d) {
  return `${d.title}, ${STATUS_WORD[d.status] || d.status}, ${courseLabel(d.course)} lecture ${d.lecture}` +
         (RECOMMENDED_IDS.has(d.id) ? ", recommended" : "");
}

// Tab can land on a node the pan left off screen: bring it into view.
function keepInView(d, el) {
  const r = el.getBoundingClientRect();
  if (r.top >= 52 && r.bottom <= window.innerHeight && r.left >= 0 && r.right <= window.innerWidth) return;
  svg.transition().duration(REDUCED_MOTION ? 0 : 250).call(zoomBehavior.translateTo, d.x, d.y);
}

function recordPathEvent(goalId, course, modality) {
  // The same choice as an activity event, joined to the visit (docs/activity-analytics-plan-2026-10.md).
  try { if (window.tutorTrack) window.tutorTrack('goal_click', {goal_id: goalId, modality, course}); } catch (e) { /* never */ }
  fetch('/path/event', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({goal_id: goalId, course: course, modality: modality, source: 'student'}),
  }).catch(() => {});
}

// ``opener`` gets focus back on close; ``focusPanel`` moves focus into the
// panel (keyboard and list opens; a click on the graph leaves it be).
function openPanel(d, opener, focusPanel) {
  _panelGoal = d;
  _panelOpener = opener || null;
  document.getElementById("panel-title").textContent = d.title;
  const badge = document.getElementById("panel-badge");
  badge.textContent = STATUS_WORD[d.status] || d.status;
  badge.className = `badge ${d.status}`;
  document.getElementById("panel-hook").textContent = d.hook || "";
  document.getElementById("panel-desc").textContent = d.description || "";
  document.getElementById("panel-meta").textContent =
    `${courseLabel(d.course)} · Lecture ${d.lecture}`;

  // Slide + book reference links (small, unobtrusive)
  const linksEl = document.getElementById("panel-links");
  linksEl.innerHTML = "";
  if (d.slides_url) {   // only a deck that is on disk (review step 0.3)
    const a = document.createElement("a");
    a.href = d.slides_url; a.target = "_blank";
    // The deck link comes from the goal's lecture when it has no slide anchor.
    a.textContent = `📊 Slides (L${(d.slide_anchor && d.slide_anchor.lecture) || d.lecture})`;
    a.style.cssText = "display:inline-flex;align-items:center;min-height:44px;font-size:0.75rem;color:#6b7280;text-decoration:underline";
    linksEl.appendChild(a);
  }
  if (d.book_anchor) {
    const bookUrl = `/book/${d.book_anchor.chapter}.html#${d.book_anchor.section_id}`;
    const a = document.createElement("a");
    a.href = bookUrl; a.target = "_blank";
    a.textContent = `📖 Book`;
    a.style.cssText = "display:inline-flex;align-items:center;min-height:44px;font-size:0.75rem;color:#6b7280;text-decoration:underline";
    linksEl.appendChild(a);
  }
  if (d.explorable_url) {
    const a = document.createElement("a");
    a.href = d.explorable_url; a.target = "_blank";
    a.textContent = `🧪 Explorable`;
    a.style.cssText = "display:inline-flex;align-items:center;min-height:44px;font-size:0.75rem;color:#7c3aed;text-decoration:underline;font-weight:600";
    linksEl.appendChild(a);
  }

  // Modality action buttons
  const modEl = document.getElementById("panel-modality");
  modEl.innerHTML = "";

  const chatUrl = `/chat?topic=${encodeURIComponent(d.title)}&course=${encodeURIComponent(d.course)}&lecture_num=${d.lecture}`;
  // Depth for the cached opener: mastered learners get a brief refresher, new
  // topics get the fuller explanation, everything else the standard version.
  const depth = d.status === "mastered" ? "brief" : (d.status === "locked" ? "thorough" : "standard");
  const teachUrl = `${chatUrl}&goal=${encodeURIComponent(d.id)}&depth=${depth}`;
  _addModBtn(modEl, "💬 Teach me",  "mod-btn mod-teach",
    () => { recordPathEvent(d.id, d.course, "teach");   window.location = teachUrl; });
  if (d.slides_url) {
    _addModBtn(modEl, "📊 Slides", "mod-btn mod-slides",
      () => { recordPathEvent(d.id, d.course, "slides"); window.open(d.slides_url); });
  }
  if (d.book_anchor) {
    _addModBtn(modEl, "📖 Book", "mod-btn mod-book",
      () => { recordPathEvent(d.id, d.course, "book"); window.open(`/book/${d.book_anchor.chapter}.html#${d.book_anchor.section_id}`); });
  }
  if (d.explorable_url) {
    _addModBtn(modEl, "🧪 Explore", "mod-btn mod-explore",
      () => { recordPathEvent(d.id, d.course, "explore"); window.open(d.explorable_url); });
  }
  const quizUrl = `/chat?topic=${encodeURIComponent(d.title)}&course=${encodeURIComponent(d.course)}&lecture_num=${d.lecture}&mode=quiz`;
  _addModBtn(modEl, "📝 Quiz me", "mod-btn mod-quiz",
    () => { recordPathEvent(d.id, d.course, "quiz"); window.location = quizUrl; });
  _addModBtn(modEl, "🔁 Review", "mod-btn mod-review",
    () => { recordPathEvent(d.id, d.course, "review"); window.location = chatUrl + "&mode=review"; });

  document.getElementById("panel").classList.add("open");
  if (focusPanel) document.getElementById("panel-title").focus();
}

function _addModBtn(container, label, cls, onClick) {
  const b = document.createElement("button");
  b.textContent = label;
  b.className = cls;
  b.addEventListener("click", onClick);
  container.appendChild(b);
}

// -----------------------------------------------------------------------
// Recommended goals: fetch /path/next and mark top-3 nodes
// -----------------------------------------------------------------------
function loadRecommended(course) {
  fetch(`/path/next?course=${encodeURIComponent(course)}&n=3`)
    .then(r => r.json())
    .then(data => {
      data.goals.forEach(g => RECOMMENDED_IDS.add(g.id));
      node.filter(d => RECOMMENDED_IDS.has(d.id))
          .classed("recommended", true)
          .attr("aria-label", d => nodeLabel(d));
      if (document.body.classList.contains("list-view")) buildList();
    })
    .catch(() => {});
}

// Load recommended for every course
COURSES.forEach(c => loadRecommended(c.id));
function closePanel() {
  const panel = document.getElementById("panel");
  if (!panel.classList.contains("open")) return;
  const hadFocus = panel.contains(document.activeElement);
  panel.classList.remove("open");
  if (hadFocus && _panelOpener && document.body.contains(_panelOpener)) _panelOpener.focus();
}

document.addEventListener("keydown", (e) => {
  if (e.key === "Escape") closePanel();
});

// -----------------------------------------------------------------------
// List view: every goal as a button, by course and lecture.
// -----------------------------------------------------------------------
function buildList() {
  const box = document.getElementById("goal-list");
  box.replaceChildren();
  const sorted = [...GRAPH.nodes].sort((a, b) =>
    (courseIndex(a.course) - courseIndex(b.course)) || String(a.course).localeCompare(String(b.course)) ||
    (a.lecture - b.lecture) || a.title.localeCompare(b.title));
  let course = null, lecture = null, ul = null;
  for (const d of sorted) {
    if (d.course !== course) {
      course = d.course; lecture = null;
      const h = document.createElement("h2");
      h.textContent = courseLabel(course);
      box.appendChild(h);
    }
    if (d.lecture !== lecture) {
      lecture = d.lecture;
      const h = document.createElement("h3");
      h.textContent = `Lecture ${lecture}`;
      box.appendChild(h);
      ul = document.createElement("ul");
      box.appendChild(ul);
    }
    const li = document.createElement("li");
    const b = document.createElement("button");
    b.type = "button";
    b.dataset.goal = d.id;
    const title = document.createElement("span");
    title.textContent = d.title;
    b.appendChild(title);
    if (RECOMMENDED_IDS.has(d.id)) {
      const rec = document.createElement("span");
      rec.className = "rec";
      rec.textContent = "Recommended";
      b.appendChild(rec);
    }
    const st = document.createElement("span");
    st.className = "status";
    st.textContent = STATUS_WORD[d.status] || d.status;
    b.appendChild(st);
    b.addEventListener("click", () => openPanel(d, b, true));
    li.appendChild(b);
    ul.appendChild(li);
  }
}

function toggleListView() {
  const on = !document.body.classList.contains("list-view");
  document.body.classList.toggle("list-view", on);
  const btn = document.getElementById("view-toggle");
  btn.setAttribute("aria-pressed", String(on));
  btn.textContent = on ? "🗺 Graph view" : "☰ List view";
  const list = document.getElementById("goal-list");
  list.hidden = !on;
  document.getElementById("graph-svg").parentElement.hidden = on;
  if (on) buildList();
  closePanel();
}

// -----------------------------------------------------------------------
// Resize
// -----------------------------------------------------------------------
window.addEventListener("resize", () => {
  sim.force("x", d3.forceX(d => courseX(d.course)).strength(0.08));
  sim.force("y", d3.forceY(H()/2).strength(0.04));
  sim.alpha(0.2).restart();
});
