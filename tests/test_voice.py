# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Rohn Springfield
"""Voice: spoken turns go through the same governed analyst; speech never authorises; audio is ephemeral by default."""

import base64
import json

import httpx
import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from lyst.api import create_app
from lyst.security import AccessDenied
from lyst.specialists.core import AnalystRequest
from lyst.voice.coordinator import classify, speak_numbers, speakable
from lyst.voice.engines import (
    GROUNDED_PERSONA,
    KIND_AUDIO,
    KIND_HANDSHAKE,
    KIND_TEXT,
    EngineError,
    FakeEngine,
    OpenRouterSpeaker,
    OpenRouterTranscriber,
    PersonaPlexEngine,
    decode_frame,
    encode_frame,
    personaplex_url,
)
from lyst.voice.gateway import AudioGateway, NoEngine

WS = "northwind"


class ExternalFake(FakeEngine):
    name = "ext"
    local = False


class OtherFake(FakeEngine):
    name = "fake2"


@pytest.fixture
def voiced(seeded):
    lyst, owner = seeded
    lyst.voice.gateway.register(FakeEngine())
    lyst.voice.set_settings(owner, WS, {"stt_order": ["fake"], "tts_order": ["fake", "browser"], "duplex_order": ["fake"]})
    return lyst, owner


def _member(lyst, owner, email, role):
    u = lyst.guard.create_user(email, email.split("@")[0])
    lyst.guard.add_member(owner, WS, u.user_id, role)
    return u


# -- what a turn means ---------------------------------------------------------------------------------------------
@pytest.mark.parametrize(("said", "kind"), [
    ("Why did operating margin decline last quarter?", "question"),
    ("okay so what was revenue last month", "question"),
    ("show me revenue by region", "question"),
    ("can you approve the pending request", "action"),
    ("please delete the targets dataset", "action"),
    ("Send this to the board", "action"),
    ("yes", "assent"),
    ("Approve.", "assent"),
    ("go ahead", "assent"),
    ("stop", "stop_speaking"),
    ("stop talking", "stop_speaking"),
    ("never mind", "stop_speaking"),
    ("cancel", "stop_speaking"),
    ("cancel the analysis", "cancel_work"),
    ("please stop the running task", "cancel_work"),
    ("what did you find while I was away", "catch_up"),
    ("modify the production ledger entry", "action"),
    ("say that again", "repeat"),
    ("is it done", "status"),
    ("   ", "silence"),
    ("um", "silence"),
])
def test_classify(said, kind):
    assert classify(said).kind == kind


def test_speakable_trims_markdown_and_long_answers():
    assert speakable("**Revenue** was [$8.2M](x).") == "Revenue was 8.2 million dollars."
    long = "Revenue rose. " * 100
    out = speakable(long, limit=100)
    assert out.endswith("The full answer is on screen.") and len(out) < 160


def test_numbers_are_spoken_without_changing_them():
    said = speak_numbers("Revenue was $8.23M in Sep 2026, -$292.1K (-3.4%) vs Aug 2026, margin +1.2pp YoY; Q3 FY26 cost $1,250.")
    assert said == ("Revenue was 8.23 million dollars in September 2026, minus 292.1 thousand dollars (minus 3.4 percent) versus "
                    "August 2026, margin plus 1.2 percentage points year over year; the third quarter of 2026 cost 1250 dollars.")
    assert speak_numbers("a change of -12 units, 3.5% churn, 40 bps") == "a change of minus 12 units, 3.5 percent churn, 40 basis points"


