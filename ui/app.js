/* SURA UI — vanilla JS, no dependencies. */
"use strict";

const $ = (id) => document.getElementById(id);
const chatLog = $("chat-log");
const chatInput = $("chat-input");
const sendBtn = $("send-btn");
const history = []; // {role, content} for context

// Phone detection: drives the responsiveness pass (no boot theatre, no
// typewriter — the network does the revealing now, via token streaming).
const IS_PHONE = window.matchMedia("(pointer: coarse) and (max-width: 950px)").matches
  || window.matchMedia("(max-width: 700px)").matches;

// ------------------------------------------------- phone pairing (LAN share)

// When the PC runs `python -m cli serve --share`, remote devices must prove
// they hold the pairing key. It is stored once per device and attached to
// every API call. The PC itself (loopback) never needs it.
const PAIRING_STORAGE = "sura-pairing-key";
function pairingKey() {
  try { return localStorage.getItem(PAIRING_STORAGE) || ""; } catch { return ""; }
}
function storePairingKey(key) {
  try { localStorage.setItem(PAIRING_STORAGE, key); } catch { /* private mode */ }
}
function authHeaders(extra = {}) {
  const key = pairingKey();
  const headers = { ...extra };
  if (key) headers["X-SURA-Key"] = key;
  return headers;
}

function promptForPairing() {
  const params = new URLSearchParams(window.location.search);
  const fromUrl = params.get("key");
  if (fromUrl) {
    // Scanned/typed URL of the form http://ip:port/?key=xxxx — pair silently.
    storePairingKey(fromUrl.trim());
    params.delete("key");
    const clean = window.location.pathname
      + (params.toString() ? `?${params}` : "") + window.location.hash;
    window.history.replaceState(null, "", clean);
    return true;
  }
  const key = prompt("Pairing key (shown in the terminal where SURA runs):", pairingKey());
  if (key === null) return false;
  storePairingKey(key.trim());
  return true;
}

async function api(path, options = {}) {
  const response = await fetch(path, {
    ...options,
    headers: authHeaders(options.headers || {}),
  });
  if (response.status === 401) {
    if (promptForPairing()) {
      const retry = await fetch(path, {
        ...options,
        headers: authHeaders(options.headers || {}),
      });
      if (retry.ok) return retry.json();
    }
    throw new Error("Pairing key required — get it from the PC running SURA");
  }
  if (!response.ok) {
    let detail = response.statusText;
    try { detail = (await response.json()).error || detail; } catch { /* ignore */ }
    throw new Error(detail);
  }
  return response.json();
}

