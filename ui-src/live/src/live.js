// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (C) 2026 Rohn Springfield
// Lyst's live answer: the AI SDK client in the page, reading Lyst's UI message stream as the engine works.
//
// Bundled by ui-src/live (esbuild) into src/lyst/ui/live_component/live.js and drawn by st.components.v2 in the page
// (no iframe). Python sends {api, ticket, chatId, ask, askId, labels}; this sends Python one of:
//   {type: "done", taskId, outcome}    an answer finished (Python can open it or refresh what depends on it)
//   {type: "open", taskId}             the person asked to open the full answer
//   {type: "error", message}           the stream could not be read (an expired ticket, the engine unreachable)
//
// The only network call is to Lyst's chat routes, with a short-lived chat ticket (lyst.tickets) that can do nothing
// else. Everything shown comes from the stream: the engine decides what each step, source and figure is; this draws.
import { DefaultChatTransport, readUIMessageStream } from 'ai';

const ICON = {
  running: '<circle cx="12" cy="12" r="8" stroke-dasharray="38 12"/>',
  done: '<path d="m5 12 5 5L20 7"/>',
  failed: '<path d="M6 6l12 12"/><path d="M18 6 6 18"/>',
  source: '<path d="M4 5c0-1 3.6-2 8-2s8 1 8 2-3.6 2-8 2-8-1-8-2z"/><path d="M4 5v14c0 1 3.6 2 8 2s8-1 8-2V5"/><path d="M4 12c0 1 3.6 2 8 2s8-1 8-2"/>',
  approval: '<path d="M12 3 5 6v6c0 4 3 7.5 7 9 4-1.5 7-5 7-9V6z"/><path d="m9 12 2 2 4-4"/>',
  open: '<path d="M7 17 17 7"/><path d="M8 7h9v9"/>',
};
const svg = (name) => `<svg viewBox="0 0 24 24" aria-hidden="true" fill="none" stroke="currentColor" stroke-width="2"
  stroke-linecap="round" stroke-linejoin="round">${ICON[name]}</svg>`;
const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
const rid = () => Math.random().toString(36).slice(2, 12);
const TOOL_STATE = { 'output-available': 'done', 'output-error': 'failed', 'output-denied': 'failed' };

class Live {
  constructor(host) {
    this.el = document.createElement('div');
    this.el.className = 'lv';
    host.appendChild(this.el);
    this.messages = [];
    this.status = 'ready';
    this.asked = null;
    this.frame = 0;
    this.el.addEventListener('click', (e) => {
      const b = e.target.closest('[data-open]');
      if (b) this.api?.setTriggerValue('event', { type: 'open', taskId: b.dataset.open });
    });
  }

  connect(api) { this.api = api; }

  update(data) {
    this.labels = data.labels || {};
    const changed = data.api !== this.url || data.chatId !== this.chatId;
    this.url = data.api;
    this.chatId = data.chatId;
    this.ticket = data.ticket;
    this.transport = new DefaultChatTransport({ api: this.url, headers: () => ({ Authorization: `Ticket ${this.ticket}` }) });
    if (changed) { this.messages = []; this.asked = null; this.resume(); }
    if (data.ask && data.askId && data.askId !== this.asked) {
      this.asked = data.askId;
      this.ask(data.ask);
    }
    this.draw();
  }

  async ask(text) {
    this.abort?.abort();
    this.abort = new AbortController();
    this.messages.push({ id: rid(), role: 'user', parts: [{ type: 'text', text }] });
    this.status = 'submitted';
    this.draw();
    try {
      const stream = await this.transport.sendMessages({
        chatId: this.chatId, messages: this.messages, trigger: 'submit-message', messageId: undefined,
        abortSignal: this.abort.signal,
      });
      await this.consume(stream);
    } catch (e) { this.fail(e); }
  }

  async resume() {
    try {
      const stream = await this.transport.reconnectToStream({ chatId: this.chatId });
      if (stream) await this.consume(stream);
    } catch (e) { this.fail(e); }
  }

  async consume(stream) {
    this.status = 'streaming';
    let last = null;
    for await (const m of readUIMessageStream({ stream })) {
      last = m;
      const i = this.messages.findIndex((x) => x.id === m.id);
      if (i >= 0) this.messages[i] = m; else this.messages.push(m);
      this.draw();
    }
    this.status = 'ready';
    this.draw();
    if (last) this.api?.setTriggerValue('event', { type: 'done', taskId: last.metadata?.taskId, outcome: last.metadata?.outcome });
  }

