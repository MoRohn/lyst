# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Rohn Springfield
"""Voice: speak a question next to the ask box, hear the verified answer read back, and set how voice works.
The browser recognises and speaks (audio stays in the browser unless the workspace turns on server recognition);
the engine takes text turns through the same governed analyst. Voice never approves or confirms anything."""

from __future__ import annotations

from pathlib import Path

import streamlit as st
import streamlit.components.v1 as components

from lyst.ui import kit
from lyst.ui.client import APIError, client, workspace
from lyst.ui.components import md

_mic = components.declare_component("lyst_voice", path=str(Path(__file__).parent / "voice_component"))
SUPPORT = {"implemented_verified": "Verified", "implemented_unverified": "Not yet verified on real hardware",
           "partial": "Partly built", "planned": "Planned", "unsupported": "Not supported here"}
STYLE = {"professional": "Professional", "conversational": "Conversational", "executive": "Executive brief",
         "technical": "Technical", "briefing": "Briefing"}


def _ctx():
    return client(), workspace()


def _session() -> str | None:
    api, ws = _ctx()
    key = f"voice_sid_{ws}"
    if st.session_state.get(key) == "denied":
        return None
    if not st.session_state.get(key):
        try:
            st.session_state[key] = api.post(f"/v1/workspaces/{ws}/voice/sessions", {})["id"]
        except APIError as e:
            st.session_state[key] = "denied" if e.status in (403, 404) else None
            return None
    return st.session_state[key]


def _turn(sid: str, body: dict) -> dict | None:
    """Send one utterance. The engine closes a session after 30 idle minutes; then start a new one and send it again."""
    api, ws = _ctx()
    try:
        return api.post(f"/v1/workspaces/{ws}/voice/sessions/{sid}/turns", body)
    except APIError as e:
        if e.status == 400 and "ended" in str(e).lower():
            st.session_state.pop(f"voice_sid_{ws}", None)
            fresh = _session()
            if fresh:
                return kit.guarded(lambda: api.post(f"/v1/workspaces/{ws}/voice/sessions/{fresh}/turns", body))
        st.error(kit.reason(e), icon=":material/error:")
        return None


def mic(on_question) -> None:
    """The Speak control beside the ask box. ``on_question(task_id)`` opens the answer Lyst started."""
    sid = _session()
    if not sid:
        return
    confirm_heard(on_question)
    api, ws = _ctx()
    speak = st.session_state.pop("voice_say", None)
    try:
        prefs = api.get(f"/v1/workspaces/{ws}/voice/preferences")
    except APIError:
        prefs = {}
    val = _mic(mode=prefs.get("mode") or "push_to_talk", speak=(speak or {}).get("text"), speak_id=(speak or {}).get("id"),
               state=st.session_state.pop("voice_state", None), stop=bool(st.session_state.pop("voice_stop", False)),
               key="lyst_voice", default=None)
    if not val or val.get("n") == st.session_state.get("voice_n"):
        return
    st.session_state["voice_n"] = val.get("n")
    if val.get("unavailable"):
        return
    body = {"text": val["text"]} | ({"confidence": val["confidence"]} if val.get("confidence") else {})
    r = _turn(sid, body)
    if r is None:
        return
    sid = st.session_state.get(f"voice_sid_{ws}") or sid
    if r.get("understood"):
        st.caption(md(f"You said: “{r['understood']}”"))
    reply = r.get("reply") or {}
    heard = r.get("confirm_transcript") or {}
    if (r.get("analyst") or {}).get("status") == "needs_confirmation" and heard.get("text"):
        st.session_state["voice_confirm"] = {"sid": sid, "text": heard["text"]}
    if r.get("stop_playback"):
        st.session_state["voice_stop"] = True
    intent = r.get("intent")
    if intent == "question" and r.get("task_id"):
        st.session_state["voice_wait"] = {"sid": sid, "task": r["task_id"]}
        on_question(r["task_id"])
        st.rerun()
    elif intent in ("action", "assent"):
        conf = r.get("confirm_on_screen") or {}
        st.info(md("Voice can't do this; confirm it on screen." + (f" ({conf.get('verb')}: {conf.get('request')})"
                                                                   if conf.get("request") else "")),
                icon=":material/touch_app:")
    elif intent == "unavailable":
        st.warning("Voice isn't available right now; type your question instead.", icon=":material/mic_off:")
    if reply.get("text"):
        st.caption(md(reply["text"]))
        if reply.get("speak"):
            st.session_state["voice_say"] = {"text": reply["text"], "id": f"{sid}:{val.get('n')}"}
            st.rerun()


def confirm_heard(on_question) -> None:
    """When Lyst was not sure it heard right: show what it heard and ask before starting anything."""
    c = st.session_state.get("voice_confirm")
    if not c:
        return
    row = st.columns([5, 1.3, 1], vertical_alignment="center")
    row[0].info(md(f"I heard: “{c['text']}”. Ask this?"), icon=":material/hearing:")
    if row[1].button("Ask this", type="primary", key="vc_yes", width="stretch"):
        st.session_state.pop("voice_confirm", None)
        r = _turn(c["sid"], {"text": c["text"], "confidence": 1})
        if r and r.get("task_id"):
            st.session_state["voice_wait"] = {"sid": st.session_state.get(f"voice_sid_{workspace()}") or c["sid"],
                                              "task": r["task_id"]}
            on_question(r["task_id"])
            st.rerun()
    if row[2].button("No", type="tertiary", key="vc_no", width="stretch"):
        st.session_state.pop("voice_confirm", None)
        st.rerun()