# -- the governed path -----------------------------------------------------------------------------------------------
def test_spoken_question_runs_as_the_person_and_reads_back_the_verified_answer(voiced):
    lyst, owner = voiced
    viewer = _member(lyst, owner, "vi@n.test", "viewer")
    s = lyst.voice.start(viewer, WS)
    out = lyst.voice.turn(viewer, WS, s["id"], audio=b"FAKE: What was revenue last month?", mime="audio/wav")
    assert out["intent"] == "question" and out["reply"]["text"].startswith("Checking that now. Lyst will")
    assert out["engines"][-1] == {"engine": "fake", "outcome": "ok", "ms": out["engines"][-1]["ms"]}
    task = lyst.stores.control.query("SELECT created_by, request FROM tasks WHERE id = ?", [out["task_id"]])[0]
    assert task["created_by"] == viewer.user_id  # the person's own identity, not a voice or system principal
    assert lyst.voice.answer(viewer, WS, s["id"], out["task_id"])["ready"] is False
    lyst.runtime.drain()
    got = lyst.voice.answer(viewer, WS, s["id"], out["task_id"])
    verified = lyst.runtime.get(viewer, out["task_id"])["result"]["answer"]
    assert got["ready"] and got["speak_text"] == speakable(verified) and "Revenue" in got["speak_text"]
    spoken = lyst.voice.speak(viewer, WS, s["id"], task_id=out["task_id"])
    assert spoken["engine"] == "fake" and base64.b64decode(spoken["audio"]) == b"FAKE:" + got["speak_text"].encode()
    again = lyst.voice.turn(viewer, WS, s["id"], text="say that again")
    assert again["reply"]["text"] == got["speak_text"]
    # the stored transcript has both sides and no audio
    turns = lyst.voice.session(viewer, WS, s["id"])["turns"]
    assert [t["role"] for t in turns] == ["person", "lyst", "person", "lyst"]
    assert not any(t["has_audio"] for t in turns)


def test_voice_questions_go_through_the_analyst_core_like_typed_ones(voiced):
    lyst, owner = voiced
    s = lyst.voice.start(owner, WS)
    q = "Why did operating margin decline last quarter?"
    out = lyst.voice.turn(owner, WS, s["id"], text=q, confidence=0.93)
    typed = lyst.analyst_core.handle(owner, AnalystRequest(workspace_id=WS, text=q, submit=False))
    assert out["analyst"]["status"] == "queued" and out["task_id"]
    req = lyst.runtime.get(owner, out["task_id"])["request"]
    assert req["channel"] == "voice"
    voiced_roles = [r["id"] for r in req["expertise"]["roles"]]
    assert voiced_roles == [r["id"] for r in typed.expertise["roles"]]  # voice and text select the same expertise
    # a shaky transcript starts nothing; the page shows what was heard
    shaky = lyst.voice.turn(owner, WS, s["id"], text="why did margin decline", confidence=0.3)
    assert shaky["analyst"]["status"] == "needs_confirmation" and "task_id" not in shaky
    assert shaky["confirm_transcript"]["text"] == "why did margin decline" and "Nothing has started" in shaky["reply"]["text"]
    assert lyst.stores.control.scalar("SELECT count(*) FROM tasks") == 1
    lyst.voice.core = lambda: None  # without the specializations lane, questions go to the runtime directly
    plain = lyst.voice.turn(owner, WS, s["id"], text="what was revenue last month")
    assert plain["reply"]["text"] == "Checking that now." and "analyst" not in plain
    lyst.runtime.drain()


def test_speech_never_approves_or_changes_anything(voiced):
    lyst, owner = voiced
    aid = lyst.runtime.request_approval(WS, owner.user_id, "export", "Export the board pack", {})
    s = lyst.voice.start(owner, WS)
    for said in ("approve", "yes", "approve the pending export", "delete the targets dataset", "go ahead"):
        out = lyst.voice.turn(owner, WS, s["id"], text=said)
        assert out["intent"] in ("assent", "action") and "task_id" not in out
        assert "screen" in out["reply"]["text"]
    assert [a["id"] for a in lyst.runtime.approvals(owner, WS)] == [aid]  # still pending
    assert lyst.stores.control.scalar("SELECT count(*) FROM tasks") == 0
    refused = lyst.stores.control.scalar("SELECT count(*) FROM audit_log WHERE action IN ('voice.action_refused', 'voice.assent_refused')")
    assert refused == 5


