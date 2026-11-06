// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (C) 2026 Rohn Springfield
// The activity zone: a Kanban board of what Lyst is doing and wants to do, the same on every page.
//
// Drawn in the page (st.components.v2, no iframe, no library, no network). It only draws the cards Python built and
// reports gestures back; it never decides what a gesture does. Every report is one of:
//   {type: "open", card}            click, Enter
//   {type: "op", card, op}          a card's action button or menu item
//   {type: "move", card, to}        a drop on another column (pointer or keyboard)
//   {type: "go", card, page}        "Go to Data"
// Python then opens the card, or asks the person to confirm before anything changes.
//
// Gestures: click opens; drag moves (columns say what a drop does, and stand back when a card cannot go there);
// press and hold (or the ... button, or M / Shift+F10) opens the card's actions; on touch, hold then drag.
// Keyboard: arrows move between cards, Space picks a card up, arrows choose a column, Space drops, Escape cancels.

const HOLD = 420; // ms before a press becomes a hold
const SLOP = 6; // px a pointer may move before a press becomes a drag (or, on touch, a scroll)
const CAP = 5; // cards a column shows before "Show more"

const PATHS = {
  open: '<path d="M7 17 17 7"/><path d="M8 7h9v9"/>',
  info: '<circle cx="12" cy="12" r="9"/><path d="M12 11v5"/><path d="M12 8h.01"/>',
  reply: '<path d="M9 14 4 9l5-5"/><path d="M4 9h10a6 6 0 0 1 6 6v3"/>',
  arrow: '<path d="M5 12h14"/><path d="m13 6 6 6-6 6"/>',
  pause: '<path d="M9 5v14"/><path d="M15 5v14"/>',
  play: '<path d="m7 4 13 8-13 8z"/>',
  refresh: '<path d="M20 11a8 8 0 1 0-2.3 5.7"/><path d="M20 4v7h-7"/>',
  close: '<path d="M6 6l12 12"/><path d="M18 6 6 18"/>',
  check: '<path d="m5 12 5 5L20 7"/>',
  clock: '<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/>',
  dots: '<circle cx="5" cy="12" r="1.4" fill="currentColor" stroke="none"/><circle cx="12" cy="12" r="1.4" fill="currentColor" stroke="none"/><circle cx="19" cy="12" r="1.4" fill="currentColor" stroke="none"/>',
  task: '<path d="M4 5h16v11H9l-5 4z"/><path d="M8 9h8M8 12h5"/>',
  activity: '<path d="M13 3 5 14h6l-1 7 8-11h-6z"/>',
  approval: '<path d="M12 3 5 6v6c0 4 3 7.5 7 9 4-1.5 7-5 7-9V6z"/><path d="m9 12 2 2 4-4"/>',
  finding: '<path d="M3 12h4l3-7 4 14 3-7h4"/>',
  suggestion: '<path d="M12 3v3M12 18v3M3 12h3M18 12h3M6.3 6.3l2.1 2.1M15.6 15.6l2.1 2.1M6.3 17.7l2.1-2.1M15.6 8.4l2.1-2.1"/>',
  move: '<path d="M12 3v18M3 12h18"/><path d="m9 6 3-3 3 3M9 18l3 3 3-3M6 9l-3 3 3 3M18 9l3 3-3 3"/>',
};

const svg = (name) =>
  `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true" focusable="false">${PATHS[name] || ""}</svg>`;

const esc = (s) =>
  String(s == null ? "" : s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);

const reducedMotion = () => window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;

function storeGet(k) {
  try { return window.sessionStorage.getItem(k); } catch (_) { return null; }
}
function storeSet(k, v) {
  try { window.sessionStorage.setItem(k, v); } catch (_) { /* private mode: the default holds */ }
}

function plural(n, one, many) { return `${n} ${n === 1 ? one : many || one + "s"}`; }

function summary(cards) {
  const n = { suggested: 0, needs: 0, working: 0, done: 0 };
  cards.forEach((c) => { n[c.col] = (n[c.col] || 0) + 1; });
  const parts = [];
  if (n.needs) parts.push(`<b>${n.needs}</b> need${n.needs === 1 ? "s" : ""} you`);
  if (n.working) parts.push(`<b>${n.working}</b> in progress`);
  if (n.suggested) parts.push(`<b>${n.suggested}</b> ${n.suggested === 1 ? "suggestion" : "suggestions"}`);
  if (!parts.length) parts.push(n.done ? `<b>${n.done}</b> done this week` : "All quiet");
  return { n, html: parts.join(" · "), text: parts.join(" · ").replace(/<[^>]+>/g, "") };
}

