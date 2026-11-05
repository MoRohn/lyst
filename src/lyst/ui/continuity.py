# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Rohn Springfield
"""Settings › Advanced › Backups and recovery (installation administrators): backups, the last restore drill and
whether the recovery objectives are met, measured rather than claimed. Reads the session on every call."""

from __future__ import annotations

import streamlit as st

from lyst.ui import kit
from lyst.ui.client import APIError, client
from lyst.ui.components import md


def _size(n: int | None) -> str:
    n = n or 0
    return f"{n / 1e9:.1f} GB" if n >= 1e9 else (f"{n / 1e6:.1f} MB" if n >= 1e6 else f"{n / 1e3:.0f} KB")


def _count(t) -> int:
    return sum(t.values()) if isinstance(t, dict) else int(t or 0)


def _secs(v: float | None) -> str:
    v = v or 0.0
    return "under 0.1 s" if v < 0.1 else (f"{v:.1f} s" if v < 120 else f"{v / 60:.1f} min")


def section() -> None:
    api = client()
    try:
        c = api.get("/v1/installation/continuity")
    except APIError:
        return  # only installation administrators see this
    with kit.group("Backups and recovery", "continuity", note=c.get("availability")):
        ob = c.get("objectives") or {}
        rpo = ob.get("rpo_hours")
        rto = ob.get("rto_seconds")
        rows = [("Data you could lose", (f"{rpo:.1f} h since the last good backup" if rpo is not None else "not measured yet")
                 + f" · target {ob.get('rpo_target_hours', 24):g} h", ob.get("rpo_met")),
                ("Time to restore", (f"{_secs(rto)} in the last drill" if rto is not None else "no drill yet")
                 + f" · target {ob.get('rto_target_minutes', 30):g} min", ob.get("rto_met"))]
        for i, (label, text, met) in enumerate(rows):
            kit.row(label, text, status=(("Met", "ok") if met else ("Not yet", "warn")), key=f"ct{i}", divider=True)
        sch = c.get("schedule") or {}
        st.caption(md((f"Automatic backups every {sch['every_hours']:g} h" if sch.get("every_hours") else "No automatic backups")
                      + f"; the last {sch.get('keep', 7)} are kept."
                      + (f" Last failure {kit.when(c['last_failure'])}: {c.get('last_error')}" if c.get("last_failure") else "")))
        dr = c.get("last_drill") or {}
        if dr:
            (st.success if dr.get("ok") else st.error)(md(
                f"Last restore drill {kit.when(dr.get('drilled_at'))}: " + ("restored and checked" if dr.get("ok") else "failed")
                + f" ({_count(dr.get('tables'))} tables in {_secs(dr.get('seconds'))})"
                + (f"; {'; '.join(map(str, dr['problems']))}" if dr.get("problems") else "")),
                icon=":material/restore:")
        b = st.columns([1.6, 4])
        if b[0].button("Back up now", icon=":material/backup:", key="ct_now"):
            out = kit.guarded(lambda: api.post("/v1/installation/backup"))
            if out:
                st.toast(f"Backed up in {out.get('seconds', 0):.1f} s", icon=":material/check:")
                st.rerun()
        for i, bk in enumerate((c.get("backups") or [])[:5]):
            hit = kit.row(kit.when(bk.get("taken_at")) or bk["name"], f"{bk.get('files', 0)} files · {_size(bk.get('bytes'))}",
                          actions=[("Test a restore", "drill")], key=f"bk{bk['name']}", divider=i < min(len(c["backups"]), 5) - 1)
            if hit == "drill":
                r = kit.guarded(lambda x=bk: api.post(f"/v1/installation/backups/{x['name']}/drill"))
                if r:
                    st.toast("Restore drill passed" if r.get("ok") else "Restore drill failed", icon=":material/restore:")
                    st.rerun()
        st.caption(md(c.get("key") or ""))
