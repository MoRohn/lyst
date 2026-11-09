// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (C) 2026 Rohn Springfield
// Brain, as a place: everything Lyst knows drawn as one calm field, gathered by collection (metrics, datasets, terms,
// documents, memories, answers). Nothing is wired up at rest; picking a thing draws only its own relationships and
// steps everything else back, so structure appears when it is asked for, not before.
// With one collection open, a picked thing's relationships still reach outside it: the things it connects to in other
// collections come in around the edge of the field, dimmed, with their lines drawn fainter (picking one follows the
// link into its own collection).
//
// Drawn in the page (st.components.v2, no iframe, no library, no network). Python decides everything; this reports:
//   {type: "pick", id}         a thing picked ("" puts it down)
//   {type: "collection", id}   a collection picked ("all" shows everything)
// Keyboard: Tab reaches the collections, then the field (one stop); arrow keys move between things; Enter or Space
// picks; Escape puts down. Positions are deterministic: the same knowledge always lands in the same place.

const esc = (s) =>
  String(s == null ? "" : s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
const GOLD = Math.PI * (3 - Math.sqrt(5));
const nonce = () => `${Date.now()}-${Math.random().toString(36).slice(2, 8)}`;

class Field {
  constructor(host) {
    this.host = host;
    this.el = document.createElement("div");
    this.el.className = "k";
    this.el.innerHTML = `<div class="k-cols" role="toolbar" aria-label="Collections"></div>
      <div class="k-field" role="group"><svg class="k-links" aria-hidden="true" focusable="false"></svg>
        <div class="k-caps" aria-hidden="true"></div><div class="k-nodes" role="list"></div>
        <p class="k-hint" aria-hidden="true"></p></div>
      <div class="sr k-say" role="status" aria-live="polite"></div>`;
    host.appendChild(this.el);
    this.cols = this.el.querySelector(".k-cols");
    this.field = this.el.querySelector(".k-field");
    this.links = this.el.querySelector(".k-links");
    this.caps = this.el.querySelector(".k-caps");
    this.nodes = this.el.querySelector(".k-nodes");
    this.hint = this.el.querySelector(".k-hint");
    this.live = this.el.querySelector(".k-say");
    this.sel = null;
    this.el.addEventListener("click", (e) => {
      const c = e.target.closest("[data-col]");
      if (c) return this.emit({ type: "collection", id: c.dataset.col });
      const n = e.target.closest("[data-id]");
      if (n) { const id = n.dataset.id === this.sel ? "" : n.dataset.id; this.select(id); return this.emit({ type: "pick", id }); }
      if (e.target.closest(".k-field") && this.sel) { this.select(""); this.emit({ type: "pick", id: "" }); }
    });
    this.nodes.addEventListener("keydown", (e) => this.key(e));
    this.ro = new ResizeObserver(() => { if (this.data) this.place(); });
    this.ro.observe(this.field);
  }

  connect(fns) { this.fns = fns; }
  emit(ev) { if (this.fns) this.fns.setTriggerValue("action", { ...ev, nonce: nonce() }); }

  visible() { return [...this.nodes.querySelectorAll(".k-n:not(.off)")]; }

  key(e) {
    const t = e.target.closest("[data-id]");
    if (!t) return;
    const all = this.visible();
    const i = all.indexOf(t);
    let to = null;
    if (e.key === "ArrowRight" || e.key === "ArrowDown") to = all[(i + 1) % all.length];
    else if (e.key === "ArrowLeft" || e.key === "ArrowUp") to = all[(i - 1 + all.length) % all.length];
    else if (e.key === "Home") to = all[0];
    else if (e.key === "End") to = all[all.length - 1];
    else if (e.key === "Escape" && this.sel) { e.preventDefault(); this.select(""); this.emit({ type: "pick", id: "" }); return; }
    if (to) { e.preventDefault(); this.rove(to); to.focus(); }
  }

  rove(on) { this.nodes.querySelectorAll(".k-n").forEach((b) => b.setAttribute("tabindex", b === on ? "0" : "-1")); }

  // The layout: each collection is a cluster on a ring around the middle, its things on a sunflower spiral (the
  // most connected at the heart). With one collection open, it spreads over the whole field.
  layout(W, H) {
    const d = this.data;
    const coll = d.collection && d.collection !== "all" ? d.collection : null;
    const groups = (d.collections || []).filter((c) => c.id !== "all" && (!coll || c.id === coll));
    const pos = {};
    const caps = [];
    const n = groups.length;
    const cx = W / 2, cy = H / 2;
    const ring = n <= 1 ? 0 : 1;
    const tall = H > W * 0.9;
    const rx = (W / 2) * (tall ? 0.5 : n <= 3 ? 0.5 : 0.58), ry = (H / 2) * (tall ? 0.6 : n <= 3 ? 0.42 : 0.52);
    // the two largest collections take the widest places (left and right), the rest fill the ring between them
    const bySize = groups.map((g, i) => [g, i]).sort((a, b) => b[0].count - a[0].count || a[1] - b[1]).map((x) => x[0]);
    const slots = [...Array(n).keys()];
    const order = n > 1 ? [0, Math.round(n / 2), ...slots.filter((x) => x !== 0 && x !== Math.round(n / 2))] : [0];
    const slotOf = new Map(bySize.map((g, i) => [g.id, order[i]]));
    groups.forEach((g, gi) => {
      const members = (d.nodes || []).filter((x) => x.kind === g.id);
      const a = Math.PI + (slotOf.get(g.id) / Math.max(n, 1)) * Math.PI * 2;
      const gx = cx + ring * rx * Math.cos(a), gy = cy + ring * ry * Math.sin(a);
      const room = n <= 1 ? Math.min(W, H) * 0.36 : Math.min(W / (n <= 3 ? 3.2 : 4.2), H / 3.2);
      const step = Math.max(24, Math.min(n <= 1 ? 72 : 44, room / Math.sqrt(members.length + 0.5)));
      const box = [Infinity, Infinity, -Infinity, -Infinity];
      members.forEach((m, i) => {
        const r = step * Math.sqrt(i + (i ? 0.5 : 0));
        const t = i * GOLD + gi;
        const x = gx + r * Math.cos(t) * (W > H * 1.3 ? 1.35 : 1), y = gy + r * Math.sin(t);
        pos[m.id] = [Math.min(W - 18, Math.max(18, x)), Math.min(H - 18, Math.max(18, y))];
        const q = pos[m.id];
        box[0] = Math.min(box[0], q[0]); box[1] = Math.min(box[1], q[1]); box[2] = Math.max(box[2], q[0]); box[3] = Math.max(box[3], q[1]);
      });
      // the collection's name sits just above its cluster (below it when there is no room above)
      const up = box[1] - 26 > 12;
      caps.push({ id: g.id, label: g.label, count: g.count, x: Math.min(W - 48, Math.max(48, (box[0] + box[2]) / 2)),
        y: up ? box[1] - 26 : Math.min(H - 12, box[3] + 26) });
    });
    return { pos, caps };
  }

  place() {
    const W = this.field.clientWidth, H = this.field.clientHeight;
    if (!W || !H) return;
    this.W = W; this.H = H;
    const { pos, caps } = this.layout(W, H);
    this.pos = pos;
    this.nodes.querySelectorAll(".k-n").forEach((b) => {
      const p = pos[b.dataset.id];
      b.classList.toggle("off", !p);
      b.classList.remove("ghost");
      b.setAttribute("aria-hidden", String(!p));
      if (p) {
        b.style.left = `${p[0]}px`; b.style.top = `${p[1]}px`;
      }
    });
    this.capList = caps;
    this.caps.innerHTML = caps.map((c) => `<span class="k-cap" style="left:${c.x}px;top:${c.y}px">${esc(c.label)} <b>${c.count}</b></span>`).join("");
    this.wire();
  }

  // Names that would sit on another name, a mark or a collection's caption stay hidden until hovered, picked or
  // related to the pick: the most connected things keep theirs.
  declutter() {
    const boxes = [];
    const live = [...this.nodes.querySelectorAll(".k-n:not(.off)")];
    const where = (id) => this.pos[id] || (this.ghosts || {})[id];
    const dot = (b) => { const p = where(b.dataset.id); const r = b.firstElementChild.offsetWidth / 2 + 3; return [p[0] - r, p[1] - r, p[0] + r, p[1] + r]; };
    live.forEach((b) => boxes.push(dot(b)));
    (this.capList || []).forEach((c) => { const w = (c.label.length + 3) * 7.2; boxes.push([c.x - w / 2, c.y - 9, c.x + w / 2, c.y + 9]); });
    const size = (b) => +b.style.getPropertyValue("--s").replace("rem", "");
    const rank = (b) => (b.getAttribute("aria-pressed") === "true" ? 3 : b.classList.contains("near") ? 2 : b.classList.contains("hit") ? 1.5 : 1) * 10 + size(b);
    const sel = !!this.sel, focused = this.el.classList.contains("focused");
    const want = live.filter((b) => b.getAttribute("aria-pressed") === "true" || b.classList.contains("near") || b.classList.contains("hit")
      || (!sel && (b.classList.contains("named") || focused))).sort((a, b) => rank(b) - rank(a));
    live.forEach((b) => b.classList.remove("crowd", "say"));
    want.forEach((b) => {
      const p = where(b.dataset.id);
      const own = dot(b);
      const w = b.lastElementChild.offsetWidth, r = b.firstElementChild.offsetWidth / 2 + 6;
      const east = [p[0] + r, p[1] - 10, p[0] + r + w, p[1] + 10], west = [p[0] - r - w, p[1] - 10, p[0] - r, p[1] + 10];
      const fits = (box) => box[0] > 4 && box[2] < this.W - 4
        && !boxes.some((o) => o !== own && !(o[0] === own[0] && o[1] === own[1]) && box[0] < o[2] && box[2] > o[0] && box[1] < o[3] && box[3] > o[1]);
      const prefer = p[0] > this.W * 0.62 ? [west, east] : [east, west];
      const ok = prefer.find(fits) || (b.getAttribute("aria-pressed") === "true" ? prefer[0] : null);
      if (ok) { b.classList.toggle("west", ok === west); b.classList.add("say"); boxes.push(ok); } else b.classList.add("crowd");
    });
  }

  // With a collection open, the picked thing's relations outside it: placed on an orbit just inside the field's edge,
  // grouped by kind and gathered on the picked thing's side (wrapping round when there are many).
  outside(sel) {
    const d = this.data;
    const out = {};
    if (!sel || !this.pos || !this.pos[sel]) return out;
    const kinds = (d.collections || []).map((c) => c.id);
    const kindOf = new Map((d.nodes || []).map((x) => [x.id, x.kind]));
    const ids = [];
    (d.links || []).forEach((l) => {
      const o = l.source === sel ? l.target : l.target === sel ? l.source : null;
      if (o && !this.pos[o] && kindOf.has(o) && !ids.includes(o)) ids.push(o);
    });
    if (!ids.length) return out;
    ids.sort((a, b) => kinds.indexOf(kindOf.get(a)) - kinds.indexOf(kindOf.get(b)));
    const shown = ids.slice(0, 48);
    const W = this.W, H = this.H, cx = W / 2, cy = H / 2;
    const rx = W / 2 - 28, ry = H / 2 - 26;
    const [sx, sy] = this.pos[sel];
    const toward = Math.atan2((sy - cy) / ry, (sx - cx) / rx);
    const step = Math.min((Math.PI * 2) / shown.length, 0.26);
    const start = toward - (step * (shown.length - 1)) / 2;
    shown.forEach((id, i) => {
      const a = start + i * step;
      out[id] = [cx + rx * Math.cos(a), cy + ry * Math.sin(a)];
    });
    return out;
  }

  // The picked thing's own relationships, drawn as soft curves; everything else steps back.
  wire() {
    const d = this.data;
    const sel = this.sel;
    const near = new Set();
    const ghost = this.outside(sel);
    this.ghosts = ghost;
    const at = (id) => (this.pos && this.pos[id]) || ghost[id];
    let paths = "";
    if (sel && this.pos && this.pos[sel]) {
      const [x1, y1] = this.pos[sel];
      (d.links || []).forEach((l, i) => {
        const other = l.source === sel ? l.target : l.target === sel ? l.source : null;
        if (!other || !at(other)) return;
        near.add(other);
        const [x2, y2] = at(other);
        const mx = (x1 + x2) / 2, my = (y1 + y2) / 2;
        const bend = 0.18 * Math.hypot(x2 - x1, y2 - y1);
        const nx = -(y2 - y1), ny = x2 - x1, nl = Math.hypot(nx, ny) || 1;
        paths += `<path pathLength="1" class="${ghost[other] ? "out" : ""}" style="--i:${i % 12}" d="M${x1},${y1} Q${mx + (nx / nl) * bend},${my + (ny / nl) * bend} ${x2},${y2}"/>`;
      });
    }
    this.nodes.querySelectorAll(".k-n").forEach((b) => {
      const id = b.dataset.id;
      if (this.pos && this.pos[id]) return;
      const g = ghost[id];
      b.classList.toggle("ghost", !!g);
      b.classList.toggle("off", !g);
      b.setAttribute("aria-hidden", String(!g));
      if (g) { b.style.left = `${g[0]}px`; b.style.top = `${g[1]}px`; }
    });
    this.links.setAttribute("viewBox", `0 0 ${this.W || 1} ${this.H || 1}`);
    this.links.innerHTML = paths;
    this.el.classList.toggle("has-sel", !!sel);
    this.nodes.querySelectorAll(".k-n").forEach((b) => {
      const id = b.dataset.id;
      b.classList.toggle("near", near.has(id));
      b.setAttribute("aria-pressed", String(id === sel));
    });
    this.declutter();
  }

  select(id) {
    this.sel = id || null;
    this.wire();
    const n = (this.data.nodes || []).find((x) => x.id === this.sel);
    this.live.textContent = n ? `${n.label}, ${n.kindLabel}` : "";
  }

  update(d) {
    this.data = d;
    const cols = (d.collections || []).map((c) => `<button type="button" class="k-col${(d.collection || "all") === c.id ? " on" : ""}" data-col="${esc(c.id)}"
        aria-pressed="${String((d.collection || "all") === c.id)}"><i class="dot k-${esc(c.id)}" aria-hidden="true"></i>${esc(c.label)}<span>${c.count}</span></button>`).join("");
    if (this.cols.innerHTML !== cols) this.cols.innerHTML = cols;
    const hits = new Set(d.hits || []);
    const html = (d.nodes || []).map((x) => {
      const size = (0.5 + Math.min(x.degree || 0, 14) * 0.055).toFixed(2);
      return `<button type="button" role="listitem" class="k-n k-${esc(x.kind)}${x.label_always ? " named" : ""}${hits.has(x.id) ? " hit" : ""}${x.status ? ` s-${esc(x.status)}` : ""}"
        data-id="${esc(x.id)}" tabindex="-1" style="--s:${size}rem"
        aria-label="${esc(x.label)}, ${esc(x.kindLabel)}, ${x.degree || 0} connection${x.degree === 1 ? "" : "s"}"><i aria-hidden="true"></i><span>${esc(x.short || x.label)}</span></button>`;
    }).join("");
    if (this.html !== html) {
      this.html = html;
      this.nodes.innerHTML = html;
    }
    this.el.classList.toggle("has-q", hits.size > 0);
    this.el.classList.toggle("focused", !!(d.collection && d.collection !== "all"));
    this.field.setAttribute("aria-label", d.alt || "What Lyst knows");
    this.hint.textContent = d.hint || "";
    this.sel = d.selected || null;
    this.place();
    const first = this.nodes.querySelector(`.k-n[aria-pressed="true"]`) || this.nodes.querySelector(".k-n.hit") || this.visible()[0];
    if (first && !this.nodes.querySelector('.k-n[tabindex="0"]:not(.off)')) this.rove(first);
  }
}

export default function (component) {
  const { data, parentElement, setTriggerValue } = component;
  let f = parentElement.__lystField;
  if (!f) { f = new Field(parentElement); parentElement.__lystField = f; }
  f.connect({ setTriggerValue });
  if (data) f.update(data);
  return () => { window.setTimeout(() => { if (!f.el.isConnected) { f.ro.disconnect(); delete parentElement.__lystField; } }, 0); };
}
