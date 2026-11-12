# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Rohn Springfield
"""Incident and change review: time to restore, change failure rate and the changes linked to incidents, for the period
asked about. A link is the incident's own reference to a change; without one, a change on the same service in the 24
hours before is only a possible link, and the answer says so."""

from __future__ import annotations

import io

import numpy as np
import pandas as pd
import pytest

from lyst.analyst import incidents as I

from .conftest import ask

WS = "ops"


def records(link: bool = True) -> tuple[pd.DataFrame, pd.DataFrame]:
    rng = np.random.default_rng(5)
    svcs, ch, inc = ["checkout", "search", "auth", "billing"], [], []
    for n in range(400):
        t = pd.Timestamp("2026-04-01") + pd.Timedelta(hours=int(rng.integers(0, 24 * 210)))
        s = svcs[int(rng.integers(0, 4))]
        risky = rng.random() < (0.25 if s == "checkout" else 0.08)
        ch.append({"change_id": f"CHG{n:04d}", "service": s, "implemented_at": t,
                   "change_type": ["standard", "normal", "emergency"][int(rng.integers(0, 3))],
                   "outcome": "failed" if risky else "successful"})
        if risky:
            o = t + pd.Timedelta(minutes=int(rng.integers(5, 180)))
            inc.append({"incident_id": f"INC{len(inc):04d}", "service": s, "opened_at": o,
                        "resolved_at": o + pd.Timedelta(minutes=int(rng.lognormal(4, 1))),
                        "severity": ["sev1", "sev2", "sev3"][int(rng.integers(0, 3))], "caused_by_change": f"CHG{n:04d}"})
    for _ in range(60):
        o = pd.Timestamp("2026-04-01") + pd.Timedelta(hours=int(rng.integers(0, 24 * 210)))
        inc.append({"incident_id": f"INC{len(inc):04d}", "service": svcs[int(rng.integers(0, 4))], "opened_at": o,
                    "resolved_at": o + pd.Timedelta(minutes=int(rng.lognormal(4, 1))) if rng.random() > 0.05 else None,
                    "severity": ["sev1", "sev2", "sev3"][int(rng.integers(0, 3))], "caused_by_change": None})
    inc = pd.DataFrame(inc)
    if not link:
        inc = inc.drop(columns=["caused_by_change"])
    return inc, pd.DataFrame(ch)


def _csv(df):
    b = io.StringIO()
    df.to_csv(b, index=False)
    return b.getvalue().encode()


def open_ops(lyst, link: bool = True):
    owner = lyst.guard.create_user("ops@co.test", "Ops")
    lyst.guard.create_workspace(owner, WS, "Platform ops")
    inc, ch = records(link)
    lyst.data.ingest(owner, WS, "incidents", _csv(inc), filename="incidents.csv", title="Incidents")
    lyst.data.ingest(owner, WS, "changes", _csv(ch), filename="changes.csv", title="Changes")
    return owner


def _sep(df, col):
    return df[(df[col] >= "2026-09-01") & (df[col] < "2026-10-01")]


def test_time_to_restore_and_change_failure_rate_for_the_last_month(lyst):
    owner = open_ops(lyst)
    r = ask(lyst, owner, "What is our mean time to resolve incidents?", ws=WS)["result"]
    assert r["status"] == "complete" and r["mode"] == "incidents"
    inc, ch = records()
    sep, aug = _sep(inc, "opened_at"), inc[(inc.opened_at >= "2026-08-01") & (inc.opened_at < "2026-09-01")]
    hours = ((sep.resolved_at - sep.opened_at).dt.total_seconds() / 3600).median()
    assert r["answer"].startswith(f"{len(sep)} incidents in Sep 2026, up {len(sep) - len(aug)} on Aug 2026 ({len(aug)})")
    assert f"median time to restore {I._h(hours)}" in r["answer"]
    cs = _sep(ch, "implemented_at")
    bad = (cs.outcome == "failed") | cs.change_id.isin(set(inc.caused_by_change.dropna()))
    assert f"{len(cs)} changes with a change failure rate of {bad.mean():.0%}" in r["answer"]
    titles = [t["title"] for t in r["tables"]]
    assert "Incidents by severity, Sep 2026" in titles and "Change failure rate by service, Sep 2026" in titles
    assert any(a.startswith("Hold a post-incident review for each of the") and "sev1" in a for a in r["actions"])
    assert {c["name"]: c["passed"] for c in r["confidence"]["checks"]}["change_links"] is True


def test_changes_that_caused_incidents_lead_when_asked(lyst):
    owner = open_ops(lyst)
    r = ask(lyst, owner, "Which changes caused incidents?", ws=WS)["result"]
    inc, ch = records()
    linked = _sep(inc, "opened_at").caused_by_change.dropna()
    assert r["answer"].startswith(f"{linked.nunique()} changes in Sep 2026 led to incidents")
    table = next(t for t in r["tables"] if t["title"].startswith("Changes linked to incidents"))
    assert {row[0] for row in table["rows"]} <= set(linked)


def test_without_a_link_column_a_nearby_change_is_only_a_possible_link(lyst):
    owner = open_ops(lyst, link=False)
    r = ask(lyst, owner, "Which changes caused incidents?", ws=WS)["result"]
    assert "were followed within 24 hours by incidents" in r["answer"]
    assert {c["name"]: c["passed"] for c in r["confidence"]["checks"]}["change_links"] is False
    assert any("time coincidence" in a for a in r["supporting"]["assumptions"])


def test_a_workspace_without_incidents_is_not_reviewed(seeded):
    lyst, owner = seeded
    assert ask(lyst, owner, "What is our change failure rate?")["result"].get("mode") != "incidents"


@pytest.mark.parametrize("text", ["What is our MTTR?", "What is our change failure rate?", "Which changes caused incidents?",
                                  "Incident review for last month", "Are incidents getting worse?", "Show open incidents"])
def test_review_phrases(text):
    assert I.asks_for_review(text)


@pytest.mark.parametrize("text", ["What was revenue last quarter?", "What do our documents say about incident policy?"])
def test_not_review_phrases(text):
    assert not I.asks_for_review(text)