function post(path, payload) {
  return api(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
}

// Streaming chat: consumes the server's newline-delimited JSON events and
// calls onText for every chunk as it arrives. This is what makes replies
// feel instant on a phone — words appear in ~one round trip instead of
// after the whole generation. Falls back to nothing: callers retry with
// the classic whole-reply endpoint if this throws.
async function streamChat(message, hist, onText) {
  const response = await fetch("/api/chat/stream", {
    method: "POST",
    headers: authHeaders({ "Content-Type": "application/json" }),
    body: JSON.stringify({ message, history: hist }),
  });
  if (response.status === 401) {
    if (promptForPairing()) return streamChat(message, hist, onText);
    throw new Error("Pairing key required — get it from the PC running SURA");
  }
  if (!response.ok || !response.body) {
    let detail = response.statusText;
    try { detail = (await response.json()).error || detail; } catch { /* ignore */ }
    throw new Error(detail);
  }
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  let final = null;
  for (;;) {
    const { value, done } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    let nl;
    while ((nl = buffer.indexOf("\n")) >= 0) {
      const line = buffer.slice(0, nl).trim();
      buffer = buffer.slice(nl + 1);
      if (!line) continue;
      let event;
      try { event = JSON.parse(line); } catch { continue; }
      if (event.type === "text") {
        final = final || { reply: "", tool_trace: [] };
        final.reply += event.text;
        onText(event.text);
      } else if (event.type === "done" || event.type === "error") {
        final = {
          reply: event.reply || (final && final.reply) || "",
          tool_trace: event.tool_trace || (final && final.tool_trace) || [],
          mode: event.mode || "llm",
        };
        if (event.type === "error") final.error = true;
      }
    }
  }
  return final || { reply: "", tool_trace: [] };
}

// ------------------------------------------------------------- orb state

// The orb is SURA's face. Every state change means something:
// booting -> checking, thinking -> LLM busy, speaking -> reply rendering,
// listening -> idle + enrolled, offline -> no brain, idle -> awake & ready.
function setOrbState(state) {
  const orb = $("orb");
  if (orb) orb.dataset.state = state;
}

// ---------------------------------------------------------- boot sequence

const BOOT_LINES = [
  "core systems … online",
  "memory index … mounted",
  "permission gate … armed",
  "reasoning core … linking",
  "all systems nominal",
];

function runBootSequence() {
  const overlay = $("boot");
  if (!overlay) return Promise.resolve();
  let seen = null;
  try { seen = sessionStorage.getItem("sura-booted"); } catch { /* private mode */ }
  // Repeat visits and phones skip the theatre entirely — open and talk.
  if (seen || IS_PHONE) {
    overlay.remove();
    try { sessionStorage.setItem("sura-booted", "1"); } catch { /* ignore */ }
    return Promise.resolve();
  }
  setOrbState("booting");
  const line = $("boot-line");
  return new Promise((resolve) => {
    let i = 0;
    const tick = () => {
      if (i < BOOT_LINES.length) {
        line.textContent = BOOT_LINES[i];
        i += 1;
        setTimeout(tick, 190);
      } else {
        setTimeout(() => {
          overlay.classList.add("done");
          setTimeout(() => overlay.remove(), 600);
          try { sessionStorage.setItem("sura-booted", "1"); } catch { /* ignore */ }
          resolve();
        }, 240);
      }
    };
    tick();
  });
}

// ------------------------------------------------------------ mobile tabs

// On phones the three panels become bottom tabs (chat / packages / status).
document.querySelectorAll(".tabbar .tab").forEach((tab) => {
  tab.addEventListener("click", () => {
    document.querySelectorAll(".tabbar .tab").forEach((t) => t.classList.remove("active"));
    tab.classList.add("active");
    document.body.dataset.tab = tab.dataset.tab;
    window.scrollTo({ top: 0 });
  });
});

// ---------------------------------------------------------------- helpers

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

function toast(message, isError = false, ttl = 2600) {
  const box = el("div", `toast${isError ? " err" : ""}`, message);
  $("toasts").appendChild(box);
  setTimeout(() => { box.style.opacity = "0"; box.style.transition = "opacity 0.4s"; }, ttl);
  setTimeout(() => box.remove(), ttl + 500);
}

function timeAgo(epochSeconds) {
  const mins = Math.floor((Date.now() / 1000 - epochSeconds) / 60);
  if (mins < 1) return "just now";
  if (mins < 60) return `${mins}m ago`;
  const hours = Math.floor(mins / 60);
  if (hours < 24) return `${hours}h ago`;
  return `${Math.floor(hours / 24)}d ago`;
}

// ---------------------------------------------------- markdown-lite render

function renderInline(text) {
  // escapes HTML, then applies **bold**, `code`, *italic*
  const esc = text
    .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
  return esc
    .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>")
    .replace(/`([^`]+)`/g, "<code>$1</code>")
    .replace(/(^|\s)\*([^*\n]+)\*/g, "$1<em>$2</em>");
}

function renderMarkdown(text) {
  const container = el("div");
  const lines = String(text).split("\n");
  let list = null;          // active ul/ol
  let para = [];

  const flushPara = () => {
    if (para.length) {
      container.appendChild(el("p", "", "")).innerHTML =
        para.map(renderInline).join("<br>");
      para = [];
    }
  };
  const closeList = () => { list = null; };

  for (const raw of lines) {
    const line = raw.replace(/\s+$/, "");
    const bullet = line.match(/^\s*[-•*]\s+(.*)/);
    const numbered = line.match(/^\s*\d+[.)]\s+(.*)/);

    if (bullet || numbered) {
      flushPara();
      const wantTag = bullet ? "ul" : "ol";
      if (!list || list.tagName.toLowerCase() !== wantTag) {
        list = document.createElement(wantTag);
        container.appendChild(list);
      }
      const item = document.createElement("li");
      item.innerHTML = renderInline((bullet || numbered)[1]);
      list.appendChild(item);
      continue;
    }

    if (!line.trim()) { flushPara(); closeList(); continue; }

    // plain text line
    flushPara();
    closeList();
    para.push(line);
  }
  flushPara();
  return container;
}

// ------------------------------------------------------------------- chat

function addActions(msg, getText) {
  const wrap = el("div", "msg-actions");
  const copy = el("button", "icon-btn", "copy");
  copy.addEventListener("click", async () => {
    try {
      await navigator.clipboard.writeText(getText());
      toast("Copied to clipboard");
    } catch {
      toast("Copy failed", true);
    }
  });
  wrap.appendChild(copy);
  msg.appendChild(wrap);
}

function addMessage(kind, text, tools, opts = {}) {
  const msg = el("div", `msg ${kind}`);
  if (opts.streaming) {
    // A live bubble the token stream fills in as words arrive.
    msg.classList.add("streaming");
    msg._fullText = "";
    msg.appendStreamText = (chunk) => {
      msg._fullText += chunk;
      msg.replaceChildren(renderMarkdown(msg._fullText));
      chatLog.scrollTop = chatLog.scrollHeight;
    };
    msg.finalizeStream = (finalText, toolTrace) => {
      const body = (finalText ?? msg._fullText ?? "").trim() || "(empty reply — try again)";
      msg.classList.remove("streaming");
      msg.replaceChildren(renderMarkdown(body));
      if (toolTrace && toolTrace.length) {
        const box = el("div", "tools");
        for (const step of toolTrace) box.appendChild(el("div", "", step.tool));
        box.appendChild(el("div", "", `${toolTrace.length} tool call${toolTrace.length > 1 ? "s" : ""}`));
        msg.appendChild(box);
      }
      addActions(msg, () => body);
      chatLog.scrollTop = chatLog.scrollHeight;
    };
  } else if (kind === "agent") {
    msg.appendChild(renderMarkdown(text));
  } else {
    msg.textContent = text;
  }
  if (tools && tools.length) {
    const box = el("div", "tools");
    for (const step of tools) box.appendChild(el("div", "", step.tool));
    if (tools.length) box.appendChild(el("div", "", `${tools.length} tool call${tools.length > 1 ? "s" : ""}`));
    msg.appendChild(box);
  }
  if (kind === "agent" && !msg.classList.contains("pending") && !opts.streaming) {
    addActions(msg, () => text);
  }
  chatLog.appendChild(msg);
  chatLog.scrollTop = chatLog.scrollHeight;
  return msg;
}

// typing indicator: three pulsing dots instead of a bare "…"
function addPendingMessage() {
  const msg = el("div", "msg agent pending");
  const dots = el("span", "dots");
  dots.appendChild(el("i"));
  dots.appendChild(el("i"));
  dots.appendChild(el("i"));
  msg.appendChild(dots);
  chatLog.appendChild(msg);
  chatLog.scrollTop = chatLog.scrollHeight;
  return msg;
}

// typewriter: reveal an agent reply at reading speed, then swap in the
// full markdown-rendered version. Only used as a fallback now — when the
// server streams, the network reveals the reply and this would just add lag.
async function addTypewriterMessage(text, tools) {
  if (IS_PHONE) return addMessage("agent", text, tools);   // phones: instant
  const msg = el("div", "msg agent typing");
  chatLog.appendChild(msg);
  const plain = String(text);
  let shown = 0;
  const step = Math.max(2, Math.round(plain.length / 60));
  while (shown < plain.length) {
    shown = Math.min(plain.length, shown + step);
    msg.textContent = plain.slice(0, shown);
    chatLog.scrollTop = chatLog.scrollHeight;
    await new Promise((r) => setTimeout(r, 12));
  }
  msg.classList.remove("typing");
  msg.textContent = "";
  msg.appendChild(renderMarkdown(text));
  if (tools && tools.length) {
    const box = el("div", "tools");
    for (const step of tools) box.appendChild(el("div", "", step.tool));
    box.appendChild(el("div", "", `${tools.length} tool call${tools.length > 1 ? "s" : ""}`));
    msg.appendChild(box);
  }
  addActions(msg, () => text);
  chatLog.scrollTop = chatLog.scrollHeight;
}

// browser text-to-speech (opt-in via the speak pill)
let speakReplies = false;
$("speak-btn").addEventListener("click", () => {
  speakReplies = !speakReplies;
  const btn = $("speak-btn");
  btn.textContent = speakReplies ? "🔊 speak: on" : "🔇 speak: off";
  if (speakReplies && !window.speechSynthesis) {
    toast("Speech not available in this browser", true);
  }
});

function speakReply(text) {
  if (!speakReplies || !window.speechSynthesis) return;
  try {
    const utterance = new SpeechSynthesisUtterance(
      String(text).replace(/[*_`#>\\\[\]]/g, " ").slice(0, 600));
    utterance.rate = 1.04;
    utterance.pitch = 1.0;
    window.speechSynthesis.cancel();
    window.speechSynthesis.speak(utterance);
  } catch { /* speech is cosmetic — never break chat over it */ }
}