// ------------------------------------------------------------------------------------------------- the board
class Zone {
  constructor(host) {
    this.host = host;
    this.el = document.createElement("div");
    this.el.className = "z";
    host.appendChild(this.el);
    this.expanded = {};
    this.focusId = null;
    this.menu = null;
    this.press = null;
    this.drag = null;
    this.lift = null;
    this.pending = new Set();
    this.queued = null;
    this.prevCols = null;
    this.seq = 0;
    this.suppressClick = false;
    this.el.addEventListener("click", (e) => this.onClick(e));
    this.el.addEventListener("keydown", (e) => this.onKey(e));
    this.el.addEventListener("pointerdown", (e) => this.onPointerDown(e));
    this.el.addEventListener("focusin", (e) => {
      const c = e.target.closest && e.target.closest(".z-card");
      if (c) this.focusId = c.dataset.id;
    });
    this.el.addEventListener("contextmenu", (e) => {
      const c = e.target.closest(".z-card");
      if (c) { e.preventDefault(); this.openMenu(c.dataset.id); }
    });
    // while a card is carried on a touch screen, the page must not scroll under the finger
    this.el.addEventListener("touchmove", (e) => { if (this.drag || (this.press && this.press.held)) e.preventDefault(); },
      { passive: false });
  }

  connect(api) { this.api = api; }

  update(data) {
    if (this.drag || this.lift || this.menu) { this.queued = data; return; }
    this.data = data;
    const key = `lyst-zone-scope-${data.page}`;
    if (!this.scope) this.scope = storeGet(key) || "page";
    this.render();
  }

  flush() {
    if (this.queued && !this.drag && !this.lift && !this.menu) {
      const d = this.queued;
      this.queued = null;
      this.update(d);
    }
  }

  // ---------------------------------------------------------------------------------------------- reading
  shown() {
    const d = this.data;
    return this.scope === "all" ? d.cards : d.cards.filter((c) => (c.pages || [c.page]).includes(d.page));
  }
  card(id) { return this.data.cards.find((c) => c.id === id); }
  col(id) { return this.data.columns.find((c) => c.id === id); }
  moveTo(card, colId) { return (card.ops || []).find((o) => o.to === colId); }
  cardEl(id) { return this.el.querySelector(`.z-card[data-id="${CSS.escape(id)}"]`); }
  say(text) {
    const live = this.el.querySelector(".z-live");
    if (!live) return;
    live.textContent = "";
    window.setTimeout(() => { live.textContent = text; }, 30);
  }

