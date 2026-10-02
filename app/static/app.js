// Librarian web UI. Every call goes to the app service's API (../main.py); this file only
// renders what comes back. Routes: / (ask), /<user id>/<run id> (a saved run).
// The run in progress has no URL of its own: it is / with {live: true} in
// history.state, so a reload lands on the ask page, as the page has lost the
// run's stream by then.
import { marked } from "https://cdn.jsdelivr.net/npm/marked@18.0.14/lib/marked.esm.js";
import DOMPurify from "https://cdn.jsdelivr.net/npm/dompurify@3.4.16/dist/purify.es.mjs";

const $ = (selector) => document.querySelector(selector);
const view = $("#view");

const EXAMPLES = [
  "Does metformin extend lifespan in mammals?",
  "What is the role of telomere shortening in cellular senescence?",
  "Which gut bacteria metabolize levodopa, and does it affect Parkinson's treatment?",
  "Do TREM2 loss-of-function variants increase Alzheimer's disease risk?",
];

// The pipeline's stages, in order. stageOf() maps each progress message the
// librarian streams onto one of them.
const STAGES = [
  ["Plan searches", "Turning the question into Europe PMC queries"],
  ["Search Europe PMC", "Fetching papers and ranking paragraphs"],
  ["Judge evidence", "Keeping only the sentences that answer"],
  ["Write answer", "Synthesizing a cited answer"],
];

const ICONS = {
  plus: '<path d="M5 12h14M12 5v14"/>',
  search: '<circle cx="11" cy="11" r="7"/><path d="m20 20-3.5-3.5"/>',
  arrowUp: '<path d="m5 12 7-7 7 7M12 19V5"/>',
  copy: '<rect width="13" height="13" x="8" y="8" rx="2"/><path d="M4 16V6a2 2 0 0 1 2-2h10"/>',
  download: '<path d="M12 3v12m-5-5 5 5 5-5M4 21h16"/>',
  braces: '<path d="M8 3H7a2 2 0 0 0-2 2v5a2 2 0 0 1-2 2 2 2 0 0 1 2 2v5a2 2 0 0 0 2 2h1M16 3h1a2 2 0 0 1 2 2v5a2 2 0 0 0 2 2 2 2 0 0 0-2 2v5a2 2 0 0 1-2 2h-1"/>',
  trash: '<path d="M3 6h18M8 6V4h8v2M6 6l1 14a2 2 0 0 0 2 2h6a2 2 0 0 0 2-2l1-14"/>',
  external: '<path d="M15 3h6v6M10 14 21 3M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6"/>',
  logout: '<path d="M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4M16 17l5-5-5-5M21 12H9"/>',
  sun: '<circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M2 12h2M20 12h2M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4"/>',
  moon: '<path d="M12 3a6 6 0 0 0 9 9 9 9 0 1 1-9-9Z"/>',
  menu: '<path d="M4 6h16M4 12h16M4 18h16"/>',
  check: '<path d="M20 6 9 17l-5-5"/>',
  alert: '<circle cx="12" cy="12" r="10"/><path d="M12 8v4M12 16h.01"/>',
  code: '<path d="m16 18 6-6-6-6M8 6l-6 6 6 6"/>',
  file: '<path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><path d="M14 2v6h6M16 13H8M16 17H8M10 9H8"/>',
};
const icon = (name) => `<svg class="i" viewBox="0 0 24 24" aria-hidden="true">${ICONS[name]}</svg>`;

const state = {
  me: null, // the signed-in user: {id, email}
  runs: [], // history list: {id, query, paper_count, duration_s, created_at}
  cache: new Map(), // run path -> full run, so reopening one is instant
  live: null, // the question being answered right now
  shown: null, // the saved run on screen, for the toolbar actions
  papers: [], // papers on screen, for re-sorting
  cited: new Set(), // indexes of those papers the answer cites
};

// ── Helpers ────────────────────────────────────────────────────────────────

const esc = (value) =>
  String(value ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);

// Europe PMC titles carry inline markup such as <i>E. coli</i>. Strip just
// those tags; parsing the text as HTML would eat a literal "IC50<10 nM".
const plain = (text) => String(text ?? "").replace(/<\/?(i|b|em|strong|sup|sub|u|h\d|p|br|span)\b[^>]*>/gi, "");

