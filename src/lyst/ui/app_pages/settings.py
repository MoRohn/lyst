# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Rohn Springfield
"""Settings: inference, Secrets & Connections (users manage connections, Lyst manages secrets), members, audit."""

import pandas as pd
import streamlit as st

from lyst.ui import advanced, anticipate, governance, integrations, kit, onboarding, voice
from lyst.ui.client import APIError, client, workspace
from lyst.ui.components import md

api, ws = client(), workspace()
s = api.get(f"/v1/workspaces/{ws}/settings")
admin = s["role"] in ("admin", "owner")
owner = s["role"] == "owner"
cred = s.get("credentials") or {}
kit.header("Settings", f"You are {s['role']} of {s['workspace']['name']} · "
           + ("encrypted at rest" if s["encrypted"] else "not encrypted at rest")
           + f" · {s['llm_usage']['calls']:,} model call{'s' * (s['llm_usage']['calls'] != 1)} so far")


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


def usd(v: float | None, *, per: int = 1) -> str:
    """Dollar amounts too small for cents keep their significant digits: $0.00028, $1.40, $1,204."""
    if v is None:
        return "n/a"
    v *= per
    if v >= 100:
        return f"${v:,.0f}"
    if v >= 1:
        return f"${v:,.2f}"
    return f"${v:.2g}" if v >= 0.0001 else f"${v:.1e}"


def price(v: float) -> str:
    """A per-1M-token price as Acumane prints it: $15, $2.50, $0.10, $0.0825."""
    if v >= 1:
        return f"${v:.2f}".removesuffix(".00")
    frac = f"{v:.4f}".split(".")[1].rstrip("0")
    return f"$0.{frac.ljust(2, '0')}"


def per_m(row: dict) -> str:
    return f"{price(row['input_per_m'])} in · {price(row['output_per_m'])} out per 1M tokens"