async function send(textOverride) {
  const text = (textOverride ?? chatInput.value).trim();
  if (!text || sendBtn.disabled) return;
  if (!textOverride) chatInput.value = "";
  addMessage("user", text);
  sendBtn.disabled = true;
  setOrbState("thinking");
  const canStream = typeof window.ReadableStream !== "undefined";
  let pending = canStream ? null : addPendingMessage();
  let live = null;
  try {
    let result;
    if (canStream) {
      // Streaming: the bubble goes live immediately and tokens fill it.
      live = addMessage("agent", "", null, { streaming: true });
      try {
        result = await streamChat(text, history, (chunk) => {
          if (live) live.appendStreamText(chunk);
        });
      } catch (streamErr) {
        // Very old server/proxy without the stream endpoint: fall back to
        // the classic whole-reply request. Pairing problems must propagate.
        if (live) { live.remove(); live = null; }
        if (/pairing/i.test(String(streamErr.message || ""))) throw streamErr;
        pending = addPendingMessage();
        result = await post("/api/chat", { message: text, history });
      }
    } else {
      result = await post("/api/chat", { message: text, history });
    }
    if (pending) pending.remove();
    setOrbState("speaking");
    if (live) {
      live.finalizeStream(result.reply, result.tool_trace);
    } else {
      await addTypewriterMessage(result.reply, result.tool_trace);
    }
    speakReply(result.reply);
    history.push({ role: "user", content: text });
    history.push({ role: "assistant", content: result.reply });
    if (history.length > 16) history.splice(0, history.length - 16);
    if (result.tool_trace && result.tool_trace.length) {
      toast(`Executed ${result.tool_trace.length} tool call${result.tool_trace.length > 1 ? "s" : ""}`);
      refreshAll();
    }
  } catch (err) {
    if (pending) pending.remove();
    if (live) live.remove();
    addMessage("agent error", `Connection problem: ${err.message}`);
  } finally {
    sendBtn.disabled = false;
    chatInput.focus();
    setOrbState("idle");
  }
}

