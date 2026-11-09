// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (C) 2026 Rohn Springfield
// Data quality, for a person to review: every flagged row of a dataset in one table, the flagged cell lit in its row,
// what Lyst would do about it beside it, and the person's call one tap away (accept as valid, fix, exclude). Staged
// changes collect in a diff (before → after, who suggested it, what it does to the totals) that is applied only after an
// approval, never silently.
//
// Drawn in the page (st.components.v2, no iframe, no library, no network). Python owns every change; this reports:
//   {type: "review", ids, decision, value?}   accept | fix | exclude | reopen, for one row or the selected rows
//   {type: "propose", ids?}                    Let Lyst resolve (every open issue it can, or the selected ones)
//   {type: "apply", approve}                   turn the staged changes into a resolution (and approve it, for an admin)
//   {type: "decide", approval, approve}        an admin's decision on a waiting resolution
//   {type: "undo", id}                         put back the version a resolution started from
// Keyboard: every control is a button or an input in reading order; Escape closes an open fix.

const esc = (s) =>
  String(s == null ? "" : s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
const ICON = {
  spark: '<path d="M12 3l1.8 4.7L18.5 9.5l-4.7 1.8L12 16l-1.8-4.7L5.5 9.5l4.7-1.8z"/><path d="M19 15l.8 2.2L22 18l-2.2.8L19 21l-.8-2.2L16 18l2.2-.8z"/>',
  check: '<path d="m5 12 4.5 4.5L19 7"/>',
  pen: '<path d="M4 20h4L19 9l-4-4L4 16z"/><path d="m14 6 4 4"/>',
  ban: '<circle cx="12" cy="12" r="8"/><path d="m6.5 17.5 11-11"/>',
  undo: '<path d="M9 14 4 9l5-5"/><path d="M4 9h10a6 6 0 0 1 0 12h-3"/>',
  x: '<path d="M6 6l12 12M18 6 6 18"/>',
  arrow: '<path d="M5 12h14M13 6l6 6-6 6"/>',
  clock: '<circle cx="12" cy="12" r="8"/><path d="M12 8v4l3 2"/>',
  shield: '<path d="M12 3 5 6v6c0 4.5 3 7.5 7 9 4-1.5 7-4.5 7-9V6z"/><path d="m9 12 2 2 4-4"/>',
};
const svg = (n) => `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true" focusable="false">${ICON[n]}</svg>`;
const KIND = { duplicate: "Duplicate", null: "Empty", negative: "Negative", outlier: "Unusual" };
const STATUS = { open: "Needs review", staged: "Staged", pending: "Awaiting approval", valid: "Accepted as valid" };
const FILTERS = [["open", "Needs review"], ["staged", "Staged"], ["valid", "Accepted"], ["all", "All"]];

function num(v) {
  if (v == null) return null;
  const a = Math.abs(v);
  if (a >= 1e9) return `${(v / 1e9).toFixed(2)}B`;
  if (a >= 1e6) return `${(v / 1e6).toFixed(2)}M`;
  if (a >= 1e4) return `${(v / 1e3).toFixed(1)}K`;
  return v.toLocaleString(undefined, { maximumFractionDigits: 2 });
}
function cell(v, type) {
  if (v == null || v === "") return '<span class="q-empty">empty</span>';
  if (type === "n" && typeof v === "number") return esc(v.toLocaleString(undefined, { maximumFractionDigits: 2 }));
  return esc(v);
}

class Quality {
  constructor(host) {
    this.host = host;
    this.filter = null;
    this.fixing = null;
    this.picked = new Set();
    this.el = document.createElement("div");
    this.el.className = "q";
    host.appendChild(this.el);
    this.el.addEventListener("click", (e) => this.click(e));
    this.el.addEventListener("change", (e) => {
      const box = e.target.closest("[data-pick]");
      if (!box) return;
      if (box.dataset.pick === "*") {
        this.visible().forEach((i) => (box.checked ? this.picked.add(i.id) : this.picked.delete(i.id)));
      } else if (box.checked) this.picked.add(box.dataset.pick);
      else this.picked.delete(box.dataset.pick);
      this.render();
    });
    this.el.addEventListener("keydown", (e) => {
      if (e.key === "Escape" && this.fixing) { this.fixing = null; this.render(); }
      if (e.key === "Enter" && e.target.matches(".q-fix input")) { e.preventDefault(); this.el.querySelector(".q-fix [data-stage]")?.click(); }
    });
  }

  connect(api) { this.api = api; }

  send(ev) {
    if (!this.api) return;
    this.el.classList.add("busy");
    this.api.setTriggerValue("action", { ...ev, nonce: `${Date.now()}-${Math.random().toString(36).slice(2, 8)}` });
  }

  visible() {
    const f = this.filter;
    return (this.data.issues || []).filter((i) => f === "all" || i.status === f || (f === "staged" && i.status === "pending"));
  }

  click(e) {
    const b = e.target.closest("button");
    if (!b || b.disabled) return;
    const d = b.dataset;
    if (d.filter) { this.filter = d.filter; this.picked.clear(); this.fixing = null; this.render(); return; }
    if (d.fix) { this.fixing = this.fixing === d.fix ? null : d.fix; this.render(); this.el.querySelector(".q-fix input")?.focus(); return; }
    if (d.stage) {
      const v = this.el.querySelector(".q-fix input")?.value;
      if (v == null || !String(v).trim()) { this.el.querySelector(".q-fix input")?.focus(); return; }
      this.fixing = null;
      this.send({ type: "review", ids: [d.stage], decision: "fix", value: v });
      return;
    }
    if (d.act) {
      const ids = d.id ? [d.id] : [...this.picked];
      if (!ids.length) return;
      if (!d.id) this.picked.clear();
      this.send({ type: "review", ids, decision: d.act });
      return;
    }
    if (d.propose != null) {
      const ids = d.propose === "picked" ? [...this.picked] : null;
      if (d.propose === "picked") this.picked.clear();
      this.send({ type: "propose", ids });
      return;
    }
    if (d.apply) { this.send({ type: "apply", approve: d.apply === "approve" }); return; }
    if (d.decide) { this.send({ type: "decide", approval: d.decide, approve: d.yes === "1" }); return; }
    if (d.undo) { this.send({ type: "undo", id: d.undo }); return; }
    if (d.clear) {
      const ids = (this.data.staged || []).filter((s) => s.status === "staged").map((s) => s.issue_id);
      if (ids.length) this.send({ type: "review", ids, decision: "reopen" });
    }
  }

  head(d) {
    const chips = Object.entries(d.counts || {}).map(([k, n]) =>
      `<span class="q-chip k-${esc(k)}"><i></i>${n} ${esc((KIND[k] || k).toLowerCase())}</span>`).join("");
    const lead = d.total
      ? `${d.open ? `<b>${d.open}</b> of ${d.total} flagged ${d.total === 1 ? "row needs" : "rows need"} review` : `All ${d.total} flagged ${d.total === 1 ? "row is" : "rows are"} reviewed`}`
      : "No flagged rows";
    const can = d.canEdit && d.fixable;
    return `<header class="q-head">
        <div class="q-lead"><div class="q-kicker">${esc(d.title)} · ${(d.rows || 0).toLocaleString()} rows · version ${esc(d.version)}</div>
          <h3 class="q-h">${lead}</h3><div class="q-chips">${chips}</div></div>
        ${d.canEdit && d.total ? `<div class="q-cta"><button type="button" class="q-btn primary" data-propose="" ${can ? "" : "disabled"}
            title="${can ? "Lyst proposes a fix for each row it can, with its reason. Nothing changes until it is approved." : "Nothing left that Lyst can resolve"}">
            ${svg("spark")}<span>Let Lyst resolve</span></button>
          <span class="q-cta-sub">${can ? `Lyst can propose ${d.fixable} fix${d.fixable === 1 ? "" : "es"}` : d.open ? "The rest need a person" : "Nothing to resolve"}</span></div>` : ""}
      </header>`;
  }

  tabs(d) {
    const n = { open: 0, staged: 0, valid: 0, all: (d.issues || []).length };
    (d.issues || []).forEach((i) => { if (i.status === "pending") n.staged++; else n[i.status] = (n[i.status] || 0) + 1; });
    return `<div class="q-tabs" role="toolbar" aria-label="Show">${FILTERS.map(([k, t]) =>
      `<button type="button" class="q-tab${this.filter === k ? " on" : ""}" data-filter="${k}" aria-pressed="${this.filter === k}">${t}<span>${n[k] || 0}</span></button>`).join("")}</div>`;
  }

  table(d) {
    const rows = this.visible();
    if (!rows.length) {
      const say = { open: d.total ? "Nothing left to review here." : "Lyst found no duplicates, empty cells, stray negatives or unusual values.",
        staged: "Nothing staged. Fix or exclude a row, or let Lyst propose.", valid: "Nothing accepted as valid yet.", all: d.total ? "Nothing flagged." : "Lyst found no duplicates, empty cells, stray negatives or unusual values." }[this.filter];
      return `<div class="q-none">${svg("shield")}<p>${esc(say)}</p></div>`;
    }
    const cols = d.columns || [];
    const all = rows.every((i) => this.picked.has(i.id));
    const head = `<tr><th class="q-sel">${d.canEdit ? `<input type="checkbox" data-pick="*" aria-label="Select all shown" ${all ? "checked" : ""}>` : ""}</th>
        <th class="q-issue">Issue and what Lyst suggests</th><th class="q-row">Row</th>${cols.map((c) => `<th class="${c.type === "n" ? "n" : ""}">${esc(c.name.replace(/_/g, " "))}</th>`).join("")}
<th class="q-act"><span class="sr">Your call</span></th></tr>`;
    const body = rows.map((i) => {
      const p = i.proposal || {};
      const rev = i.review || {};
      const dup = i.kind === "duplicate";
      const tds = cols.map((c) => {
        const hit = c.name === i.column;
        const fixed = hit && (i.status === "staged" || i.status === "pending") && rev.decision === "fix";
        const body = fixed ? `<s>${cell(i.values[c.name], c.type)}</s> <span class="q-after">${cell(rev.value, c.type)}</span>`
          : cell(i.values[c.name], c.type);
        return `<td data-l="${esc(c.name.replace(/_/g, " "))}" class="${c.type === "n" ? "n" : ""}${hit ? ` hit k-${esc(i.kind)}` : ""}">${hit ? `<span class="q-hit">${body}</span>` : body}</td>`;
      }).join("");
      let sug = "";
      if (p.action === "drop") sug = `<span class="q-sug-a">Remove copy</span>`;
      else if (p.action === "set") sug = `<span class="q-sug-a">${svg("arrow")}${esc(typeof p.value === "number" ? p.value.toLocaleString(undefined, { maximumFractionDigits: 2 }) : p.value)}</span>`;
      else if (p.action === "accept") sug = `<span class="q-sug-a">Keep as is</span>`;
      else sug = `<span class="q-sug-none">Needs a person</span>`;
      const conf = p.action && p.action !== "none" ? `<span class="q-conf c-${esc(p.confidence)}" title="${esc(p.confidence)} confidence"><i></i>${esc(p.confidence)}</span>` : "";
      let act = "";
      if (!d.canEdit) act = `<span class="q-state s-${esc(i.status)}">${esc(STATUS[i.status])}</span>`;
      else if (i.status === "open") {
        act = `<div class="q-acts">
            <button type="button" class="q-ib" data-act="accept" data-id="${esc(i.id)}" title="Accept as valid: it is a real edge case; nothing changes">${svg("check")}<span>Valid</span></button>
            ${dup ? "" : `<button type="button" class="q-ib" data-fix="${esc(i.id)}" aria-expanded="${this.fixing === i.id}" title="Fix: give the right value">${svg("pen")}<span>Fix</span></button>`}
            <button type="button" class="q-ib" data-act="exclude" data-id="${esc(i.id)}" title="Exclude: leave this row out">${svg("ban")}<span>Exclude</span></button></div>`;
      } else if (i.status === "pending") act = `<span class="q-state s-pending">${svg("clock")}Awaiting approval</span>`;
      else {
        const what = i.status === "valid" ? "Accepted as valid" : rev.decision === "exclude" ? "Excluded" : "Fixed";
        act = `<span class="q-state s-${esc(i.status)}">${what}${rev.origin === "lyst" ? ' <em>by Lyst</em>' : ""}</span>
          <button type="button" class="q-ib q-ib-quiet" data-act="reopen" data-id="${esc(i.id)}" title="Undo this call">${svg("undo")}<span class="sr">Undo</span></button>`;
      }
      const tone = i.severity === "high" ? "bad" : i.severity === "medium" ? "warn" : "info";
      const fix = this.fixing === i.id ? `<tr class="q-fixrow"><td></td><td colspan="${cols.length + 3}"><div class="q-fix">
          <label>New ${esc((i.column || "").replace(/_/g, " "))} for row ${i.row}<input type="text" inputmode="${cols.find((c) => c.name === i.column)?.type === "n" ? "decimal" : "text"}"
            value="${esc(p.action === "set" ? p.value : i.value ?? "")}" aria-describedby="qfx-${esc(i.id)}"></label>
          <button type="button" class="q-btn" data-stage="${esc(i.id)}">Stage fix</button><button type="button" class="q-btn quiet" data-fix="${esc(i.id)}">Cancel</button>
          <span id="qfx-${esc(i.id)}" class="q-fix-why">${esc(p.action === "set" ? `Lyst suggests ${p.value}: ${p.reason}` : p.reason || "")}</span></div></td></tr>` : "";
      return `<tr class="q-tr t-${tone} st-${esc(i.status)}${dup ? " dup" : ""}${this.picked.has(i.id) ? " picked" : ""}">
          <td class="q-sel">${d.canEdit && i.status === "open" ? `<input type="checkbox" data-pick="${esc(i.id)}" aria-label="Select row ${i.row}" ${this.picked.has(i.id) ? "checked" : ""}>` : ""}</td>
          <td class="q-issue"><span class="q-pill k-${esc(i.kind)}"><i></i>${esc(i.label)}</span><span class="q-why">${esc(i.why)}</span>
            <span class="q-sug" title="${esc(p.reason || "")}"><span class="q-sug-l">Lyst</span>${sug}${conf}<span class="q-reason">${esc(p.reason || "")}</span></span></td>
          <td class="q-row" data-l="Row">${i.row.toLocaleString()}</td>${tds}
          <td class="q-act">${act}</td></tr>${fix}`;
    }).join("");
    const bulk = this.picked.size && d.canEdit ? `<div class="q-bulk" role="toolbar" aria-label="Selected rows"><b>${this.picked.size} selected</b>
        <button type="button" class="q-btn" data-act="accept">${svg("check")}Accept as valid</button>
        <button type="button" class="q-btn" data-act="exclude">${svg("ban")}Exclude</button>
        <button type="button" class="q-btn primary" data-propose="picked">${svg("spark")}Let Lyst resolve these</button></div>` : "";
    return `${bulk}<div class="q-scroll" tabindex="0" role="region" aria-label="Flagged rows"><table class="q-t"><thead>${head}</thead><tbody>${body}</tbody></table></div>
      ${d.truncated ? `<p class="q-note">Showing the first ${(d.issues || []).length} flagged rows.</p>` : ""}`;
  }

  diff(d) {
    const staged = d.staged || [];
    if (!staged.length) return "";
    const lines = staged.map((s) => {
      const where = s.action === "drop" ? `Row ${s.row.toLocaleString()}` : `Row ${s.row.toLocaleString()} · ${esc(String(s.column).replace(/_/g, " "))}`;
      const change = s.action === "drop"
        ? `<span class="q-del">Remove the row</span>`
        : `<span class="q-del">${cell(s.before, typeof s.before === "number" ? "n" : "t")}</span>${svg("arrow")}<span class="q-add">${cell(s.after, typeof s.after === "number" ? "n" : "t")}</span>`;
      return `<li class="q-ch"><span class="q-pill k-${esc(s.kind)}"><i></i>${esc(KIND[s.kind] || s.kind)}</span>
          <span class="q-ch-w">${where}</span><span class="q-ch-c">${change}</span>
          <span class="q-by ${s.origin === "lyst" ? "lyst" : ""}">${s.origin === "lyst" ? `${svg("spark")}Lyst` : "You"}</span>
          ${s.reason ? `<span class="q-ch-r">${esc(s.reason)}</span>` : ""}
          ${d.canEdit && s.status === "staged" ? `<button type="button" class="q-ib q-ib-quiet q-ch-x" data-act="reopen" data-id="${esc(s.issue_id)}" title="Take this change out">${svg("x")}<span class="sr">Take out</span></button>` : ""}</li>`;
    }).join("");
    const im = d.impact;
    const impact = im ? `<div class="q-impact"><span><b>Rows</b> ${im.rows_before.toLocaleString()} ${svg("arrow")} ${im.rows_after.toLocaleString()}</span>${(im.totals || []).map((t) =>
      `<span><b>${esc(t.column.replace(/_/g, " "))}</b> ${num(t.before)} ${svg("arrow")} ${num(t.after)} <em class="${t.change >= 0 ? "up" : "down"}">${t.change >= 0 ? "+" : "−"}${num(Math.abs(t.change))}</em></span>`).join("")}</div>` : "";
    return `<section class="q-diff" aria-label="Changes ready to apply"><div class="q-diff-h"><div><h4>${staged.length} change${staged.length === 1 ? "" : "s"} ready</h4>
        <p>Applied together as a new version of ${esc(d.title)}, after an approval. The current version is kept, so this can be undone.</p></div>
        ${d.canEdit ? `<div class="q-diff-b"><button type="button" class="q-btn quiet" data-clear="1">Clear</button>
          ${d.canApprove ? `<button type="button" class="q-btn" data-apply="request">Request approval</button><button type="button" class="q-btn primary" data-apply="approve">${svg("shield")}Approve and apply</button>`
            : `<button type="button" class="q-btn primary" data-apply="request">${svg("shield")}Request approval</button>`}</div>` : ""}</div>
      ${impact}<ul class="q-chs">${lines}</ul></section>`;
  }

  waiting(d) {
    const p = (d.pending || []).map((r) => `<div class="q-wait">${svg("clock")}<span><b>Awaiting approval</b> · ${esc(r.summary)}</span>
        ${d.canApprove && r.approval_id ? `<span class="q-wait-b"><button type="button" class="q-btn quiet" data-decide="${esc(r.approval_id)}" data-yes="0">Reject</button>
          <button type="button" class="q-btn primary" data-decide="${esc(r.approval_id)}" data-yes="1">Approve and apply</button></span>` : `<span class="q-wait-s">An admin approves it in Work › Approvals</span>`}</div>`).join("");
    const r = (d.recent || []).slice(0, 3).map((x) => {
      const say = { applied: `Applied as version ${x.result_version}`, undone: `Undone (version ${x.undone_version})`, stale: "Not applied: the data changed",
        rejected: "Rejected", failed: "Not applied" }[x.status] || x.status;
      return `<li class="q-log s-${esc(x.status)}"><span>${esc(say)}</span> · ${esc(x.summary.replace(/^Data quality: /, ""))}${x.error ? ` <em>${esc(x.error)}</em>` : ""}
        ${x.status === "applied" && d.canEdit ? `<button type="button" class="q-ib q-ib-quiet" data-undo="${esc(x.id)}" title="Put back version ${x.base_version}">${svg("undo")}<span>Undo</span></button>` : ""}</li>`;
    }).join("");
    return p + (r ? `<ul class="q-logs" aria-label="Recent resolutions">${r}</ul>` : "");
  }

  render() {
    const d = this.data;
    if (!d) return;
    if (!this.filter || !d.total) this.filter = !d.total ? "all" : d.open ? "open" : (d.staged || []).length ? "staged" : "all";
    this.el.classList.remove("busy");
    this.el.classList.toggle("compact", !!d.compact);
    this.el.innerHTML = `${this.head(d)}${this.waiting(d)}${this.diff(d)}${d.total ? this.tabs(d) : ""}${this.table(d)}
      <div class="sr" role="status" aria-live="polite">${esc(d.say || "")}</div>`;
  }

  update(d) {
    const same = this.data && this.data.dataset === d.dataset;
    if (!same) { this.filter = null; this.picked.clear(); this.fixing = null; }
    const ids = new Set((d.issues || []).filter((i) => i.status === "open").map((i) => i.id));
    [...this.picked].forEach((id) => { if (!ids.has(id)) this.picked.delete(id); });
    if (this.fixing && !ids.has(this.fixing)) this.fixing = null;
    if (same && this.filter === "open" && !d.open) this.filter = "all";  // everything decided: show where it went
    this.data = d;
    this.render();
  }
}

export default function (component) {
  const { data, parentElement, setTriggerValue } = component;
  let q = parentElement.__lystQuality;
  if (!q) { q = new Quality(parentElement); parentElement.__lystQuality = q; }
  q.connect({ setTriggerValue });
  if (data) q.update(data);
  return () => { window.setTimeout(() => { if (!q.el.isConnected) { delete parentElement.__lystQuality; } }, 0); };
}
