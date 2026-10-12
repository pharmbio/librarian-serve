// Librarian web UI. Every call goes to the app service's API (../main.py); this file only
// renders what comes back. Routes: / (ask), /<user id>/<conversation id> (a conversation).
// A conversation is a thread of turns: a question and its answer, each searching Europe PMC
// or answered from the conversation itself. Follow-ups are asked at the bottom of the thread.
// The first turn of a new conversation has no URL of its own while it runs: it is / with
// {live: true} in history.state, so a reload lands on the ask page, as the page has lost the
// run's stream by then. A follow-up runs on its conversation's own page.
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

// Follow-ups that show what a conversation can do: dig into an answer, or search again.
const FOLLOW_UPS = [
  "What are the main limitations of this evidence?",
  "Find more recent papers on this.",
  "Which study had the largest cohort?",
];

// The pipeline's stages, in order. stageOf() maps each progress message the
// librarian streams onto one of them.
const STAGES = [
  ["Read the question", "Deciding whether it needs a new search"],
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
  message: '<path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"/>',
  followUp: '<path d="m15 10 5 5-5 5"/><path d="M4 4v7a4 4 0 0 0 4 4h12"/>',
};
const icon = (name) => `<svg class="i" viewBox="0 0 24 24" aria-hidden="true">${ICONS[name]}</svg>`;

const state = {
  me: null, // the signed-in user: {id, email}
  conversations: [], // history list: {id, title, turn_count, paper_count, created_at, last_turn_at}
  cache: new Map(), // conversation path -> full conversation, so reopening one is instant
  live: null, // the turn being answered right now
  shown: null, // the conversation on screen, for the toolbar actions
  drafts: new Map(), // conversation id -> unsent follow-up, kept across re-renders
  evidenceKey: null, // whose papers the evidence column shows: a turn's index, or "live"
  citedRefs: new Set(), // "<turn>:<paper index>" of every paper an answer on screen cites
  papers: [], // papers in the evidence column, for re-sorting
  cited: new Set(), // indexes of those papers some answer cites
};

// ── Helpers ────────────────────────────────────────────────────────────────

const esc = (value) =>
  String(value ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);

// Europe PMC titles carry inline markup such as <i>E. coli</i>. Strip just
// those tags; parsing the text as HTML would eat a literal "IC50<10 nM".
const plain = (text) => String(text ?? "").replace(/<\/?(i|b|em|strong|sup|sub|u|h\d|p|br|span)\b[^>]*>/gi, "");

// Times come as UTC without a zone marker.
const parseTime = (stamp) => new Date(stamp.replace(" ", "T") + "Z");
const nowStamp = () => new Date().toISOString().slice(0, 19).replace("T", " ");
const clock = (ms) => `${Math.floor(ms / 60000)}:${String(Math.floor(ms / 1000) % 60).padStart(2, "0")}`;
const slug = (text) => text.toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-|-$/g, "").slice(0, 60) || "librarian";
const clip = (text, length) => (text.length > length ? `${text.slice(0, length - 1).trimEnd()}…` : text);
const plural = (n, word) => `${n} ${word}${n === 1 ? "" : "s"}`;

function shortAuthors(authors) {
  const names = String(authors).replace(/\.$/, "").split(",").map((name) => name.trim()).filter(Boolean);
  return names.length > 3 ? `${names.slice(0, 3).join(", ")}, et al.` : names.join(", ");
}