sendBtn.addEventListener("click", () => send());
chatInput.addEventListener("keydown", (event) => {
  if (event.key === "Enter" && !event.shiftKey) {
    event.preventDefault();
    send();
  }
});

// quick chips
document.querySelectorAll(".chip").forEach(function (chip) {
  chip.addEventListener("click", function () { send(chip.dataset.ask); });
});

// fast labs bar — one tap to apply a canned preference/command to the brain.
document.querySelectorAll(".teach").forEach(function (btn) {
  btn.addEventListener("click", function () {
    var lab = btn.dataset.lab;
    var mapping = {
      improve: "SURA, improve my next thumbnail: stronger first read, less clutter, one idea only.",
      oppose: "SURA, oppose weak defaults on my next thumbnail: no tiny text, no logo roulette, no generic action pose.",
      approve: "SURA, approve this idea in principle: keep the current Subhra Gaming thumbnail direction — tighten it, don't restart it.",
      "brain-spin": "SURA, spin: give me 5 thumbnail directions for my next Subhra Gaming video, ranked by first-read strength.",
      "list-labs": "SURA, list: what thumbnail rules, preferences, and face-library gaps are active right now?",
      "clear-labs": "SURA, clear: stop any in-progress lab and reset the thumbnail ideas queue for my next video.",
    };
    var text = mapping[lab] || ("SURA, lab: " + lab);
    send(text);
  });
});

// Keep track of the last sent text so the labs bar can re-fire it.
(function () {
  var _orig = send;
  send = function (textOverride) {
    var text = (textOverride !== undefined ? textOverride : chatInput.value).trim();
    if (text) lastSendText = text;
    return _orig(textOverride);
  };
})();

// show the labs bar on desktop once the page is alive
function initTeachbar() {
  const bar = document.getElementById("teachbar");
  if (!bar) return;
  bar.hidden = IS_PHONE;
}

// ------------------------------------------------- push: laptop <-> phone

// Desktop side: a "to phone" button that drops a note into SURA's inbox.
const pushBtn = $("push-btn");
if (pushBtn && !IS_PHONE) {
  pushBtn.hidden = false;
  pushBtn.addEventListener("click", async () => {
    const text = prompt("Send to your phone (pops up there):");
    if (!text || !text.trim()) return;
    try {
      await post("/api/push", { text: text.trim() });
      toast("Pushed to paired devices 📲");
    } catch (err) {
      toast(`Push failed: ${err.message}`, true);
    }
  });
}

// Phone side: quiet polling + toast + (optional) system notification.
let lastPush = 0;
try { lastPush = Number(localStorage.getItem("sura-last-push") || 0) || 0; } catch { lastPush = 0; }