def test_permissions_and_session_ownership(voiced):
    lyst, owner = voiced
    admin = _member(lyst, owner, "ad@n.test", "admin")
    viewer = _member(lyst, owner, "vw@n.test", "viewer")
    stranger = lyst.guard.create_user("x@else.test", "X")
    with pytest.raises(AccessDenied):
        lyst.voice.start(stranger, WS)
    with pytest.raises(AccessDenied):
        lyst.voice.set_settings(viewer, WS, {"retain_audio": True})
    s = lyst.voice.start(viewer, WS)
    with pytest.raises(AccessDenied):  # an administrator cannot speak into, or read, someone else's session
        lyst.voice.turn(admin, WS, s["id"], text="what was revenue")
    with pytest.raises(AccessDenied):
        lyst.voice.session(admin, WS, s["id"])
    lyst.voice.set_settings(admin, WS, {"enabled": False})
    with pytest.raises(AccessDenied):
        lyst.voice.start(viewer, WS)


def test_audio_is_ephemeral_unless_retention_is_on_and_transcripts_are_redacted(voiced):
    lyst, owner = voiced
    s = lyst.voice.start(owner, WS)
    lyst.voice.turn(owner, WS, s["id"], audio=b"FAKE: what did pat@example.com spend last month", mime="audio/wav")
    db = lyst.stores.workspace(WS)
    assert db.scalar("SELECT count(*) FROM voice_turns WHERE audio IS NOT NULL") == 0
    texts = [r["text"] for r in db.query("SELECT text FROM voice_turns")]
    assert not any("pat@example.com" in t for t in texts)
    lyst.voice.set_settings(owner, WS, {"retain_audio": True})
    lyst.voice.turn(owner, WS, s["id"], audio=b"FAKE: what was revenue", mime="audio/wav")
    assert db.scalar("SELECT count(*) FROM voice_turns WHERE audio IS NOT NULL") == 1
    lyst.voice.set_settings(owner, WS, {"retain_audio": False})
    assert lyst.voice.prune(WS)["audio"] == 1
    assert db.scalar("SELECT count(*) FROM voice_turns WHERE audio IS NOT NULL") == 0
    lyst.voice.set_settings(owner, WS, {"transcript_days": 0})
    db.execute("UPDATE voice_turns SET created_at = now() - INTERVAL 1 DAY")
    assert lyst.voice.prune(WS)["turns"] == 4
    lyst.runtime.drain()


def test_cancel_and_status_by_voice(voiced):
    lyst, owner = voiced
    s = lyst.voice.start(owner, WS)
    q = lyst.voice.turn(owner, WS, s["id"], text="why did operating margin decline last quarter")
    assert lyst.voice.turn(owner, WS, s["id"], text="status")["reply"]["text"].startswith("Still working")
    # stopping speech is not stopping work
    hush = lyst.voice.turn(owner, WS, s["id"], text="stop")
    assert hush["stop_playback"] and "still running" in hush["reply"]["text"]
    assert lyst.runtime.get(owner, q["task_id"])["status"] == "queued"
    gone = lyst.voice.turn(owner, WS, s["id"], text="cancel the analysis")
    assert gone["cancelled_task_id"] == q["task_id"] and lyst.runtime.get(owner, q["task_id"])["status"] == "cancelled"
    assert lyst.voice.turn(owner, WS, s["id"], text="cancel the task")["reply"]["text"] == "Nothing is running."
    quiet = lyst.voice.turn(owner, WS, s["id"], text="stop")
    assert quiet["reply"] == {"text": "", "speak": False}
    lyst.voice.end(owner, WS, s["id"])
    with pytest.raises(ValueError):
        lyst.voice.turn(owner, WS, s["id"], text="hello")