def models() -> None:
    """Which model Lyst may use, at what level and cost, and what it has spent."""
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
        quick_scope = ("installation", None) if cred.get("installation_admin") else \
            (("workspace", ws) if owner and (cred.get("policy") or {}).get("workspace_override") else None)
        if quick_scope:
            # the common case needs one field: paste the key, Lyst tests it with OpenRouter and seals it
            with st.form("connect-openrouter", clear_on_submit=True, border=True):
                st.markdown("**Connect OpenRouter**")
                q = st.columns([4, 1], vertical_alignment="bottom")
                key = q[0].text_input("API key", type="password", autocomplete="off", placeholder="sk-or-…",
                                      help="Checked with OpenRouter before it is saved, sealed, and never shown again.")
                if q[1].form_submit_button("Connect", type="primary", icon=":material/key:") and key:
                    try:
                        out = api.post("/v1/credentials", {"provider": "openrouter", "name": "OpenRouter", "secret": key,
                                                           "scope_kind": quick_scope[0], "scope_id": quick_scope[1]})
                        if admin and not s["workspace"]["allow_external_llm"]:  # the workspace that connected it may use it at once
                            api.patch(f"/v1/workspaces/{ws}/settings", {"allow_external_llm": True})
                        st.success(f"Connected. OpenRouter reports {out['test'].get('label') or 'the key'} is valid; the model is available now.")
                        st.rerun()
                    except APIError as e:
                        _reauth(e)
                st.caption("Shared by every workspace." if quick_scope[0] == "installation" else "Used by this workspace only.")
        elif owner:
            st.info("An installation administrator connects the OpenRouter key in Settings; workspace-level keys are not allowed by "
                    "the installation policy.", icon=":material/key:")
    else:
        st.write("No model configured. Set LYST_LLM_BASE_URL and LYST_LLM_MODEL for a local OpenAI-compatible model, or use OpenRouter.")
    opts = None
    if m and m.get("provider") == "openrouter":
        try:
            opts = api.get(f"/v1/workspaces/{ws}/model-options")
        except APIError as e:
            st.caption(str(e))
    if opts and opts.get("selection"):
        sel, b, cat = opts["selection"], opts["basis"], opts["catalog"]
        st.markdown("**Model level**")
        fetched = kit.when(cat.get("lastRefreshedAt"))
        src = (f"OpenRouter's live catalog, updated {fetched}" if cat.get("source") == "openrouter"
               else f"the snapshot shipped with Lyst ({fetched}); it is replaced by OpenRouter's live catalog on the next refresh")
        basis_txt = (f"this workspace's average call over 30 days ({b['input_tokens']:,} tokens in, {b['output_tokens']:,} out)"
                     if b["observed"] else "a typical call of 800 tokens in and 400 out")
        st.caption(md(f"Prices are OpenRouter's published per-token prices, from {src}. Estimates use {basis_txt}; "
                      "the cost OpenRouter reports for each call is what Model usage records."))
        cols = st.columns(3)
        for col, t in zip(cols, opts["tiers"], strict=True):
            with col.container(border=True):
                head = st.columns([3, 2], vertical_alignment="center")
                head[0].markdown(f"**{t['label']}**")
                if sel["mode"] == "auto" and sel["tier"] == t["id"]:
                    kit.chip("In use", "ok", head[1])
                elif not t["allowed"]:
                    kit.chip("Not allowed", "off", head[1])
                st.caption(t["purpose"])
                if t["primary"]:
                    st.metric("Per call", usd(t["per_call_usd"]), help="On the tier's first model, with the token counts above.")
                    lo, hi = t["range_usd"]
                    st.caption(md(f"{usd(t['per_call_usd'], per=1000)} per 1,000 calls · {usd(lo)} to {usd(hi)} across the tier"
                                  + (f" · about {usd(t['primary']['per_question_usd'])} per question" if t["primary"].get("per_question_usd") else "")))
                    st.markdown(md(f"{t['primary']['modelLabel']} first, then {len(t['models']) - 1} fallbacks"))
                    st.caption(md(per_m(t["primary"])))
        if sel["mode"] == "pinned":
            st.info(md(f"Pinned to **{sel['model_label'] or sel['model']}** (used alone, about {usd(sel['per_call_usd'])} per call)."),
                    icon=":material/push_pin:")
        where = {"workspace": "chosen for this workspace", "installation": "the installation default",
                 "default": "Lyst's default"}[sel["source"]]
        st.caption(md(f"Now: {where}. Calls walk {' → '.join(f'`{x}`' for x in sel['attempts'][:6])}"
                      + (" …" if len(sel["attempts"]) > 6 else "")
                      + (f". This installation allows tiers up to {sel['ceiling']}." if sel["ceiling"] != "frontier" else ".")))
        if admin:
            with st.form("model-level", border=True):
                ws_row = s["workspace"]
                labels = {"": "Follow the installation default"} | {t["id"]: f"{t['label']} · about {usd(t['per_call_usd'])} per call"
                                                                    for t in opts["tiers"] if t["allowed"]}
                cur_tier = ws_row.get("llm_tier") or ""
                f = st.columns(2)
                tier = f[0].selectbox("Level for this workspace", list(labels), format_func=labels.get,
                                      index=list(labels).index(cur_tier) if cur_tier in labels else 0)
                allowed = [r for r in opts["models"] if r["allowed"]]
                by_id = {r["modelId"]: r for r in allowed}
                choices = ["auto"] + [r["modelId"] for r in allowed]
                cur_model = ws_row.get("llm_model") or "auto"
                if cur_model not in choices:
                    choices.append(cur_model)

                def model_label(mid: str) -> str:
                    if mid == "auto":
                        return "Auto: the tier's best-priced model, with fallbacks (recommended)"
                    r = by_id.get(mid)
                    if not r:
                        return mid
                    tier_tag = f" · {r['tier']}" if r.get("tier") else ""
                    return f"{r['vendorLabel']} · {r['modelLabel']}{tier_tag} · {price(r['input_per_m'])}/{price(r['output_per_m'])} per 1M · ≈{usd(r['per_call_usd'])}/call"

                model = f[1].selectbox("Model", choices, index=choices.index(cur_model), format_func=model_label,
                                       help="Auto walks the level's models cheapest first and falls back on errors, as in Acumane. "
                                            "A specific model is used alone. Models above this installation's highest allowed "
                                            "level are not listed.")
                if st.form_submit_button("Save model choice", icon=":material/save:"):
                    try:
                        api.patch(f"/v1/workspaces/{ws}/settings", {"llm_tier": tier, "llm_model": model})
                        st.toast("Model choice saved; the next call uses it.", icon=":material/check:")
                        st.rerun()
                    except APIError as e:
                        st.error(str(e))
        with st.expander("Every model in each level"):
            rows = [{"Level": t["label"], "Order": i + 1, "Provider": r["vendorLabel"], "Model": r["modelLabel"], "Id": r["modelId"],
                     "Input $/1M": r["input_per_m"], "Output $/1M": r["output_per_m"], "Cached $/1M": r["cached_per_m"],
                     "Per call": r["per_call_usd"], "Per 1,000 calls": r["per_call_usd"] * 1000, "Context": r["context"]}
                    for t in opts["tiers"] for i, r in enumerate(t["models"])]
            money = {k: st.column_config.NumberColumn(k, format="$%.4f") for k in ("Input $/1M", "Output $/1M", "Cached $/1M", "Per 1,000 calls")}
            st.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch",
                         column_config=money | {"Per call": st.column_config.NumberColumn("Per call", format="$%.6f"),
                                                "Context": st.column_config.NumberColumn("Context", format="%d")})
        with st.expander(f"All {cat.get('selectable') or len(opts['models'])} OpenRouter models Lyst can use"):
            st.caption("Text models that support tool calls, with a published price and at least 8,000 tokens of context. "
                       "Newest first within each provider.")
            allm = pd.DataFrame([{"Provider": r["vendorLabel"], "Model": r["modelLabel"], "Id": r["modelId"], "Level": r["tier"] or "",
                                  "Input $/1M": r["input_per_m"], "Output $/1M": r["output_per_m"], "Per 1,000 calls": r["per_call_usd"] * 1000,
                                  "Context": r["context"], "Reasoning": bool(r["reasoning"]), "Allowed here": r["allowed"]}
                                 for r in opts["models"]])
            st.dataframe(allm, hide_index=True, width="stretch",
                         column_config={k: st.column_config.NumberColumn(k, format="$%.4f") for k in ("Input $/1M", "Output $/1M", "Per 1,000 calls")})

    if not admin:
        return
    allow = st.toggle("Allow this workspace to use external model providers", value=bool(s["workspace"]["allow_external_llm"]))
    if allow != bool(s["workspace"]["allow_external_llm"]):
        kit.guarded(lambda: api.patch(f"/v1/workspaces/{ws}/settings", {"allow_external_llm": allow}), "Saved")
        st.rerun()
    usage = api.get(f"/v1/workspaces/{ws}/model-usage")
    if usage:
        st.markdown("**Model usage**")
        if opts and opts.get("spend"):
            sp = opts["spend"]
            u = st.columns(4)
            u[0].metric("Spend, last 30 days", usd(sp["cost_usd"]) if sp["cost_usd"] else "$0")
            u[1].metric("Calls, last 30 days", f"{sp['calls']:,}")
            u[2].metric("Average per call", usd(sp["cost_usd"] / sp["calls"]) if sp["calls"] and sp["cost_usd"] else "n/a")
            top = next((r for r in sp["by_tier"] if r["tier"] != "unrecorded"), None)
            u[3].metric("Most spend on", top["tier"].capitalize() if top else "n/a")
        st.dataframe(pd.DataFrame(usage), hide_index=True, width="stretch",
                     column_config={"tier": st.column_config.TextColumn("Level", help="The level the call ran under; custom is a pinned model outside the levels"),
                                    "cost_usd": st.column_config.NumberColumn("Cost", format="$%.4f", help="As reported by OpenRouter for each call, else priced from the catalog"),
                                    "input_tokens": st.column_config.NumberColumn("Input tokens", format="%d"),
                                    "output_tokens": st.column_config.NumberColumn("Output tokens", format="%d"),
                                    "p95_ms": st.column_config.NumberColumn("p95 latency (ms)", format="%d")})
    if s.get("gateway") and s["gateway"].get("configured"):
        g = s["gateway"]
        waiting = g.get("waiting") or {}
        queued = sum(waiting.values()) if isinstance(waiting, dict) else int(waiting or 0)
        st.caption(f"Right now: {g['inflight']} model call{'s' * (g['inflight'] != 1)} in flight, {queued} waiting"
                   + (f" ({', '.join(f'{v} {k}' for k, v in waiting.items() if v)})" if isinstance(waiting, dict) and queued else "")
                   + (f"; refused: {g['refusals']}" if g.get("refusals") else "") + ".")


