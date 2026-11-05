# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Rohn Springfield
"""Settings › Advanced › Health (installation administrators): every check Lyst runs on itself, grouped by area,
problems first, each with what to do. Reads the session on every call."""

from __future__ import annotations

import streamlit as st

from lyst.ui import kit
from lyst.ui.client import APIError, client
from lyst.ui.components import md

AREA = {"start": "Starting up", "storage": "Storage", "security": "Security", "runtime": "Running work",
        "models": "Models", "platform": "Platform"}
STATUS = {"fail": ("Needs fixing", "bad"), "warn": ("Check", "warn"), "ok": ("OK", "ok")}
ORDER = {"fail": 0, "warn": 1, "ok": 2}
HEADLINE = {"healthy": ("Everything checks out", "ok"), "attention": ("Some things need a look", "warn"),
            "unhealthy": ("Something needs fixing", "bad")}


def section() -> None:
    api = client()
    try:
        d = api.get("/v1/diagnostics")
    except APIError:
        return  # installation administrators only
    s = d.get("summary") or {}
    text, tone = HEADLINE.get(d.get("status"), (str(d.get("status")), "info"))
    with kit.group("Health", "health", note=f"{s.get('fail', 0)} to fix · {s.get('warn', 0)} to check · {s.get('ok', 0)} fine "
                                            f"· {d.get('profile', '')} setup · checked {kit.when(d.get('checked_at'))}"):
        kit.chip(text, tone)
        checks = sorted(d.get("checks") or [], key=lambda c: (ORDER.get(c.get("status"), 3), c.get("area") or ""))
        bad = [c for c in checks if c.get("status") != "ok"]
        for i, c in enumerate(bad):
            meta = " · ".join(x for x in (AREA.get(c.get("area"), c.get("area")), c.get("detail")) if x)
            kit.row(c["id"].replace("_", " ").capitalize(), meta + (f"\n\n**What to do:** {c['fix']}" if c.get("fix") else ""),
                    status=STATUS.get(c.get("status"), (c.get("status"), "info")), key=f"hc{c['id']}", divider=i < len(bad) - 1)
        good = [c for c in checks if c.get("status") == "ok"]
        if good:
            with st.expander(f"Passing checks · {len(good)}", icon=":material/check_circle:", expanded=not bad):
                for area in [a for a in AREA if any(c.get("area") == a for c in good)] + \
                        sorted({c.get("area") for c in good} - set(AREA)):
                    kit.label(AREA.get(area, str(area)))
                    for c in [c for c in good if c.get("area") == area]:
                        st.caption(md(f"{c['id'].replace('_', ' ').capitalize()}: {c.get('detail') or 'ok'}"))
        if st.button("Check again", type="tertiary", icon=":material/refresh:", key="hc_again"):
            st.rerun()