# -- engines, fallback and capacity --------------------------------------------------------------------------------------
def test_fallback_circuit_breaker_and_external_gate():
    t = [0.0]
    bad, good, ext = FakeEngine(fail=True), OtherFake(), ExternalFake()
    gw = AudioGateway({"fake": bad, "fake2": good, "ext": ext}, failure_threshold=2, cool_off=30, clock=lambda: t[0])
    # an external engine first in the order is passed over unless external audio is allowed
    tr, attempts = gw.transcribe(b"FAKE: hi", mime="audio/wav", workspace=WS, order=["ext", "fake2"], allow_external=False)
    assert tr.engine == "fake2" and attempts[0]["reason"] == "external audio is not allowed here"
    assert gw.transcribe(b"FAKE: hi", mime="audio/wav", workspace=WS, order=["ext"], allow_external=True)[0].engine == "ext"
    bad.fail = False
    bad.status = lambda: FakeEngine.status(FakeEngine())  # reports ready while failing on every call
    bad.transcribe = lambda *a, **k: (_ for _ in ()).throw(EngineError("GPU host gone"))
    for _ in range(2):
        tr, attempts = gw.transcribe(b"FAKE: hi", mime="audio/wav", workspace=WS, order=["fake", "fake2"], allow_external=False)
        assert tr.engine == "fake2" and attempts[0]["outcome"] == "failed"
    _, attempts = gw.transcribe(b"FAKE: hi", mime="audio/wav", workspace=WS, order=["fake", "fake2"], allow_external=False)
    assert attempts[0]["outcome"] == "skipped" and "paused" in attempts[0]["reason"]
    assert next(e for e in gw.status() if e["name"] == "fake")["state"] == "unavailable"
    t[0] = 31.0  # after the cool-off the engine is tried again
    _, attempts = gw.transcribe(b"FAKE: hi", mime="audio/wav", workspace=WS, order=["fake", "fake2"], allow_external=False)
    assert attempts[0]["outcome"] == "failed"
    with pytest.raises(NoEngine) as e:
        gw.transcribe(b"FAKE: hi", mime="audio/wav", workspace=WS, order=["nope"], allow_external=False)
    assert e.value.attempts == [{"engine": "nope", "outcome": "skipped", "reason": "not installed"}]
    with pytest.raises(EngineError):
        gw.transcribe(b"x" * (8 * 1024 * 1024 + 1), mime="audio/wav", workspace=WS, order=["fake2"], allow_external=False)


def test_duplex_capacity_falls_back_instead_of_queueing():
    pp = PersonaPlexEngine("ws://127.0.0.1:1")
    gw = AudioGateway({"personaplex": pp, "fake": FakeEngine()})
    with gw.duplex(["personaplex", "fake"]) as first:
        assert first.name == "personaplex"  # one conversation per PersonaPlex server
        with gw.duplex(["personaplex", "fake"]) as second:
            assert second.name == "fake"
        with pytest.raises(NoEngine), gw.duplex(["personaplex"]):
            pass
    with gw.duplex(["personaplex"]) as again:
        assert again.name == "personaplex"


def test_external_speech_never_used_without_permission(voiced):
    lyst, owner = voiced
    lyst.voice.gateway.register(ExternalFake())
    lyst.voice.set_settings(owner, WS, {"tts_order": ["ext", "browser"]})
    s = lyst.voice.start(owner, WS)
    out = lyst.voice.speak(owner, WS, s["id"], text="Hello")
    assert out["engine"] == "browser" and out["text"] == "Hello"
    assert out["engines"][0] == {"engine": "ext", "outcome": "skipped", "reason": "external audio is not allowed here"}
    assert lyst.voice.engines(owner, WS)["external_audio_allowed"] is False


def test_personaplex_protocol_and_status():
    url = personaplex_url("https://gpu.internal:8998", voice="NATM1", seed=7)
    assert url.startswith("wss://gpu.internal:8998/api/chat?voice_prompt=NATM1.pt&text_prompt=")
    assert url.endswith("&seed=7")
    with pytest.raises(ValueError):
        personaplex_url("ws://h:8998", voice="nobody")
    assert decode_frame(encode_frame(KIND_AUDIO, b"\x01\x02")) == (KIND_AUDIO, b"\x01\x02")
    assert encode_frame(KIND_HANDSHAKE) == b"\x00"
    with pytest.raises(ValueError):
        encode_frame(9)
    assert "Never state numbers" in GROUNDED_PERSONA
    st = PersonaPlexEngine(None).status()
    assert st.state == "unconfigured" and st.support == "implemented_unverified" and st.capacity == 1
    assert st.detail["latency"] == "not measured on this installation"
    assert PersonaPlexEngine("ws://127.0.0.1:1", connect_timeout=0.2).status().state == "unavailable"