  // ---------------------------------------------------------------------------------------------- drawing
  render() {
    const d = this.data;
    const before = this.rects();
    const hadFocus = this.host.activeElement && this.el.contains(this.host.activeElement);
    const cards = this.shown();
    const all = summary(d.cards);
    const here = summary(d.cards.filter((c) => (c.pages || [c.page]).includes(d.page)));
    const pageName = d.pageNames[d.page] || d.page;
    const now = this.scope === "all" ? all : here;
    const tone = now.n.working ? "run" : now.n.needs ? "wait" : "";
    const scope = `<div class="z-scope" role="radiogroup" aria-label="Which activity to show">
        <button type="button" role="radio" data-scope="page" aria-checked="${this.scope !== "all"}">On ${esc(pageName)} <b>${here.n.suggested + here.n.needs + here.n.working + here.n.done}</b></button>
        <button type="button" role="radio" data-scope="all" aria-checked="${this.scope === "all"}">Everywhere <b>${d.cards.length}</b></button>
      </div>`;
    const head = `<header class="z-head">
        <div class="z-title"><span class="z-dot ${tone}" aria-hidden="true"></span><h2 id="z-h-${esc(d.page)}">Activity</h2>
          <span class="z-sum">${cards.length ? now.html : ""}</span></div>${scope}</header>`;
    const help = `<p class="sr" id="z-help-${esc(d.page)}">Press Enter to open. Press Space to pick the card up, then the left
        and right arrow keys to choose a column and Space to drop it. Press M for the card's actions.</p>`;
    let body;
    if (!cards.length) {
      const elsewhere = d.cards.length;
      body = `<div class="z-quiet" role="status">${
        this.scope === "all" || !elsewhere
          ? `<span><b>All quiet.</b> When Lyst suggests something, needs you or starts work, it appears here.</span>`
          : `<span><b>Nothing on ${esc(pageName)} right now.</b> Elsewhere: ${all.html}.</span>
             <button type="button" data-scope="all">Show everything</button>`
      }</div>`;
    } else {
      body = `<div class="z-cols">${d.columns.map((col) => this.column(col, cards.filter((c) => c.col === col.id))).join("")}</div>
        <div class="z-foot" aria-hidden="true"><span class="keys">Drag a card to act on it <span class="sep">·</span> hold
        or <kbd>M</kbd> for actions <span class="sep">·</span> <kbd>Space</kbd> to move with the keyboard</span></div>`;
    }
    this.el.setAttribute("role", "region");
    this.el.setAttribute("aria-labelledby", `z-h-${d.page}`);
    this.el.innerHTML = head + help + body + `<div class="z-live sr" aria-live="polite"></div>`;
    this.roving();
    this.flip(before);
    this.announceMoves();
    if (hadFocus && this.focusId) {
      const c = this.cardEl(this.focusId);
      if (c) c.focus({ preventScroll: true });
    }
  }

  column(col, cards) {
    const open = this.expanded[col.id];
    const shown = open ? cards : cards.slice(0, CAP);
    const rest = cards.length - shown.length;
    const list = cards.length
      ? `<ul class="z-list" role="list">${shown.map((c) => `<li>${this.cardHtml(c)}</li>`).join("")}</ul>`
      : `<div class="z-empty">${esc(col.empty)}</div>`;
    const more = rest > 0 || (open && cards.length > CAP)
      ? `<button type="button" class="z-more-cards" data-expand="${esc(col.id)}" aria-expanded="${!!open}">${
          open ? "Show fewer" : `Show ${rest} more`}</button>`
      : "";
    return `<section class="z-col${cards.length ? " has" : ""}" data-col="${esc(col.id)}" data-tone="${esc(col.tone)}"
        aria-label="${esc(col.title)}, ${plural(cards.length, "card")}. ${esc(col.hint)}">
      <header class="z-col-head" title="${esc(col.hint)}"><span class="z-col-mark" aria-hidden="true"></span>
        <h3>${esc(col.title)}</h3><span class="z-count" aria-hidden="true">${cards.length}</span></header>
      <div class="z-drop" aria-hidden="true"></div>${list}${more}</section>`;
  }

  cardHtml(c) {
    const d = this.data;
    const primary = (c.ops || []).find((o) => o.primary);
    const other = this.scope === "all" && c.page !== d.page ? `<span class="z-page">${esc(d.pageNames[c.page] || c.page)}</span>` : "";
    const progress = c.col === "working"
      ? (typeof c.progress === "number"
        ? `<div class="z-progress" role="progressbar" aria-label="Steps done" aria-valuemin="0" aria-valuemax="100" aria-valuenow="${Math.round(c.progress * 100)}" aria-valuetext="${esc(c.progressText || "")}"><i style="width:${Math.max(4, Math.round(c.progress * 100))}%"></i></div>`
        : `<div class="z-progress indet" aria-hidden="true"><i></i></div>`)
      : "";
    const label = [`${c.kindLabel}: ${c.title}`, c.status, c.when].filter(Boolean).join(". ") + ".";
    return `<article class="z-card t-${esc(c.tone)}${this.pending.has(c.id) ? " pending" : ""}" data-id="${esc(c.id)}" tabindex="-1"
        aria-roledescription="card" aria-label="${esc(label)}" aria-describedby="z-help-${esc(d.page)}">
      <div class="z-card-top"><span class="z-kind">${svg(c.kind)}${esc(c.kindLabel)}</span><span class="z-when">${esc(c.when)}</span>
        <button type="button" class="z-menu-btn" tabindex="-1" aria-haspopup="menu" aria-expanded="false"
          aria-label="Actions for ${esc(c.title)}" data-menu>${svg("dots")}</button></div>
      <p class="z-card-title">${esc(c.title)}</p>${c.meta ? `<p class="z-card-meta">${esc(c.meta)}</p>` : ""}
      <div class="z-card-foot">${c.status ? `<span class="z-status s-${esc(c.tone)}"><i aria-hidden="true"></i><span>${esc(c.status)}</span></span>` : ""}${other}
        ${primary ? `<button type="button" class="z-act" tabindex="-1" data-op="${esc(primary.id)}">${svg(primary.icon)}${esc(primary.label)}</button>` : ""}</div>
      ${progress}${c.progressText && c.col === "working" ? `<div class="z-progress-text">${esc(c.progressText)}</div>` : ""}
      <div class="z-hold" aria-hidden="true"></div></article>`;
  }