function stageOf(message) {
  if (message.startsWith("Writing")) return 4;
  if (message.startsWith("Judging")) return 3;
  if (message.startsWith("Searching") || message.startsWith("Found")) return 2;
  if (message.startsWith("Generating") || message.startsWith("Validating")) return 1;
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

// A user id and a conversation id are 16 hex characters (db_client.py's new_id),
// the only paths the server answers with this page.
const PAGE_PATH = /^\/([0-9a-f]{16})\/([0-9a-f]{16})$/;
const conversationPath = (conversationId) => `/${state.me.id}/${conversationId}`;
const onLive = () => history.state?.live === true;
// Whether the turn being answered is on screen: a new conversation's first
// turn on / (live), a follow-up on its conversation's page.
const liveHere = () =>
  Boolean(state.live) && (state.live.isNew ? onLive() : location.pathname === conversationPath(state.live.conversationId));

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

async function loadConversations() {
  state.conversations = await api(`/api/users/${state.me.id}/conversations`);
  renderHistory();
}

// Ask a question: a new conversation's first, or a follow-up in `conversationId`.
// Stream the run's events from the server and render them as they come. The
// server keeps going if this page goes away, and the finished turn lands in its
// conversation either way. `isNew` stays true when retrying a first turn that
// failed: its conversation has no answer to show yet, so it stays on /.
async function ask(query, conversationId = null, { isNew = !conversationId } = {}) {
  if (state.live && !state.live.finished) return toast("A question is already running.");
  const live = {
    query, conversationId, isNew, stage: 0, message: "Starting", started: Date.now(),
    route: null, queries: null, evidence: null, error: null, finished: false,
  };
  state.live = live;
  if (isNew) go("/", { live: true });
  else {
    state.drafts.delete(conversationId);
    go(conversationPath(conversationId));
    $("#live-turn")?.scrollIntoView({ behavior: "smooth", block: "start" });
  }
  try {
    const response = await fetch("/run-agent/stream", {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ query, conversation_id: conversationId }),
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
  if (event === "started") live.conversationId = data.conversation_id;
  if (event === "progress") Object.assign(live, { stage: stageOf(data.message), message: data.message });
  if (event === "route") live.route = data;
  if (event === "queries") live.queries = data.search_queries;
  if (event === "evidence") live.evidence = data;
  if (event === "error") Object.assign(live, { error: data.error, finished: true });
  if (event === "result") return onResult(live, data);
  if (liveHere()) {
    if (live.isNew) renderNewLive();
    else {
      const el = $("#live-turn");
      if (el) el.innerHTML = liveTurnHtml(live);
      // The evidence column follows the turn being answered once its papers are in.
      if (event === "evidence") {
        state.evidenceKey = "live";
        renderEvidence();
      }
    }
  }
  renderHistory();
}

// The turn is answered: add it to its conversation and show it there.
function onResult(live, data) {
  live.finished = true;
  const here = liveHere();
  const path = conversationPath(data.conversation_id);
  const turn = {
    id: data.run_id, query: live.query, action: data.action, answer: data.answer, evidence: data.evidence,
    paper_count: data.evidence.papers.length, duration_s: (Date.now() - live.started) / 1000, created_at: nowStamp(),
  };
  let conversation = state.cache.get(path);
  if (conversation) conversation.turns.push(turn);
  else if (live.isNew) {
    conversation = { id: data.conversation_id, title: live.query, created_at: nowStamp(), turns: [turn] };
    state.cache.set(path, conversation);
  }
  state.live = null;
  // Show the new turn's papers, if it found any; otherwise keep what was shown.
  if (here && conversation && turn.paper_count) state.evidenceKey = String(conversation.turns.length - 1);
  else if (state.evidenceKey === "live") state.evidenceKey = null;
  loadConversations();
  if (!here) return toast("Your answer is ready. Its conversation is at the top of your history.");
  go(path, { replace: true });
  if (conversation && conversation.turns.length > 1) {
    document.getElementById(`turn-${conversation.turns.length - 1}`)?.scrollIntoView({ behavior: "smooth", block: "start" });
  }
}

// ── Routing ────────────────────────────────────────────────────────────────

function route() {
  document.body.classList.remove("nav-open");
  const [, userId, conversationId] = location.pathname.match(PAGE_PATH) ?? [];
  if (conversationId) openConversation(userId, conversationId);
  else if (onLive() && state.live?.isNew) renderNewLive();
  else renderHome();
  renderHistory();
}

// The server decides who may see a conversation: another account's user id is
// a 403, and a conversation id that isn't in this account is a 404.
async function openConversation(userId, conversationId) {
  const path = location.pathname;
  let conversation = state.cache.get(path);
  if (!conversation) {
    view.innerHTML = `<div class="run"><div class="skeleton" style="width:30%"></div><div class="skeleton" style="width:70%;height:30px"></div>
      <div class="panel">${"<div class=skeleton></div>".repeat(6)}</div></div>`;
    try {
      conversation = await api(`/api/users/${userId}/conversations/${conversationId}`);
    } catch (error) {
      if (location.pathname === path) view.innerHTML = errorPanel("Couldn't open this conversation", error.message);
      return;
    }
    state.cache.set(path, conversation);
  }
  if (location.pathname === path) renderConversation(conversation); // unless the reader moved on while it loaded
}

// ── Views ──────────────────────────────────────────────────────────────────

function composerHtml({ followUp }) {
  return `<form class="composer${followUp ? " follow" : ""}" id="composer">
      <textarea name="query" rows="${followUp ? 2 : 3}" maxlength="2000" aria-label="Your question"
        placeholder="${followUp ? "Ask a follow-up…" : "Ask a biomedical research question…"}"></textarea>
      <div class="composer-bar">
        ${followUp ? '<span class="composer-hint">Follow-ups see the conversation so far.</span>' : ""}
        <button class="btn primary" type="submit">Ask ${icon("arrowUp")}</button>
      </div>
    </form>`;
}

// Enter sends, Shift+Enter is a new line; a chip fills the box.
function wireComposer(onSubmit, { draftKey = null, focus = true } = {}) {
  const form = $("#composer");
  const box = form.elements.query;
  if (draftKey) {
    box.value = state.drafts.get(draftKey) ?? "";
    box.addEventListener("input", () => state.drafts.set(draftKey, box.value));
  }
  if (focus) box.focus({ preventScroll: true });
  box.addEventListener("keydown", (event) => {
    if (event.key === "Enter" && !event.shiftKey && !event.isComposing) {
      event.preventDefault();
      form.requestSubmit();
    }
  });
  form.addEventListener("submit", (event) => {
    event.preventDefault();
    const query = box.value.trim();
    if (query) onSubmit(query);
  });
  view.querySelectorAll(".chip").forEach((chip) =>
    chip.addEventListener("click", () => {
      box.value = chip.textContent;
      if (draftKey) state.drafts.set(draftKey, box.value);
      box.focus();
    }),
  );
}

function renderHome() {
  state.shown = null;
  view.innerHTML = `
    <section class="home">
      <h1>Ask the Librarian.</h1>
      <p class="lede">Librarian searches Europe PMC, reads open-access full text, and answers with citations to the exact sentences it relied on.</p>
      ${composerHtml({ followUp: false })}
      <div class="examples">${EXAMPLES.map((q) => `<button class="chip" type="button">${esc(q)}</button>`).join("")}</div>
      <div class="how">
        <div><span class="num">1</span><b>Plan</b><p>Your question becomes several complementary Europe PMC searches.</p></div>
        <div><span class="num">2</span><b>Read</b><p>Hundreds of papers are split into paragraphs and ranked against each search.</p></div>
        <div><span class="num">3</span><b>Judge &amp; cite</b><p>A relevance judge keeps only the sentences that answer, and the answer cites them.</p></div>
        <div><span class="num">4</span><b>Follow up</b><p>Ask about an answer, or narrow, widen and redo the search, in the same conversation.</p></div>
      </div>
    </section>`;
  wireComposer((query) => ask(query));
}

// The first turn of a new conversation, while it runs.
function renderNewLive() {
  const live = state.live;
  state.shown = null;
  state.citedRefs = new Set();
  view.innerHTML = `
    <article class="run">
      <div class="eyebrow">${live.error ? "Failed" : "Answering now"}</div>
      <h1 class="question">${esc(live.query)}</h1>
      <div class="run-grid">
        <section class="thread"><div class="turn" id="live-turn">${liveTurnHtml(live)}</div></section>
        <aside class="evidence-col" id="evidence"></aside>
      </div>
    </article>`;
  state.evidenceKey = "live";
  renderEvidence();
}

// A conversation: its title, then each turn, the one being answered, and the
// follow-up box. The evidence column shows one turn's papers at a time.
function renderConversation(conversation) {
  if (state.shown?.id !== conversation.id) state.evidenceKey = null; // a new conversation starts on its latest papers
  state.shown = conversation;
  state.citedRefs = new Set();
  const turns = conversation.turns.map((turn, i) => turnHtml(conversation.turns, i));
  const live = liveHere() ? state.live : null;
  view.innerHTML = `
    <article class="run">
      <h1 class="question">${esc(conversation.title)}</h1>
      <div class="run-bar">
        <span class="run-meta">${plural(conversation.turns.length, "question")} · started ${esc(conversation.created_at.slice(0, 10))}</span>
        <div class="actions">
          <button class="btn" data-action="markdown" title="Download the conversation and its evidence as Markdown">${icon("download")} Markdown</button>
          <button class="btn" data-action="json" title="Download the raw API payload">${icon("braces")} JSON</button>
          <button class="btn icon danger" data-action="delete" title="Delete this conversation" aria-label="Delete this conversation">${icon("trash")}</button>
        </div>
      </div>
      <div class="run-grid">
        <section class="thread">
          ${turns.join("")}
          ${live ? `<div class="turn" id="live-turn">${liveTurnHtml(live)}</div>` : ""}
          ${composerHtml({ followUp: true })}
          <div class="examples follow-ups">${FOLLOW_UPS.map((q) => `<button class="chip" type="button">${esc(q)}</button>`).join("")}</div>
        </section>
        <aside class="evidence-col" id="evidence"></aside>
      </div>
    </article>`;
  wireComposer((query) => ask(query, conversation.id), { draftKey: conversation.id, focus: false });
  renderEvidence();
}

// One answered turn. The first one's question is the page's title; a
// follow-up's sits above its answer.
function turnHtml(turns, i) {
  const turn = turns[i];
  const answer = document.createElement("div");
  renderAnswer(answer, turn.answer, turns, i);
  const papers = turn.evidence?.papers ?? [];
  const searched = turn.evidence?.query;
  const meta =
    turn.action === "reply"
      ? `<div class="turn-meta"><span>${icon("message")} Answered from the conversation, without a new search</span></div>`
      : `<div class="turn-meta">
          ${i > 0 && searched && searched !== turn.query ? `<span>${icon("search")} Searched for “${esc(searched)}”</span>` : ""}
          ${papers.length ? `<button class="pill" type="button" data-evidence="${i}" title="Show this answer's evidence">${icon("file")} ${plural(papers.length, "paper")}</button>` : `<span>No paper passed the relevance judge</span>`}
        </div>
        ${searchesHtml(turn.evidence?.search_queries ?? [])}`;
  return `<div class="turn" id="turn-${i}">
      ${i > 0 ? askHtml(turn.query) : ""}
      ${meta}
      <div class="panel">
        <div class="panel-head"><span class="label">Answer</span>
          <button class="btn icon ghost sm" data-action="copy" data-turn="${i}" title="Copy this answer as Markdown" aria-label="Copy this answer">${icon("copy")}</button></div>
        <div class="prose">${answer.innerHTML}</div>
      </div>
    </div>`;
}

const askHtml = (query) => `<div class="ask"><span class="label">${icon("followUp")} Follow-up</span><p>${esc(query)}</p></div>`;

// The turn being answered: its progress, then a placeholder for the answer.
function liveTurnHtml(live) {
  const question = live.isNew ? "" : askHtml(live.query);
  if (live.error) {
    return question + errorPanel("The run failed", live.error, `<button class="btn" data-action="retry">Try again</button>`);
  }
  const route = live.route;
  const rewritten =
    route?.action === "search" && route.query !== live.query
      ? `<div class="turn-meta"><span>${icon("search")} Searching for “${esc(route.query)}”</span></div>`
      : "";
  const note =
    route?.action === "reply"
      ? "Answering from the conversation…"
      : live.evidence
      ? "Evidence is in. Writing the answer…"
      : `Reading open-access full text takes a few minutes. You can leave this page: the answer is saved to ${live.isNew ? "your history" : "this conversation"}.`;
  return `${question}${stepsHtml(live)}${rewritten}${live.queries ? searchesHtml(live.queries, true) : ""}
    <div class="panel"><div class="label">Answer</div>${"<div class=skeleton></div>".repeat(4)}<div class="skeleton" style="width:60%"></div>
      <p class="note">${note}</p></div>`;
}

// The stages as a row of titles, then what the current one is doing right now.
function stepsHtml(live) {
  const steps = STAGES.map(([title, detail], i) => {
    const status = i < live.stage ? "done" : i === live.stage ? "active" : "";
    return `<li class="step ${status}" title="${esc(detail)}"><span class="dot">${status === "done" ? icon("check") : ""}</span><b>${title}</b></li>`;
  });
  return `<ol class="steps">${steps.join("")}
    <li class="now"><span>${esc(live.message)}</span><span class="elapsed" id="elapsed">${clock(Date.now() - live.started)}</span></li></ol>`;
}

// Where a link in turn `t`'s answer points: the paper with that URL in the
// turn's own evidence, else in the latest earlier turn that found it. A reply,
// or a follow-up building on an earlier answer, cites papers found before.
function paperRef(turns, t, url) {
  for (let i = t; i >= 0; i--) {
    const j = (turns[i].evidence?.papers ?? []).findIndex((paper) => paper.url === url);
    if (j !== -1) return `${i}:${j}`;
  }
  return null;
}

// Render the Markdown answer of turn `t` into `el` and turn every link that
// points at a retrieved paper into a citation chip, noting it in state.citedRefs.
function renderAnswer(el, markdown, turns, t) {
  el.innerHTML = DOMPurify.sanitize(marked.parse(markdown));
  for (const link of el.querySelectorAll("a[href]")) {
    link.target = "_blank";
    link.rel = "noopener";
    const ref = paperRef(turns, t, link.getAttribute("href"));
    if (!ref) continue;
    const [i, j] = ref.split(":");
    const paper = turns[i].evidence.papers[j];
    state.citedRefs.add(ref);
    link.className = "cite";
    link.dataset.ref = ref;
    link.title = plain(paper.title);
    // A reply sometimes writes the bare URL as the link text: name the paper instead.
    if (link.textContent.trim() === link.getAttribute("href") && paper.citation_key) link.textContent = paper.citation_key;
  }
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

// Every set of papers the evidence column can show: each searched turn's, then
// the running turn's once they are in.
function evidenceSources() {
  const sources = [];
  (state.shown?.turns ?? []).forEach((turn, i) => {
    const papers = turn.evidence?.papers ?? [];
    if (papers.length) sources.push({ key: String(i), label: `${i + 1}. ${clip(turn.query, 44)}`, papers });
  });
  const live = liveHere() ? state.live : null;
  if (live?.evidence) sources.push({ key: "live", label: `Now: ${clip(live.query, 44)}`, papers: live.evidence.papers });
  return sources;
}

function renderEvidence() {
  const el = $("#evidence");
  if (!el) return;
  const sources = evidenceSources();
  const current = sources.find((source) => source.key === state.evidenceKey) ?? sources.at(-1);
  state.evidenceKey = current?.key ?? null;
  state.papers = current?.papers ?? [];
  state.cited = new Set(state.papers.map((_, i) => i).filter((i) => state.citedRefs.has(`${current.key}:${i}`)));
  view.querySelectorAll("[data-evidence]").forEach((pill) => pill.classList.toggle("active", pill.dataset.evidence === state.evidenceKey));
  if (!current) {
    el.innerHTML = noEvidenceHtml();
    return;
  }
  const picker =
    sources.length > 1
      ? `<select id="evidence-turn" aria-label="Show the evidence of">${sources
          .map((source) => `<option value="${source.key}"${source === current ? " selected" : ""}>${esc(source.label)}</option>`)
          .join("")}</select>`
      : "";
  el.innerHTML = `
    <div class="evidence-head">
      <span class="label">Evidence<span>${plural(state.papers.length, "paper")}</span></span>
      <div class="evidence-tools">${picker}
        <select id="sort" aria-label="Sort papers">
          <option value="rank">By relevance</option><option value="cited">Cited first</option><option value="year">Newest first</option>
        </select>
      </div>
    </div>
    <div class="papers" id="papers">${paperCards("rank")}</div>`;
}

// Nothing to show yet: the running turn is still searching, or no turn searched.
function noEvidenceHtml() {
  const live = liveHere() ? state.live : null;
  if (live && !live.error && live.route?.action !== "reply") {
    return `<div class="evidence-head"><span class="label">Evidence</span></div>
      <div class="papers">${`<div class="paper">${"<div class=skeleton></div>".repeat(3)}</div>`.repeat(3)}</div>`;
  }
  if (!state.shown) return "";
  const searched = state.shown.turns.some((turn) => turn.action !== "reply");
  const why = searched
    ? "No paper passed the relevance judge in this conversation. Try rephrasing or broadening the question."
    : "These answers came from the conversation itself, without a literature search. Ask for papers to search Europe PMC.";
  return `<div class="evidence-head"><span class="label">Evidence</span></div>
    <p class="evidence-empty">${icon(searched ? "search" : "message")} ${why}</p>`;
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
  const ref = `${state.evidenceKey}:${i}`;
  return `<article class="paper${isCited ? " cited" : ""}" id="paper-${state.evidenceKey}-${i}" data-ref="${ref}">
    <div class="paper-top">
      <span class="key" title="${isCited ? "Cited in an answer" : "Retrieved, not cited in an answer"}">${esc(paper.citation_key || `#${i + 1}`)}</span>
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
  for (const conversation of state.conversations.filter((c) => c.title.toLowerCase().includes(filter))) {
    const days = Math.round((today - new Date(parseTime(conversation.last_turn_at)).setHours(0, 0, 0, 0)) / 864e5);
    const label = days < 1 ? "Today" : days < 2 ? "Yesterday" : days < 7 ? "Previous 7 days" : days < 30 ? "Previous 30 days" : "Older";
    if (!groups.has(label)) groups.set(label, []);
    groups.get(label).push(conversation);
  }
  const live = state.live;
  const liveItem = live
    ? `<div class="group">In progress</div><a class="hist live${liveHere() ? " active" : ""}"
          href="${live.isNew ? "/" : conversationPath(live.conversationId)}"${live.isNew ? " data-live" : ""}>
        <span>${esc(live.query)}</span><small>${live.error ? "Failed" : `<i class="pulse"></i>${STAGES[live.stage][0]}…`}</small></a>`
    : "";
  const items = [...groups].map(
    ([label, conversations]) =>
      `<div class="group">${label}</div>` +
      conversations
        .map((conversation) => {
          const counts = conversation.turn_count > 1 ? `${plural(conversation.turn_count, "question")} · ` : "";
          return `<a class="hist${location.pathname === conversationPath(conversation.id) ? " active" : ""}" href="${conversationPath(conversation.id)}">
            <span>${esc(conversation.title)}</span><small>${counts}${plural(conversation.paper_count, "paper")}</small></a>`;
        })
        .join(""),
  );
  $("#history").innerHTML =
    liveItem + items.join("") ||
    `<p class="empty">${filter ? "No conversations match." : "Your conversations will be kept here."}</p>`;
}

// Jump to a cited paper, switching the evidence column to the turn that found it.
function focusPaper(ref) {
  const [key, i] = ref.split(":");
  if (key !== state.evidenceKey) {
    state.evidenceKey = key;
    renderEvidence();
  }
  const card = document.getElementById(`paper-${key}-${i}`);
  if (!card) return;
  card.scrollIntoView({ behavior: "smooth", block: "center" });
  card.classList.remove("flash");
  void card.offsetWidth; // restart the animation on a repeat click
  card.classList.add("flash");
}

function toMarkdown(conversation) {
  // One question reads like a report; several get a section each.
  const single = conversation.turns.length === 1;
  const h = (level) => "#".repeat(single ? level : level + 1);
  const lines = [`# ${conversation.title}`, ""];
  for (const turn of conversation.turns) {
    if (!single) lines.push(`## ${turn.query}`, "");
    lines.push(turn.answer, "");
    const papers = turn.evidence?.papers ?? [];
    if (!papers.length) continue;
    lines.push(`${h(2)} Evidence`, "");
    for (const paper of papers) {
      const ids = [paper.pmid && `PMID ${paper.pmid}`, paper.doi && `doi:${paper.doi}`].filter(Boolean);
      lines.push(`${h(3)} [${paper.citation_key}] ${plain(paper.title)}`, "");
      lines.push([paper.authors, [paper.journal, paper.year].filter(Boolean).join(" "), ...ids].filter(Boolean).join(" · "));
      if (paper.url) lines.push(paper.url);
      lines.push("", ...paper.evidence_snippets.flatMap((text) => [`> ${plain(text)}`, ""]));
    }
  }
  return lines.join("\n");
}

const ACTIONS = {
  retry: () => ask(state.live.query, state.live.conversationId, { isNew: state.live.isNew }),
  copy: async (button) => {
    await navigator.clipboard.writeText(state.shown.turns[button.dataset.turn].answer);
    toast("Answer copied as Markdown");
  },
  markdown: () => download(`${slug(state.shown.title)}.md`, toMarkdown(state.shown), "text/markdown"),
  json: () => download(`${slug(state.shown.title)}.json`, JSON.stringify(state.shown, null, 2), "application/json"),
  delete: async () => {
    if (!confirm("Delete this conversation, with all its questions and answers, from your history?")) return;
    await api(`/api/users/${state.me.id}/conversations/${state.shown.id}`, { method: "DELETE" });
    state.cache.delete(conversationPath(state.shown.id));
    await loadConversations();
    go("/", { replace: true }); // the conversation's URL is gone, so Back shouldn't return to it
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
  // to the browser, so cmd/ctrl-click still opens a conversation in a new tab.
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
      return focusPaper(cite.dataset.ref);
    }
    const pill = event.target.closest("[data-evidence]");
    if (pill) {
      state.evidenceKey = pill.dataset.evidence;
      renderEvidence();
      return $("#evidence").scrollIntoView({ behavior: "smooth", block: "nearest" });
    }
    const snippet = event.target.closest(".snippets li.long");
    if (snippet) return snippet.classList.toggle("open");
    const button = event.target.closest("[data-action]");
    if (button) Promise.resolve(ACTIONS[button.dataset.action](button)).catch((error) => toast(error.message));
  });
  view.addEventListener("change", (event) => {
    if (event.target.id === "sort") $("#papers").innerHTML = paperCards(event.target.value);
    if (event.target.id === "evidence-turn") {
      state.evidenceKey = event.target.value;
      renderEvidence();
    }
  });
  // Hovering a paper lights up every place an answer cites it. Listening on
  // the document means leaving the view also turns the highlight off.
  let lit = null;
  document.addEventListener("mouseover", (event) => {
    const ref = event.target.closest(".paper")?.dataset.ref ?? null;
    if (ref === lit) return;
    lit = ref;
    view.querySelectorAll("a.cite").forEach((cite) => cite.classList.toggle("lit", cite.dataset.ref === ref));
  });
  setInterval(() => {
    const el = $("#elapsed");
    if (el && state.live && !state.live.finished) el.textContent = clock(Date.now() - state.live.started);
  }, 1000);

  window.addEventListener("popstate", route);
  route();
  loadConversations().catch((error) => toast(error.message));
}

boot();