function showPushItem(item) {
  toast(`📲 ${item.text}`, false, 8000);
  if ("Notification" in window && Notification.permission === "granted") {
    try {
      const n = new Notification("SURA", { body: item.text, tag: item.id });
      n.addEventListener("click", () => { window.focus(); n.close(); });
    } catch { /* notification is best-effort */ }
  }
}

function startPushPolling() {
  if (!IS_PHONE) return;                    // pushes target the phone
  // Silent raw fetch: a missing key must not trigger the pairing prompt
  // every five seconds — loadStatus() handles pairing properly.
  setInterval(async () => {
    try {
      const response = await fetch(`/api/push/poll?since=${lastPush}`, {
        headers: authHeaders(),
      });
      if (!response.ok) return;
      const data = await response.json();
      const items = data.items || [];
      if (!items.length) return;
      for (const item of items) showPushItem(item);
      lastPush = items[items.length - 1].at;
      try { localStorage.setItem("sura-last-push", String(lastPush)); } catch { /* ignore */ }
    } catch { /* offline — poll again later */ }
  }, 5000);
}

// -------------------------------------------------- voice in the chat log
// Spoken turns (the wake-word command and SURA's spoken reply) are recorded
// by the voice listener and mirrored here, so a voice exchange can be read on
// the chat screen later — and, because the turns also go into this client's
// history, the conversation can be continued by typing.

let lastVoiceTurnAt = 0;

function showVoiceTurn(item) {
  const mine = item.role === "you";
  // Plain text on purpose: only agent bubbles render markdown, so markers
  // like ** would show up literally in the user's own bubble.
  addMessage(mine ? "user" : "agent",
    `🎙 ${mine ? "you (spoken)" : "SURA (spoken)"}: ${item.text}`);
  history.push({ role: mine ? "user" : "assistant", content: item.text });
  if (history.length > 16) history.splice(0, history.length - 16);
}

async function pollVoiceTurns() {
  try {
    const query = lastVoiceTurnAt ? `?since=${lastVoiceTurnAt}` : "?tail=20";
    // Raw fetch on purpose: a missing pairing key must not pop a prompt
    // every few seconds (loadStatus handles pairing properly).
    const response = await fetch(`/api/voice/transcript${query}`,
      { headers: authHeaders() });
    if (!response.ok) return;
    const data = await response.json();
    for (const item of data.items || []) {
      lastVoiceTurnAt = Math.max(lastVoiceTurnAt, item.at || 0);
      showVoiceTurn(item);
    }
  } catch { /* offline — try again on the next tick */ }
}

// One-tap opt-in for system notifications (phone only, only when undecided).
const notifyBtn = $("notify-btn");
if (notifyBtn && IS_PHONE && "Notification" in window
    && Notification.permission === "default") {
  notifyBtn.hidden = false;
  notifyBtn.addEventListener("click", async () => {
    try {
      await Notification.requestPermission();
    } catch { /* ignore */ }
    notifyBtn.hidden = true;
  });
}

// ----------------------------------------------------------------- status