  roving() {
    const all = [...this.el.querySelectorAll(".z-card")];
    if (!all.length) return;
    const cur = all.find((c) => c.dataset.id === this.focusId) || all[0];
    all.forEach((c) => { c.tabIndex = c === cur ? 0 : -1; });
  }

  rects() {
    const out = new Map();
    this.el.querySelectorAll(".z-card").forEach((c) => out.set(c.dataset.id, c.getBoundingClientRect()));
    return out;
  }

  // cards that moved slide from where they were; new ones rise in
  flip(before) {
    if (!before.size || reducedMotion()) return;
    this.el.querySelectorAll(".z-card").forEach((c) => {
      const was = before.get(c.dataset.id);
      if (!was) { c.classList.add("enter"); return; }
      const now = c.getBoundingClientRect();
      const dx = was.left - now.left, dy = was.top - now.top;
      if (Math.abs(dx) < 1 && Math.abs(dy) < 1) return;
      c.style.transition = "none";
      c.style.transform = `translate(${dx}px, ${dy}px)`;
      requestAnimationFrame(() => {
        c.style.transition = "transform var(--z-slow) var(--z-spring)";
        c.style.transform = "";
        window.setTimeout(() => { c.style.transition = ""; }, 400);
      });
    });
  }

  announceMoves() {
    const cols = new Map(this.data.cards.map((c) => [c.id, c.col]));
    if (this.prevCols) {
      const moved = this.data.cards.filter((c) => this.prevCols.has(c.id) && this.prevCols.get(c.id) !== c.col).slice(0, 2);
      if (moved.length) this.say(moved.map((c) => `${c.title} is now in ${this.col(c.col).title}.`).join(" "));
    }
    this.prevCols = cols;
  }

  // ---------------------------------------------------------------------------------------------- telling Python
  send(ev) {
    if (!this.api) return;
    this.seq += 1;
    ev.nonce = `${Date.now()}-${this.seq}`;
    if (ev.card) {
      this.pending.add(ev.card);
      const el = this.cardEl(ev.card);
      if (el) el.classList.add("pending");
      window.setTimeout(() => {
        this.pending.delete(ev.card);
        const e2 = this.cardEl(ev.card);
        if (e2) e2.classList.remove("pending");
      }, 1600);
    }
    this.api.setTriggerValue("action", ev);
  }

  // ---------------------------------------------------------------------------------------------- clicks
  onClick(e) {
    const t = e.target;
    const scopeBtn = t.closest("[data-scope]");
    if (scopeBtn) { this.setScope(scopeBtn.dataset.scope); return; }
    const exp = t.closest("[data-expand]");
    if (exp) { this.expanded[exp.dataset.expand] = !this.expanded[exp.dataset.expand]; this.render(); return; }
    const cardEl = t.closest(".z-card");
    if (!cardEl) return;
    if (this.suppressClick) { this.suppressClick = false; return; }
    const id = cardEl.dataset.id;
    if (t.closest("[data-menu]")) { e.stopPropagation(); this.openMenu(id); return; }
    const op = t.closest("[data-op]");
    if (op) { this.send({ type: "op", card: id, op: op.dataset.op }); return; }
    this.send({ type: "open", card: id });
  }