// SQLite's CURRENT_TIMESTAMP is UTC without a zone marker.
const parseTime = (stamp) => new Date(stamp.replace(" ", "T") + "Z");
const nowStamp = () => new Date().toISOString().slice(0, 19).replace("T", " ");
const clock = (ms) => `${Math.floor(ms / 60000)}:${String(Math.floor(ms / 1000) % 60).padStart(2, "0")}`;
const slug = (text) => text.toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-|-$/g, "").slice(0, 60) || "librarian";

function shortAuthors(authors) {
  const names = String(authors).replace(/\.$/, "").split(",").map((name) => name.trim()).filter(Boolean);
  return names.length > 3 ? `${names.slice(0, 3).join(", ")}, et al.` : names.join(", ");
}

function stageOf(message) {
  if (message.startsWith("Writing")) return 3;
  if (message.startsWith("Judging")) return 2;
  if (message.startsWith("Searching") || message.startsWith("Found")) return 1;
  return 0;
}

function errorText(body, status) {
  return typeof body.detail === "string" ? body.detail : `Request failed (${status})`;
}

let toastTimer;
function toast(message) {
  const el = $("#toast");
  el.textContent = message;
  el.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => (el.hidden = true), 2600);
}

function download(filename, text, type) {
  const link = document.createElement("a");
  link.href = URL.createObjectURL(new Blob([text], { type }));
  link.download = filename;
  link.click();
  setTimeout(() => URL.revokeObjectURL(link.href), 1000);
}

// A run's user id and run id are 16 hex characters (models.py's new_id),
// the only paths the server answers with this page.
const RUN_PATH = /^\/([0-9a-f]{16})\/([0-9a-f]{16})$/;
const runPath = (runId) => `/${state.me.id}/${runId}`;
const onLive = () => history.state?.live === true;

// Navigate and re-render. Going where we already are replaces the history
// entry rather than stacking a duplicate.
function go(path, { live = false, replace = false } = {}) {
  const here = location.pathname === path && onLive() === live;
  history[replace || here ? "replaceState" : "pushState"](live ? { live } : null, "", path);
  route();
}

// Sign in, then come back here.
const toLogin = () => location.replace(`/login.html?next=${encodeURIComponent(location.pathname)}`);

// ── API ────────────────────────────────────────────────────────────────────

async function api(path, options = {}) {
  const response = await fetch(path, { ...options, headers: { "content-type": "application/json" } });
  if (response.status === 401) {
    toLogin();
    throw new Error("Signed out");
  }
  const body = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(errorText(body, response.status));
  return body;
}

async function loadHistory() {
  state.runs = await api(`/api/users/${state.me.id}/runs`);
  renderHistory();
}

// Ask a question: stream the run's events from the server and render
// them as they come. The server keeps going if this page goes away, and the
// finished run lands in the history either way.
async function ask(query) {
  if (state.live && !state.live.finished) return toast("A question is already running.");
  const live = { query, stage: 0, message: "Starting", started: Date.now(), queries: null, evidence: null, error: null, finished: false };
  state.live = live;
  go("/", { live: true });
  try {
    const response = await fetch("/run-agent/stream", {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ query }),
    });
    if (response.status === 401) return toLogin();
    if (!response.ok) throw new Error(errorText(await response.json().catch(() => ({})), response.status));
    await readEvents(response, onRunEvent);
    if (!live.finished) throw new Error("The connection closed before the answer arrived.");
  } catch (error) {
    onRunEvent("error", { error: error.message });
  }
}

// Server-Sent Events arrive as "event: <name>\ndata: <json>\n\n" frames.
async function readEvents(response, handle) {
  const reader = response.body.pipeThrough(new TextDecoderStream()).getReader();
  let buffer = "";
  for (;;) {
    const { value, done } = await reader.read();
    if (done) return;
    buffer += value;
    let end;
    while ((end = buffer.indexOf("\n\n")) !== -1) {
      const frame = buffer.slice(0, end);
      buffer = buffer.slice(end + 2);
      const event = frame.match(/^event: (.*)$/m)?.[1];
      const data = frame.match(/^data: (.*)$/m)?.[1];
      if (event && data) handle(event, JSON.parse(data));
    }
  }
}

function onRunEvent(event, data) {
  const live = state.live;
  if (!live || live.finished) return;
  if (event === "progress") Object.assign(live, { stage: stageOf(data.message), message: data.message });
  if (event === "queries") live.queries = data.search_queries;
  if (event === "evidence") live.evidence = data;
  if (event === "error") Object.assign(live, { error: data.error, finished: true });
  if (event === "result") {
    live.finished = true;
    const path = runPath(data.run_id);
    state.cache.set(path, {
      id: data.run_id, query: live.query, answer: data.answer, evidence: data.evidence,
      created_at: nowStamp(), duration_s: (Date.now() - live.started) / 1000,
    });
    state.live = null;
    loadHistory();
    if (onLive()) go(path, { replace: true });
    else toast("Your answer is ready. It's at the top of your history.");
    return;
  }
  if (onLive()) renderLive();
  renderHistory();
}

