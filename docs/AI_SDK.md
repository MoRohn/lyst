# The AI SDK in Lyst

How Lyst uses the [AI SDK](https://ai-sdk.dev) (Vercel, Apache-2.0) for streaming answers, generative interfaces and
micro-apps, and why the pieces sit where they do. Version checked: `ai` 7.0.165, `@ai-sdk/react` 4.0.169.

## The decision: the engine speaks the protocol; the AI SDK runs in the client

Every model call in Lyst goes through one gateway (`src/lyst/llm/gateway.py`: admission, adaptive concurrency,
retries, OpenRouter routing, the cost ledger), every action through `Guard.require`, and every task through a
contract and a receipt (`docs/HARNESS.md`). Running the AI SDK's `streamText` or `ToolLoopAgent` on a Node server
would call models around all of that, and each guarantee would have to be rebuilt and kept in step in a second
language.

The AI SDK separates its server (model calls) from its client (`useChat`, `Chat`, `readUIMessageStream`) with a
small, versioned wire format: the **UI message stream** (`x-vercel-ai-ui-message-stream: v1`; Server-Sent Events of
JSON chunks; ends with `data: [DONE]`). Lyst's engine writes that format itself (`src/lyst/uistream.py`), so any AI SDK
client reads Lyst's governed work unchanged, and the engine stays the only place a model is called.

Node is used only at build and test time. `tests/aisdk` pins `ai` and reads Lyst's stream with the package's own
chunk schema (`uiMessageChunkSchema` through `parseJsonEventStream`) and its own message assembly
(`readUIMessageStream`), which is what `useChat` does. A change that the AI SDK would reject fails CI.

Licenses: `ai`, `@ai-sdk/*` and `@openrouter/ai-sdk-provider` are Apache-2.0; React and zod are MIT. All can be
combined with AGPL-3.0-only (Apache-2.0 is GPLv3-compatible). The OpenRouter provider is not needed: the gateway
keeps calling OpenRouter.

## Asking from an AI SDK client (shipped)

```ts
import { useChat } from '@ai-sdk/react';
import { DefaultChatTransport } from 'ai';

const { messages, sendMessage } = useChat({
  id: chatId,
  resume: true,
  transport: new DefaultChatTransport({
    api: `${LYST}/v1/workspaces/${ws}/chat`,
    headers: { Authorization: `Bearer ${token}` },
  }),
});
```

`POST /v1/workspaces/{ws}/chat` takes what `useChat` sends (`{id, messages, trigger, messageId}`, plus optional
`autonomy`). The last user message's text is the question and goes through the same pipeline as
`POST /v1/workspaces/{ws}/tasks`. One user message is one task: a retried request returns the same task, and
`regenerate` asks again on purpose. `GET /v1/workspaces/{ws}/chat/{id}/stream` resumes the chat's running task, or
answers 204 when none is running.

| Lyst | UI message stream |
|---|---|
| the task | `start` (`messageId` = task id) ... `finish` (`stop` or `error`), `abort` when cancelled |
| where it is (lifecycle) | `data-lyst-status`, one part updated in place by id |
| each engine step (capability) | `start-step`, dynamic `tool-input-available` (tool = capability, input = its arguments), then `tool-output-available` or `tool-output-error`, `finish-step` |
| an approval on the task | `data-lyst-approval` with `decide: /v1/approvals/{id}`. A person decides through that route, under its role checks. The stream never approves anything, and neither does a model |
| the answer | `text-start`, `text-delta`, `text-end` |
| each dataset version read | `source-document` (`sourceId` `dataset@version`, `mediaType` `application/vnd.lyst.dataset`) |
| each chart | `data-lyst-chart` |
| confidence, outcome, mode | `messageMetadata` on `start` and `finish` |

Every value is copied from the stored task, its steps and its result. The stream re-reads the task through
`runtime.get`, which checks `workspace.read` on every poll.

## Next, in order

1. **Token streaming in the gateway.** `Gateway.stream` streams from OpenRouter and keeps the same admission, retries
   and cost ledger. Narrative and conversational steps then emit `text-delta` and `reasoning-delta` as they are
   written, not when the task ends.
2. **A live answer component.** A bundled Streamlit component built on the AI SDK's `Chat` that renders the stream:
   steps as they run, sources and charts, approvals inline. Steering (`steering-message`) lets a person add
   "also by region" mid-run as a follow-up. Its placement on pages is the Redesign lane's call.
3. **Generative micro-apps (Lyst Apps).** The model streams an app spec as structured output, rendered live while
   it is generated (`tool-input-delta` / partial JSON), against a closed component catalog: KPI, trend, breakdown,
   table, filter, what-if slider, note. The model never writes code and never supplies a number. Every value is a
   binding to a governed metric or dataset that the semantic layer resolves, as with Studio dashboards. Apps render
   in a sandboxed iframe (`sandbox="allow-scripts"`, `default-src 'none'`, data by `postMessage`). Each app is a
   versioned asset with dependencies and health scans (workspace migration 130).
4. **Approvals in the chat.** Lyst approvals become AI SDK `tool-approval-request` parts, so `addToolApprovalResponse`
   works in any client. The response is enforced by `runtime.decide`: role checks still apply, and the proposer of
   a capability still cannot approve it.