  fail(e) {
    if (e?.name === 'AbortError') return;
    this.status = 'error';
    this.error = String(e?.message || e);
    this.draw();
    this.api?.setTriggerValue('event', { type: 'error', message: this.error });
  }

  draw() {
    if (this.frame) return;
    this.frame = requestAnimationFrame(() => { this.frame = 0; this.render(); });
  }

  render() {
    const L = this.labels;
    const out = [];
    for (const m of this.messages) {
      if (m.role === 'user') {
        out.push(`<div class="lv-q">${esc(m.parts.map((p) => p.text || '').join(' '))}</div>`);
        continue;
      }
      out.push(this.assistant(m, L));
    }
    if (this.status === 'submitted') out.push(`<div class="lv-wait">${esc(L.starting || 'Starting…')}</div>`);
    if (this.status === 'error') out.push(`<div class="lv-err" role="alert">${esc(L.error || 'The live view stopped.')} ${esc(this.error)}</div>`);
    this.el.innerHTML = out.join('');
  }

  assistant(m, L) {
    const status = m.parts.find((p) => p.type === 'data-lyst-status')?.data;
    const steps = m.parts.filter((p) => p.type === 'dynamic-tool');
    const text = m.parts.filter((p) => p.type === 'text');
    const sources = m.parts.filter((p) => p.type === 'source-document');
    const approvals = m.parts.filter((p) => p.type === 'data-lyst-approval').map((p) => p.data);
    const meta = m.metadata || {};
    const finished = meta.status && ['done', 'failed', 'cancelled'].includes(meta.status);
    const writing = text.some((t) => t.state === 'streaming');
    const head = `<div class="lv-head"><span class="lv-dot ${finished ? 'is-done' : 'is-live'}"></span>
      <span>${esc(status?.label || status?.state || (finished ? (L.done || 'Done') : (L.working || 'Working')))}</span>
      ${meta.confidence?.level ? `<span class="lv-pill">${esc(meta.confidence.level)}</span>` : ''}
      ${finished && meta.taskId ? `<button class="lv-open" data-open="${esc(meta.taskId)}">${esc(L.open || 'Open answer')} ${svg('open')}</button>` : ''}</div>`;
    const stepRows = steps.map((s) => {
      const st = TOOL_STATE[s.state] || 'running';
      return `<li class="lv-step is-${st}">${svg(st)}<span>${esc(s.title || s.toolName)}</span></li>`;
    }).join('');
    const stepsHtml = steps.length ? `<details class="lv-steps" ${finished ? '' : 'open'}><summary>${esc(L.steps || 'Steps')}
      <span class="lv-count">${steps.filter((s) => TOOL_STATE[s.state]).length}/${steps.length}</span></summary><ol>${stepRows}</ol></details>` : '';
    const answer = text.map((t) => `<p class="lv-text${t.state === 'streaming' ? ' is-writing' : ''}">${esc(t.text)}</p>`).join('');
    const src = sources.length ? `<div class="lv-sources" aria-label="${esc(L.sources || 'Sources')}">${sources.map((s) =>
      `<span class="lv-src">${svg('source')}${esc(s.title)}</span>`).join('')}</div>` : '';
    const ap = approvals.filter((a) => a.status === 'pending').map((a) =>
      `<div class="lv-approval">${svg('approval')}<span>${esc(a.summary || a.action)}</span><em>${esc(L.approve || 'Waiting for approval in Work › Approvals')}</em></div>`).join('');
    return `<section class="lv-a" aria-busy="${finished ? 'false' : 'true'}">${head}${stepsHtml}
      <div class="lv-answer" aria-live="polite">${answer}${writing ? '<span class="lv-caret" aria-hidden="true"></span>' : ''}</div>${src}${ap}</section>`;
  }

  destroy() { this.abort?.abort(); }
}

// Streamlit calls this on mount and on every data change; the view lives on the host element, so a redraw of the page
// never interrupts a stream that is being read.
export default function (component) {
  const { data, parentElement, setTriggerValue } = component;
  let live = parentElement.__lystLive;
  if (!live) {
    live = new Live(parentElement);
    parentElement.__lystLive = live;
  }
  live.connect({ setTriggerValue });
  if (data) live.update(data);
  return () => {
    window.setTimeout(() => {
      if (!live.el.isConnected) { live.destroy(); delete parentElement.__lystLive; }
    }, 0);
  };
}
