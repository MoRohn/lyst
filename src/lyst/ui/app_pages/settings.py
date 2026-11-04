# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Rohn Springfield
import pandas as pd
import streamlit as st

from lyst.ui.client import APIError, client, workspace

api, ws = client(), workspace()
st.markdown("## Settings")
s = api.get(f"/v1/workspaces/{ws}/settings")
admin = s["role"] in ("admin", "owner")
c = st.columns(4)
c[0].metric("Your role", s["role"].capitalize())
c[1].metric("Autonomy", s["workspace"]["autonomy"].capitalize())
c[2].metric("Encryption at rest", "On" if s["encrypted"] else "Off")
c[3].metric("Model calls", s["llm_usage"]["calls"])

st.markdown("#### Inference")
m = s["model"]
if m:
    where = "OpenRouter" if m["provider"] == "openrouter" else f"local endpoint {m['base_url']}"
    tier = f", {m['tier']} tier" if m.get("tier") else ", pinned model"
    st.write(f"{where}{tier}. Calls walk: " + " → ".join(f"`{x}`" for x in m["attempts"]))
    st.caption("Every call goes through Lyst's gateway: pooled admission, adaptive limits, retries with fallback to "
               "the next model, and per-workload routing, as in Acumane. Models see definitions and computed findings, "
               "never dataset rows, and text is redacted before it leaves.")
else:
    st.write("No model configured. Lyst plans with rules and writes answers from templates; every number comes from "
             "SQL either way. Set LYST_OPENROUTER_API_KEY for OpenRouter, or LYST_LLM_BASE_URL and LYST_LLM_MODEL for a "
             "local OpenAI-compatible model.")
if admin:
    allow = st.toggle("Allow this workspace to use external model providers", value=bool(s["workspace"]["allow_external_llm"]))
    mode = st.segmented_control("Autonomy", ["assist", "delegate", "autonomous", "governed"],
                                default=s["workspace"]["autonomy"])
    if allow != bool(s["workspace"]["allow_external_llm"]) or (mode and mode != s["workspace"]["autonomy"]):
        try:
            api.patch(f"/v1/workspaces/{ws}/settings", {"allow_external_llm": allow, "autonomy": mode})
            st.rerun()
        except APIError as e:
            st.error(str(e))
    usage = api.get(f"/v1/workspaces/{ws}/model-usage")
    if usage:
        st.markdown("**Model usage**")
        st.dataframe(pd.DataFrame(usage), hide_index=True, width="stretch",
                     column_config={"cost_usd": st.column_config.NumberColumn("Cost", format="$%.4f")})
    if s.get("gateway") and s["gateway"].get("configured"):
        g = s["gateway"]
        st.caption(f"Gateway now: {g['inflight']} calls in flight, waiting {g['waiting']}, refusals {g['refusals'] or 'none'}")

st.markdown("#### Audit log")
try:
    st.dataframe(pd.DataFrame(api.get(f"/v1/workspaces/{ws}/audit")), hide_index=True, width="stretch")
except APIError as e:
    st.caption(str(e))
