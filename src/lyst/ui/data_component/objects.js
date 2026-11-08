// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (C) 2026 Rohn Springfield
// Data, as objects: where the data comes from (sources) and what Lyst can answer from (datasets), each drawn as a
// small recognisable thing rather than a row of words. A dataset's face is its shape: one bar per column, coloured by
// what the column holds (numbers, words, dates), so two tables look different before anyone reads a name.
//
// Laid out as a flow, left to right: where data comes from (sources), what Lyst can answer from (datasets), and what
// those datasets feed (approved metrics), joined by wires. Hovering or picking anything lights its path and lets the
// rest step back, so "what depends on this?" is answered by looking. On a narrow screen the lanes stack and the wires
// give way to the order of the lanes.
//
// Drawn in the page (st.components.v2, no iframe, no library, no network). Python decides everything; this reports:
//   {type: "source", id}   a source picked (its path lights; picked again or "all": everything)
//   {type: "pick", name}   a dataset picked (picked again: put down)
//   {type: "use", id}      a metric a dataset feeds, picked (Python opens its definition)
// Keyboard: Tab reaches each object; arrow keys move within a row of objects; Enter or Space picks; Escape puts down.

const esc = (s) =>
  String(s == null ? "" : s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
const ICON = {
  all: '<path d="M4 6h16M4 12h16M4 18h16"/>',
  file: '<path d="M7 3h7l5 5v13H7z"/><path d="M14 3v5h5"/>',
  database: '<ellipse cx="12" cy="6" rx="7" ry="3"/><path d="M5 6v12c0 1.7 3.1 3 7 3s7-1.3 7-3V6"/><path d="M5 12c0 1.7 3.1 3 7 3s7-1.3 7-3"/>',
  api: '<path d="M8 8 4 12l4 4M16 8l4 4-4 4M14 5l-4 14"/>',
  built: '<path d="M4 7h7v7H4zM13 10h7v7h-7z"/><path d="M11 10.5h2"/>',
  add: '<path d="M12 5v14M5 12h14"/>',
  metric: '<path d="M4 18a8 8 0 1 1 16 0"/><path d="m12 18 4-5"/>',
};
const svg = (n) => `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true" focusable="false">${ICON[n] || ICON.file}</svg>`;
const KIND_WORD = { n: "number", t: "text", d: "date", b: "yes/no" };

function shape(types) {
  const n = Math.max(types.length, 1);
  return `<svg class="o-shape" viewBox="0 0 ${n * 9} 28" style="width:${(n * 0.56).toFixed(2)}rem" aria-hidden="true" focusable="false">${types
    .map((t, i) => {
      const h = t === "d" ? 28 : t === "n" ? 18 + ((i * 5) % 10) : t === "b" ? 9 : 13;
      return `<rect class="k-${t}" x="${i * 9}" y="${28 - h}" width="6" height="${h}" rx="2"/>`;
    }).join("")}</svg>`;
}

class Objects {
  constructor(host) {
    this.host = host;
    this.el = document.createElement("div");
    this.el.className = "o";
    this.el.innerHTML = `<div class="o-flow"><svg class="o-wires" aria-hidden="true" focusable="false"></svg>
        <section class="o-lane o-lane-src"><h3 class="o-lane-h">From</h3><div class="o-sources" role="toolbar" aria-label="Sources"></div></section>
        <section class="o-lane o-lane-ds"><h3 class="o-lane-h">Datasets <span class="o-ds-n"></span></h3><div class="o-grid" role="list" aria-label="Datasets"></div></section>
        <section class="o-lane o-lane-use"><h3 class="o-lane-h">Feeds</h3><div class="o-uses" role="list" aria-label="Approved metrics the datasets feed"></div></section>
      </div><div class="sr o-say" role="status" aria-live="polite"></div>`;
    host.appendChild(this.el);
    this.flow = this.el.querySelector(".o-flow");
    this.wires = this.el.querySelector(".o-wires");
    this.src = this.el.querySelector(".o-sources");
    this.grid = this.el.querySelector(".o-grid");
    this.uses = this.el.querySelector(".o-uses");
    this.count = this.el.querySelector(".o-ds-n");
    this.live = this.el.querySelector(".o-say");
    this.el.addEventListener("click", (e) => {
      const s = e.target.closest("[data-source]");
      if (s) {
        const id = s.dataset.source;
        return this.emit({ type: "source", id: id !== "+" && this.data && this.data.source === id ? "all" : id });
      }
      const u = e.target.closest("[data-use]");
      if (u) return this.emit({ type: "use", id: u.dataset.use });
      const d = e.target.closest("[data-name]");
      if (d) { this.pickLocal(d.dataset.name); this.emit({ type: "pick", name: d.dataset.name }); }
    });
    // hover or focus lights a thing's path at once; leaving it returns to what is picked
    const lit = (e) => { const t = e.target.closest("[data-name],[data-source],[data-use]"); this.light(t ? this.keyOf(t) : null); };
    this.el.addEventListener("pointerover", lit);
    this.el.addEventListener("focusin", lit);
    this.el.addEventListener("pointerleave", () => this.light(null));
    this.el.addEventListener("focusout", (e) => { if (!this.el.contains(e.relatedTarget)) this.light(null); });
    if (typeof ResizeObserver !== "undefined") {
      this.ro = new ResizeObserver(() => this.draw());
      this.ro.observe(this.flow);
    }
    this.el.addEventListener("animationend", () => this.draw());
    this.el.addEventListener("keydown", (e) => {
      const t = e.target.closest("[data-name],[data-source],[data-use]");
      if (!t) return;
      const sel = t.dataset.name ? "[data-name]" : t.dataset.use ? "[data-use]" : "[data-source]";
      const row = [...this.el.querySelectorAll(sel)];
      const i = row.indexOf(t);
      if (e.key === "ArrowRight" || e.key === "ArrowDown") { e.preventDefault(); row[(i + 1) % row.length].focus(); }
      else if (e.key === "ArrowLeft" || e.key === "ArrowUp") { e.preventDefault(); row[(i - 1 + row.length) % row.length].focus(); }
      else if (e.key === "Escape" && this.data && this.data.selected) { this.emit({ type: "pick", name: this.data.selected }); }
    });
  }

  connect(fns) { this.fns = fns; }
  emit(ev) { if (this.fns) this.fns.setTriggerValue("action", { ...ev, nonce: `${Date.now()}-${Math.random().toString(36).slice(2, 8)}` }); }

  // picked at once on screen, before Python answers: the object rises and the rest step back
  pickLocal(name) {
    const on = this.data && this.data.selected === name ? null : name;
    this.grid.querySelectorAll("[data-name]").forEach((b) => b.setAttribute("aria-pressed", String(b.dataset.name === on)));
    this.el.classList.toggle("has-pick", !!on);
    if (this.data) { this.data = { ...this.data, selected: on }; this.light(null); }
  }

  keyOf(t) { return t.dataset.name ? `d:${t.dataset.name}` : t.dataset.use ? `u:${t.dataset.use}` : `s:${t.dataset.source}`; }

  // every link as [from-key, to-key]: source -> dataset, dataset -> metric
  links(d) {
    const shown = new Set((d.datasets || []).map((x) => x.name));
    const out = [];
    (d.datasets || []).forEach((x) => { if (x.from) out.push([`s:${x.from}`, `d:${x.name}`]); });
    (d.uses || []).forEach((u) => (u.from || []).forEach((n) => { if (shown.has(n)) out.push([`d:${n}`, `u:${u.id}`]); }));
    return out;
  }

  // the keys on a thing's path: itself, everything upstream of it and everything downstream
  path(key) {
    const ls = this.data ? this.links(this.data) : [];
    const on = new Set([key]);
    const walk = (from, to) => {
      const seen = new Set([key]);
      let grew = true;
      while (grew) {
        grew = false;
        ls.forEach((l) => { if (seen.has(l[from]) && !seen.has(l[to])) { seen.add(l[to]); on.add(l[to]); grew = true; } });
      }
    };
    walk(0, 1);
    walk(1, 0);
    return on;
  }

  // light a path: the hovered thing, else the picked dataset, else the picked source, else nothing
  light(key) {
    const d = this.data || {};
    const k = key || (d.selected ? `d:${d.selected}` : d.source && d.source !== "all" ? `s:${d.source}` : null);
    const on = k ? this.path(k) : null;
    this.el.classList.toggle("lit", !!on);
    this.el.querySelectorAll("[data-name],[data-source],[data-use]").forEach((n) => {
      n.classList.toggle("on-path", !!on && on.has(this.keyOf(n)));
    });
    this.wires.querySelectorAll("path").forEach((p) => {
      p.classList.toggle("on-path", !!on && on.has(p.dataset.a) && on.has(p.dataset.b));
    });
  }

  // wires: a soft curve from the right edge of one thing to the left edge of the next
  draw() {
    if (!this.data) return;
    const box = this.flow.getBoundingClientRect();
    const stacked = getComputedStyle(this.flow).getPropertyValue("--o-stacked").trim() === "1";
    if (stacked || !box.width) { this.wires.innerHTML = ""; return; }
    const at = {};
    this.el.querySelectorAll("[data-name],[data-source],[data-use]").forEach((n) => {
      const r = n.getBoundingClientRect();
      at[this.keyOf(n)] = { l: r.left - box.left, r: r.right - box.left, y: r.top - box.top + r.height / 2 };
    });
    this.wires.setAttribute("viewBox", `0 0 ${box.width} ${box.height}`);
    this.wires.innerHTML = this.links(this.data).map(([a, b]) => {
      const p = at[a], q = at[b];
      if (!p || !q) return "";
      const x1 = p.r + 4, x2 = q.l - 4, mid = (x1 + x2) / 2;
      return `<path data-a="${esc(a)}" data-b="${esc(b)}" class="${b.startsWith("u:") ? "w-use" : "w-src"}" d="M${x1.toFixed(1)} ${p.y.toFixed(1)} C${mid.toFixed(1)} ${p.y.toFixed(1)} ${mid.toFixed(1)} ${q.y.toFixed(1)} ${x2.toFixed(1)} ${q.y.toFixed(1)}"/>`;
    }).join("");
    this.light(null);
  }

  update(d) {
    this.data = d;
    const sources = (d.sources || []).filter((s) => s.id !== "all");
    const src = sources.map((s) => `<button type="button" class="o-src${s.id === d.source ? " on" : ""}${s.add ? " add" : ""}" data-source="${esc(s.id)}"
        aria-pressed="${String(s.id === d.source)}" title="${esc(s.hint || s.label)}">
        <span class="o-src-i ${esc(s.tone || "")}">${svg(s.icon)}</span><span class="o-src-t">${esc(s.label)}</span>${s.count != null ? `<span class="o-src-n">${s.count}</span>` : ""}</button>`).join("");
    if (this.src.innerHTML !== src) this.src.innerHTML = src;
    const sel = d.selected;
    const html = (d.datasets || []).map((x, i) => {
      const types = (x.types || []).slice(0, 18);
      const legend = Object.entries(types.reduce((a, t) => ((a[t] = (a[t] || 0) + 1), a), {}))
        .map(([t, n]) => `${n} ${KIND_WORD[t] || "other"}`).join(", ");
      return `<div role="listitem" class="o-cell" style="--i:${i}"><button type="button" class="o-ds ${esc(x.tone || "ok")}" data-name="${esc(x.name)}" aria-pressed="${String(x.name === sel)}"
          aria-label="${esc(x.title)}: ${esc(x.meta)}${x.status ? `, ${esc(x.status)}` : ""}${x.feeds ? `, feeds ${x.feeds}` : ""}">
        <span class="o-ds-top">${shape(types)}${x.status ? `<span class="o-flag" title="${esc(x.status)}"><i></i>${esc(x.status)}</span>` : ""}</span>
        <span class="o-ds-t">${esc(x.title)}</span>
        <span class="o-ds-m">${esc(x.meta)}</span>
        <span class="sr">Columns: ${esc(legend)}</span></button></div>`;
    }).join("") || `<p class="o-none">${esc(d.empty || "Nothing here yet.")}</p>`;
    if (this.grid.innerHTML !== html) this.grid.innerHTML = html;
    this.grid.classList.toggle("dense", (d.datasets || []).length > 7);
    const uses = (d.uses || []).map((u) => `<div role="listitem"><button type="button" class="o-use" data-use="${esc(u.id)}"
        title="${esc(u.hint || u.title)}" aria-label="${esc(u.title)}, approved metric from ${esc((u.fromTitles || []).join(", "))}">
        <span class="o-use-i">${svg("metric")}</span><span class="o-use-t">${esc(u.title)}</span></button></div>`).join("")
      || `<p class="o-none o-none-use">${esc(d.noUses || "Nothing yet. Approve a metric and it appears here.")}</p>`;
    if (this.uses.innerHTML !== uses) this.uses.innerHTML = uses;
    this.count.textContent = (d.datasets || []).length ? String((d.datasets || []).length) : "";
    this.el.classList.toggle("has-pick", !!sel);
    this.el.classList.toggle("no-uses", !(d.uses || []).length);
    if (d.say && d.say !== this.said) { this.said = d.say; this.live.textContent = d.say; }
    window.requestAnimationFrame(() => this.draw());
  }
}

export default function (component) {
  const { data, parentElement, setTriggerValue } = component;
  let o = parentElement.__lystObjects;
  if (!o) { o = new Objects(parentElement); parentElement.__lystObjects = o; }
  o.connect({ setTriggerValue });
  if (data) o.update(data);
  return () => { window.setTimeout(() => { if (!o.el.isConnected) { delete parentElement.__lystObjects; } }, 0); };
}
