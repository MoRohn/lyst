// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (C) 2026 Rohn Springfield
// Reads a Lyst UI message stream (SSE bytes on stdin) with the real AI SDK, exactly as useChat does:
// parseJsonEventStream + uiMessageChunkSchema validate every chunk, then readUIMessageStream assembles the message.
// Prints {chunks, messages, error} as JSON. Used by tests/test_uistream.py; nothing here ships.
import { parseJsonEventStream, readUIMessageStream, uiMessageChunkSchema } from 'ai';

const input = await new Promise((resolve) => {
  const parts = [];
  process.stdin.on('data', (d) => parts.push(d));
  process.stdin.on('end', () => resolve(Buffer.concat(parts)));
});

const bytes = new ReadableStream({ start(c) { c.enqueue(new Uint8Array(input)); c.close(); } });
const chunks = [];
let error = null;
const parsed = parseJsonEventStream({ stream: bytes, schema: uiMessageChunkSchema }).pipeThrough(
  new TransformStream({
    transform(part, controller) {
      if (!part.success) { error = error ?? String(part.error?.message ?? part.error); return; }
      chunks.push(part.value.type);
      controller.enqueue(part.value);
    },
  }),
);
const messages = [];
try {
  for await (const m of readUIMessageStream({ stream: parsed, onError: (e) => { error = error ?? String(e?.message ?? e); } })) {
    messages.push(structuredClone(m));
  }
} catch (e) {
  error = error ?? String(e?.message ?? e);
}
process.stdout.write(JSON.stringify({ chunks, messages, error }));
