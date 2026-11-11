# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Rohn Springfield
"""Receivables: who owes the business money, how late it is, and who to chase first.

"Who owes me money?", "Which invoices are overdue?" and "How long do clients take to pay?" are the questions a
freelancer or small business owner asks most about their invoices, and none of them is a metric over a period: they are
about the invoices still open today. Lyst finds the invoices dataset from its columns (a due date, and a paid date or a
status, an amount and usually a client), then reads, as the asker through the SQL guard:

1. **What is owed**: every open invoice (no paid date, or a status that is not paid or void), its amount and how many
   days past its due date it is, grouped into the usual aging buckets.
2. **By client**: what each client owes, how much of it is overdue, and their oldest overdue invoice.
3. **How fast clients pay**: days from issue to payment on paid invoices, per client, so a client paying slower than
   they used to is named.

Lyst only reports and suggests. It drafts nothing on its own and never contacts a client: a reminder is a recommended
action the owner takes.
"""

from __future__ import annotations

import re
from datetime import UTC, date, datetime
from typing import Any

import pandas as pd

from ..semantic import _q
from . import brief as brief_mod
from .registry import REGISTRY, Context, capability

ASKS = re.compile(
    r"\b(who owes|owe[sd]? (me|us)|how much (do|does) .{1,40}? owes?|owing (me|us)|outstanding (invoices?|balances?|payments?|amounts?|receivables?)|"
    r"(what|how much) is outstanding|overdue|past due|unpaid|late (payments?|payers?|invoices?)|pa(y|ying|id) (me |us )?late|"
    r"receivables?|a/?r aging|aging (report|buckets?)|(chase|follow up on) (payments?|invoices?|clients?)|"
    r"days to pay|take to pay|slow (payers?|paying)|collect(ed|ions?)? (on|from)? ?(invoices?|clients?))\b", re.I)

BUCKETS = [(0, "not yet due"), (30, "1-30 days late"), (60, "31-60 days late"), (90, "61-90 days late"),
           (10**6, "over 90 days late")]
SETTLED = {"paid", "settled", "closed", "complete", "completed", "void", "voided", "cancelled", "canceled",
           "written off", "write off", "refunded", "credited"}
VOID = {"void", "voided", "cancelled", "canceled", "written off", "write off", "credited"}
_SPEED = re.compile(r"\b(days to pay|take to pay|slow (payers?|paying)|pa(y|ying|id) (me |us )?late|how (fast|quickly|long))\b", re.I)
SLOWER = 1.5  # a client whose recent invoices took this many times their usual days to pay is named
STALE_DAYS = 14  # an export whose latest activity is older than this is called out: payments since may be missing

_DUE = re.compile(r"^(due|due_?date|date_?due|due_?on|payment_?due)$", re.I)
_PAID = re.compile(r"^(paid|paid_?(date|on|at)|date_?paid|payment_?date|settled_?(date|on|at)|received_?(date|on))$", re.I)
_ISSUE = re.compile(r"^(issue_?date|issued(_?(on|at|date))?|date_?issued|invoice_?date|date|created(_?(at|on))?|sent(_?(on|at|date))?)$", re.I)
_STATUS = re.compile(r"^(status|state|payment_?status|invoice_?status)$", re.I)
_AMOUNT = ["amount_due", "balance", "balance_due", "outstanding", "open_amount", "amount", "total", "total_amount",
           "invoice_amount", "gross_amount", "net_amount", "value"]
_CLIENT = ["client", "customer", "client_name", "customer_name", "company", "account", "client_id", "customer_id"]
_ID = re.compile(r"^(invoice_?(no|number|num|id)?|number|ref(erence)?|id)$", re.I)


def today() -> date:
    return datetime.now(UTC).date()


# "overdue", "unpaid" or "late" alone could be tickets or maintenance; they count with a word about money owed
_GENERIC = re.compile(r"^(overdue|past due|unpaid|late \w+|pa(y|ying|id) (me |us )?late|collect\w*.*)$", re.I)
_MONEY = re.compile(r"\b(invoices?|bills?|payments?|paid|clients?|customers?|money|cash|owe[sd]?|owing|balances?)\b", re.I)