// ── Routing ────────────────────────────────────────────────────────────────

function route() {
  document.body.classList.remove("nav-open");
  const [, userId, runId] = location.pathname.match(RUN_PATH) ?? [];
  if (runId) openRun(userId, runId);
  else if (onLive() && state.live) renderLive();
  else renderHome();
  renderHistory();
}

// The server decides who may see a run: another account's user id is a 403,
// and a run id that isn't in this account is a 404.
async function openRun(userId, runId) {
  const path = location.pathname;
  let run = state.cache.get(path);
  if (!run) {
    view.innerHTML = `<div class="run"><div class="skeleton" style="width:30%"></div><div class="skeleton" style="width:70%;height:30px"></div>
      <div class="panel">${"<div class=skeleton></div>".repeat(6)}</div></div>`;
    try {
      run = await api(`/api/users/${userId}/runs/${runId}`);
    } catch (error) {
      if (location.pathname === path) view.innerHTML = errorPanel("Couldn't open this question", error.message);
      return;
    }
    state.cache.set(path, run);
  }
  if (location.pathname === path) renderRun(run); // unless the reader moved on while it loaded
}

// ── Views ──────────────────────────────────────────────────────────────────

function renderHome() {
  state.shown = null;
  view.innerHTML = `
    <section class="home">
      <h1>Ask the literature.</h1>
      <p class="lede">Librarian searches Europe PMC, reads open-access full text, and answers with citations to the exact sentences it relied on.</p>
      <form class="composer" id="composer">
        <textarea name="query" rows="3" maxlength="2000" placeholder="Ask a biomedical research question…" aria-label="Your question"></textarea>
        <div class="composer-bar">
          <button class="btn primary" type="submit">Ask ${icon("arrowUp")}</button>
        </div>
      </form>
      <div class="examples">${EXAMPLES.map((q) => `<button class="chip" type="button">${esc(q)}</button>`).join("")}</div>
      <div class="how">
        <div><span class="num">1</span><b>Plan</b><p>Your question becomes several complementary Europe PMC searches.</p></div>
        <div><span class="num">2</span><b>Read</b><p>Hundreds of papers are split into paragraphs and ranked against each search.</p></div>
        <div><span class="num">3</span><b>Judge &amp; cite</b><p>A relevance judge keeps only the sentences that answer, and the answer cites them.</p></div>
      </div>
    </section>`;
  const form = $("#composer");
  const box = form.elements.query;
  box.focus();
  box.addEventListener("keydown", (event) => {
    if (event.key === "Enter" && !event.shiftKey && !event.isComposing) {
      event.preventDefault();
      form.requestSubmit();
    }
  });
  form.addEventListener("submit", (event) => {
    event.preventDefault();
    const query = box.value.trim();
    if (query) ask(query);
  });
  view.querySelectorAll(".chip").forEach((chip) =>
    chip.addEventListener("click", () => {
      box.value = chip.textContent;
      box.focus();
    }),
  );
}

function renderLive() {
  const live = state.live;
  state.shown = null;
  state.papers = live.evidence?.papers ?? [];
  state.cited = new Set();
  const answer = live.error
    ? errorPanel("The run failed", live.error, `<button class="btn" data-action="retry">Try again</button>`)
    : `<div class="panel"><div class="label">Answer</div>${"<div class=skeleton></div>".repeat(5)}<div class="skeleton" style="width:60%"></div>
        <p class="note">${live.evidence ? "Evidence is in. Writing the answer…" : "Reading open-access full text takes a few minutes. You can leave this page: the answer is saved to your history."}</p></div>`;
  const evidence = live.evidence
    ? evidenceColumn(state.papers)
    : live.error
    ? ""
    : `<aside class="evidence-col"><div class="evidence-head"><span class="label">Evidence</span></div>
        <div class="papers">${`<div class="paper">${"<div class=skeleton></div>".repeat(3)}</div>`.repeat(3)}</div></aside>`;
  view.innerHTML = `
    <article class="run">
      <div class="eyebrow">${live.error ? "Failed" : "Answering now"}</div>
      <h1 class="question">${esc(live.query)}</h1>
      ${live.error ? "" : stepsHtml(live)}
      <div class="run-grid"><section>${live.queries ? searchesHtml(live.queries, true) : ""}${answer}</section>${evidence}</div>
    </article>`;
}