def connections() -> None:
    """Users manage connections; Lyst manages the secrets."""
    st.caption("You manage connections; Lyst manages the secrets. Values are entered once, tested, sealed with AES-256-GCM under "
               "a master key kept outside the database, and never displayed or returned again. Every connect, test, replace, "
               "revoke and use is in a tamper-evident history.")
    personal = bool((cred.get("policy") or {}).get("user_credentials"))
    names = ["This workspace"] + (["Installation"] if cred.get("installation_admin") else []) + (["Personal"] if personal else [])
    tabs = st.tabs(names)
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
                healthy = inst["backend"]["ok"] and inst["audit"]["ok"]
                (st.caption if healthy else st.warning)(
                    f"Secrets in the {inst['backend']['backend']} backend ({'healthy' if inst['backend']['ok'] else 'down'}) · "
                    f"master key from {inst['master_key_source'] or 'nowhere'} · {inst['active_credentials']} active "
                    f"credential{'s' * (inst['active_credentials'] != 1)} · audit chain "
                    + ("intact" if inst["audit"]["ok"] else f"broken at {inst['audit'].get('broken_at')}"))
                credential_panel("installation", None, key="inst", title="Installation credentials",
                                 intro="Shared by every workspace: the OpenRouter key (Acumane's rails) and the sign-in provider. "
                                       "An installation credential is authoritative; the policy below says where it may be overridden.")
                st.markdown("**Policy and model settings**")
                pol = inst["policy"].get("openrouter", {})
                p1 = st.columns(2)
                wo = p1[0].toggle("Workspaces may connect their own OpenRouter key", value=bool(pol.get("workspace_override")))
                uc = p1[1].toggle("People may connect a personal OpenRouter key", value=bool(pol.get("user_credentials")))
                st_ = inst["settings"]
                levels = ["economy", "balanced", "frontier"]
                p2 = st.columns(2)
                tier = p2[0].selectbox("Default level", levels, format_func=str.capitalize,
                                       index=levels.index(st_.get("LYST_LLM_TIER") or "balanced"),
                                       help="What every workspace uses unless it chooses its own.")
                ceiling = p2[1].selectbox("Highest level a workspace may choose", levels, format_func=str.capitalize,
                                          index=levels.index(st_.get("LYST_LLM_MAX_TIER") or "frontier"),
                                          help="Also caps a workspace's specific model at the dearest model of this level. "
                                               "A workspace above a lowered ceiling falls back to the default.")
                p3 = st.columns(2)
                pinned = p3[0].text_input("Pinned model for every workspace (optional)", value=st_.get("LYST_LLM_MODEL") or "",
                                          help="An OpenRouter slug used alone, e.g. anthropic/claude-opus-5.5. Administrators may "
                                               "pin a model outside the catalog.")
                embed = p3[1].text_input("Embedding model (optional)", value=st_.get("LYST_EMBED_MODEL") or "",
                                         help="e.g. openai/text-embedding-3-small; turns on semantic search")
                b1, b2 = st.columns([1, 1])
                if b1.button("Save installation settings", icon=":material/save:"):
                    try:
                        api.patch("/v1/installation", {"policy": {"provider": "openrouter", "workspace_override": wo, "user_credentials": uc},
                                                       "settings": {"LYST_LLM_TIER": tier, "LYST_LLM_MAX_TIER": ceiling,
                                                                    "LYST_LLM_MODEL": pinned or None, "LYST_EMBED_MODEL": embed or None}})
                        st.rerun()
                    except APIError as e:
                        _reauth(e)
                if inst.get("provider") == "openrouter" and b2.button("Refresh models and prices from OpenRouter",
                                                                      icon=":material/sync:"):
                    try:
                        out = api.post("/v1/models/catalog/refresh", {})
                        st.success(md(f"Catalog updated: {out['selectable']} usable models of {out['modelCount']}. "
                                      + ("; ".join(out["changes"][:8]) or "No tier changes.")))
                    except APIError as e:
                        _reauth(e)
                with st.expander("Trust anchors and administrators"):
                    st.caption(f"Administrators: {', '.join(inst['admins'])}. Data directory: {inst['home']}.")
                    st.dataframe(pd.DataFrame(inst["anchors"]), hide_index=True, width="stretch")
                    if inst["events"]:
                        st.dataframe(pd.DataFrame(inst["events"])[["occurred_at", "actor", "action", "credential_id", "outcome", "detail"]],
                                     hide_index=True, width="stretch")
    if personal:
        with tabs[-1]:
            credential_panel("user", None, key="me", title="Your personal credentials",
                             intro="Used only for your own work, never for anyone else's.")

