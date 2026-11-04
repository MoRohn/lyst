import pandas as pd
import streamlit as st

from lyst.ui.client import APIError, client, workspace

api, ws = client(), workspace()
st.markdown("## Settings")
s = api.get(f"/v1/workspaces/{ws}/settings")
c = st.columns(4)
c[0].metric("Your role", s["role"].capitalize())
c[1].metric("Autonomy", s["workspace"]["autonomy"].capitalize())
c[2].metric("Encryption at rest", "On" if s["encrypted"] else "Off")
c[3].metric("Model calls", s["llm_usage"]["calls"])
st.markdown("**Inference**")
if s["model"]:
    st.write(f"{s['model']['name']} at {s['model']['base_url']} · "
             f"{'external provider' if s['model']['external'] else 'local'}")
else:
    st.write("No model configured. Lyst plans with rules and writes answers from templates; every number comes from "
             "SQL either way. Set LYST_LLM_BASE_URL and LYST_LLM_MODEL to an OpenAI-compatible endpoint (Ollama, "
             "llama.cpp, vLLM) to add one.")
st.write(f"External providers allowed for this workspace: {'yes' if s['workspace']['allow_external_llm'] else 'no'}")
st.markdown("**Audit log**")
try:
    st.dataframe(pd.DataFrame(api.get(f"/v1/workspaces/{ws}/audit")), hide_index=True, width="stretch")
except APIError as e:
    st.caption(str(e))