def read_back(task_id: str) -> None:
    """On the answer page: once a spoken question's answer is verified and ready, Lyst says it."""
    w = st.session_state.get("voice_wait")
    if not w or w.get("task") != task_id:
        return
    api, ws = _ctx()
    try:
        a = api.get(f"/v1/workspaces/{ws}/voice/sessions/{w['sid']}/answer/{task_id}")
    except APIError:
        return
    if not a.get("ready"):
        return
    st.session_state.pop("voice_wait", None)
    if a.get("superseded"):  # a later question replaced this one: show it, do not speak it
        return
    if a.get("speak_text"):
        st.session_state["voice_say"] = {"text": a["speak_text"], "id": f"{w['sid']}:{task_id}"}


def settings_section() -> None:
    """Settings › Voice & Audio: personal preferences, the workspace's rules (admins), and the engines."""
    api, ws = _ctx()
    admin = st.session_state.get("role") in ("admin", "owner")
    try:
        prefs = api.get(f"/v1/workspaces/{ws}/voice/preferences")
        cfg = api.get(f"/v1/workspaces/{ws}/voice/settings")
        eng = api.get(f"/v1/workspaces/{ws}/voice/engines")
    except APIError as e:
        st.caption(kit.reason(e))
        return
    kit.label("You")
    c = st.columns(3)
    mode = c[0].selectbox("Talk by", ["push_to_talk", "hands_free"], index=["push_to_talk", "hands_free"].index(prefs.get("mode") or "push_to_talk"),
                          format_func={"push_to_talk": "Pressing Speak", "hands_free": "Hands-free"}.get, key="vp_mode")
    say = c[1].selectbox("Read answers aloud", [None, True, False], index=[None, True, False].index(prefs.get("speak_results")),
                         format_func={None: "Workspace default", True: "Yes", False: "No"}.get, key="vp_say")
    style = c[2].selectbox("Speaking style", list(STYLE), index=list(STYLE).index(prefs.get("style") or "professional"),
                           format_func=STYLE.get, key="vp_style")
    new = {"mode": mode, "speak_results": say, "style": style}
    if any(prefs.get(k) != v for k, v in new.items()) and st.button("Save", type="primary", key="vp_save"):
        kit.guarded(lambda: api.put(f"/v1/workspaces/{ws}/voice/preferences", new), "Saved")
    st.caption("Speech is recognised and spoken in your browser; audio does not leave it unless this workspace turns on "
               "server recognition. Voice can ask and read back, but it never approves or confirms anything.")
    kit.label("This workspace")
    if admin:
        a = st.columns(2)
        enabled = a[0].toggle("Voice is on", value=bool(cfg.get("enabled", True)), key="vs_on")
        speak_def = a[1].toggle("Read answers aloud by default", value=bool(cfg.get("speak_results", True)), key="vs_say")
        ext = a[0].toggle("Allow an external speech service", value=bool(cfg.get("allow_external_audio")), key="vs_ext",
                          help="Off by default. When on, audio may go to OpenRouter speech, and only for data allowed to "
                               "reach an external model.")
        keep = a[1].toggle("Keep audio recordings", value=bool(cfg.get("retain_audio")), key="vs_keep",
                           help="Off by default: audio is transcribed and dropped.")
        days = st.slider("Keep transcripts for (days)", 0, 400, int(cfg.get("transcript_days") or 30), key="vs_days")
        newc = {"enabled": enabled, "speak_results": speak_def, "allow_external_audio": ext, "retain_audio": keep,
                "transcript_days": days}
        if any(cfg.get(k) != v for k, v in newc.items()) and st.button("Save workspace settings", type="primary", key="vs_save"):
            kit.guarded(lambda: api.patch(f"/v1/workspaces/{ws}/voice/settings", newc), "Saved")
    else:
        st.caption(md(("Voice is on" if cfg.get("enabled", True) else "Voice is off")
                      + ("; an external speech service is allowed" if cfg.get("allow_external_audio") else "")
                      + ("; audio is kept" if cfg.get("retain_audio") else "; audio is not kept") + "."))
    kit.label("Engines")
    for i, e in enumerate(eng.get("engines") or []):
        state = {"ready": ("Ready", "ok"), "unavailable": ("Unavailable", "off"), "unconfigured": ("Not set up", "off")}.get(
            e.get("state"), (str(e.get("state")), "info"))
        h = e.get("health") or {}
        meta = " · ".join(x for x in (SUPPORT.get(e.get("support"), e.get("support")), "on this machine" if e.get("local") else "external",
                                      e.get("reason"), f"typically {h['p50_ms']} ms" if h.get("p50_ms") is not None else None) if x)
        kit.row(e.get("title") or e["name"], meta, status=state, key=f"ve{i}", divider=i < len(eng["engines"]) - 1)
