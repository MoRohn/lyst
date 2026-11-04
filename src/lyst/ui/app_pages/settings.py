# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Rohn Springfield
"""Settings: inference, Secrets & Connections (users manage connections, Lyst manages secrets), members, audit."""

import pandas as pd
import streamlit as st

from lyst.ui.client import APIError, client, workspace

api, ws = client(), workspace()
st.markdown("## Settings")
s = api.get(f"/v1/workspaces/{ws}/settings")
admin = s["role"] in ("admin", "owner")
owner = s["role"] == "owner"
cred = s.get("credentials") or {}
c = st.columns(4)
c[0].metric("Your role", s["role"].capitalize())
c[1].metric("Autonomy", s["workspace"]["autonomy"].capitalize())
c[2].metric("Encryption at rest", "On" if s["encrypted"] else "Off")
c[3].metric("Model calls", s["llm_usage"]["calls"])


def _reauth(e: APIError) -> None:
    if e.status == 401:
        st.warning("Sign in again to manage credentials (recent authentication is required for this action).", icon=":material/lock:")
        st.link_button("Sign in again", f"{api.base}/auth/login", icon=":material/login:")
    else:
        st.error(str(e))


@st.cache_data(ttl=300)
def _providers():
    return {p["id"]: p for p in api.get("/v1/credentials/providers")}


def credential_panel(scope_kind: str, scope_id: str | None, *, title: str, intro: str, key: str) -> None:
    """One scope's credentials: connect (masked, tested before it is saved), test, replace, revoke, history."""
    providers = _providers()
    st.markdown(f"**{title}**")
    st.caption(intro)
    try:
        data = api.get("/v1/credentials", scope_kind=scope_kind, **({"scope_id": scope_id} if scope_id else {}))
    except APIError as e:
        _reauth(e)
        return
    rows = [r for r in data["credentials"] if not r["provider"].startswith("connector:")] if scope_kind != "workspace" else data["credentials"]
    if rows:
        for r in rows:
            label = providers.get(r["provider"], {}).get("label") or r["provider"].replace("connector:", "connection: ")
            status = {"active": "🟢", "revoked": "⚪", "replaced": "⚪"}.get(r["status"], "🟡")
            last = r.get("last_test") or {}
            with st.expander(f"{status} {label} · {r['name']} · v{r['version']} · {r['status']}"
                             + (f" · last test: {last.get('outcome')}" if last else ""), expanded=False):
                meta = {k: v for k, v in r["fields"].items()}
                meta.update({"owner": r.get("owner"), "backend": r["backend"], "created": str(r["created_at"])[:19],
                             "replaced": str(r["replaced_at"])[:19] if r.get("replaced_at") else None})
                st.caption(" · ".join(f"{k}: {v}" for k, v in meta.items() if v))
                if r["status"] == "active":
                    b = st.columns([1, 1, 1, 1, 4])
                    if b[0].button("Test", key=f"{key}-test-{r['id']}", icon=":material/network_check:"):
                        try:
                            out = api.post(f"/v1/credentials/{r['id']}/test")
                            (st.success if out["ok"] else st.error)(f"{out['outcome']}" + (f" · {out.get('label')}" if out.get("label") else ""))
                        except APIError as e:
                            _reauth(e)
                    with b[1].popover("Replace", icon=":material/autorenew:"):
                        with st.form(f"{key}-replace-{r['id']}", clear_on_submit=True, border=False):
                            new = st.text_input("New value", type="password", autocomplete="off",
                                                help="Validated before it replaces the current one; the old value is deleted.")
                            if st.form_submit_button("Replace") and new:
                                try:
                                    api.post(f"/v1/credentials/{r['id']}/replace", {"secret": new})
                                    st.rerun()
                                except APIError as e:
                                    _reauth(e)
                    with b[2].popover("Revoke", icon=":material/block:"):
                        st.caption(providers.get(r["provider"], {}).get("revocation_note") or "Revoking here stops Lyst using it.")
                        if st.button("Revoke now", key=f"{key}-revoke-{r['id']}", type="primary"):
                            try:
                                out = api.delete(f"/v1/credentials/{r['id']}")
                                st.info(out["provider_side"])
                                st.rerun()
                            except APIError as e:
                                _reauth(e)
                    if b[3].button("History", key=f"{key}-hist-{r['id']}", icon=":material/history:"):
                        try:
                            hist = api.get(f"/v1/credentials/{r['id']}/history")
                            st.dataframe(pd.DataFrame(hist)[["occurred_at", "actor", "action", "outcome", "detail"]], hide_index=True,
                                         width="stretch")
                        except APIError as e:
                            _reauth(e)
    else:
        st.caption("Nothing connected at this level yet.")

    choices = [p for p in providers.values() if scope_kind in p["scopes"]]
    with st.expander("Connect a new credential", icon=":material/add_link:"):
        pid = st.selectbox("Provider", [p["id"] for p in choices], format_func=lambda i: providers[i]["label"], key=f"{key}-prov")
        p = providers[pid]
        if p.get("notes"):
            st.caption(p["notes"])
        with st.form(f"{key}-connect-{pid}", clear_on_submit=True):
            name = st.text_input("Name", value=p["label"] if scope_kind == "installation" else "")
            fields = {}
            cols = st.columns(2) if p["fields"] else []
            for i, f in enumerate(p["fields"]):
                fields[f["name"]] = cols[i % 2].text_input(f["label"] + ("" if f["required"] else " (optional)"), autocomplete="off")
            secret = st.text_input(p["secret_label"], type="password", autocomplete="off",
                                   help="Entered once, tested, then sealed. Lyst never shows it again.")
            if st.form_submit_button("Connect and test", type="primary"):
                try:
                    out = api.post("/v1/credentials", {"provider": pid, "name": name or p["label"], "secret": secret, "fields": fields,
                                                       "scope_kind": scope_kind, "scope_id": scope_id})
                    st.success(f"Connected {out['name']}: {out['test']['outcome']}.")
                    st.rerun()
                except APIError as e:
                    _reauth(e)