async function loadStatus() {
  try {
    const status = await api("/api/status");

    $("agent-name").textContent = status.agent || "SURA";
    document.title = status.agent_full || document.title;

    // Shared-mode indicator (visible on the PC; the phone hides it to save space)
    const sharePill = $("share-state");
    if (sharePill) sharePill.hidden = !status.shared;

    // LLM pill — configured vs. actually answering are different things.
    const llm = $("llm-state");
    llm.innerHTML = "";
    setOrbState("thinking");
    let llmOk = null;
    if (status.llm.enabled) {
      try {
        const health = await api("/api/llm/health");
        llmOk = !!health.ok;
        llm.appendChild(el("span", `dot ${llmOk ? "on" : "err"}`));
        llm.appendChild(el("span", "",
          `${status.llm.provider} · ${status.llm.model}${llmOk ? ` · ${health.latency_ms}ms` : " · no answer"}`));
        llm.title = llmOk
          ? `Brain verified online — answered in ${health.latency_ms} ms`
          : `Key saved but the brain is not answering: ${health.detail || "unknown error"}`;
      } catch {
        llmOk = null;
        llm.appendChild(el("span", "dot off"));
        llm.appendChild(el("span", "", `${status.llm.provider} · ?`));
        llm.title = "Could not reach the brain for verification";
      }
    } else {
      llm.appendChild(el("span", "dot off"));
      llm.appendChild(el("span", "", "rule mode"));
      llm.title = "No key yet — paste your NVIDIA key in the box below, or run: python -m cli set-key nvidia <key>";
    }
    setOrbState(llmOk === false ? "offline" : "idle");

    // voice pill — reflects enrollment state from status payload
    const voice = $("voice-state");
    voice.innerHTML = "";
    const voiceState = status.voice || { enrolled: false, listening: false };
    if (voiceState.listening) {
      const bars = el("span", "mic-bars");
      for (let i = 0; i < 4; i += 1) bars.appendChild(el("i"));
      voice.appendChild(bars);
      voice.appendChild(el("span", "", "listening"));
    } else {
      voice.appendChild(el("span", `dot ${voiceState.enrolled ? "on" : "off"}`));
      voice.appendChild(el("span", "", voiceState.enrolled ? "voice locked" : "voice off"));
    }
    voice.title = voiceState.enrolled
      ? "Voiceprint enrolled — SURA answers only to Subhra"
      : "Not enrolled. Run: python -m cli enroll-voice";

    // memory pill
    const mem = status.memory || {};
    const memPill = $("memory-state");
    memPill.textContent =
      `${mem.preferences ?? 0} prefs · ${status.face_library?.total ?? 0} faces`;
    memPill.dataset.faces = String(status.face_library?.total ?? 0);
    memPill.dataset.prefs = String(mem.preferences ?? 0);

    // status panel
    const body = $("status-body");
    body.innerHTML = "";

    const row = (k, v) => {
      const line = el("div", "kv");
      line.appendChild(el("span", "k", k));
      line.appendChild(el("span", "v", v));
      body.appendChild(line);
    };
    const meter = (label, value, max, gold = false) => {
      const m = el("div", "meter");
      const top = el("div", "meter-top");
      top.appendChild(el("span", "", label));
      top.appendChild(el("span", "v", String(value)));
      const bar = el("div", "bar");
      const fill = el("i");
      const pct = max > 0 ? Math.min(100, Math.round((value / max) * 100)) : 0;
      fill.style.width = `${pct}%`;
      if (gold) fill.classList.add("gold");
      bar.appendChild(fill);
      m.appendChild(top); m.appendChild(bar);
      body.appendChild(m);
      return m;
    };

    const faceTotal = status.face_library?.total ?? 0;
    meter("Face references", faceTotal, 16, true);      // 8 expressions x 2 photos
    const missing = status.face_library?.missing ?? [];
    if (missing.length) row("Missing expressions", missing.slice(0, 4).join(", ") + (missing.length > 4 ? ` +${missing.length - 4}` : ""));

    for (const brand of status.brands ?? []) {
      const line = el("div", "kv");
      line.appendChild(el("span", "k", brand.name));
      const badge = brand.identity_filled
        ? el("span", "badge ok", "filled")
        : el("span", "badge warn", "unfilled");
      const wrap = el("span");
      wrap.appendChild(badge);
      line.appendChild(wrap);
      body.appendChild(line);
    }

    const memStats = mem.stats || {};
    const noteCount = Object.values(memStats.notes || {}).reduce((a, b) => a + b, 0);
    if (noteCount) row("Memory notes", String(noteCount));
    if (memStats.corrections) row("Corrections logged", String(memStats.corrections));

    // anywhere access: Tailscale state (works from any network when running)
    try {
      const ts = await api("/api/tailscale");
      row("Anywhere access",
        !ts.installed ? "install Tailscale (free)"
          : !ts.running ? "Tailscale not connected"
            : (ts.dns || ts.ip || "connected"));
    } catch { /* optional endpoint */ }

      // reveal the editing panel only once styles exist
    const editingPanel = $("editing-panel");
    editingPanel.hidden = !((status.editing && status.editing.styles));
    if (editingPanel) editingPanel.dataset.ready = (status.editing && status.editing.styles) ? "" : "";
    startPushPolling();   // phone: begin listening for laptop pushes
  } catch (err) {
    $("status-body").innerHTML = "";
    $("status-body").appendChild(el("div", "empty", `status unavailable: ${err.message}`));
  }
}

// ----------------------------------------------------- learned preferences

async function loadPrefs() {
  const body = $("prefs-body");
  try {
    const data = await api("/api/preferences");
    body.innerHTML = "";
    if (!data.preferences.length) {
      body.appendChild(el("div", "empty", "none yet — correct the agent and it learns"));
      return;
    }
    const order = { established: 0, high: 1, medium: 2, low: 3, unknown: 4 };
    const sorted = [...data.preferences].sort(
      (a, b) => (order[a.confidence] ?? 9) - (order[b.confidence] ?? 9) || b.evidence - a.evidence
    );
    for (const pref of sorted.slice(0, 8)) {
      const item = el("div", "pref");
      const line = el("div", "rule");
      line.appendChild(el("span", `conf ${pref.confidence}`, pref.confidence));
      line.appendChild(document.createTextNode(" " + pref.rule));
      item.appendChild(line);
      item.appendChild(el("div", "meta",
        `scope: ${pref.scope} · evidence: ${pref.evidence}`));
      body.appendChild(item);
    }
    if (sorted.length > 8) body.appendChild(el("div", "empty", `+${sorted.length - 8} more…`));
  } catch (err) {
    body.innerHTML = "";
    body.appendChild(el("div", "empty", `unavailable: ${err.message}`));
  }
}