  setScope(s) {
    this.scope = s === "all" ? "all" : "page";
    storeSet(`lyst-zone-scope-${this.data.page}`, this.scope);
    this.render();
    const btn = this.el.querySelector(`.z-scope [data-scope="${this.scope}"]`);
    if (btn) btn.focus({ preventScroll: true });
    this.say(this.scope === "all" ? "Showing activity from every page." : `Showing activity on ${this.data.pageNames[this.data.page]}.`);
  }

  // ---------------------------------------------------------------------------------------------- keyboard
  onKey(e) {
    if (this.menu) return; // the menu handles its own keys
    const scopeBtn = e.target.closest && e.target.closest(".z-scope button");
    if (scopeBtn && (e.key === "ArrowLeft" || e.key === "ArrowRight")) {
      e.preventDefault();
      this.setScope(this.scope === "all" ? "page" : "all");
      return;
    }
    const el = e.target.closest && e.target.closest(".z-card");
    if (!el || e.target !== el) return;
    const id = el.dataset.id;
    const k = e.key;
    if (this.lift) {
      if (k === "ArrowLeft" || k === "ArrowRight") { e.preventDefault(); this.liftStep(k === "ArrowRight" ? 1 : -1); }
      else if (k === " " || k === "Enter") { e.preventDefault(); this.liftDrop(); }
      else if (k === "Escape" || k === "Tab") { if (k === "Escape") e.preventDefault(); this.liftCancel(true); }
      return;
    }
    if (k === "Enter") { e.preventDefault(); this.send({ type: "open", card: id }); }
    else if (k === " ") { e.preventDefault(); this.liftStart(id); }
    else if (k === "m" || k === "M" || k === "ContextMenu" || (k === "F10" && e.shiftKey)) { e.preventDefault(); this.openMenu(id); }
    else if (k === "ArrowDown" || k === "ArrowUp" || k === "Home" || k === "End") { e.preventDefault(); this.focusInColumn(el, k); }
    else if (k === "ArrowLeft" || k === "ArrowRight") { e.preventDefault(); this.focusAcross(el, k === "ArrowRight" ? 1 : -1); }
  }

  focusCard(el) {
    if (!el) return;
    this.el.querySelectorAll(".z-card").forEach((c) => { c.tabIndex = -1; });
    el.tabIndex = 0;
    el.focus();
    this.focusId = el.dataset.id;
  }

  focusInColumn(el, k) {
    const list = [...el.closest(".z-col").querySelectorAll(".z-card")];
    const i = list.indexOf(el);
    const j = k === "Home" ? 0 : k === "End" ? list.length - 1 : Math.min(list.length - 1, Math.max(0, i + (k === "ArrowDown" ? 1 : -1)));
    this.focusCard(list[j]);
  }

  focusAcross(el, step) {
    const cols = [...this.el.querySelectorAll(".z-col")];
    const ci = cols.indexOf(el.closest(".z-col"));
    const row = [...cols[ci].querySelectorAll(".z-card")].indexOf(el);
    for (let j = ci + step; j >= 0 && j < cols.length; j += step) {
      const list = [...cols[j].querySelectorAll(".z-card")];
      if (list.length) { this.focusCard(list[Math.min(row, list.length - 1)]); return; }
    }
  }

  markTargets(card) {
    this.el.classList.add("dragging");
    this.el.querySelectorAll(".z-col").forEach((colEl) => {
      const id = colEl.dataset.col;
      const op = this.moveTo(card, id);
      colEl.classList.remove("ok", "no", "over", "origin");
      if (id === card.col) colEl.classList.add("origin");
      else if (op) {
        colEl.classList.add("ok");
        colEl.querySelector(".z-drop").innerHTML = `${svg(op.icon)}<span>Drop to ${esc(op.label.toLowerCase())}</span>`;
      } else colEl.classList.add("no");
    });
  }

  clearTargets() {
    this.el.classList.remove("dragging");
    this.el.querySelectorAll(".z-col").forEach((c) => c.classList.remove("ok", "no", "over", "origin"));
  }

  liftStart(id) {
    const card = this.card(id);
    if (!card) return;
    const moves = (card.ops || []).filter((o) => o.to);
    const el = this.cardEl(id);
    if (!moves.length) { this.say(`${card.title} cannot be moved. Press M for its actions.`); return; }
    this.lift = { id, target: card.col };
    el.classList.add("lifted");
    this.markTargets(card);
    this.el.querySelector(`.z-col[data-col="${card.col}"]`).classList.add("over");
    this.say(`Picked up ${card.title}. It can move to ${moves.map((m) => `${this.col(m.to).title} to ${m.label.toLowerCase()}`).join(", ")}. Use the left and right arrows, Space to drop, Escape to cancel.`);
  }