# ------------------------------------------------------------------------------- inference

st.markdown("#### Inference")
m = s["model"]
if m:
    where = "OpenRouter" if m["provider"] == "openrouter" else f"local endpoint {m['base_url']}"
    tier = f", {m['tier']} tier" if m.get("tier") else ", pinned model"
    via = cred.get("model_credential")
    st.write(f"{where}{tier}. Calls walk: " + " → ".join(f"`{x}`" for x in m["attempts"])
             + (f". Credential: *{via['name']}* ({via['scope']} scope)." if via else "."))
    st.caption("Every call goes through Lyst's gateway: pooled admission, adaptive limits, retries with fallback to "
               "the next model, and per-workload routing, as in Acumane. Models see definitions and computed findings, "
               "never dataset rows, and text is redacted before it leaves.")
    if not s.get("model_ready") and admin:
        st.info("This workspace is not allowed to use an external provider yet. Turn it on below.", icon=":material/toggle_off:")
elif s.get("provider") == "openrouter":
    st.write("No model credential is connected yet. Lyst plans with rules and writes answers from templates; every number "
             "comes from SQL either way.")
    if cred.get("installation_admin"):
        st.info("Connect an OpenRouter key under **Secrets & Connections › Installation** below; it is tested before it is "
                "saved and never shown again.", icon=":material/key:")
    elif owner:
        st.info("An installation administrator connects the OpenRouter key in Settings. "
                + ("This workspace may also connect its own key below." if (cred.get("policy") or {}).get("workspace_override")
                   else "Workspace-level keys are not allowed by the installation policy."), icon=":material/key:")
else:
    st.write("No model configured. Set LYST_LLM_BASE_URL and LYST_LLM_MODEL for a local OpenAI-compatible model, or use OpenRouter.")
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

# ---------------------------------------------------------------------- secrets & connections