def asks_for_receivables(text: str) -> bool:
    text = text or ""
    m = ASKS.search(text)
    if not m or brief_mod.ELSEWHERE.search(text):
        return False
    return not _GENERIC.match(m.group(0)) or bool(_MONEY.search(text))


def understanding(text: str) -> dict[str, Any]:
    return {**brief_mod.understanding(text), "intent": "receivables"}


def _query(ctx: Context, sql: str):
    return ctx.services.data.query(ctx.actor, ctx.workspace_id, sql, task_id=ctx.task_id)


def _evidence(r, purpose: str) -> dict[str, Any]:
    from .engine import _evidence as ev  # noqa: PLC0415

    return ev(r, purpose)


def find_invoices(datasets: list[dict[str, Any]]) -> dict[str, Any] | None:
    """The dataset that looks like invoices, and its columns, from names and semantic types only.

    It needs an amount and a way to tell an open invoice from a paid one (a paid date or a status); a due date or an
    issue date dates it. Among several, the one whose name says invoice wins, then the one with the most of these."""
    best, best_score = None, -1
    for d in datasets:
        cols = d.get("columns", [])
        dates = [c["name"] for c in cols if c.get("semantic_type") == "date"]
        numeric = {c["name"].lower(): c["name"] for c in cols if c.get("semantic_type") in ("amount", "quantity", "count")}
        names = {c["name"].lower(): c["name"] for c in cols}
        pick = {
            "due": next((c for c in dates if _DUE.match(c)), None),
            "paid": next((c for c in dates if _PAID.match(c)), None),
            "issued": next((c for c in dates if _ISSUE.match(c) and not _DUE.match(c) and not _PAID.match(c)), None),
            "status": next((names[n] for n in names if _STATUS.match(n)), None),
            "amount": next((numeric[a] for a in _AMOUNT if a in numeric), None),
            "client": next((names[c] for c in _CLIENT if c in names), None),
            "id": next((names[n] for n in names if _ID.match(n)), None),
        }
        if not pick["amount"] or not (pick["paid"] or pick["status"]) or not (pick["due"] or pick["issued"]):
            continue
        label = f"{d['name']} {d.get('title') or ''} {d.get('description') or ''}".lower()
        score = 10 * ("invoice" in label) + 3 * bool(pick["due"]) + 2 * bool(pick["client"]) + bool(pick["paid"]) \
            + bool(pick["status"]) + bool(pick["id"])
        if score > best_score:
            best, best_score = {"dataset": d["name"], "title": d.get("title") or d["name"], **pick}, score
    return best


def _metadata(ctx: Context) -> list[dict[str, Any]]:
    s = ctx.services
    out = []
    for d in s.catalog.overview(ctx.actor, ctx.workspace_id):
        try:
            ins = s.catalog.insights(ctx.actor, ctx.workspace_id, d["name"])
        except Exception:  # noqa: BLE001, S112 - a dataset the asker cannot inspect is not a candidate
            continue
        out.append({"name": d["name"], "title": d.get("title"), "description": d.get("description"),
                    "columns": ins.get("columns", [])})
    return out


def _bucket(days_late: int) -> str:
    return next(label for limit, label in BUCKETS if days_late <= limit)


@capability(REGISTRY, "receivables", agent="business_analyst", phase="execute", permission="data.query",
            description="Open invoices today: what each client owes, how late it is (aging buckets), who to chase "
                        "first, and which clients are paying slower than they used to.")