  liftStep(step) {
    const order = this.data.columns.map((c) => c.id);
    const card = this.card(this.lift.id);
    let i = order.indexOf(this.lift.target) + step;
    i = (i + order.length) % order.length;
    this.lift.target = order[i];
    this.el.querySelectorAll(".z-col").forEach((c) => c.classList.toggle("over", c.dataset.col === order[i]));
    const op = this.moveTo(card, order[i]);
    const name = this.col(order[i]).title;
    this.say(order[i] === card.col ? `${name}: where it is now.` : op ? `${name}: drop to ${op.label.toLowerCase()}.` : `${name}: it cannot move here.`);
  }

  liftDrop() {
    const { id, target } = this.lift;
    const card = this.card(id);
    const op = this.moveTo(card, target);
    if (target === card.col) { this.liftCancel(true); return; }
    if (!op) { this.say(`${card.title} cannot move to ${this.col(target).title}.`); return; }
    this.liftCancel(false);
    this.say(`${op.label}: confirm to continue.`);
    this.send({ type: "move", card: id, to: target });
  }

  liftCancel(announce) {
    if (!this.lift) return;
    const el = this.cardEl(this.lift.id);
    if (el) el.classList.remove("lifted");
    this.clearTargets();
    if (announce) this.say("Put back.");
    this.lift = null;
    this.flush();
  }

  // ---------------------------------------------------------------------------------------------- pointer
  onPointerDown(e) {
    if (e.button > 0 || this.drag) return;
    if (this.menu && !e.composedPath().includes(this.menu)) this.closeMenu(false);
    const el = e.target.closest(".z-card");
    if (!el || e.target.closest("button")) return;
    const p = { id: el.dataset.id, el, x0: e.clientX, y0: e.clientY, type: e.pointerType, held: false, pid: e.pointerId };
    this.press = p;
    el.style.setProperty("--hold", `${HOLD}ms`);
    el.classList.add("pressing");
    p.timer = window.setTimeout(() => {
      p.held = true;
      el.classList.remove("pressing");
      el.classList.add("held");
      if (navigator.vibrate) { try { navigator.vibrate(8); } catch (_) { /* not allowed */ } }
    }, HOLD);
    this._move = (ev) => this.onPointerMove(ev);
    this._up = (ev) => this.onPointerUp(ev, false);
    this._cancel = (ev) => this.onPointerUp(ev, true);
    window.addEventListener("pointermove", this._move);
    window.addEventListener("pointerup", this._up);
    window.addEventListener("pointercancel", this._cancel);
  }

  endPress() {
    const p = this.press;
    if (!p) return;
    window.clearTimeout(p.timer);
    p.el.classList.remove("pressing", "held");
    window.removeEventListener("pointermove", this._move);
    window.removeEventListener("pointerup", this._up);
    window.removeEventListener("pointercancel", this._cancel);
    this.press = null;
  }

  onPointerMove(e) {
    const p = this.press;
    if (!p || e.pointerId !== p.pid) return;
    if (this.drag) { this.dragMove(e); return; }
    const dist = Math.hypot(e.clientX - p.x0, e.clientY - p.y0);
    if (dist < SLOP) return;
    if (p.held || p.type !== "touch") this.dragStart(e);
    else this.endPress(); // a finger moving before the hold is a scroll
  }

  onPointerUp(e, cancelled) {
    const p = this.press;
    if (!p || e.pointerId !== p.pid) return;
    if (this.drag) { this.dragEnd(cancelled); this.endPress(); return; }
    const held = p.held;
    const id = p.id;
    this.endPress();
    if (held && !cancelled) { this.suppressClick = true; this.openMenu(id); }
  }