AUTONOMY = {"assist": "Lyst proposes; you run each step.", "delegate": "Lyst runs analyses and asks before it changes anything.",
            "autonomous": "Lyst runs and acts within this workspace's policies.", "governed": "Every action waits for an approval."}


QUOTA = {"max_running": ("Tasks running at once", 1, None), "max_queued": ("Tasks waiting at once", 1, None),
         "monthly_spend_usd": ("Model spend a month (USD)", 0.0, None), "monthly_tokens": ("Model tokens a month", 0, None),
         "task_budget_seconds": ("Longest a task may run (seconds)", 30, None)}


def usage_and_limits() -> None:
    """What this workspace uses this month against its limits, whether anything is blocked or stuck, and (for an
    installation administrator) the limits themselves."""
    try:
        op = api.get(f"/v1/workspaces/{ws}/operations")
    except APIError as e:
        st.caption(str(e))
        return
    kit.label("Usage & limits")
    me = next((u for u in op.get("usage") or [] if u["workspace_id"] == ws), {})
    q, mm = me.get("quotas") or {}, me.get("model_month") or {}
    if me.get("blocked"):
        st.warning(md(me["blocked"]), icon=":material/block:")
    spend, cap = mm.get("cost_usd") or 0.0, q.get("monthly_spend_usd")
    toks, tcap = mm.get("tokens") or 0, q.get("monthly_tokens")
    mine = next((w for w in op.get("workspaces") or [] if w["workspace_id"] == ws), {})
    rows = [("Model spend this month", f"${spend:,.2f}" + (f" of ${cap:,.0f}" if cap else " · no limit"),
             (spend / cap) if cap else None),
            ("Model tokens this month", f"{toks:,}" + (f" of {tcap:,}" if tcap else " · no limit"),
             (toks / tcap) if tcap else None),
            ("Tasks now", f"{mine.get('running', 0)} running, {mine.get('queued', 0)} waiting"
             + (f" · at most {q['max_running']} at once" if q.get("max_running") else ""), None)]
    for i, (label, text, share) in enumerate(rows):
        tone = None if share is None else (("Near the limit", "warn") if share >= 0.8 else ("Within limit", "ok"))
        kit.row(label, text, status=tone, key=f"use{i}", divider=i < len(rows) - 1)
    sup = op.get("supervisor") or {}
    stuck = [x for x in sup.get("stalled") or [] if x.get("workspace_id") == ws]
    late = [x for x in sup.get("stale_approvals") or [] if x.get("workspace_id") == ws]
    overdue = [x for x in sup.get("overdue_schedules") or [] if x.get("workspace_id") == ws]
    fails = sum((op.get("failures_24h") or {}).values())
    notes = ([f"{len(stuck)} task{'s' * (len(stuck) != 1)} running past its time budget"] if stuck else []) + \
            ([f"{len(late)} approval{'s' * (len(late) != 1)} waiting more than a day: "
              + "; ".join(f"{x['summary']} ({x['waiting_hours']:.0f} h)" for x in late[:3])] if late else []) + \
            ([f"{len(overdue)} schedule{'s' * (len(overdue) != 1)} late: "
              + "; ".join(f"{x['title']} ({x['late_minutes']:.0f} min)" for x in overdue[:3])] if overdue else [])
    if notes:
        st.warning("\n".join(f"- {md(n)}" for n in notes), icon=":material/schedule:")
    st.caption(f"Installation: {op.get('workers', '?')} workers · {fails} failed task{'s' * (fails != 1)} in the last "
               f"24 h · checked {kit.when(op.get('generated_at'))}.")
    try:
        api.get("/v1/operations")  # only an installation administrator may read (and change) the limits
        install_admin = True
    except APIError:
        install_admin = False
    if not install_admin:
        st.caption("Limits are set by the installation's administrators.")
        return
    with st.expander("Change the limits", icon=":material/tune:"):
        st.caption("Empty means no limit. A full queue or an exhausted monthly budget stops new work with a clear "
                   "message, and running work finishes.")
        new = {}
        for k, (label, lo, _) in QUOTA.items():
            cur = q.get(k)
            v = st.number_input(label, min_value=lo, value=cur, key=f"q_{k}", placeholder="No limit")
            new[k] = None if v is None else (float(v) if isinstance(lo, float) else int(v))
        if new != {k: q.get(k) for k in QUOTA} and st.button("Save limits", type="primary", key="q_save"):
            kit.guarded(lambda: api.put(f"/v1/workspaces/{ws}/quotas", new), "Limits saved")