def receivables(ctx: Context, args: dict[str, Any]) -> dict[str, Any]:
    inv = find_invoices(_metadata(ctx))
    if not inv:
        return {"empty": True}
    as_of = today()
    cols = [c for c in ("id", "client", "issued", "due", "paid", "status", "amount") if inv[c]]
    select = ", ".join(f"{_q(inv[c])} AS {c}" for c in cols)
    r = _query(ctx, f"SELECT {select} FROM {_q(inv['dataset'])}")
    evidence = [_evidence(r, f"{inv['dataset']}: every invoice, to find the open ones and how fast each was paid")]
    df = pd.DataFrame(r.rows, columns=cols)
    for c in ("issued", "due", "paid"):
        if c in df:
            df[c] = pd.to_datetime(df[c], errors="coerce").dt.tz_localize(None).dt.normalize()
    df["amount"] = pd.to_numeric(df["amount"], errors="coerce")
    status = df["status"].astype(str).str.strip().str.lower() if "status" in df else None
    void = status.isin(VOID) if status is not None else pd.Series(False, index=df.index)
    if "paid" in df:
        open_ = df["paid"].isna() & ~void
        if status is not None:
            open_ &= ~status.isin(SETTLED)
    else:
        open_ = ~status.isin(SETTLED)
    if "client" not in df:
        df["client"] = "(no client column)"
    df["client"] = df["client"].fillna("(no client)").astype(str)
    ts = pd.Timestamp(as_of)
    due = df["due"] if "due" in df else df["issued"]
    df["days_late"] = (ts - due).dt.days.fillna(0).astype(int)
    o = df[open_ & df["amount"].notna()].copy()
    o["bucket"] = o["days_late"].map(_bucket)
    overdue = o[o["days_late"] > 0]

    latest = max((df[c].max() for c in ("issued", "paid") if c in df and df[c].notna().any()), default=None)
    by_client = []
    for client, g in o.groupby("client"):
        late = g[g["days_late"] > 0]
        by_client.append({"client": client, "owed": float(g["amount"].sum()), "invoices": len(g),
                          "overdue": float(late["amount"].sum()), "overdue_invoices": len(late),
                          "oldest_days_late": int(late["days_late"].max()) if len(late) else 0})
    by_client.sort(key=lambda x: (-x["overdue"], -x["owed"]))
    aging = [{"bucket": label, "amount": float(o.loc[o["bucket"] == label, "amount"].sum()),
              "invoices": int((o["bucket"] == label).sum())} for _, label in BUCKETS]

    pay: list[dict[str, Any]] = []
    if "paid" in df and "issued" in df:
        paid = df[df["paid"].notna() & df["issued"].notna() & ~void].copy()
        paid["days"] = (paid["paid"] - paid["issued"]).dt.days
        paid = paid[paid["days"] >= 0].sort_values("paid")
        overall = float(paid["days"].median()) if len(paid) else None
        for client, g in paid.groupby("client"):
            recent, before = g.tail(3), g.iloc[:-3]
            usual = float(before["days"].median()) if len(before) >= 3 else None
            now = float(recent["days"].median())
            pay.append({"client": client, "paid_invoices": len(g), "median_days": float(g["days"].median()),
                        "recent_days": now, "usual_days": usual,
                        "slower": bool(usual and now >= SLOWER * usual and now - usual >= 7)})
        pay.sort(key=lambda x: -x["median_days"])
    else:
        overall = None
    top = overdue.sort_values(["days_late", "amount"], ascending=False)
    return {"invoices": inv, "as_of": as_of.isoformat(), "latest": latest.date().isoformat() if latest is not None else None,
            "open_total": float(o["amount"].sum()), "open_count": len(o),
            "overdue_total": float(overdue["amount"].sum()), "overdue_count": len(overdue),
            "aging": aging, "by_client": by_client, "pay": pay, "median_days_to_pay": overall,
            "oldest": [{"id": str(x.get("id", "")) if "id" in o else "", "client": x["client"], "amount": float(x["amount"]),
                        "due": x["due"].date().isoformat() if "due" in o and pd.notna(x["due"]) else None,
                        "days_late": int(x["days_late"])} for _, x in top.head(10).iterrows()],
            "evidence": evidence}


def _money(v: float) -> str:
    return f"${v:,.0f}" if abs(v) >= 100 else f"${v:,.2f}"


def _plural(n: int, word: str) -> str:
    return f"{n} {word}{'' if n == 1 else 's'}"


