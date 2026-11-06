// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (C) 2026 Rohn Springfield
// The Lyst agent bar: one line at the top of every page to talk to Lyst (typed or spoken) and watch it work.
//
// Drawn in the page (st.components.v2, no iframe, no library, no network). Python decides what everything means;
// this only draws what Python sent and reports one of:
//   {type: "submit", text}              Enter in the field (Python reads it: a place, follow, or a question)
//   {type: "place", id}                 a place picked from the suggestions
//   {type: "voice", text, confidence}   one finished utterance from the microphone
//   {type: "heard", yes}                "Did you say ...?" answered
//   {type: "follow", on}                the Follow Lyst switch
//   {type: "go", go}                    Go: the page and section where something happened
//   {type: "open", task}                open an answer
//   {type: "clear"}                     clear the conversation
//
// Voice rules (docs/VOICE.md): the microphone starts only on a press of the mic button (or Alt+M, also a press) and
// stops when the person stops or pauses. Recognition runs in the browser; no audio leaves it or is kept. Speech
// never approves or runs anything: Python sends each utterance to the voice engine, which refuses that.

const PATHS = {
  mic: '<rect x="9" y="3" width="6" height="11" rx="3"/><path d="M5 11a7 7 0 0 0 14 0"/><path d="M12 18v3"/>',
  stop: '<rect x="7" y="7" width="10" height="10" rx="2" fill="currentColor" stroke="none"/>',
  send: '<path d="M5 12h14"/><path d="m13 6 6 6-6 6"/>',
  chev: '<path d="m6 9 6 6 6-6"/>',
  go: '<path d="M7 17 17 7"/><path d="M8 7h9v9"/>',
  eye: '<path d="M2 12s3.5-7 10-7 10 7 10 7-3.5 7-10 7S2 12 2 12z"/><circle cx="12" cy="12" r="3"/>',
  place: '<path d="M4 6h16M4 12h16M4 18h10"/>',
  ask: '<path d="M4 5h16v11H9l-5 4z"/>',
  keys: '<rect x="3" y="6" width="18" height="12" rx="2"/><path d="M7 10h.01M11 10h.01M15 10h.01M7 14h10"/>',
  close: '<path d="M6 6l12 12"/><path d="M18 6 6 18"/>',
};
const svg = (n) =>
  `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true" focusable="false">${PATHS[n] || ""}</svg>`;