function stepsHtml(live) {
  const steps = STAGES.map(([title, detail], i) => {
    const status = i < live.stage ? "done" : i === live.stage ? "active" : "";
    return `<li class="step ${status}"><span class="dot">${status === "done" ? icon("check") : ""}</span>
      <div><b>${title}</b><small>${esc(i === live.stage ? live.message : detail)}</small></div></li>`;
  });
  return `<ol class="steps">${steps.join("")}<li class="elapsed" id="elapsed">${clock(Date.now() - live.started)}</li></ol>`;
}

function renderRun(run) {
  state.shown = run;
  state.papers = run.evidence.papers;
  const answer = document.createElement("div");
  state.cited = renderAnswer(answer, run.answer, state.papers);
  view.innerHTML = `
    <article class="run">
      <h1 class="question">${esc(run.query)}</h1>
      <div class="run-bar">
        <div class="actions">
          <button class="btn" data-action="copy">${icon("copy")} Copy answer</button>
          <button class="btn" data-action="markdown" title="Download the answer and evidence as Markdown">${icon("download")} Markdown</button>
          <button class="btn" data-action="json" title="Download the raw API payload">${icon("braces")} JSON</button>
          <button class="btn icon danger" data-action="delete" title="Delete from history" aria-label="Delete from history">${icon("trash")}</button>
        </div>
      </div>
      <div class="run-grid">
        <section>
          ${searchesHtml(run.evidence.search_queries)}
          <div class="panel"><div class="label">Answer</div><div class="prose">${answer.innerHTML}</div></div>
        </section>
        ${evidenceColumn(state.papers)}
      </div>
    </article>`;
}

// Render the Markdown answer into `el` and turn every link that points at a
// retrieved paper into a citation chip. Returns the indexes of cited papers.
function renderAnswer(el, markdown, papers) {
  el.innerHTML = DOMPurify.sanitize(marked.parse(markdown));
  const byUrl = new Map(papers.map((paper, i) => [paper.url, i]).filter(([url]) => url));
  const cited = new Set();
  for (const link of el.querySelectorAll("a[href]")) {
    link.target = "_blank";
    link.rel = "noopener";
    const i = byUrl.get(link.getAttribute("href"));
    if (i === undefined) continue;
    cited.add(i);
    link.className = "cite";
    link.dataset.idx = i;
    link.title = plain(papers[i].title);
  }
  return cited;
}

// The Europe PMC sub-queries, framed above the answer: open while the run
// searches them, folded to one line once the answer is there.
function searchesHtml(queries, open = false) {
  if (!queries.length) return "";
  const items = queries.map(
    (q) => `<li><a href="https://europepmc.org/search?query=${encodeURIComponent(q)}" target="_blank" rel="noopener">${esc(q)}</a></li>`,
  );
  const count = `${queries.length} ${queries.length === 1 ? "query" : "queries"}`;
  return `<details class="searches"${open ? " open" : ""}><summary>Europe PMC searches · ${count}</summary><ol>${items.join("")}</ol></details>`;
}

function evidenceColumn(papers) {
  return `<aside class="evidence-col">
    <div class="evidence-head">
      <span class="label">Evidence<span>${papers.length} ${papers.length === 1 ? "paper" : "papers"}</span></span>
      <select id="sort" aria-label="Sort papers">
        <option value="rank">By relevance</option><option value="cited">Cited first</option><option value="year">Newest first</option>
      </select>
    </div>
    <div class="papers" id="papers">${paperCards("rank")}</div>
  </aside>`;
}

function paperCards(sort) {
  const { papers, cited } = state;
  const order = papers.map((_, i) => i); // sort() is stable: ties keep relevance order
  if (sort === "year") order.sort((a, b) => (parseInt(papers[b].year, 10) || 0) - (parseInt(papers[a].year, 10) || 0));
  if (sort === "cited") order.sort((a, b) => cited.has(b) - cited.has(a));
  return order.map((i) => paperCard(papers[i], i, cited.has(i))).join("") || `<p class="empty">No paper passed the relevance judge.</p>`;
}