def result(ctx: Context) -> dict[str, Any]:
    o = ctx.outputs
    u, rc = o["understand"], o["receivables"]
    if rc.get("empty"):
        answer = ("Lyst could not find an invoices dataset it can read here. It looks for one with an amount, a due or "
                  "issue date, and a paid date or a status; upload an export from your invoicing app (or ask an admin "
                  "for access) and ask again.")
        out = brief_mod._shape(u["question"], u, answer, [], [], [], [], [], [], {}, status="needs_input")
        out["mode"] = "receivables"
        return out
    inv, as_of = rc["invoices"], date.fromisoformat(rc["as_of"])
    when = f"{as_of:%-d %b %Y}"
    if not rc["open_count"]:
        answer = f"Nobody owes you money as of {when}: every invoice in {inv['dataset']} is paid."
    else:
        answer = (f"Clients owe you {_money(rc['open_total'])} across {_plural(rc['open_count'], 'open invoice')} as of "
                  f"{when}")
        if rc["overdue_count"]:
            answer += f"; {_money(rc['overdue_total'])} of it is overdue ({_plural(rc['overdue_count'], 'invoice')})"
        answer += "."
        late = [c for c in rc["by_client"] if c["overdue"] > 0]
        if late:
            c = late[0]
            answer += (f" {c['client']} owes the most overdue: {_money(c['overdue'])}, the oldest "
                       f"{_plural(c['oldest_days_late'], 'day')} late.")
    slower = [p for p in rc["pay"] if p["slower"]]
    speed = ""
    if rc.get("median_days_to_pay") is not None:
        speed = f"Clients usually pay {rc['median_days_to_pay']:.0f} days after you invoice"
        if rc["pay"]:
            slow = rc["pay"][0]
            speed += f"; {slow['client']} takes longest ({slow['median_days']:.0f} days)"
        speed += "."
        if slower:
            speed += (f" {slower[0]['client']} is paying slower than before ({slower[0]['recent_days']:.0f} days on "
                      f"their last invoices, against {slower[0]['usual_days']:.0f}).")
    q = u["question"].lower()
    named = next((c for c in rc["by_client"] if c["client"].lower() in q), None) or \
        next(({"client": p["client"], "owed": 0.0, "invoices": 0, "overdue": 0.0, "oldest_days_late": 0}
              for p in rc["pay"] if p["client"].lower() in q), None)
    if named:
        lead = (f"{named['client']} owes you {_money(named['owed'])} on {_plural(named['invoices'], 'open invoice')} as "
                f"of {when}" + (f", {_money(named['overdue'])} of it overdue (the oldest "
                                f"{_plural(named['oldest_days_late'], 'day')} late)." if named["overdue"] else
                                (", none of it overdue." if named["owed"] else ".")))
        answer = lead + f" In all, clients owe you {_money(rc['open_total'])}" + (
            f", {_money(rc['overdue_total'])} of it overdue." if rc["overdue_total"] else ".")
        speed = ""
    elif _SPEED.search(q) and speed:
        answer = speed + " " + answer
        speed = ""
    if speed:
        answer += " " + speed
    findings: list[str] = []
    for b in rc["aging"]:
        if b["invoices"]:
            findings.append(f"{b['bucket'].capitalize()}: {_money(b['amount'])} ({_plural(b['invoices'], 'invoice')}).")
    for c in rc["by_client"][:8]:
        findings.append(f"{c['client']}: owes {_money(c['owed'])} on {_plural(c['invoices'], 'invoice')}"
                        + (f", {_money(c['overdue'])} overdue (oldest {_plural(c['oldest_days_late'], 'day')} late)"
                           if c["overdue"] else ", none overdue") + ".")
    for p in slower:
        findings.append(f"{p['client']} is paying slower: their last invoices took {p['recent_days']:.0f} days to pay, "
                        f"against {p['usual_days']:.0f} before.")
    actions = []
    for x in rc["oldest"][:3]:
        ref = f" {x['id']}" if x["id"] else ""
        actions.append(f"Send {x['client']} a reminder for invoice{ref} ({_money(x['amount'])}, "
                       f"{_plural(x['days_late'], 'day')} overdue).")
    if slower:
        actions.append(f"Talk to {slower[0]['client']} about payment terms, or ask for a deposit on the next job.")
    implications = []
    if rc["overdue_total"] and rc["open_total"]:
        share = rc["overdue_total"] / rc["open_total"]
        implications.append(f"{share:.0%} of what you are owed is already late; that is cash you have earned but cannot "
                            "use yet.")
    if rc["by_client"] and rc["open_total"]:
        c = max(rc["by_client"], key=lambda x: x["owed"])
        if c["owed"] / rc["open_total"] >= 0.5 and len(rc["by_client"]) > 1:
            implications.append(f"{c['client']} accounts for {c['owed'] / rc['open_total']:.0%} of what you are owed.")
    checks = [{"name": "numbers_backed", "passed": bool(rc["evidence"]), "severity": "error",
               "detail": "every figure comes from the recorded query of the invoices dataset"}]
    assumptions = [f"Open means no {inv['paid']}" if inv["paid"] else f"Open means a {inv['status']} that is not paid",
                   f"Days late count from {inv['due'] or inv['issued']} to {when}."]
    if inv["paid"] and inv["status"]:
        assumptions[0] += f" and a {inv['status']} that is not paid or void"
    assumptions[0] += "; void and cancelled invoices are left out."
    if not inv["due"]:
        assumptions.append(f"{inv['dataset']} has no due date, so an invoice counts as late from the day it was issued.")
    stale = rc.get("latest") and (as_of - date.fromisoformat(rc["latest"])).days > STALE_DAYS
    checks.append({"name": f"current:{inv['dataset']}", "passed": not stale, "severity": "warning",
                   "detail": f"latest invoice or payment on {rc['latest']}" + (
                       f", {(as_of - date.fromisoformat(rc['latest'])).days} days ago: payments received since then are "
                       "not in this export yet" if stale else "")})
    if stale:
        actions.append(f"Upload a fresh export of {inv['title']}: the latest activity in it is from {rc['latest']}.")
    assumptions.append("Lyst does not contact clients or send reminders; the actions are for you to take.")
    tables = [{"title": f"Owed by client, as of {when}", "columns": ["client", "owed", "overdue", "open invoices",
                                                                      "oldest days late"],
               "rows": [[c["client"], round(c["owed"], 2), round(c["overdue"], 2), c["invoices"], c["oldest_days_late"]]
                        for c in rc["by_client"]]},
              {"title": "Aging", "columns": ["bucket", "amount", "invoices"],
               "rows": [[b["bucket"], round(b["amount"], 2), b["invoices"]] for b in rc["aging"]]}]
    if rc["oldest"]:
        tables.append({"title": "Overdue invoices, oldest first", "columns": ["invoice", "client", "amount", "due", "days late"],
                       "rows": [[x["id"], x["client"], round(x["amount"], 2), x["due"], x["days_late"]] for x in rc["oldest"]]})
    if rc["pay"]:
        tables.append({"title": "Days from invoice to payment", "columns": ["client", "paid invoices", "median days",
                                                                            "last 3 invoices", "before"],
                       "rows": [[p["client"], p["paid_invoices"], p["median_days"], p["recent_days"], p["usual_days"]]
                                for p in rc["pay"]]})
    charts = [{"type": "bar", "title": "Owed by client", "x": [c["client"] for c in rc["by_client"][:8]],
               "y": [round(c["owed"], 2) for c in rc["by_client"][:8]], "format": "currency"}] if rc["by_client"] else []
    out = brief_mod._shape(u["question"], u, answer, findings, implications, actions, tables, charts, checks,
                           {"evidence": rc["evidence"]}, status="complete", assumptions=assumptions)
    out["mode"] = "receivables"
    out["supporting"]["plan"] = [{"capability": "receivables", "agent": "business_analyst"}]
    out["supporting"]["workflow"]["strategy"] = "receivables"
    out["supporting"]["brief"] = None
    out["supporting"]["receivables"] = {k: rc[k] for k in ("as_of", "open_total", "overdue_total", "aging", "by_client")}
    return out


__all__ = ["ASKS", "asks_for_receivables", "find_invoices", "receivables", "result", "understanding"]