const teachInput = $("teach-input");
teachInput.addEventListener("keydown", async (event) => {
  if (event.key !== "Enter") return;
  const rule = teachInput.value.trim();
  if (!rule) return;
  teachInput.value = "";
  try {
    const result = await post("/api/learn", { rule, scope: "general" });
    toast("SURA learned a new preference");
    addMessage("agent", result.message);
    loadPrefs();
  } catch (err) {
    toast(`Could not record: ${err.message}`, true);
  }
});

// ---------------------------------------------------------------- packages

function renderPackageMarkdown(text, view) {
  view.innerHTML = "";
  let list = null;
  for (const raw of String(text).split("\n")) {
    const line = raw.replace(/\s+$/, "");
    if (line.startsWith("# ")) {
      view.appendChild(el("h3", "", line.slice(2)));
    } else if (line.startsWith("## ")) {
      view.appendChild(el("h3", "", "› " + line.slice(3)));
    } else if (/^\s*[-•]\s+/.test(line)) {
      if (!list) { list = document.createElement("ul"); view.appendChild(list); }
      const item = document.createElement("li");
      item.innerHTML = renderInline(line.replace(/^\s*[-•]\s+/, ""));
      list.appendChild(item);
    } else {
      list = null;
      if (line.trim()) {
        const p = document.createElement("p");
        p.innerHTML = renderInline(line);
        view.appendChild(p);
      }
    }
  }
}

async function loadPackages() {
  const box = $("packages");
  try {
    const data = await api("/api/packages");
    box.innerHTML = "";
    if (!data.packages.length) {
      box.appendChild(el("div", "empty", "None yet — run the thumbnail workflow."));
      return;
    }
    for (const pkg of data.packages) {
      const item = el("div", "pkg");
      item.appendChild(el("span", "", pkg.name));
      item.appendChild(el("span", "when", timeAgo(pkg.modified)));
      item.addEventListener("click", () => showPackage(pkg.name, item));
      box.appendChild(item);
    }
  } catch (err) {
    box.innerHTML = "";
    box.appendChild(el("div", "empty", `unavailable: ${err.message}`));
  }
}

async function showPackage(name, item) {
  document.querySelectorAll(".pkg.active").forEach((n) => n.classList.remove("active"));
  item.classList.add("active");
  const view = $("package-view");
  view.hidden = false;
  try {
    const data = await api(`/api/package?name=${encodeURIComponent(name)}`);
    view.innerHTML = "";
    view.appendChild(el("div", "pkg-meta", `${name} · ${timeAgo(data.modified ?? Date.now() / 1000)}`));
    renderPackageMarkdown(data.markdown || data.error || "", view);
  } catch (err) {
    view.textContent = `Could not load: ${err.message}`;
  }
}

// ------------------------------------------------------------------ search

const searchInput = $("search-input");
searchInput.addEventListener("keydown", async (event) => {
  if (event.key !== "Enter") return;
  const query = searchInput.value.trim();
  if (!query) return;
  const hits = $("search-hits");
  try {
    const data = await api(`/api/search?q=${encodeURIComponent(query)}`);
    hits.innerHTML = "";
    if (!data.results.length) {
      hits.appendChild(el("div", "empty", "no matches"));
      return;
    }
    for (const hit of data.results) {
      const item = el("div", "hit");
      item.appendChild(el("div", "name", `${hit.memory_class}/${hit.name}`));
      item.appendChild(el("div", "excerpt", hit.excerpt));
      hits.appendChild(item);
    }
  } catch (err) {
    hits.innerHTML = "";
    hits.appendChild(el("div", "empty", `search failed: ${err.message}`));
  }
});

// ------------------------------------------------------ editing (Tier 4, v1)

