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

## The answer as the model writes it (shipped)

When a model words the answer (`deliver`) or a conversational reply (`converse`), the call streams: `Gateway.complete`
with `on_delta` sends `stream: true` and passes each piece of answer text on as it arrives. Admission, retries, the
model walk and the `llm_calls` row are unchanged, and the call still returns the whole reply. Reasoning is never
passed on. JSON calls never stream, so a whole object is checked before anyone sees it. Before a retry or the next
model, the gateway calls `on_delta(None)` so the text already shown is discarded. A server that ignores `stream` and
answers with one completion still works: its text is passed on whole.

The text goes through a **figure check as it is written** (`lyst.live.DraftGuard`). A figure is judged only after
three more words have arrived, so its unit and scale ("2.69 million", "3.5 percentage points") are read with it. Text
is passed on only while every figure in it matches a tool-produced value, using the analyst's own rule
(`unbacked_numbers`). The first unbacked figure stops the draft: nothing after it is shown. The analyst's check on the
whole reply then decides as before. If it keeps its computed answer, the draft is withdrawn at once.

Drafts live in memory per task (`lyst.live.DRAFTS`, 15 minutes after their last change). They are never stored,
never sent as events and never treated as evidence. In the UI message stream, a draft is a step of its own: a
`text-start` followed by `text-delta` chunks as it grows. Step and approval changes wait while it is open, so the
draft stays the newest step. At the task's end the stored answer decides. A draft that is exactly the stored answer
gets `text-end`, and the answer is not sent twice. Any other draft is removed with `reset-step` and the stored answer
follows. A new epoch (a retry, or the wording withdrawn) removes the draft the same way. If workers run in another
process, the stream has no draft and shows the answer when the task ends.

## Next, in order

1. **A live answer component.** A bundled Streamlit component built on the AI SDK's `Chat` that renders the stream:
   steps as they run, sources and charts, approvals inline. Steering (`steering-message`) lets a person add
   "also by region" mid-run as a follow-up. Its placement on pages is the Redesign lane's call.
2. **Generative micro-apps (Lyst Apps).** The model streams an app spec as structured output, rendered live while
   it is generated (`tool-input-delta` / partial JSON), against a closed component catalog: KPI, trend, breakdown,
   table, filter, what-if slider, note. The model never writes code and never supplies a number. Every value is a
   binding to a governed metric or dataset that the semantic layer resolves, as with Studio dashboards. Apps render
   in a sandboxed iframe (`sandbox="allow-scripts"`, `default-src 'none'`, data by `postMessage`). Each app is a
   versioned asset with dependencies and health scans (workspace migration 130).
3. **Approvals in the chat.** Lyst approvals become AI SDK `tool-approval-request` parts, so `addToolApprovalResponse`
   works in any client. The response is enforced by `runtime.decide`: role checks still apply, and the proposer of
   a capability still cannot approve it.
