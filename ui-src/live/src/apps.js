// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (C) 2026 Rohn Springfield
// A Lyst App in the page: the AI SDK reads the app as Lyst generates it, and a sandboxed iframe draws it block by block.
//
// Bundled by ui-src/live (esbuild) into src/lyst/ui/apps_component/apps.js and drawn by st.components.v2. Python sends
// {apps, ticket, ask, askId, appId, labels}; this sends Python one of:
//   {type: "done", appId, saved, version}   an app finished generating (saved when the person may save)
//   {type: "error", message}                the stream or the app could not be read
//
// Two parts with different trust:
// * this host holds the chat ticket (lyst.tickets: 15 minutes, this workspace's chat and app routes only) and reads
//   Lyst's UI message stream with the AI SDK (DefaultChatTransport, readUIMessageStream), or opens a saved app;
// * the renderer (frame.js) runs in an iframe with sandbox="allow-scripts" and no same origin, under a CSP of
//   default-src 'none': it has no network, no cookies, no storage and no access to this page. It receives only the
//   checked spec and the values Lyst resolved, by postMessage, and draws them with textContent and SVG.
import { DefaultChatTransport, readUIMessageStream } from 'ai';

/* global __LYST_FRAME__ */
const FRAME_SRC = __LYST_FRAME__;
const CSP = "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'";
const TOKENS = { '--text': '--lyst-text', '--muted': '--lyst-text-muted', '--raised': '--lyst-raised', '--surface': '--lyst-surface',
  '--border': '--lyst-border', '--accent': '--lyst-accent', '--good': '--lyst-good', '--bad': '--lyst-bad', '--warn': '--lyst-warn',
  '--r': '--lyst-radius-md', '--font': '--lyst-font' };
const FALLBACK = { '--text': '#16181d', '--muted': '#5f6470', '--raised': '#fff', '--surface': '#f5f6f8', '--border': '#e3e5ea',
  '--accent': '#0b6b64', '--good': '#176f31', '--bad': '#b42318', '--warn': '#865a00', '--r': '10px',
  '--ease': 'cubic-bezier(.2, .7, .2, 1)' };
const rid = () => Math.random().toString(36).slice(2, 12);

class AppView {
  constructor(host) {
    this.host = host;
    this.el = document.createElement('div');
    this.el.className = 'la';
    this.status = document.createElement('div');
    this.status.className = 'la-status';
    this.status.setAttribute('role', 'status');
    this.frame = document.createElement('iframe');
    this.frame.className = 'la-frame';
    this.frame.setAttribute('sandbox', 'allow-scripts');
    this.frame.setAttribute('referrerpolicy', 'no-referrer');
    this.frame.setAttribute('title', 'Lyst app');
    this.frame.srcdoc = `<!doctype html><html><head><meta charset="utf-8"><meta http-equiv="Content-Security-Policy" content="${CSP}">`
      + `</head><body><script>${FRAME_SRC.replace(/<\/script/gi, '<\\/script')}</script></body></html>`;
    this.el.append(this.status, this.frame);
    host.appendChild(this.el);
    this.ready = false;
    this.queue = [];
    this.onMessage = (e) => {
      if (e.source !== this.frame.contentWindow) return;  // only this app's own frame
      const m = e.data || {};
      if (m.lyst === 'ready') { this.ready = true; this.theme(); for (const q of this.queue.splice(0)) this.post(q); }
      if (m.lyst === 'height' && Number.isFinite(m.height)) this.frame.style.height = `${Math.min(4000, Math.max(80, m.height))}px`;
    };
    window.addEventListener('message', this.onMessage);
  }

  connect(api) { this.api = api; }

  post(m) {
    if (!this.ready) { this.queue.push(m); return; }
    this.frame.contentWindow.postMessage(m, '*');  // an opaque-origin frame: '*' is the only target it has
  }

  theme() {
    const cs = getComputedStyle(this.host);
    const vars = {};
    for (const [mine, page] of Object.entries(TOKENS)) vars[mine] = cs.getPropertyValue(page).trim() || FALLBACK[mine] || '';
    vars['--ease'] = FALLBACK['--ease'];
    this.post({ lyst: 'theme', vars, labels: this.labels });
  }

  say(text, cls = '') {
    this.status.textContent = text;
    this.status.className = `la-status ${cls}`;
  }

  update(data) {
    this.labels = data.labels || {};
    this.base = data.apps;
    this.ticket = data.ticket;
    if (this.ready) this.theme();
    if (data.ask && data.askId && data.askId !== this.asked) {
      this.asked = data.askId;
      this.generate(data.ask, data.askId);
    } else if (data.appId && data.appId !== this.opened && !data.ask) {
      this.opened = data.appId;
      this.open(data.appId);
    }
  }

  headers() { return { Authorization: `Ticket ${this.ticket}` }; }