if owner:
    st.markdown("#### Secrets & Connections")
    st.caption("You manage connections; Lyst manages the secrets. Values are entered once, tested, sealed with AES-256-GCM under "
               "a master key kept outside the database, and never displayed or returned again. Every connect, test, replace, "
               "revoke and use is in a tamper-evident history.")
    tabs = st.tabs(["This workspace", "Installation", "Personal"]) if cred.get("installation_admin") else st.tabs(["This workspace", "Personal"])
    with tabs[0]:
        credential_panel("workspace", ws, key="ws", title=f"Credentials of {s['workspace']['name']}",
                         intro="Database passwords, API tokens and OAuth clients used by this workspace's connections. "
                               "Connections made on the Data page store their secrets here automatically.")
    if cred.get("installation_admin"):
        with tabs[1]:
            try:
                inst = api.get("/v1/installation")
            except APIError as e:
                inst = None
                _reauth(e)
            if inst:
                k = st.columns(4)
                k[0].metric("Secret backend", inst["backend"]["backend"], "healthy" if inst["backend"]["ok"] else "down")
                k[1].metric("Master key from", inst["master_key_source"] or "none")
                k[2].metric("Active credentials", inst["active_credentials"])
                k[3].metric("Audit chain", "intact" if inst["audit"]["ok"] else f"broken at {inst['audit'].get('broken_at')}")
                credential_panel("installation", None, key="inst", title="Installation credentials",
                                 intro="Shared by every workspace: the OpenRouter key (Acumane's rails) and the sign-in provider. "
                                       "An installation credential is authoritative; the policy below says where it may be overridden.")
                st.markdown("**Policy and model settings**")
                pol = inst["policy"].get("openrouter", {})
                p1 = st.columns(2)
                wo = p1[0].toggle("Workspaces may connect their own OpenRouter key", value=bool(pol.get("workspace_override")))
                uc = p1[1].toggle("People may connect a personal OpenRouter key", value=bool(pol.get("user_credentials")))
                st_ = inst["settings"]
                p2 = st.columns(3)
                tier = p2[0].selectbox("Model tier", ["economy", "balanced", "frontier"],
                                       index=["economy", "balanced", "frontier"].index(st_.get("LYST_LLM_TIER") or "balanced"))
                pinned = p2[1].text_input("Pinned model (optional)", value=st_.get("LYST_LLM_MODEL") or "")
                embed = p2[2].text_input("Embedding model (optional)", value=st_.get("LYST_EMBED_MODEL") or "",
                                         help="e.g. openai/text-embedding-3-small; turns on semantic search")
                if st.button("Save installation settings", icon=":material/save:"):
                    try:
                        api.patch("/v1/installation", {"policy": {"provider": "openrouter", "workspace_override": wo, "user_credentials": uc},
                                                       "settings": {"LYST_LLM_TIER": tier, "LYST_LLM_MODEL": pinned or None,
                                                                    "LYST_EMBED_MODEL": embed or None}})
                        st.rerun()
                    except APIError as e:
                        _reauth(e)
                with st.expander("Trust anchors and administrators"):
                    st.caption(f"Administrators: {', '.join(inst['admins'])}. Data directory: {inst['home']}.")
                    st.dataframe(pd.DataFrame(inst["anchors"]), hide_index=True, width="stretch")
                    if inst["events"]:
                        st.dataframe(pd.DataFrame(inst["events"])[["occurred_at", "actor", "action", "credential_id", "outcome", "detail"]],
                                     hide_index=True, width="stretch")
    with tabs[-1]:
        if (cred.get("policy") or {}).get("user_credentials"):
            credential_panel("user", None, key="me", title="Your personal credentials",
                             intro="Used only for your own work, never for anyone else's.")
        else:
            st.caption("Personal credentials are not enabled by the installation policy.")

# ---------------------------------------------------------------------------- notifications

if admin:
    st.markdown("#### Notifications")
    hook = st.text_input("Webhook URL for monitor findings (https, JSON POST; empty to disable)",
                         value=s["workspace"].get("webhook_url") or "")
    if hook != (s["workspace"].get("webhook_url") or "") and st.button("Save webhook"):
        try:
            api.patch(f"/v1/workspaces/{ws}/settings", {"webhook_url": hook})
            st.rerun()
        except APIError as e:
            st.error(str(e))
    st.markdown("#### Members")
    members = api.get(f"/v1/workspaces/{ws}/members")
    st.dataframe(pd.DataFrame(members)[["email", "name", "role"]], hide_index=True, width="stretch")
    with st.form("member"):
        c2 = st.columns([3, 1, 1], vertical_alignment="bottom")
        email = c2[0].text_input("Email (the person must have signed in once)")
        role = c2[1].selectbox("Role", ["viewer", "editor", "admin", "owner"])
        if c2[2].form_submit_button("Add") and email:
            try:
                api.post(f"/v1/workspaces/{ws}/members", {"email": email.strip().lower(), "role": role})
                st.rerun()
            except APIError as e:
                st.error(str(e))

st.markdown("#### Audit log")
try:
    st.dataframe(pd.DataFrame(api.get(f"/v1/workspaces/{ws}/audit")), hide_index=True, width="stretch")
except APIError as e:
    st.caption(str(e))