  dragStart(e) {
    const p = this.press;
    const card = this.card(p.id);
    if (!card) return;
    window.clearTimeout(p.timer);
    p.el.classList.remove("pressing", "held");
    const r = p.el.getBoundingClientRect();
    const ghost = p.el.cloneNode(true);
    ghost.classList.add("z-ghost");
    ghost.removeAttribute("tabindex");
    ghost.setAttribute("aria-hidden", "true");
    ghost.style.width = `${r.width}px`;
    ghost.style.left = `${r.left}px`;
    ghost.style.top = `${r.top}px`;
    this.el.appendChild(ghost);
    p.el.classList.add("placeholder");
    this.drag = { card, ghost, from: r, dx: p.x0 - r.left, dy: p.y0 - r.top, over: null };
    this.suppressClick = true;
    this.markTargets(card);
    requestAnimationFrame(() => { ghost.style.transform = "rotate(-1.5deg) scale(1.03)"; });
    const moves = (card.ops || []).filter((o) => o.to);
    this.say(moves.length ? `Moving ${card.title}.` : `${card.title} cannot be moved; drop it to put it back.`);
    this.dragMove(e);
  }

  dragMove(e) {
    const d = this.drag;
    d.ghost.style.left = `${e.clientX - d.dx}px`;
    d.ghost.style.top = `${e.clientY - d.dy}px`;
    let over = null;
    this.el.querySelectorAll(".z-col").forEach((c) => {
      const r = c.getBoundingClientRect();
      const inside = e.clientX >= r.left && e.clientX <= r.right && e.clientY >= r.top - 24 && e.clientY <= r.bottom + 24;
      if (inside) over = c;
    });
    if (over !== d.over) {
      this.el.querySelectorAll(".z-col").forEach((c) => c.classList.toggle("over", c === over && c.classList.contains("ok")));
      d.over = over;
      if (over && over.classList.contains("ok")) {
        const op = this.moveTo(d.card, over.dataset.col);
        this.say(`${this.col(over.dataset.col).title}: drop to ${op.label.toLowerCase()}.`);
      }
    }
    // on a narrow screen the columns scroll sideways: carry the card to the next one
    const strip = this.el.querySelector(".z-cols");
    if (strip && strip.scrollWidth > strip.clientWidth) {
      const r = strip.getBoundingClientRect();
      if (e.clientX > r.right - 36) strip.scrollLeft += 12;
      else if (e.clientX < r.left + 36) strip.scrollLeft -= 12;
    }
  }

  dragEnd(cancelled) {
    const d = this.drag;
    const over = d.over;
    const ok = !cancelled && over && over.classList.contains("ok");
    const fly = (r, fade) => {
      d.ghost.classList.add("fly");
      d.ghost.style.left = `${r.left}px`;
      d.ghost.style.top = `${r.top}px`;
      d.ghost.style.transform = fade ? "scale(.96)" : "none";
      if (fade) d.ghost.style.opacity = "0";
    };
    if (ok) {
      const list = over.querySelector(".z-list, .z-empty") || over;
      const lr = list.getBoundingClientRect();
      fly({ left: lr.left, top: lr.top }, true);
    } else {
      fly(d.from, false);
    }
    const id = d.card.id;
    const to = over && over.dataset.col;
    const placeholder = this.cardEl(id);
    window.setTimeout(() => {
      d.ghost.remove();
      if (placeholder) placeholder.classList.remove("placeholder");
      this.clearTargets();
      this.drag = null;
      if (ok) this.send({ type: "move", card: id, to });
      this.flush();
    }, reducedMotion() ? 0 : 260);
    if (!ok && over && !over.classList.contains("origin")) this.say(`${d.card.title} cannot move to ${this.col(to).title}.`);
  }