async function loadEditing() {
  const body = $("editing-body");
  try {
    const data = await api("/api/editing/plans");
    body.innerHTML = "";
    if (!data.plans.length) {
      body.appendChild(el("div", "empty", "no edit plans yet"));
      return;
    }
    for (const plan of data.plans) {
      const item = el("div", "pkg");
      item.appendChild(el("span", "", plan.name));
      item.appendChild(el("span", "when", timeAgo(plan.modified)));
      item.addEventListener("click", async () => {
        document.querySelectorAll("#editing-body .pkg.active").forEach((n) => n.classList.remove("active"));
        item.classList.add("active");
        try {
          const full = await api(`/api/editing/plan?name=${encodeURIComponent(plan.name)}`);
          const view = el("div", "pkg-meta");
          view.textContent = `${plan.name} · ${full.plan.topic || ""}`;
          body.appendChild(view);
          renderPackageMarkdown(full.plan.markdown || "", view);
        } catch (err) {
          toast(`Could not load plan: ${err.message}`, true);
        }
      });
      body.appendChild(item);
    }
  } catch {
    body.innerHTML = "";
    body.appendChild(el("div", "empty", "editing plans unavailable"));
  }
}

const editLinkInput = $("edit-link-input");
editLinkInput.addEventListener("keydown", async (event) => {
  if (event.key !== "Enter") return;
  const text = editLinkInput.value.trim();
  if (!text) return;
  editLinkInput.value = "";
  try {
    const result = await post("/api/editing/learn", { text });
    toast(result.saved ? "Style profile saved" : "Noted (keep pasting links/notes)");
    addMessage("agent", result.message);
    loadEditing();
  } catch (err) {
    toast(`Could not save: ${err.message}`, true);
  }
});

$("edit-plan-btn").addEventListener("click", async () => {
  const topic = prompt("Video topic?", "my next Subhra Gaming video");
  if (topic === null) return;
  try {
    const result = await post("/api/editing/plan", { topic });
    addMessage("agent", result.message);
    loadEditing();
  } catch (err) {
    toast(`Could not build plan: ${err.message}`, true);
  }
});

// --------------------------------------------------------------------- boot

function refreshAll() {
  loadStatus();
  loadPrefs();
  loadPackages();
  loadEditing();
}

// The app window (installed PWA, phone, or a browser tab) can open a second
// or two before the login launcher's server finishes binding. Everything
// below needs a live server, so wait for one instead of rendering a dead UI.
function initTeachbar() {
  const bar = document.getElementById("teachbar");
  if (!bar) return;
  bar.hidden = IS_PHONE;
}

async function waitForServer() {
  let announced = false;
  for (let attempt = 0; attempt < 90; attempt += 1) {
    try {
      await fetch("/api/status", { headers: authHeaders() });
      return;                    // any HTTP reply means the server is up
    } catch {
      setOrbState("booting");
      if (!announced) {
        announced = true;
        addMessage("agent",
          "⏳ **Starting SURA…** the local server is coming online — hang on a second.");
      }
      await new Promise((resolve) => setTimeout(resolve, 1000));
    }
  }
}

(async () => {
  await runBootSequence();
  await waitForServer();
  initTeachbar();
  refreshAll();
  pollVoiceTurns();
  setInterval(pollVoiceTurns, 3000);
  addMessage("agent",
    "**SURA online.** Ask me anything — replies stream in live now, no more " +
    "waiting. I can run the thumbnail workflow, search memory, and learn your " +
    "preferences. From your laptop, use **📲 to phone** to flick notes and " +
    "links straight to this device.");
})();

// --------------------------------------------------- brain pill + key save

async function saveKey() {
  const field = $("setkey-input");
  const key = field.value.trim();
  if (!key) return;
  field.value = "";
  try {
    const result = await post("/api/set-key", { key });
    if (result.verified) {
      toast(`Brain online: ${result.provider} (${result.health.latency_ms} ms)`);
    } else {
      toast("Key saved — but the brain did not answer. Check the chat.", true);
    }
    addMessage("agent", result.message);
    loadStatus();
  } catch (err) {
    toast(`Could not save key: ${err.message}`, true);
  }
}
$("setkey-input").addEventListener("keydown", (event) => {
  if (event.key === "Enter") saveKey();
});

// --------------------------------------------------------- PWA installation

let deferredPrompt = null;
window.addEventListener("beforeinstallprompt", (event) => {
  event.preventDefault();
  deferredPrompt = event;
  $("install-btn").hidden = false;
});
$("install-btn").addEventListener("click", async () => {
  if (!deferredPrompt) return;
  deferredPrompt.prompt();
  const choice = await deferredPrompt.userChoice;
  if (choice.outcome === "accepted") toast("SURA pinned — home-screen app installed");
  deferredPrompt = null;
  $("install-btn").hidden = true;
});
window.addEventListener("appinstalled", () => {
  $("install-btn").hidden = true;
  toast("SURA is now an app");
});
if ("serviceWorker" in navigator) {
  navigator.serviceWorker.register("/sw.js").catch(() => { /* offline shell is optional */ });
}
