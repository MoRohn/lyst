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

## The live answer in the interface (shipped)

`lyst.ui.live_answer.live_answer(chat_id, ask=..., ask_id=...)` draws a `st.components.v2` component that runs the
AI SDK client in the browser (`DefaultChatTransport` and `readUIMessageStream` from `ai`). It reads the engine's
stream directly, so the answer is drawn as it is written and Streamlit reruns never interrupt it. It shows the status,
each step with what it worked on ("metric value · revenue · Q3 2026"), the answer forming with a caret, the datasets
read, and approvals waited on. When the answer is finished it offers "Open answer". It reports `done`, `open` and
`error` to Python. A stream cut by a reload resumes. Where it sits on each page is the page owner's decision.

**The browser holds only a chat ticket** (`lyst.tickets`, decided 2026-11-08). The interface keeps the person's
session on the server. To stream, it asks `POST /v1/workspaces/{ws}/chat/tickets` for a ticket and hands only the
ticket to the page, sent as `Authorization: Ticket <ticket>`. A ticket:

- opens the two chat routes, for its workspace, and nothing else;
- acts as the person who holds it, so every permission check still runs, and removal from the workspace takes
  effect at once;
- expires in 15 minutes, and the interface renews it on redraw;
- is an HMAC over its claims with a key held only in the engine's memory, so a restart invalidates every ticket.

The chat routes answer cross-origin requests only from the interface's origins (`LYST_UI_ORIGINS`; by default the
local interface on `LYST_UI_PORT`). Every other route stays same-origin. `LYST_PUBLIC_API_URL` is where the browser
reaches the engine, when that differs from `LYST_API_URL`.

**Build.** `ui-src/live` holds the source and pins `ai`. `npm ci && npm run build` writes
`src/lyst/ui/live_component/live.js` (about 190 KB, 53 KB gzipped) and `THIRD_PARTY_LICENSES.txt` beside it, and fails
on a license Lyst may not ship. The bundle is committed, so installing Lyst needs no Node.
`tests/test_chat_tickets.py` checks that the bundle carries the pinned version and only allowed licenses. It was also
checked in Chromium against a running engine, cross-origin with a ticket, in light and dark.

## Lyst Apps: generated micro-apps (shipped)

A sentence ("an app to watch operating margin and revenue by region") becomes a small interactive app. The app is a
**spec, never code**: a title and up to 24 blocks from a closed catalog (`lyst.apps.CATALOG`).

| Block | Binds | Shows |
|---|---|---|
| `kpi` | metric (grain) | the latest value and its change against the prior period |
| `trend` | metric (grain, 2 to 60 periods) | the metric over its last periods |
| `breakdown` | metric, dimension (grain) | the latest period split by the dimension |
| `table` | metric, dimension (grain, 1 to 24 periods) | the metric by period and dimension |
| `filter` | dimension | narrows every block whose metric has the dimension to one value (one filter per app) |
| `whatif` | metric (min and max percent) | a slider moving the latest value; a projection, labelled as one |
| `note` | none | a short text |

**The model never supplies a number.** Blocks bind only to approved metrics, their own dimensions and a grain. Text
the model writes (titles, notes) may not hold figures; years and quarter names are allowed. Every value is resolved by
the semantic layer's compiled SQL through the guard (`data.query`), as for Studio dashboards. A filter's values for
each block are computed by the same SQL per dimension value, so a ratio is never re-aggregated in the browser.

**Rendered while it is written.** The model writes the spec as JSON through the gateway (`stream_json`: the caller
checks each piece itself, so JSON may stream). `SpecReader` reads each block the moment its object closes; it is
checked, resolved, and shown. A block that breaks a rule is left out and the reason is reported. A retry by the
gateway removes what was shown. Without a model, or when the model's spec has no usable block, the rules build an app
from the metrics the request names.

`POST /v1/workspaces/{ws}/apps/generate` takes what `useChat` sends and answers a UI message stream:

| App | UI message stream |
|---|---|
| the app | one dynamic tool call, `lyst_app`; `useChat` shows `part.input` growing while `state` is `input-streaming` |
| the spec as it grows | `tool-input-delta`, written from checked blocks only, so the client's partial JSON is always a checked prefix |
| each block with its values | `data-lyst-app-block` (id `{call}:{block}`) |
| a block left out | `data-lyst-app-dropped` with the reason |
| a retry, or the rules replacing the model | `reset-step`, then a new call |
| the end | `tool-input-available` (the whole spec), `tool-output-available` (the saved app), a `source-document` per dataset version |

A viewer can generate an app to look at; saving needs an editor. A retried request saves no second app. Saved apps
keep every version (`apps`, `app_versions`, workspace migration 130) and are assets of kind `app`: they read current
data when opened, so a new dataset version does not make them stale, but a bound metric redefined or retired does
(`invalidated`), and the repair saves the app again, re-checking every binding. Routes: `GET .../apps/catalog`,
`GET|POST .../apps`, `GET|PUT .../apps/{id}`, `GET .../apps/{id}/open` (values read now), `POST .../apps/{id}/retire`.

## Next, in order

1. **Apps in the interface.** A renderer for the spec in a sandboxed iframe (`sandbox="allow-scripts"`,
   `default-src 'none'`, values by `postMessage`), drawn block by block from the stream, with chat tickets extended
   to the generate and open routes.
2. **Approvals in the chat.** Lyst approvals become AI SDK `tool-approval-request` parts, so `addToolApprovalResponse`
   works in any client. The response is enforced by `runtime.decide`: role checks still apply, and the proposer of
   a capability still cannot approve it.
