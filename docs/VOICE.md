# Voice

Speak a question, hear Lyst's verified answer. Voice is another way into the same governed analyst, not a second
assistant: a spoken turn runs as the signed-in person, through `TaskRuntime.submit` and every check behind it
(`task.create`, data policy and `governance.check_read`, model gates, budgets, admission).

```
Voice control (page) ──► Audio gateway ──► speech engine: browser | OpenRouter STT / TTS (external) | test engine
        │                    │         └─► duplex engine: PersonaPlex server (operator's GPU host) | test engine
        │                    ▼
        │             Voice coordinator ──► question → analyst task (same permissions) → verified answer read back
        │                                └─► action / "yes" → refused by voice, confirm on screen
        └── duplex websocket (one-time ticket) ◄──► PersonaPlex /api/chat
```

## Rules

* **Speech is never authorisation or authentication.** A voice session belongs to the person whose signed-in
  session opened it (administrators cannot speak into or read someone else's). Anything that would approve, delete,
  send, publish, export, schedule, grant or change something is answered with "confirm it on screen" and audited
  (`voice.action_refused`); a spoken "yes" or "approve" never decides an approval (`voice.assent_refused`).
* **Stopping speech is not stopping work.** "Stop", "quiet", "never mind" stop playback (`stop_playback`) and leave
  the analysis running, and Lyst says so; only "cancel the analysis" / "stop the task" cancels it, with the person's
  own cancel permission.
* **Stale answers are not spoken.** Every turn has a `seq`; a read-back for a question that a later question in the
  same session replaced is marked `superseded`, so the page shows it without starting to speak it.
* **Only verified answers are spoken.** Read-back is the task's own `result.answer` when the outcome is `complete`;
  otherwise Lyst says the result needs attention on screen. The duplex model never composes figures. Figures are
  rewritten for the ear only ("-$292.1K (-3.4%)" is read as "minus 292.1 thousand dollars (minus 3.4 percent)"); the
  exact text stays on screen. "What did I miss?" reads back verified answers finished since the person's last voice
  session, and how many approvals wait for them.
* **Audio is ephemeral by default.** It is transcribed and dropped. An administrator may turn on `retain_audio`;
  turning it off removes stored audio at the next retention pass. Transcripts are redacted before storage and pruned
  after `transcript_days` (default 30). Idle sessions close after 30 minutes.
* **External audio is off until allowed.** An external engine (OpenRouter speech-to-text or text-to-speech) is used only when the
  workspace has `allow_external_audio` on *and* external models are allowed for it. A computed answer is spoken by an
  external engine only if `governance.model_gate` would let it go to an external model. Every OpenRouter request is
  a row in `llm_calls` (`purpose = 'voice.stt'` or `'voice.tts'`, with the reported cost). The OpenRouter key comes
  from the credential broker at call time and never reaches the page.

## Status

| Part | Status |
|---|---|
| Voice coordinator (intents, permission path, action refusal, read-back, cancel, status, repeat) | Implemented & Verified (tests/test_voice.py) |
| Audio gateway (engine order, capacity, circuit breaker, fallback, external gate) | Implemented & Verified |
| Sessions, turns, preferences, settings, retention, redaction | Implemented & Verified |
| HTTP API and duplex websocket with one-time ticket (test engine) | Implemented & Verified |
| Browser speech (page recognises and speaks; server takes text turns and returns `speak_text`) | Partial: server side verified; the page component belongs to the UI |
| PersonaPlex adapter (URL/query, framing, status probe, websocket relay) | Implemented but Unverified: never run against a real PersonaPlex server |
| Spoken-number formatter, speaking styles, catch-up, stale read-back guard | Implemented & Verified |
| OpenRouter speech-to-text (`POST /audio/transcriptions`) | Implemented but Unverified: request, response and call record tested against a stub; needs `LYST_VOICE_STT_MODEL` and a live check |
| OpenRouter text-to-speech (`POST /audio/speech`, mp3) | Implemented but Unverified: same; needs `LYST_VOICE_TTS_MODEL` and `LYST_VOICE_TTS_VOICE` |
| OpenRouter model discovery (`GET /models?output_modalities=speech` or `transcription`) | Implemented but Unverified (adapter method; no admin route yet) |
| OpenRouter multimodal audio (chat `input_audio`) | Planned: the dedicated endpoints cover transcription and speech |
| Local (offline) STT/TTS such as whisper.cpp or Piper | Planned |
| PersonaPlex latency, GPU profiling, multi-GPU pooling | Unsupported here: no NVIDIA GPU on the development machine; nothing has been measured, so no latency is claimed |

## PersonaPlex

NVIDIA PersonaPlex is a full-duplex speech-to-speech model built on Moshi. Code is MIT; the weights
(`nvidia/personaplex-7b-v1`) are under the NVIDIA Open Model License and gated on Hugging Face. **Lyst does not bundle,
download or vendor the weights**; an operator who has accepted the licence runs NVIDIA's server on a CUDA host
(NVIDIA tested on A100 80 GB; Ampere/Hopper listed, Linux preferred):

```bash
SSL_DIR=$(mktemp -d); python -m moshi.server --ssl "$SSL_DIR" --host 0.0.0.0 --port 8998 --static none
```

and sets `LYST_PERSONAPLEX_URL=https://gpu-host:8998` for the Lyst engine. The Hugging Face token stays on the GPU
host; Lyst never sees it.

What the published server allows, and how Lyst uses it:

* `GET /api/chat?voice_prompt=<NATF0-3|NATM0-3|VARF0-4|VARM0-4>.pt&text_prompt=...&seed=...`; binary frames, first byte
  0 handshake, 1 Opus audio, 2 a text piece. 24 kHz audio.
* **One conversation at a time** (the server holds a single lock), so the gateway gives PersonaPlex capacity 1 and a
  second duplex request falls back down the order instead of waiting.
* **The persona is fixed at connect**, and nothing can be injected mid-conversation. PersonaPlex therefore runs under
  a grounded persona that forbids stating numbers or agreeing to actions, and provides conversational presence
  (turn-taking, acknowledgement, barge-in). Answers are spoken from Lyst's verified text by a speech engine or the
  page, never improvised by the duplex model.
* **No transcript of the person's speech.** The person's words come from a speech-to-text engine (or the page's own
  recognition) before the coordinator acts on them.
* No health endpoint: status is a TCP probe of the configured address.

## Configuration

| Setting | Where | Default |
|---|---|---|
| `LYST_PERSONAPLEX_URL` | environment | unset (PersonaPlex unconfigured) |
| `LYST_VOICE_STT_MODEL` | environment | unset (OpenRouter STT unconfigured) |
| `LYST_VOICE_TTS_MODEL`, `LYST_VOICE_TTS_VOICE` | environment | unset (OpenRouter TTS unconfigured) |
| `enabled`, `stt_order`, `tts_order`, `duplex_order`, `voice`, `allow_external_audio`, `retain_audio`, `transcript_days`, `speak_results` | `PATCH /v1/workspaces/{ws}/voice/settings` (admin) | on, `browser → openrouter_stt`, `browser`, `personaplex`, `NATF2`, off, off, 30, on |
| `mode` (`push_to_talk` / `hands_free`), `speak_results`, `voice`, `style` (professional, conversational, executive, technical, briefing) | `PUT /v1/workspaces/{ws}/voice/preferences` (each person) | push to talk, workspace default |

## API

| Method | Path | Purpose |
|---|---|---|
| GET/PATCH | `/v1/workspaces/{ws}/voice/settings` | workspace settings (PATCH: `voice.admin`) |
| GET/PUT | `/v1/workspaces/{ws}/voice/preferences` | the caller's own preferences |
| GET | `/v1/workspaces/{ws}/voice/engines` | every engine's state, support label, capacity and health |
| POST | `/v1/workspaces/{ws}/voice/sessions` | start a session (`{mode?}`) |
| GET | `/v1/workspaces/{ws}/voice/sessions/{sid}` | the session and its transcript |
| POST | `/v1/workspaces/{ws}/voice/sessions/{sid}/turns` | one utterance: `{text}` from the device, or `{audio_b64, mime}` |
| GET | `/v1/workspaces/{ws}/voice/sessions/{sid}/answer/{task_id}` | read-back when the task is done (`ready`, `speak_text`) |
| POST | `/v1/workspaces/{ws}/voice/sessions/{sid}/speak` | speech for `{task_id}` or `{text}`; `engine: "browser"` means the page speaks `text` |
| POST | `/v1/workspaces/{ws}/voice/sessions/{sid}/stream-ticket` | one-time 60 s ticket for the duplex websocket |
| WS | `/v1/workspaces/{ws}/voice/sessions/{sid}/duplex?ticket=` | duplex audio, PersonaPlex framing |
| POST | `/v1/workspaces/{ws}/voice/sessions/{sid}/end` | end the session |

## Known limits and risks

* Nothing here has run against a real PersonaPlex server or a GPU; capacity 1 per server is taken from the published
  server's single lock, not measured. Latency, VRAM per session and concurrency must be profiled on the target host
  before any number is stated.
* Echo cancellation, noise suppression and voice-activity detection are left to the browser (`getUserMedia`
  constraints); they are part of the page component, which is not built yet.
* Intent rules are English-only and word-based. A misheard question is re-asked; a phrase that starts with an action
  verb ("share why revenue fell") is treated as an action and sent to the screen, which is the safe side of the error.
* Read-back covers analyst tasks. Spoken dashboard building, workflow control and chart-context follow-ups route
  through the same question path and are only as good as the analyst's own handling of those requests.
* Stream tickets live in the API process's memory, so a multi-process deployment needs session affinity for the
  ticket request and the websocket (or a shared ticket store).