  // ---------------------------------------------------------------------------------------------- the actions menu
  openMenu(id) {
    this.closeMenu(false);
    const card = this.card(id);
    const el = this.cardEl(id);
    if (!card || !el) return;
    const d = this.data;
    const ops = card.ops || [];
    const now = ops.filter((o) => !o.to);
    const moves = ops.filter((o) => o.to);
    const item = (o, extra) => `<button type="button" role="menuitem" tabindex="-1" class="z-item${o.primary ? " primary" : ""}${o.danger ? " danger" : ""}"
        data-mop="${esc(o.id)}">${svg(o.icon)}<span>${esc(o.label)}</span>${extra || ""}</button>`;
    let html = `<div class="z-menu-head"><b>${esc(card.title)}</b>${esc([card.kindLabel, card.status].filter(Boolean).join(" · "))}</div>`;
    html += now.map((o) => item(o, o.confirm ? `<span class="sure">asks first</span>` : "")).join("");
    if (moves.length) {
      html += `<div class="z-menu-sep" role="separator"></div><div class="z-menu-label" aria-hidden="true">Move</div>`;
      html += moves.map((o) => {
        const col = this.col(o.to);
        return item(o, `<span class="to" style="--tone: var(--z-${col.tone === "accent" ? "link" : col.tone})"><i></i>${esc(col.title)}</span>`);
      }).join("");
    }
    if (card.page !== d.page) {
      html += `<div class="z-menu-sep" role="separator"></div><button type="button" role="menuitem" tabindex="-1" class="z-item" data-go="${esc(card.page)}">${svg("arrow")}<span>Go to ${esc(d.pageNames[card.page] || card.page)}</span></button>`;
    }
    const m = document.createElement("div");
    m.className = "z-menu";
    m.setAttribute("role", "menu");
    m.setAttribute("aria-label", `Actions for ${card.title}`);
    m.innerHTML = html;
    this.el.appendChild(m);
    const host = this.el.getBoundingClientRect();
    const r = el.getBoundingClientRect();
    const w = m.offsetWidth;
    let left = r.right - host.left - w;
    left = Math.max(0, Math.min(left, host.width - w));
    m.style.left = `${left}px`;
    m.style.top = `${r.top - host.top + 34}px`;
    this.menu = m;
    this.menuFor = id;
    const btn = el.querySelector("[data-menu]");
    if (btn) btn.setAttribute("aria-expanded", "true");
    const items = [...m.querySelectorAll('[role="menuitem"]')];
    if (items.length) items[0].focus();
    m.addEventListener("keydown", (e) => {
      const i = items.indexOf(this.host.activeElement);
      if (e.key === "ArrowDown") { e.preventDefault(); items[(i + 1) % items.length].focus(); }
      else if (e.key === "ArrowUp") { e.preventDefault(); items[(i - 1 + items.length) % items.length].focus(); }
      else if (e.key === "Home") { e.preventDefault(); items[0].focus(); }
      else if (e.key === "End") { e.preventDefault(); items[items.length - 1].focus(); }
      else if (e.key === "Escape") { e.preventDefault(); this.closeMenu(true); }
      else if (e.key === "Tab") { this.closeMenu(true); }
    });
    m.addEventListener("click", (e) => {
      e.stopPropagation();
      const b = e.target.closest('[role="menuitem"]');
      if (!b) return;
      const cardId = this.menuFor;
      this.closeMenu(true);
      if (b.dataset.go) this.send({ type: "go", card: cardId, page: b.dataset.go });
      else this.send({ type: "op", card: cardId, op: b.dataset.mop });
    });
    this._outside = (e) => { if (this.menu && !e.composedPath().includes(this.menu)) this.closeMenu(false); };
    window.setTimeout(() => document.addEventListener("pointerdown", this._outside, true), 0);
    this.say(`Actions for ${card.title}. ${items.length} options.`);
  }

  closeMenu(refocus) {
    if (!this.menu) return;
    document.removeEventListener("pointerdown", this._outside, true);
    this.menu.remove();
    this.menu = null;
    const el = this.cardEl(this.menuFor);
    if (el) {
      const btn = el.querySelector("[data-menu]");
      if (btn) btn.setAttribute("aria-expanded", "false");
      if (refocus) this.focusCard(el);
    }
    this.flush();
  }

  destroy() {
    this.endPress();
    this.closeMenu(false);
  }
}

// Streamlit calls this on mount and again whenever the data changes (after running the previous cleanup), so the
// board lives on the host element and is only updated, never rebuilt: focus, open menus and scroll survive.
export default function (component) {
  const { data, parentElement, setTriggerValue } = component;
  let zone = parentElement.__lystZone;
  if (!zone) {
    zone = new Zone(parentElement);
    parentElement.__lystZone = zone;
  }
  zone.connect({ setTriggerValue });
  if (data) zone.update(data);
  return () => {
    window.setTimeout(() => {
      if (!zone.el.isConnected) { zone.destroy(); delete parentElement.__lystZone; }
    }, 0);
  };
}