def workspace_section() -> None:
    """How this workspace is governed: autonomy, notifications, members and the audit trail."""
    if admin:
        kit.label("Autonomy")
        cur = s["workspace"]["autonomy"]
        mode = st.segmented_control("Autonomy", list(AUTONOMY), default=cur, format_func=str.capitalize,
                                    label_visibility="collapsed", key="autonomy")
        st.caption(AUTONOMY.get(mode or cur, ""))
        if mode and mode != cur:
            kit.guarded(lambda: api.patch(f"/v1/workspaces/{ws}/settings", {"autonomy": mode}), "Autonomy saved")
            st.rerun()
        anticipate.level_control()
        kit.label("Notifications")
        with st.form("webhook", border=False):
            w = st.columns([5, 1.2], vertical_alignment="bottom")
            hook = w[0].text_input("Webhook for monitor findings", value=s["workspace"].get("webhook_url") or "",
                                   placeholder="https://… (empty turns it off)",
                                   help="Lyst sends each new monitor finding as a JSON POST to this https address.")
            if w[1].form_submit_button("Save", width="stretch") and hook != (s["workspace"].get("webhook_url") or ""):
                kit.guarded(lambda: api.patch(f"/v1/workspaces/{ws}/settings", {"webhook_url": hook}), "Webhook saved")
                st.rerun()
    onboarding.profile_section()
    governance.policy_section()
    if admin:
        usage_and_limits()
        integrations.webhooks_section()
    try:
        people = api.get(f"/v1/workspaces/{ws}/members")
    except APIError as e:
        st.caption(str(e))
        people = []
    names = {p["user_id"]: p["email"] for p in people}
    kit.label(f"Members · {len(people)}")
    for i, p in enumerate(people):
        acts = [("Remove", "rm")] if admin and p["role"] != "owner" else []
        if kit.row(p.get("name") or p["email"], p["email"] if p.get("name") else "",
                   status=(p["role"].capitalize(), "ok" if p["role"] in ("owner", "admin") else "info"), actions=acts,
                   key=f"mem{i}", divider=i < len(people) - 1) == "rm":
            kit.guarded(lambda u=p["user_id"]: api.delete(f"/v1/workspaces/{ws}/members/{u}"), f"Removed {p['email']}")
            st.rerun()
    if admin:
        st.write("")
        with st.form("member", clear_on_submit=True, border=False):
            c2 = st.columns([4, 1.4, 1.2], vertical_alignment="bottom")
            email = c2[0].text_input("Add someone", placeholder="name@company.com",
                                     help="The person must have signed in to Lyst once.")
            role = c2[1].selectbox("Role", ["viewer", "editor", "admin", "owner"], format_func=str.capitalize)
            if c2[2].form_submit_button("Add", width="stretch") and email.strip():
                done = kit.guarded(lambda: api.post(f"/v1/workspaces/{ws}/members",
                                                    {"email": email.strip().lower(), "role": role}), f"Added {email.strip()}")
                if done is not None:
                    st.rerun()
    kit.label("Audit trail")
    try:
        log = api.get(f"/v1/workspaces/{ws}/audit")
    except APIError as e:
        st.caption(str(e))
        return
    if not log:
        st.caption("Nothing recorded yet.")
        return
    st.caption("Every change, query and decision in this workspace, newest first. The trail is hash-chained, so an edit "
               "to a past entry is detected.")
    df = pd.DataFrame([{"When": kit.when(r["occurred_at"]), "Who": names.get(r["actor"], r["actor"]),
                        "Action": r["action"], "On": r["target"] or "", "Outcome": r["outcome"],
                        "Detail": "" if r["detail"] in (None, "{}", {}) else str(r["detail"])} for r in log])
    st.dataframe(df, hide_index=True, width="stretch", height=min(38 + 35 * len(df), 420))


SECTIONS = ["Models"] + (["Connections"] if owner else []) + ["Workspace", "Voice", "Advanced"]
{"Models": models, "Connections": connections, "Workspace": workspace_section,
 "Voice": voice.settings_section, "Advanced": advanced.skills}[kit.sections(SECTIONS, key="view")]()
