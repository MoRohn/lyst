// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (C) 2026 Rohn Springfield
// Data, as objects: where the data comes from (sources) and what Lyst can answer from (datasets), each drawn as a
// small recognisable thing rather than a row of words. A dataset's face is its shape: one bar per column, coloured by
// what the column holds (numbers, words, dates), so two tables look different before anyone reads a name.
//
// Drawn in the page (st.components.v2, no iframe, no library, no network). Python decides everything; this reports:
//   {type: "source", id}   a source picked (its datasets come forward; "all" shows everything)
//   {type: "pick", name}   a dataset picked (picked again: put down)
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
    this.el.innerHTML = `<div class="o-sources" role="toolbar" aria-label="Sources"></div>
      <div class="o-grid" role="list" aria-label="Datasets"></div><div class="sr o-say" role="status" aria-live="polite"></div>`;
    host.appendChild(this.el);
    this.src = this.el.querySelector(".o-sources");
    this.grid = this.el.querySelector(".o-grid");
    this.live = this.el.querySelector(".o-say");
    this.el.addEventListener("click", (e) => {
      const s = e.target.closest("[data-source]");
      if (s) return this.emit({ type: "source", id: s.dataset.source });
      const d = e.target.closest("[data-name]");
      if (d) { this.pickLocal(d.dataset.name); this.emit({ type: "pick", name: d.dataset.name }); }
    });
    this.el.addEventListener("keydown", (e) => {
      const t = e.target.closest("[data-name],[data-source]");
      if (!t) return;
      const row = [...t.parentElement.querySelectorAll(t.dataset.name ? "[data-name]" : "[data-source]")];
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
  }

  update(d) {
    this.data = d;
    const src = (d.sources || []).map((s) => `<button type="button" class="o-src${s.id === d.source ? " on" : ""}${s.add ? " add" : ""}" data-source="${esc(s.id)}"
        aria-pressed="${String(s.id === d.source)}" title="${esc(s.hint || s.label)}">
        <span class="o-src-i ${esc(s.tone || "")}">${svg(s.icon)}</span><span class="o-src-t">${esc(s.label)}</span>${s.count != null ? `<span class="o-src-n">${s.count}</span>` : ""}</button>`).join("");
    if (this.src.innerHTML !== src) this.src.innerHTML = src;
    const sel = d.selected;
    const html = (d.datasets || []).map((x, i) => {
      const types = (x.types || []).slice(0, 18);
      const legend = Object.entries(types.reduce((a, t) => ((a[t] = (a[t] || 0) + 1), a), {}))
        .map(([t, n]) => `${n} ${KIND_WORD[t] || "other"}`).join(", ");
      return `<div role="listitem" class="o-cell" style="--i:${i}"><button type="button" class="o-ds ${esc(x.tone || "ok")}" data-name="${esc(x.name)}" aria-pressed="${String(x.name === sel)}"
          aria-label="${esc(x.title)}: ${esc(x.meta)}${x.status ? `, ${esc(x.status)}` : ""}">
        <span class="o-ds-top">${shape(types)}${x.status ? `<span class="o-flag" title="${esc(x.status)}"><i></i>${esc(x.status)}</span>` : ""}</span>
        <span class="o-ds-t">${esc(x.title)}</span>
        <span class="o-ds-m">${esc(x.meta)}</span>
        <span class="sr">Columns: ${esc(legend)}</span></button></div>`;
    }).join("") || `<p class="o-none">${esc(d.empty || "Nothing here yet.")}</p>`;
    if (this.grid.innerHTML !== html) this.grid.innerHTML = html;
    this.el.classList.toggle("has-pick", !!sel);
    if (d.say && d.say !== this.said) { this.said = d.say; this.live.textContent = d.say; }
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