def _stub_llm(lyst):
    class LLM:
        settings = type("S", (), {"provider": "openrouter", "base_url": "https://openrouter.test/api/v1"})()
        available = True
        stores = lyst.stores

        def _key_for(self, ws):
            return "test-key"

    return LLM()


def test_openrouter_transcription_endpoint_and_call_record(voiced):
    lyst, _ = voiced
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/models"):
            seen["models"] = dict(request.url.params)
            return httpx.Response(200, json={"data": [{"id": "openai/whisper-1", "name": "Whisper", "supported_voices": None}]})
        seen["path"], seen["body"], seen["auth"] = request.url.path, json.loads(request.read()), request.headers["authorization"]
        return httpx.Response(200, json={"text": " what was revenue ", "usage": {"seconds": 2.1, "input_tokens": 10, "cost": 0.0001}})

    eng = OpenRouterTranscriber(_stub_llm(lyst), model="openai/whisper-1", http=httpx.Client(transport=httpx.MockTransport(handler)))
    assert eng.status().state == "ready" and eng.status().local is False
    with pytest.raises(EngineError):
        eng.request(b"x", "video/mp4")
    assert eng.transcribe(b"RIFF", mime="audio/wav", workspace=WS).text == "what was revenue"
    assert seen["path"] == "/api/v1/audio/transcriptions" and seen["auth"] == "Bearer test-key"
    assert seen["body"]["input_audio"] == {"data": base64.b64encode(b"RIFF").decode(), "format": "wav"}
    row = lyst.stores.control.query("SELECT purpose, external, input_tokens, cost_usd FROM llm_calls WHERE purpose = 'voice.stt'")
    assert row == [{"purpose": "voice.stt", "external": True, "input_tokens": 10, "cost_usd": 0.0001}]
    assert eng.discover("transcription") == [{"id": "openai/whisper-1", "name": "Whisper", "voices": []}]
    assert seen["models"] == {"output_modalities": "transcription"}
    assert OpenRouterTranscriber(_stub_llm(lyst), model=None).status().state == "unconfigured"


def test_openrouter_speech_endpoint(voiced):
    lyst, _ = voiced
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"], seen["body"] = request.url.path, json.loads(request.read())
        return httpx.Response(200, content=b"ID3mp3", headers={"content-type": "audio/mpeg"})

    http = httpx.Client(transport=httpx.MockTransport(handler))
    assert OpenRouterSpeaker(_stub_llm(lyst), model="m/tts", voice=None, http=http).status().state == "unconfigured"
    eng = OpenRouterSpeaker(_stub_llm(lyst), model="m/tts", voice="alloy", http=http)
    sp = eng.speak("Revenue rose.", voice=None, workspace=WS)
    assert sp.audio == b"ID3mp3" and sp.mime == "audio/mpeg"
    assert seen["path"] == "/api/v1/audio/speech"
    assert seen["body"] == {"model": "m/tts", "input": "Revenue rose.", "voice": "alloy", "response_format": "mp3"}
    bad = OpenRouterSpeaker(_stub_llm(lyst), model="m/tts", voice="alloy",
                            http=httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, json={"error": "x"}))))
    with pytest.raises(EngineError):
        bad.speak("hi", voice=None, workspace=WS)


def test_catch_up_and_superseded_read_back(voiced):
    lyst, owner = voiced
    first = lyst.voice.start(owner, WS)
    lyst.voice.end(owner, WS, first["id"])
    s = lyst.voice.start(owner, WS)
    assert lyst.voice.turn(owner, WS, s["id"], text="what did I miss")["reply"]["text"] == "Nothing new has finished since you were last here."
    a = lyst.voice.turn(owner, WS, s["id"], text="what was revenue last month")
    b = lyst.voice.turn(owner, WS, s["id"], text="actually, compare that to the same month last year")
    assert b["seq"] > a["seq"]
    lyst.runtime.drain()
    assert lyst.voice.answer(owner, WS, s["id"], a["task_id"])["superseded"] is True
    assert lyst.voice.answer(owner, WS, s["id"], b["task_id"])["superseded"] is False
    lyst.runtime.request_approval(WS, owner.user_id, "export", "Export the board pack", {})
    said = lyst.voice.turn(owner, WS, s["id"], text="catch me up")["reply"]["text"]
    assert "1 approval is waiting for you on screen." in said and "dollars" in said