const esc = (s) =>
  String(s == null ? "" : s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
const norm = (s) => String(s || "").toLowerCase().replace(/[^a-z0-9 ]+/g, " ").replace(/\s+/g, " ").trim();
const Recognition = () => window.SpeechRecognition || window.webkitSpeechRecognition || null;
const store = {
  get(k) { try { return window.sessionStorage.getItem(k); } catch (_) { return null; } },
  set(k, v) { try { window.sessionStorage.setItem(k, v); } catch (_) { /* private mode */ } },
};
const GO_WORDS = /^(?:go(?: back)? to|open|show(?: me)?|take me to|jump to|navigate to|switch to)\s+(?:the\s+)?/i;

class Bar {
  constructor(host) {
    this.host = host;
    this.data = null;
    this.open = store.get("lyst.bar.open") === "1";
    this.sel = -1;
    this.items = [];
    this.rec = null;
    this.listening = false;
    this.spoken = store.get("lyst.bar.spoken");
    this.el = document.createElement("div");
    this.el.className = "b";
    this.el.innerHTML = `
      <div class="b-bar" role="search">
        <span class="b-mark" aria-hidden="true"><i></i></span>
        <div class="b-field">
          <label class="sr" for="b-q">Ask Lyst, or go to a page</label>
          <input id="b-q" class="b-q" type="text" autocomplete="off" spellcheck="false" enterkeyhint="send"
                 role="combobox" aria-autocomplete="list" aria-expanded="false" aria-controls="b-list"
                 placeholder="Ask Lyst anything, or go to a page" />
          <kbd class="b-kbd" aria-hidden="true">/</kbd>
          <ul id="b-list" class="b-list" role="listbox" aria-label="Suggestions" hidden></ul>
        </div>
        <button type="button" class="b-btn b-mic" aria-pressed="false">${svg("mic")}</button>
        <button type="button" class="b-btn b-send" aria-label="Send" hidden>${svg("send")}</button>
        <div class="b-now"></div>
        <button type="button" class="b-btn b-chev" aria-expanded="false" aria-controls="b-panel">${svg("chev")}</button>
      </div>
      <div id="b-panel" class="b-panel" role="region" aria-label="Lyst" hidden>
        <section class="b-talk" aria-label="Conversation">
          <header><h3>Conversation</h3><button type="button" class="b-link b-clear">Clear</button></header>
          <div class="b-heard"></div>
          <ol class="b-turns"></ol>
        </section>
        <section class="b-live" aria-label="Live">
          <header><h3><span class="b-pip"></span>Live</h3>
            <label class="b-switch"><input type="checkbox" class="b-follow" role="switch" /><span aria-hidden="true"></span>Follow Lyst</label>
          </header>
          <ol class="b-running"></ol>
          <h4>Recently</h4>
          <ol class="b-feed"></ol>
        </section>
      </div>
      <div class="sr b-say" role="status" aria-live="polite"></div>`;
    host.appendChild(this.el);
    const $ = (s) => this.el.querySelector(s);
    this.q = $(".b-q"); this.list = $(".b-list"); this.mic = $(".b-mic"); this.send = $(".b-send");
    this.nowEl = $(".b-now"); this.chev = $(".b-chev"); this.panel = $(".b-panel"); this.mark = $(".b-mark");
    this.turnsEl = $(".b-turns"); this.heardEl = $(".b-heard"); this.runEl = $(".b-running");
    this.feedEl = $(".b-feed"); this.followEl = $(".b-follow"); this.live = $(".b-say");
    this.q.value = store.get("lyst.bar.draft") || "";
    this.wire();
  }

  connect(fns) { this.fns = fns; }

  emit(ev) {
    if (!this.fns) return;
    this.fns.setTriggerValue("action", { ...ev, nonce: `${Date.now()}-${Math.random().toString(36).slice(2, 8)}` });
  }

  say(text) { this.live.textContent = ""; window.setTimeout(() => { this.live.textContent = text; }, 30); }

  // ------------------------------------------------------------------------------------------- wiring
  wire() {
    this.q.addEventListener("input", () => { store.set("lyst.bar.draft", this.q.value); this.suggest(); });
    this.q.addEventListener("focus", () => { if (this.q.value) this.suggest(); });
    this.q.addEventListener("blur", () => window.setTimeout(() => this.close(), 120));
    this.q.addEventListener("keydown", (e) => this.key(e));
    this.list.addEventListener("pointerdown", (e) => e.preventDefault());
    this.list.addEventListener("click", (e) => {
      const li = e.target.closest("[data-i]");
      if (li) this.pick(Number(li.dataset.i));
    });
    this.send.addEventListener("click", () => this.submit());
    this.mic.addEventListener("click", () => (this.listening ? this.stopMic() : this.startMic()));
    this.chev.addEventListener("click", () => this.toggle());
    this.followEl.addEventListener("change", () => this.emit({ type: "follow", on: this.followEl.checked }));
    this.el.querySelector(".b-clear").addEventListener("click", () => this.emit({ type: "clear" }));
    this.el.addEventListener("click", (e) => {
      const b = e.target.closest("[data-go],[data-open],[data-heard],[data-watch],[data-need],[data-ask]");
      if (!b) return;
      if (b.dataset.ask) this.emit({ type: "submit", text: b.dataset.ask });
      else if (b.dataset.go) this.emit({ type: "go", go: JSON.parse(b.dataset.go) });
      else if (b.dataset.open) this.emit({ type: "open", task: b.dataset.open });
      else if (b.dataset.heard) this.emit({ type: "heard", yes: b.dataset.heard === "yes" });
      else if (b.dataset.watch !== undefined) { this.toggle(true); this.runEl.querySelector("button")?.focus(); }
      else if (b.dataset.need !== undefined)
        this.emit({ type: "go", go: { page: "app_pages/brain.py", params: { view: "work", work: "needs you" } } });
    });
    this.onDoc = (e) => {
      const t = e.composedPath ? e.composedPath()[0] : e.target;
      const typing = t && (t.isContentEditable || /^(INPUT|TEXTAREA|SELECT)$/.test(t.tagName || ""));
      if (e.key === "/" && !typing && !e.metaKey && !e.ctrlKey && !e.altKey) {
        e.preventDefault(); this.q.focus(); this.q.select();
      } else if (e.key === "Escape" && this.open && !typing && this.el.contains(t)) {
        this.toggle(false); this.chev.focus();
      }
    };
    document.addEventListener("keydown", this.onDoc);
  }

  key(e) {
    const n = this.items.length;
    if (e.key === "ArrowDown" && n) { e.preventDefault(); this.mark_(this.sel < n - 1 ? this.sel + 1 : 0); }
    else if (e.key === "ArrowDown" && !this.q.value) { e.preventDefault(); this.toggle(true); }
    else if (e.key === "ArrowUp" && n) { e.preventDefault(); this.mark_(this.sel > 0 ? this.sel - 1 : n - 1); }
    else if (e.key === "Enter" && !e.isComposing) {
      e.preventDefault();
      if (this.sel >= 0 && n) this.pick(this.sel); else this.submit();
    } else if (e.key === "Escape") {
      if (n) { e.stopPropagation(); this.close(); }
      else if (this.q.value) { e.stopPropagation(); this.q.value = ""; store.set("lyst.bar.draft", ""); this.suggest(); }
      else if (this.open) { e.stopPropagation(); this.toggle(false); }
      else this.q.blur();
    } else if (e.code === "KeyM" && e.altKey) { e.preventDefault(); this.mic.click(); }
  }

  // --------------------------------------------------------------------------------------- suggestions
  suggest() {
    const raw = this.q.value.trim();
    this.send.hidden = !raw;
    const places = (this.data && this.data.places) || [];
    if (!raw) { this.items = []; return this.close(); }
    const w = norm(raw.replace(GO_WORDS, ""));
    const hits = w ? places
      .map((p) => {
        const label = norm(p.label);
        const words = `${label} ${p.words}`;
        const terms = w.split(" ");
        if (!terms.every((t) => words.includes(t))) return null;
        const score = (label.startsWith(w) ? 0 : label.split(" ").some((x) => x.startsWith(terms[0])) ? 1 : 2) + label.length / 100;
        return { p, score };
      })
      .filter(Boolean).sort((a, b) => a.score - b.score).slice(0, 5).map((x) => x.p) : [];
    const asking = GO_WORDS.test(raw) && hits.length ? [] : [{ ask: raw }];
    const named = hits.length && raw.split(/\s+/).length <= 2;  // "sql", "definitions": the place first
    this.items = GO_WORDS.test(raw) || named ? [...hits, ...asking] : [...asking, ...hits];
    this.list.innerHTML = this.items.map((it, i) => it.ask
      ? `<li id="b-o${i}" role="option" data-i="${i}" class="b-opt ask">${svg("ask")}<span><span class="b-o-k">Ask Lyst</span> ${esc(it.ask)}</span><kbd>↵</kbd></li>`
      : `<li id="b-o${i}" role="option" data-i="${i}" class="b-opt">${svg("place")}<span>${esc(it.label)}</span><span class="b-o-k">Go</span></li>`).join("");
    this.list.hidden = false;
    this.q.setAttribute("aria-expanded", "true");
    this.mark_(-1);
  }

  mark_(i) {
    this.sel = i;
    [...this.list.children].forEach((li, j) => li.setAttribute("aria-selected", String(j === i)));
    if (i >= 0) { this.q.setAttribute("aria-activedescendant", `b-o${i}`); this.list.children[i].scrollIntoView({ block: "nearest" }); }
    else this.q.removeAttribute("aria-activedescendant");
  }

  close() {
    this.list.hidden = true; this.sel = -1; this.items = [];
    this.q.setAttribute("aria-expanded", "false"); this.q.removeAttribute("aria-activedescendant");
  }

  pick(i) {
    const it = this.items[i];
    if (!it) return;
    if (it.ask) return this.submit();
    this.clearField();
    this.say(`Going to ${it.label}`);
    this.emit({ type: "place", id: it.id });
  }

  submit() {
    const text = this.q.value.trim();
    if (!text) return;
    this.clearField();
    this.el.classList.add("sent");
    window.setTimeout(() => this.el.classList.remove("sent"), 500);
    this.emit({ type: "submit", text });
  }

  clearField() { this.q.value = ""; store.set("lyst.bar.draft", ""); this.send.hidden = true; this.close(); }

  toggle(force) {
    this.open = typeof force === "boolean" ? force : !this.open;
    store.set("lyst.bar.open", this.open ? "1" : "0");
    this.paintOpen();
  }

  paintOpen() {
    this.panel.hidden = !this.open;
    this.el.classList.toggle("open", this.open);
    this.chev.setAttribute("aria-expanded", String(this.open));
    this.chev.setAttribute("aria-label", this.open ? "Hide conversation and live activity" : "Show conversation and live activity");
    this.chev.title = this.open ? "Hide (Esc)" : "Conversation and live activity";
  }

  // ------------------------------------------------------------------------------------------- voice
  startMic() {
    const R = Recognition();
    if (!R || !(this.data && this.data.voice)) return;
    try { window.speechSynthesis && window.speechSynthesis.cancel(); } catch (_) { /* nothing speaking */ }
    const rec = new R();
    rec.lang = document.documentElement.lang || navigator.language || "en-US";
    rec.interimResults = true;
    rec.continuous = false;
    rec.maxAlternatives = 1;
    this.before = this.q.value;
    let final = "", conf = null;
    rec.onresult = (e) => {
      let interim = "";
      for (let i = e.resultIndex; i < e.results.length; i++) {
        const r = e.results[i];
        if (r.isFinal) { final += r[0].transcript; conf = r[0].confidence || null; } else interim += r[0].transcript;
      }
      this.q.value = (final + interim).trim();
      this.el.classList.toggle("hearing", !!interim);
    };
    rec.onerror = (e) => {
      if (e.error === "not-allowed" || e.error === "service-not-allowed") this.say("The microphone is blocked for this site.");
      else if (e.error === "no-speech") this.say("Didn't hear anything.");
    };
    rec.onend = () => {
      this.listening = false; this.rec = null; this.paintMic();
      const text = final.trim();
      this.q.value = this.before || "";
      if (text) this.emit({ type: "voice", text, confidence: conf });
    };
    try { rec.start(); } catch (_) { return; }
    this.rec = rec; this.listening = true; this.paintMic();
    this.q.focus();
  }

  stopMic() { try { this.rec && this.rec.stop(); } catch (_) { /* already stopped */ } }

  paintMic() {
    const ok = !!Recognition() && !!(this.data && this.data.voice);
    this.mic.hidden = !Recognition();
    this.mic.disabled = !ok;
    this.mic.innerHTML = svg(this.listening ? "stop" : "mic");
    this.mic.setAttribute("aria-pressed", String(this.listening));
    this.mic.setAttribute("aria-label", this.listening ? "Stop listening" : "Speak to Lyst");
    this.mic.title = !ok ? "Voice is off for this workspace" : this.listening ? "Stop listening" : "Speak to Lyst (Alt+M)";
    this.el.classList.toggle("listening", this.listening);
    this.q.placeholder = this.listening ? "Listening…" : "Ask Lyst anything, or go to a page";
    if (this.listening) this.say("Listening");
  }

  speak(say) {
    if (!say || !say.text || say.id === this.spoken || !window.speechSynthesis) return;
    this.spoken = say.id; store.set("lyst.bar.spoken", say.id);
    try {
      const u = new SpeechSynthesisUtterance(say.text);
      u.lang = document.documentElement.lang || navigator.language || "en-US";
      window.speechSynthesis.speak(u);
    } catch (_) { /* no voice on this device: the reply is on screen */ }
  }

  // ------------------------------------------------------------------------------------------- render
  update(data) {
    const first = !this.data;
    this.data = data;
    if (data.open && !this.open) this.toggle(true);
    this.mark.className = `b-mark ${data.tone || "ok"}`;
    this.mark.title = data.head || "";
    this.paintOpen();
    this.paintMic();
    this.paintNow(data);
    this.paintTalk(data);
    this.paintLive(data);
    this.followEl.checked = !!data.follow;
    this.el.classList.toggle("following", !!data.follow);
    if (!first && document.activeElement === this.q && this.q.value) this.suggest();
    this.speak(data.say);
  }

  paintNow(d) {
    const now = d.now || [];
    const need = d.need || 0;
    let html = "";
    if (now.length) {
      const top = now[0];
      const pct = typeof top.progress === "number" ? `<span class="b-pct">${Math.round(top.progress * 100)}%</span>` : "";
      html += `<button type="button" class="b-chip run" data-watch title="Watch what Lyst is doing">
        <span class="b-spin" aria-hidden="true"></span><span class="b-chip-t">${esc(top.title)}</span>${pct}
        ${now.length > 1 ? `<span class="b-more">+${now.length - 1}</span>` : ""}<span class="b-watch">${svg("eye")}Watch</span></button>`;
    } else if (!need) {
      html += `<span class="b-state">${esc(d.head || "")}</span>`;
    }
    if (need) html += `<button type="button" class="b-chip wait" data-need aria-label="${need} need${need === 1 ? "s" : ""} you: open Brain › Work"><b>${need}</b><span class="b-need-t">&nbsp;need${need === 1 ? "s" : ""} you</span></button>`;
    if (this.nowEl.innerHTML !== html) this.nowEl.innerHTML = html;
  }

  paintTalk(d) {
    this.heardEl.innerHTML = d.heard
      ? `<div class="b-confirm" role="alert"><p>Did you say <q>${esc(d.heard)}</q>?</p>
          <button type="button" class="b-pill pri" data-heard="yes">Ask this</button>
          <button type="button" class="b-pill" data-heard="no">No</button></div>` : "";
    const turns = d.turns || [];
    const html = turns.length ? turns.map((t) => {
      const status = t.status ? `<span class="b-tag ${esc(t.tone)}">${esc(t.status)}</span>` : "";
      const body = t.answer ? `<p class="b-ans">${esc(t.answer)}</p>`
        : t.reply ? `<p class="b-ans">${esc(t.reply)}</p>` : t.note ? `<p class="b-note">${esc(t.note)}</p>` : "";
      const offers = (t.suggest || []).map((x) => `<button type="button" class="b-pill b-offer" data-ask="${esc(x)}">${esc(x)}</button>`).join("");
      const open = t.task && !t.talk ? `<button type="button" class="b-link" data-open="${esc(t.task)}">${t.done ? "Open answer" : "Watch it"}${svg("go")}</button>` : "";
      return `<li class="b-turn"><div class="b-q-line"><span class="b-via" title="${t.via === "voice" ? "Spoken" : "Typed"}">${svg(t.via === "voice" ? "mic" : "keys")}</span>
        <span class="b-said">${esc(t.q)}</span><span class="b-when">${esc(t.when)}</span></div>
        <div class="b-a-line">${status}${body}${open}</div>${offers ? `<div class="b-offers">${offers}</div>` : ""}</li>`;
    }).join("") : `<li class="b-empty">Ask a question, say “go to SQL”, or press the mic and speak. Lyst answers here while you keep working.</li>`;
    if (this.turnsEl.innerHTML !== html) {
      this.turnsEl.innerHTML = html;
      this.turnsEl.scrollTop = this.turnsEl.scrollHeight;
    }
    this.el.querySelector(".b-clear").hidden = !turns.length;
  }

  paintLive(d) {
    const now = d.now || [];
    const go = (g) => (g ? ` data-go="${esc(JSON.stringify(g))}"` : "");
    const runHtml = now.length ? now.map((n) => {
      const bar = typeof n.progress === "number"
        ? `<span class="b-prog" role="progressbar" aria-valuemin="0" aria-valuemax="100" aria-valuenow="${Math.round(n.progress * 100)}" aria-label="${esc(n.progressText || "Progress")}"><i style="width:${Math.max(4, Math.round(n.progress * 100))}%"></i></span>`
        : `<span class="b-prog indet" aria-hidden="true"><i></i></span>`;
      return `<li class="b-run"><div><span class="b-k">${esc(n.kind)}</span><b>${esc(n.title)}</b>
        <span class="b-sub">${esc(n.progressText || n.status || "")}</span>${bar}</div>
        ${n.go ? `<button type="button" class="b-go"${go(n.go)} aria-label="Go to ${esc(n.go.label)}">${esc(n.go.label)}${svg("go")}</button>` : ""}</li>`;
    }).join("") : `<li class="b-empty">Nothing running. ${esc(d.head || "")}${(d.parts || []).length ? ` · ${esc(d.parts.join(" · "))}` : ""}</li>`;
    if (this.runEl.innerHTML !== runHtml) this.runEl.innerHTML = runHtml;
    const feed = d.feed || [];
    const feedHtml = feed.length ? feed.map((f) => `<li class="b-ev ${esc(f.tone)}"><span class="b-ev-dot" aria-hidden="true"></span>
      <span class="b-ev-t">${esc(f.verb)} <b>${esc(f.what)}</b></span><span class="b-when">${esc(f.when)}</span>
      ${f.go ? `<button type="button" class="b-go sm"${go(f.go)} aria-label="Go to ${esc(f.go.label)}: ${esc(f.what)}">Go${svg("go")}</button>` : "<span></span>"}</li>`).join("")
      : `<li class="b-empty">Nothing yet. What Lyst does in this workspace shows up here as it happens.</li>`;
    if (this.feedEl.innerHTML !== feedHtml) this.feedEl.innerHTML = feedHtml;
    this.el.querySelector(".b-pip").className = `b-pip ${now.length ? "run" : ""}`;
  }

  destroy() {
    document.removeEventListener("keydown", this.onDoc);
    this.stopMic();
    this.el.remove();
  }
}

export default function (component) {
  const { data, parentElement, setTriggerValue } = component;
  let bar = parentElement.__lystBar;
  if (!bar) {
    bar = new Bar(parentElement);
    parentElement.__lystBar = bar;
  }
  bar.connect({ setTriggerValue });
  if (data) bar.update(data);
  return () => {
    window.setTimeout(() => {
      if (!bar.el.isConnected) { bar.destroy(); delete parentElement.__lystBar; }
    }, 0);
  };
}