function paperCard(paper, i, isCited) {
  const external = (href, text) => `<a href="${esc(href)}" target="_blank" rel="noopener">${text} ${icon("external")}</a>`;
  const links = [
    paper.url && external(paper.url, "Europe PMC"),
    paper.pmid && external(`https://pubmed.ncbi.nlm.nih.gov/${paper.pmid}/`, `PMID ${esc(paper.pmid)}`),
    paper.doi && external(`https://doi.org/${paper.doi}`, "DOI"),
  ];
  const title = esc(plain(paper.title));
  const venue = [paper.journal, paper.year, paper.affiliation].filter(Boolean).join(" · ");
  const snippets = paper.evidence_snippets.map(
    (text) => `<li${text.length > 480 ? ' class="long" title="Click to expand"' : ""}>${esc(plain(text))}</li>`,
  );
  return `<article class="paper${isCited ? " cited" : ""}" id="paper-${i}" data-idx="${i}">
    <div class="paper-top">
      <span class="key" title="${isCited ? "Cited in the answer" : "Retrieved, not cited in the answer"}">${esc(paper.citation_key || `#${i + 1}`)}</span>
      ${paper.has_fulltext ? '<span class="badge" title="Free full text is available in Europe PMC">Free full text</span>' : ""}
      ${paper.epmcSource === "PPR" ? '<span class="badge warn">Preprint</span>' : ""}
      <span class="rank" title="Relevance rank">#${i + 1}</span>
    </div>
    <h3>${paper.url ? `<a href="${esc(paper.url)}" target="_blank" rel="noopener">${title}</a>` : title}</h3>
    <div class="byline" title="${esc(paper.authors)}">${esc(shortAuthors(paper.authors))}</div>
    <div class="venue" title="${esc(venue)}">${esc(venue)}</div>
    <ul class="snippets">${snippets.join("")}</ul>
    <div class="links">${links.filter(Boolean).join("")}</div>
  </article>`;
}

function errorPanel(title, message, extra = "") {
  return `<div class="panel error">${icon("alert")}<div><b>${esc(title)}</b><p>${esc(message)}</p>${extra}</div></div>`;
}

function renderHistory() {
  const filter = $("#history-filter").value.trim().toLowerCase();
  const today = new Date().setHours(0, 0, 0, 0);
  const groups = new Map();
  for (const run of state.runs.filter((r) => r.query.toLowerCase().includes(filter))) {
    const days = Math.round((today - new Date(parseTime(run.created_at)).setHours(0, 0, 0, 0)) / 864e5);
    const label = days < 1 ? "Today" : days < 2 ? "Yesterday" : days < 7 ? "Previous 7 days" : days < 30 ? "Previous 30 days" : "Older";
    if (!groups.has(label)) groups.set(label, []);
    groups.get(label).push(run);
  }
  const live = state.live;
  const liveItem = live
    ? `<div class="group">In progress</div><a class="hist live${onLive() ? " active" : ""}" href="/" data-live>
        <span>${esc(live.query)}</span><small>${live.error ? "Failed" : `<i class="pulse"></i>${STAGES[live.stage][0]}…`}</small></a>`
    : "";
  const items = [...groups].map(
    ([label, runs]) =>
      `<div class="group">${label}</div>` +
      runs
        .map(
          (run) => `<a class="hist${location.pathname === runPath(run.id) ? " active" : ""}" href="${runPath(run.id)}">
            <span>${esc(run.query)}</span><small>${run.paper_count} ${run.paper_count === 1 ? "paper" : "papers"}</small></a>`,
        )
        .join(""),
  );
  $("#history").innerHTML =
    liveItem + items.join("") ||
    `<p class="empty">${filter ? "No questions match." : "Your questions and answers will be kept here."}</p>`;
}

function focusPaper(i) {
  const card = document.getElementById(`paper-${i}`);
  if (!card) return;
  card.scrollIntoView({ behavior: "smooth", block: "center" });
  card.classList.remove("flash");
  void card.offsetWidth; // restart the animation on a repeat click
  card.classList.add("flash");
}

function toMarkdown(run) {
  const lines = [`# ${run.query}`, "", run.answer, "", "## Evidence", ""];
  for (const paper of run.evidence.papers) {
    const ids = [paper.pmid && `PMID ${paper.pmid}`, paper.doi && `doi:${paper.doi}`].filter(Boolean);
    lines.push(`### [${paper.citation_key}] ${plain(paper.title)}`, "");
    lines.push([paper.authors, [paper.journal, paper.year].filter(Boolean).join(" "), ...ids].filter(Boolean).join(" · "));
    if (paper.url) lines.push(paper.url);
    lines.push("", ...paper.evidence_snippets.flatMap((text) => [`> ${plain(text)}`, ""]));
  }
  return lines.join("\n");
}