# -- over HTTP ----------------------------------------------------------------------------------------------------------
def test_voice_over_http_and_duplex_stream(voiced):
    lyst, owner = voiced
    h = {"Authorization": f"Bearer {lyst.guard.issue_token(owner)}"}
    with TestClient(create_app(lyst, start_workers=False)) as c:
        engines = c.get(f"/v1/workspaces/{WS}/voice/engines", headers=h).json()
        names = {e["name"]: e for e in engines["engines"]}
        assert names["personaplex"]["state"] == "unconfigured" and names["browser"]["state"] == "ready"
        assert c.patch(f"/v1/workspaces/{WS}/voice/settings", headers=h, json={"stt_order": ["nope"]}).status_code == 400
        assert c.put(f"/v1/workspaces/{WS}/voice/preferences", headers=h, json={"mode": "hands_free"}).json()["mode"] == "hands_free"
        s = c.post(f"/v1/workspaces/{WS}/voice/sessions", headers=h, json={}).json()
        assert s["mode"] == "hands_free"
        t = c.post(f"/v1/workspaces/{WS}/voice/sessions/{s['id']}/turns", headers=h,
                   json={"audio_b64": base64.b64encode(b"FAKE: what was revenue last month").decode()}).json()
        assert t["intent"] == "question" and t["task_id"]
        assert c.post(f"/v1/workspaces/{WS}/voice/sessions/{s['id']}/turns", headers=h, json={"audio_b64": "%%%"}).status_code == 400
        low = c.post(f"/v1/workspaces/{WS}/voice/sessions/{s['id']}/turns", headers=h,
                     json={"text": "what was revenue", "confidence": 0.2}).json()
        assert low["analyst"]["status"] == "needs_confirmation"
        assert c.post(f"/v1/workspaces/{WS}/voice/sessions/{s['id']}/turns", headers=h,
                      json={"text": "x", "confidence": 2}).status_code == 400
        lyst.runtime.drain()
        a = c.get(f"/v1/workspaces/{WS}/voice/sessions/{s['id']}/answer/{t['task_id']}", headers=h).json()
        assert a["ready"] and "Revenue" in a["speak_text"]
        ticket = c.post(f"/v1/workspaces/{WS}/voice/sessions/{s['id']}/stream-ticket", headers=h).json()["ticket"]
        url = f"/v1/workspaces/{WS}/voice/sessions/{s['id']}/duplex"
        with pytest.raises(WebSocketDisconnect):  # a ticket in the URL is not accepted
            with c.websocket_connect(url + f"?ticket={ticket}") as w:
                w.receive_bytes()
        ticket = c.post(f"/v1/workspaces/{WS}/voice/sessions/{s['id']}/stream-ticket", headers=h).json()["ticket"]
        with c.websocket_connect(url, subprotocols=["lyst.ticket", ticket]) as w:
            assert w.receive_bytes() == b"\x00"
            w.send_bytes(bytes([KIND_AUDIO]) + b"opus")
            assert w.receive_bytes() == bytes([KIND_AUDIO]) + b"opus"
            assert w.receive_bytes()[0] == KIND_TEXT
        with pytest.raises(WebSocketDisconnect) as closed:  # a ticket is spent once
            with c.websocket_connect(url, subprotocols=["lyst.ticket", ticket]) as w:
                w.receive_bytes()
        assert closed.value.code == 1008
        assert c.post(f"/v1/workspaces/{WS}/voice/sessions/{s['id']}/end", headers=h).json()["status"] == "ended"
