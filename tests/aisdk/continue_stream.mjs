// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (C) 2026 Rohn Springfield
// Continues an assistant message with a Lyst UI message stream, as useChat does after addToolApprovalResponse:
// stdin is {message, sse}; readUIMessageStream({message, stream}) assembles the continued message. Prints
// {messages, error}. Used by tests/test_chat_approvals.py; nothing here ships.
import { parseJsonEventStream, readUIMessageStream, uiMessageChunkSchema } from 'ai';

const input = JSON.parse(await new Promise((resolve) => {
  const parts = [];
  process.stdin.on('data', (d) => parts.push(d));
  process.stdin.on('end', () => resolve(Buffer.concat(parts).toString('utf8')));
}));
const bytes = new ReadableStream({ start(c) { c.enqueue(new TextEncoder().encode(input.sse)); c.close(); } });
let error = null;
const parsed = parseJsonEventStream({ stream: bytes, schema: uiMessageChunkSchema }).pipeThrough(
  new TransformStream({
    transform(part, controller) {
      if (!part.success) { error = error ?? String(part.error?.message ?? part.error); return; }
      controller.enqueue(part.value);
    },
  }),
);
const messages = [];
try {
  for await (const m of readUIMessageStream({ message: input.message, stream: parsed,
    onError: (e) => { error = error ?? String(e?.message ?? e); } })) messages.push(structuredClone(m));
} catch (e) { error = error ?? String(e?.message ?? e); }
process.stdout.write(JSON.stringify({ messages, error }));