const ACTIONS = {
  retry: () => ask(state.live.query),
  copy: async () => {
    await navigator.clipboard.writeText(state.shown.answer);
    toast("Answer copied as Markdown");
  },
  markdown: () => download(`${slug(state.shown.query)}.md`, toMarkdown(state.shown), "text/markdown"),
  json: () =>
    download(
      `${slug(state.shown.query)}.json`,
      JSON.stringify({ answer: state.shown.answer, evidence: state.shown.evidence }, null, 2),
      "application/json",
    ),
  delete: async () => {
    if (!confirm("Delete this question and its answer from your history?")) return;
    await api(`/api/users/${state.me.id}/runs/${state.shown.id}`, { method: "DELETE" });
    state.cache.delete(runPath(state.shown.id));
    await loadHistory();
    go("/", { replace: true }); // the run's URL is gone, so Back shouldn't return to it
    toast("Deleted from your history");
  },
};

// ── Wiring ─────────────────────────────────────────────────────────────────

function paintThemeButton() {
  $("#theme").innerHTML = icon(document.documentElement.dataset.theme === "dark" ? "sun" : "moon");
}

async function boot() {
  document.querySelectorAll("[data-icon]").forEach((el) => el.insertAdjacentHTML("afterbegin", icon(el.dataset.icon)));
  paintThemeButton();

  let me;
  try {
    me = state.me = await api("/api/me");
  } catch (error) {
    if (error.message !== "Signed out") view.innerHTML = errorPanel("Librarian is unavailable", error.message);
    return;
  }
  $("#user-email").textContent = me.email;
  $("#user-email").title = me.email; // long addresses are cut off with an ellipsis
  $("#avatar").textContent = me.email[0].toUpperCase();

  $("#new-question").addEventListener("click", () => go("/"));
  // Links to this app's own pages navigate in place. Modified clicks are left
  // to the browser, so cmd/ctrl-click still opens a run in a new tab.
  document.addEventListener("click", (event) => {
    const link = event.target.closest('a[href^="/"]:not([target])');
    if (!link || event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
    event.preventDefault();
    go(link.getAttribute("href"), { live: link.hasAttribute("data-live") });
  });
  $("#history-filter").addEventListener("input", renderHistory);
  $("#menu").addEventListener("click", () => document.body.classList.add("nav-open"));
  $("#scrim").addEventListener("click", () => document.body.classList.remove("nav-open"));
  $("#logout").addEventListener("click", async () => {
    await api("/api/auth/logout", { method: "POST" });
    location.replace("/login.html");
  });
  $("#theme").addEventListener("click", () => {
    const next = document.documentElement.dataset.theme === "dark" ? "light" : "dark";
    document.documentElement.dataset.theme = next;
    try {
      localStorage.setItem("theme", next);
    } catch {} // private mode: the choice just won't persist
    paintThemeButton();
  });

  // The view is re-rendered wholesale, so its controls are handled here once.
  view.addEventListener("click", (event) => {
    const cite = event.target.closest("a.cite");
    if (cite && !event.metaKey && !event.ctrlKey) {
      event.preventDefault(); // a plain click jumps to the evidence; cmd/ctrl-click opens the paper
      return focusPaper(cite.dataset.idx);
    }
    const snippet = event.target.closest(".snippets li.long");
    if (snippet) return snippet.classList.toggle("open");
    const action = event.target.closest("[data-action]")?.dataset.action;
    if (action) Promise.resolve(ACTIONS[action]()).catch((error) => toast(error.message));
  });
  view.addEventListener("change", (event) => {
    if (event.target.id === "sort") $("#papers").innerHTML = paperCards(event.target.value);
  });
  // Hovering a paper lights up every place the answer cites it. Listening on
  // the document means leaving the view also turns the highlight off.
  let lit = null;
  document.addEventListener("mouseover", (event) => {
    const idx = event.target.closest(".paper")?.dataset.idx ?? null;
    if (idx === lit) return;
    lit = idx;
    view.querySelectorAll("a.cite").forEach((cite) => cite.classList.toggle("lit", cite.dataset.idx === idx));
  });
  setInterval(() => {
    const el = $("#elapsed");
    if (el && state.live && !state.live.finished) el.textContent = clock(Date.now() - state.live.started);
  }, 1000);

  window.addEventListener("popstate", route);
  route();
  loadHistory().catch((error) => toast(error.message));
}

boot();