  async open(appId) {
    this.say(this.labels.opening || 'Opening…');
    try {
      const r = await fetch(`${this.base}/${encodeURIComponent(appId)}/open`, { headers: this.headers() });
      if (!r.ok) throw new Error((await r.json().catch(() => ({}))).detail || `HTTP ${r.status}`);
      const app = await r.json();
      this.post({ lyst: 'reset' });
      this.post({ lyst: 'title', title: app.spec.title, subtitle: app.spec.subtitle });
      for (const b of app.spec.blocks) this.post({ lyst: 'block', block: b, data: app.blocks[b.id] || {} });
      this.say(`${this.labels.version || 'Version'} ${app.version} · ${this.sources(app.sources)}`);
    } catch (e) { this.fail(e); }
  }

  sources(src) {
    const ds = Object.entries(src?.datasets || {}).map(([d, v]) => `${d} v${v}`);
    return ds.length ? `${this.labels.from || 'From'} ${ds.join(', ')}` : '';
  }

  async generate(text, askId) {
    this.abort?.abort();
    this.abort = new AbortController();
    this.callId = null;
    this.sent = new Map();
    this.title = null;
    this.post({ lyst: 'reset' });
    this.say(this.labels.building || 'Building the app…', 'is-live');
    const transport = new DefaultChatTransport({ api: `${this.base}/generate`, headers: () => this.headers() });
    let last = null;
    try {
      const stream = await transport.sendMessages({
        chatId: `app-${askId}`, messages: [{ id: rid(), role: 'user', parts: [{ type: 'text', text }] }],
        trigger: 'submit-message', messageId: undefined, abortSignal: this.abort.signal,
      });
      for await (const m of readUIMessageStream({ stream })) { last = m; this.step(m); }
    } catch (e) { this.fail(e); return; }
    const tool = last && this.tool(last);
    if (!tool || tool.state === 'output-error') { this.fail(new Error(tool?.errorText || 'The app could not be built.')); return; }
    const out = tool.output || {};
    const left = (out.dropped || []).length;
    this.say([out.saved ? `${this.labels.saved || 'Saved'} · ${this.labels.version || 'Version'} ${out.version}` : (this.labels.notSaved || 'Not saved: editors save apps'),
      this.sources(out.sources), left ? `${left} ${this.labels.leftOut || 'left out'}` : ''].filter(Boolean).join(' · '));
    this.status.title = (out.dropped || []).map((d) => `${d.block || '?'}: ${d.reason}`).join('\n');
    this.api?.setTriggerValue('event', { type: 'done', appId: out.appId, saved: !!out.saved, version: out.version });
  }

  tool(m) { return m.parts.findLast((p) => p.type === 'dynamic-tool' && p.toolName === 'lyst_app'); }

  // each message snapshot from the AI SDK: send the frame what is new (a retry starts over)
  step(m) {
    const tool = this.tool(m);
    if (!tool) return;
    if (tool.toolCallId !== this.callId) {
      if (this.callId) this.post({ lyst: 'reset' });
      this.callId = tool.toolCallId; this.sent = new Map(); this.title = null;
    }
    const title = tool.input?.title;
    if (title && title !== this.title) { this.title = title; this.post({ lyst: 'title', title, subtitle: tool.input?.subtitle }); }
    for (const p of m.parts) {
      if (p.type !== 'data-lyst-app-block' || !p.id?.startsWith(`${this.callId}:`)) continue;
      const seen = JSON.stringify(p.data);  // a block is sent again when Lyst updates its values in place
      if (this.sent.get(p.id) === seen) continue;
      this.sent.set(p.id, seen);
      this.post({ lyst: 'block', block: p.data.block, data: p.data.data });
    }
    if (tool.state === 'input-streaming') this.say(`${this.labels.building || 'Building the app…'} ${this.sent.size}`, 'is-live');
  }

  fail(e) {
    if (e?.name === 'AbortError') return;
    const message = String(e?.message || e);
    this.say(`${this.labels.error || 'The app stopped:'} ${message}`, 'is-error');
    this.api?.setTriggerValue('event', { type: 'error', message });
  }

  destroy() { this.abort?.abort(); window.removeEventListener('message', this.onMessage); }
}

// Streamlit calls this on mount and on every data change; the view lives on the host element, so a redraw of the page
// never interrupts an app that is being generated.
export default function (component) {
  const { data, parentElement, setTriggerValue } = component;
  let view = parentElement.__lystApp;
  if (!view) {
    view = new AppView(parentElement);
    parentElement.__lystApp = view;
  }
  view.connect({ setTriggerValue });
  if (data) view.update(data);
  return () => {
    window.setTimeout(() => {
      if (!view.el.isConnected) { view.destroy(); delete parentElement.__lystApp; }
    }, 0);
  };
}
