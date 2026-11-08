# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Rohn Springfield
"""Lyst Apps: small interactive apps generated from a sentence, where every number is a governed one.

An app is a **spec**, never code: a title and up to ``MAX_BLOCKS`` blocks from a closed catalog (:data:`CATALOG`):

* ``kpi``: a metric's latest value and its change against the prior period;
* ``trend``: a metric over its last ``periods`` periods;
* ``breakdown``: a metric in the latest period, split by one of its dimensions;
* ``table``: a metric by period and dimension;
* ``filter``: one dimension the person can narrow the app to (one value, or all);
* ``whatif``: a slider that moves a metric's latest value by a percentage, and shows the result as a projection;
* ``note``: a short text.

Blocks bind to approved metrics, their dimensions and a time grain, nothing else. A model never supplies a number: the
text it writes (titles, notes) may not hold figures (only years and quarter names), and every value is resolved here
by the semantic layer's compiled SQL through the guard (``data.query``), as for Studio dashboards. A filter narrows
the blocks whose metric has its dimension, with values the same SQL computed per dimension value, so a ratio is never
re-aggregated by the client.

**Generating** (:meth:`AppService.generate`) streams: the model writes the spec as JSON, and each block is checked and
resolved as soon as it is complete (:class:`SpecReader`), so an app appears block by block while it is written. A block
that fails a check is left out and the reason is reported. Without a model, or when the model's spec has no usable
block, the rules build one from the metrics the request names. ``generate`` reports to an ``emit`` callback; the API
writes those events as an AI SDK UI message stream (``lyst.api.apps``).

**Saving** keeps each version (``app_versions``, workspace migration 130) and registers the app as an asset (kind
``app``) whose dependencies are its metric definitions and the datasets they read. An app reads current data whenever
it is opened, so a new dataset version does not make it stale; a metric redefined or retired after it was saved does
(the asset scan), and saving it again re-checks every binding.
"""

from __future__ import annotations

import json
import re
import uuid
from collections.abc import Callable
from typing import Any, Literal

from pydantic import BaseModel, Field, ValidationError, field_validator

from .security import Guard, Principal
from .semantic import MetricSpec, compile_metric
from .store import Stores

MAX_BLOCKS = 24
GRAINS = ("day", "week", "month", "quarter", "year")
_ID = re.compile(r"^[a-z][a-z0-9_-]{0,39}$")
# what model-written text may hold that looks like a figure: years and period names, never a value
_ALLOWED_FIGURES = re.compile(r"\b(?:(?:19|20)\d{2}|FY\d{2}(?:\d{2})?|[QH][1-4])\b")

CATALOG: dict[str, dict[str, Any]] = {
    "kpi": {"binds": ("metric",), "options": {"grain": "month"},
            "does": "the metric's latest value and its change against the prior period"},
    "trend": {"binds": ("metric",), "options": {"grain": "month", "periods": 12},
              "does": "the metric over its last `periods` periods (2 to 60)"},
    "breakdown": {"binds": ("metric", "dimension"), "options": {"grain": "month"},
                  "does": "the metric in the latest period, split by `dimension`"},
    "table": {"binds": ("metric", "dimension"), "options": {"grain": "month", "periods": 6},
              "does": "the metric by period (the last `periods`, 1 to 24) and `dimension`"},
    "filter": {"binds": ("dimension",), "options": {},
               "does": "lets the person narrow every block whose metric has `dimension` to one value; at most one per app"},
    "whatif": {"binds": ("metric",), "options": {"grain": "month", "min_pct": -20, "max_pct": 20},
               "does": "a slider moving the metric's latest value by min_pct..max_pct percent (a projection, not data)"},
    "note": {"binds": (), "options": {}, "does": "a short text (`text`, no figures)"},
}


class AppInvalid(ValueError):
    """A spec, or a block of one, that breaks a rule; the message says which."""


def _no_figures(text: str, what: str) -> str:
    text = " ".join(text.split())
    if re.search(r"\d", _ALLOWED_FIGURES.sub("", text)):
        raise AppInvalid(f"{what} may not contain figures; every number comes from a governed metric")
    return text


class Block(BaseModel):
    id: str
    type: Literal["kpi", "trend", "breakdown", "table", "filter", "whatif", "note"]
    title: str | None = Field(None, max_length=80)
    metric: str | None = None
    dimension: str | None = None
    grain: Literal["day", "week", "month", "quarter", "year"] | None = None
    periods: int | None = None
    min_pct: float | None = None
    max_pct: float | None = None
    text: str | None = Field(None, max_length=600)

    model_config = {"extra": "ignore"}

    @field_validator("id")
    @classmethod
    def _ident(cls, v: str) -> str:
        if not _ID.match(v):
            raise ValueError("a block id is lowercase letters, digits, - and _, starting with a letter")
        return v


class AppSpec(BaseModel):
    title: str = Field(min_length=1, max_length=120)
    subtitle: str | None = Field(None, max_length=240)
    blocks: list[Block] = Field(min_length=1, max_length=MAX_BLOCKS)


class Checker:
    """Checks blocks against the workspace's approved metrics, one at a time, as a stream delivers them."""

    def __init__(self, metrics: dict[str, MetricSpec]) -> None:
        self.metrics = metrics
        self.ids: set[str] = set()
        self.filter: str | None = None

    def title(self, title: Any, subtitle: Any = None) -> tuple[str, str | None]:
        if not isinstance(title, str) or not title.strip():
            raise AppInvalid("an app needs a title")
        t = _no_figures(title, "the title")[:120]
        s = _no_figures(subtitle, "the subtitle")[:240] if isinstance(subtitle, str) and subtitle.strip() else None
        return t, s

    def block(self, raw: Any) -> dict[str, Any]:
        try:
            b = Block.model_validate(raw)
        except ValidationError as exc:
            first = exc.errors()[0]
            raise AppInvalid(f"{'.'.join(str(p) for p in first['loc']) or 'block'}: {first['msg']}") from None
        if b.id in self.ids:
            raise AppInvalid(f"block id {b.id} is used twice")
        need = CATALOG[b.type]["binds"]
        out: dict[str, Any] = {"id": b.id, "type": b.type}
        if b.title:
            out["title"] = _no_figures(b.title, "a block title")
        spec = None
        if "metric" in need:
            spec = self.metrics.get(b.metric or "")
            if spec is None:
                raise AppInvalid(f"{b.metric or 'no metric'} is not an approved metric in this workspace")
            out["metric"] = spec.name
        if "dimension" in need:
            if b.type == "filter":
                if self.filter is not None:
                    raise AppInvalid("an app has at most one filter")
                if not any(b.dimension in m.dimensions for m in self.metrics.values()):
                    raise AppInvalid(f"no approved metric can be split by {b.dimension}")
            elif spec is not None and b.dimension not in spec.dimensions:
                raise AppInvalid(f"{spec.name} cannot be split by {b.dimension}; allowed: {', '.join(spec.dimensions) or 'none'}")
            out["dimension"] = b.dimension
        opts = CATALOG[b.type]["options"]
        if "grain" in opts:
            if not spec.time_column:
                raise AppInvalid(f"{spec.name} has no time column, so it has no periods")
            out["grain"] = b.grain or opts["grain"]
        if "periods" in opts:
            hi = 60 if b.type == "trend" else 24
            out["periods"] = max(2 if b.type == "trend" else 1, min(hi, int(b.periods or opts["periods"])))
        if b.type == "whatif":
            lo, hi = b.min_pct if b.min_pct is not None else -20, b.max_pct if b.max_pct is not None else 20
            lo, hi = max(-90.0, min(0.0, float(lo))), max(0.0, min(200.0, float(hi)))
            if lo == hi:
                raise AppInvalid("a what-if slider needs a range")
            out["min_pct"], out["max_pct"] = lo, hi
        if b.type == "note":
            if not (b.text or "").strip():
                raise AppInvalid("a note needs text")
            out["text"] = _no_figures(b.text, "a note")
        self.ids.add(b.id)
        if b.type == "filter":
            self.filter = b.dimension
        return out


class SpecReader:
    """Reads a spec as a model writes it: the title once it is complete, then each block once its object closes.

    ``feed`` returns the new events: ``("title", title, subtitle)`` and ``("block", raw_dict)``. It reads JSON text
    without waiting for the end of the reply, and never guesses at an unfinished value."""

    _TITLE = re.compile(r'"title"\s*:\s*("(?:[^"\\]|\\.)*")')
    _SUBTITLE = re.compile(r'"subtitle"\s*:\s*("(?:[^"\\]|\\.)*")')

    def __init__(self) -> None:
        self.reset()

    def reset(self) -> None:
        self.text = ""
        self.pos = -1  # where block scanning continues; -1 until the blocks array opened
        self.depth = 0
        self.in_string = self.escape = False
        self.start = -1
        self.head_done = False
        self.closed = False

    def feed(self, piece: str) -> list[tuple]:
        self.text += piece
        out: list[tuple] = []
        if self.pos < 0:
            m = re.search(r'"blocks"\s*:\s*\[', self.text)
            if not m:
                return out
            self.pos = m.end()
        if not self.head_done:
            head = self.text[: self.pos]
            t, s = self._TITLE.search(head), self._SUBTITLE.search(head)
            out.append(("title", json.loads(t.group(1)) if t else None, json.loads(s.group(1)) if s else None))
            self.head_done = True
        text, i = self.text, self.pos
        while i < len(text) and not self.closed:
            ch = text[i]
            if self.in_string:
                if self.escape:
                    self.escape = False
                elif ch == "\\":
                    self.escape = True
                elif ch == '"':
                    self.in_string = False
            elif ch == '"':
                self.in_string = True
            elif ch in "{[":
                if self.depth == 0 and ch == "{":
                    self.start = i
                self.depth += 1
            elif ch in "}]":
                if self.depth == 0 and ch == "]":
                    self.closed = True
                else:
                    self.depth -= 1
                    if self.depth == 0 and self.start >= 0:
                        try:
                            out.append(("block", json.loads(text[self.start: i + 1])))
                        except ValueError:
                            out.append(("block", None))
                        self.start = -1
            i += 1
        self.pos = i
        return out

    def tail(self) -> tuple[str | None, str | None]:
        """A title written after the blocks (the model was asked to write it first, but it is still read)."""
        try:
            whole = json.loads(self.text[self.text.find("{"): self.text.rfind("}") + 1])
        except ValueError:
            return None, None
        return whole.get("title"), whole.get("subtitle")


class Resolver:
    """Every value of an app, from the semantic layer's compiled SQL through the guard. Queries are shared between
    blocks; a filter adds the per-value series its blocks need."""

    def __init__(self, svc: AppService, actor: Principal, ws: str, metrics: dict[str, MetricSpec], *,
                 task_id: str | None = None) -> None:
        from .onboarding import fiscal_start  # noqa: PLC0415

        self.svc, self.actor, self.ws, self.task_id, self.specs = svc, actor, ws, task_id, metrics
        self.fs = fiscal_start(svc.stores.workspace(ws))
        self.cache: dict[tuple, Any] = {}
        self.bound: list[str] = []  # metrics the app binds, when known before its blocks are read
        self.datasets: dict[str, int] = {}
        self.metrics: dict[str, int] = {}
        self.queries: list[int] = []
        self._dv = {d["name"]: int(d["version"]) for d in svc.data.catalog(actor, ws)}
        self._mv = {r["name"]: int(r["current_version"]) for r in
                    svc.stores.workspace(ws).query("SELECT name, current_version FROM metrics")}

    def _rows(self, spec: MetricSpec, grain: str, by: str | None = None) -> list[list[Any]]:
        key = (spec.name, grain, by)
        if key not in self.cache:
            sql = compile_metric(spec, grain=grain, by=[by] if by else None, fiscal_start=self.fs)
            r = self.svc.data.query(self.actor, self.ws, sql, task_id=self.task_id, max_rows=10_000)
            self.cache[key] = [[str(row[0])[:10], *row[1:]] for row in r.rows]
            self.queries.append(r.query_id)
            self.metrics[spec.name] = self._mv.get(spec.name, 0)
            if spec.dataset in self._dv:
                self.datasets[spec.dataset] = self._dv[spec.dataset]
        return self.cache[key]

    @staticmethod
    def _num(v: Any) -> float | None:
        return None if v is None else float(v)

    def _series(self, spec: MetricSpec, grain: str, n: int | None = None) -> list[dict[str, Any]]:
        rows = [{"period": r[0], "value": self._num(r[1])} for r in self._rows(spec, grain)]
        return rows[-n:] if n else rows

    def _by(self, spec: MetricSpec, grain: str, dim: str, n: int | None = None) -> dict[str, list[dict[str, Any]]]:
        out: dict[str, list[dict[str, Any]]] = {}
        for p, d, v in self._rows(spec, grain, dim):
            out.setdefault(str(d), []).append({"period": p, "value": self._num(v)})
        return {k: (s[-n:] if n else s) for k, s in sorted(out.items())}

    @staticmethod
    def _latest(series: list[dict[str, Any]]) -> dict[str, Any]:
        s = [p for p in series if p["value"] is not None]
        cur, prior = (s[-1] if s else None), (s[-2] if len(s) > 1 else None)
        return {"period": cur and cur["period"], "value": cur and cur["value"],
                "prior_period": prior and prior["period"], "prior": prior and prior["value"],
                "change": (cur["value"] - prior["value"]) if cur and prior else None}

    def block(self, b: dict[str, Any], filter_dim: str | None) -> dict[str, Any]:
        if b["type"] == "note":
            return {}
        if b["type"] == "filter":
            values: set[str] = set()
            # the values come from a metric the app binds when it is known (its own data), else any that has them
            bound = [self.specs[n] for n in self.bound if n in self.specs]
            spec = next((m for m in [*bound, *self.specs.values()] if b["dimension"] in m.dimensions and m.time_column), None)
            if spec is not None:
                values = {str(r[1]) for r in self._rows(spec, "month", b["dimension"])}
            return {"dimension": b["dimension"], "values": sorted(values)}
        spec = self.specs[b["metric"]]
        meta = {"name": spec.name, "title": spec.title, "format": spec.format, "unit": spec.unit, "currency": spec.currency,
                "higher_is_better": spec.higher_is_better, "version": self._mv.get(spec.name), "dataset": spec.dataset}
        grain = b["grain"]
        narrow = filter_dim if filter_dim and filter_dim in spec.dimensions else None
        data: dict[str, Any] = {"metric": meta, "grain": grain}
        if b["type"] in ("kpi", "whatif"):
            data["latest"] = self._latest(self._series(spec, grain))
            if narrow:
                data["by"] = {"dimension": narrow, "values": {k: self._latest(s) for k, s in self._by(spec, grain, narrow).items()}}
        elif b["type"] == "trend":
            data["series"] = self._series(spec, grain, b["periods"])
            if narrow:
                data["by"] = {"dimension": narrow, "values": self._by(spec, grain, narrow, b["periods"])}
        elif b["type"] in ("breakdown", "table"):
            rows = self._rows(spec, grain, b["dimension"])
            periods = sorted({r[0] for r in rows})
            keep = set(periods[-(b.get("periods") or 1):])
            data["dimension"] = b["dimension"]
            data["periods"] = sorted(keep)
            data["rows"] = [{"period": p, "key": str(d), "value": self._num(v)} for p, d, v in rows if p in keep]
            if b["type"] == "breakdown":
                data["rows"].sort(key=lambda r: (r["value"] is None, -(r["value"] or 0)))
        return data

    def sources(self) -> dict[str, Any]:
        return {"datasets": dict(self.datasets), "metrics": dict(self.metrics), "queries": list(self.queries)}


class AppService:
    def __init__(self, stores: Stores, guard: Guard, data, semantic, llm=None) -> None:
        self.stores, self.guard, self.data, self.semantic, self.llm = stores, guard, data, semantic, llm
        self.assets = None  # the asset registry (lane.attach)

    def _db(self, ws: str):
        return self.stores.workspace(ws)

    def _metrics(self, actor: Principal, ws: str) -> dict[str, MetricSpec]:
        return {m.name: m for m in self.semantic.metrics(actor, ws)}

    def _resolver(self, actor: Principal, ws: str, metrics: dict[str, MetricSpec], task_id: str | None = None) -> Resolver:
        return Resolver(self, actor, ws, metrics, task_id=task_id)

    # -- checking and resolving -------------------------------------------------------------

    def check(self, actor: Principal, ws: str, spec: dict[str, Any]) -> dict[str, Any]:
        """A whole spec, checked: the canonical spec, or :class:`AppInvalid` naming the first broken rule."""
        self.guard.require(actor, ws, "data.read")
        try:
            AppSpec.model_validate(spec)
        except ValidationError as exc:
            first = exc.errors()[0]
            raise AppInvalid(f"{'.'.join(str(p) for p in first['loc']) or 'spec'}: {first['msg']}") from None
        ck = Checker(self._metrics(actor, ws))
        title, subtitle = ck.title(spec.get("title"), spec.get("subtitle"))
        blocks = [ck.block(b) for b in spec["blocks"]]
        return {"title": title, **({"subtitle": subtitle} if subtitle else {}), "blocks": blocks}

    def resolve(self, actor: Principal, ws: str, spec: dict[str, Any], *, task_id: str | None = None) -> dict[str, Any]:
        """Every block's values, read now, and what they were read from."""
        self.guard.require(actor, ws, "data.query")
        spec = self.check(actor, ws, spec)
        r = self._resolver(actor, ws, self._metrics(actor, ws), task_id)
        r.bound = [b["metric"] for b in spec["blocks"] if b.get("metric")]
        fdim = next((b["dimension"] for b in spec["blocks"] if b["type"] == "filter"), None)
        blocks = {b["id"]: r.block(b, fdim) for b in spec["blocks"]}
        return {"spec": spec, "blocks": blocks, "sources": r.sources()}

    # -- generating -------------------------------------------------------------------------

    def inventory(self, metrics: dict[str, MetricSpec]) -> list[dict[str, Any]]:
        return [{"name": m.name, "title": m.title, "description": m.description[:200], "format": m.format,
                 "dimensions": m.dimensions, "has_periods": bool(m.time_column)} for m in metrics.values()]

    def _prompt(self, request: str, metrics: dict[str, MetricSpec]) -> list[dict[str, str]]:
        catalog = {k: {"binds": list(v["binds"]), "options": v["options"], "does": v["does"]} for k, v in CATALOG.items()}
        system = (
            "You design small interactive analytics apps for Lyst. You write a spec as one JSON object, never code: "
            '{"title": str, "subtitle": str (optional), "blocks": [{"id": str, "type": str, "title": str (optional), ...}]}. '
            "Write the title (and subtitle) first, then the blocks. Block types, their bindings and options: "
            + json.dumps(catalog) + ". Use only these metrics (bind by name; split only by a metric's own dimensions): "
            + json.dumps(self.inventory(metrics)) + ". Rules: you never supply a number; every value comes from a "
            "binding. No figures in any text you write (titles, notes); years and quarter names are fine. Block ids are "
            f"short lowercase words. At most {MAX_BLOCKS} blocks; four to eight is usually right: KPIs first, then trends "
            "and breakdowns, a filter when the request names a dimension to compare. Answer with the JSON object only.")
        return [{"role": "system", "content": system}, {"role": "user", "content": request[:2000]}]

    def rules_spec(self, request: str, metrics: dict[str, MetricSpec], resolved: list[str]) -> dict[str, Any]:
        """An app from the request without a model: the metrics it names (or the first few), a dimension it names."""
        names = [n for n in resolved if n in metrics] or [n for n, m in metrics.items() if m.time_column][:3]
        if not names:
            raise AppInvalid("this workspace has no approved metric with periods to build an app on")
        specs = [metrics[n] for n in names[:4]]
        words = set(re.findall(r"[a-z_]+", request.lower()))
        dims = [d for d in specs[0].dimensions if d in words or d.replace("_", " ") in request.lower() or d + "s" in words]
        dim = dims[0] if dims else (specs[0].dimensions[0] if specs[0].dimensions else None)
        blocks: list[dict[str, Any]] = [{"id": f"kpi-{s.name}"[:40].replace("_", "-"), "type": "kpi", "metric": s.name}
                                        for s in specs if s.time_column]
        if dim and sum(dim in s.dimensions for s in specs) >= 1:
            blocks.append({"id": "filter", "type": "filter", "dimension": dim})
        lead = specs[0]
        if lead.time_column:
            blocks.append({"id": "trend", "type": "trend", "metric": lead.name, "periods": 12})
            if dim in lead.dimensions:
                blocks.append({"id": "breakdown", "type": "breakdown", "metric": lead.name, "dimension": dim})
                blocks.append({"id": "table", "type": "table", "metric": lead.name, "dimension": dim, "periods": 6})
            blocks.append({"id": "whatif", "type": "whatif", "metric": lead.name, "min_pct": -20, "max_pct": 20})
        title = " and ".join(s.title for s in specs[:2]) + (f" by {dim.replace('_', ' ')}" if dim else "")
        return {"title": title, "blocks": blocks}

    def generate(self, actor: Principal, ws: str, request: str, *, emit: Callable[[dict[str, Any]], None],
                 save: bool = True, prompt_id: str | None = None) -> dict[str, Any]:
        """Generate an app for ``request``, block by block. ``emit`` gets ``{"type": "title"|"block"|"dropped"|"reset"}``
        events as they happen (``"update"``: a block shown earlier has new values, when a filter arrives after it); the
        return value is the checked spec, its data and sources, and the saved app (when the
        person may save: editor and above)."""
        self.guard.require(actor, ws, "analyst.ask")
        self.guard.require(actor, ws, "data.query")
        metrics = self._metrics(actor, ws)
        state: dict[str, Any] = {}

        def begin() -> None:
            state.update(checker=Checker(metrics), resolver=self._resolver(actor, ws, metrics), blocks=[], data={},
                         dropped=[], title=None, subtitle=None, filter=None, pending=[])

        def on_title(title: Any, subtitle: Any) -> None:
            try:
                state["title"], state["subtitle"] = state["checker"].title(title, subtitle)
            except AppInvalid as exc:
                state["dropped"].append({"block": "title", "reason": str(exc)})
                emit({"type": "dropped", "block": "title", "reason": str(exc)})
                return
            emit({"type": "title", "title": state["title"], "subtitle": state["subtitle"]})

        def on_block(raw: Any) -> None:
            if len(state["blocks"]) >= MAX_BLOCKS:
                return
            bid = raw.get("id") if isinstance(raw, dict) else None
            try:
                if raw is None:
                    raise AppInvalid("the block is not valid JSON")
                b = state["checker"].block(raw)
            except AppInvalid as exc:
                state["dropped"].append({"block": bid, "reason": str(exc)})
                emit({"type": "dropped", "block": bid, "reason": str(exc)})
                return
            if b["type"] == "filter":
                state["filter"] = b["dimension"]
            try:
                d = state["resolver"].block(b, state["filter"])
            except Exception as exc:  # noqa: BLE001 - a binding that does not run is left out, with why
                state["checker"].ids.discard(b["id"])
                state["dropped"].append({"block": b["id"], "reason": f"its data could not be read: {exc}"[:300]})
                emit({"type": "dropped", "block": b["id"], "reason": state["dropped"][-1]["reason"]})
                return
            state["blocks"].append(b)
            state["data"][b["id"]] = d
            emit({"type": "block", "block": b, "data": d})
            if b["type"] == "filter":  # blocks shown before the filter gain the values it narrows them to
                for prev in state["blocks"][:-1]:
                    spec = metrics.get(prev.get("metric") or "")
                    if spec is None or b["dimension"] not in spec.dimensions or prev["type"] not in ("kpi", "whatif", "trend"):
                        continue
                    state["data"][prev["id"]] = state["resolver"].block(prev, state["filter"])
                    emit({"type": "update", "block": prev, "data": state["data"][prev["id"]]})

        reader = SpecReader()

        def on_delta(piece: str | None) -> None:
            if piece is None:  # the gateway retries or moves to another model: what was shown goes
                reader.reset()
                begin()
                emit({"type": "reset"})
                return
            for ev in reader.feed(piece):
                (on_title(ev[1], ev[2]) if ev[0] == "title" else on_block(ev[1]))

        begin()
        source = "rules"
        if self.llm is not None and self.llm.available_for(ws) and metrics:
            try:
                self.llm.chat(ws, self._prompt(request, metrics), purpose="app.generate", json_mode=True, stream_json=True,
                              on_delta=on_delta, workload="structured", max_tokens=3000)
                if state["title"] is None:
                    t, s = reader.tail()
                    if t:
                        on_title(t, s)
                source = "model" if state["blocks"] else "rules"
            except Exception as exc:  # noqa: BLE001 - the rules still build an app; the reason is reported
                state.setdefault("model_error", str(exc)[:300])
        if source == "rules":
            model_dropped = state["dropped"]
            if state["blocks"] or state["title"]:
                emit({"type": "reset"})
            begin()
            state["dropped"] = model_dropped
            resolved = self.semantic.resolve(actor, ws, request).get("metrics", [])
            spec = self.rules_spec(request, metrics, resolved)
            on_title(spec["title"], None)
            for b in spec["blocks"]:
                on_block(b)
        if not state["blocks"]:
            raise AppInvalid("no block of the app could be built: " + "; ".join(d["reason"] for d in state["dropped"][:3]))
        if state["title"] is None:
            state["title"] = " and ".join(metrics[b["metric"]].title for b in state["blocks"] if b.get("metric"))[:120] or "App"
            emit({"type": "title", "title": state["title"], "subtitle": None})
        spec = {"title": state["title"], **({"subtitle": state["subtitle"]} if state["subtitle"] else {}),
                "blocks": state["blocks"]}
        out = {"spec": spec, "blocks": state["data"], "sources": state["resolver"].sources(), "source": source,
               "dropped": state["dropped"], "app": None}
        if save and self.guard.can(actor, ws, "data.ingest"):
            out["app"] = self._save(actor, ws, spec, out["sources"], prompt=request, note=f"generated ({source})",
                                    prompt_id=prompt_id)
        self.guard.audit(actor, ws, "app.generate", (out["app"] or {}).get("app_id") or "-", "ok",
                         {"source": source, "blocks": len(spec["blocks"]), "dropped": len(state["dropped"])})
        return out

    # -- saving and reading -----------------------------------------------------------------

    def save(self, actor: Principal, ws: str, spec: dict[str, Any], *, app_id: str | None = None,
             note: str = "") -> dict[str, Any]:
        """Save a spec (a new app, or a new version of ``app_id``): checked and resolved first, so a saved app runs."""
        self.guard.require(actor, ws, "data.ingest", app_id)
        res = self.resolve(actor, ws, spec)
        return self._save(actor, ws, res["spec"], res["sources"], app_id=app_id, note=note)

    def _save(self, actor: Principal, ws: str, spec: dict[str, Any], sources: dict[str, Any], *, app_id: str | None = None,
              prompt: str | None = None, note: str = "", prompt_id: str | None = None) -> dict[str, Any]:
        db = self._db(ws)
        if prompt_id and app_id is None:  # one app per generation request: a retried request finds it
            found = db.query("SELECT id FROM apps WHERE prompt_id = ?", [prompt_id])
            if found:
                row = db.query("SELECT id, version FROM apps WHERE id = ?", [found[0]["id"]])[0]
                return {"app_id": row["id"], "version": row["version"], "asset": None, "resumed": True}
        depends = {"datasets": sources.get("datasets", {}), "metrics": sources.get("metrics", {})}

        def tx(c):
            if app_id:
                row = c.execute("SELECT version, status FROM apps WHERE id = ?", [app_id]).fetchone()
                if not row:
                    raise KeyError(app_id)
                if row[1] == "retired":
                    raise AppInvalid("this app is retired")
                version = row[0] + 1
                c.execute("UPDATE apps SET title = ?, spec = ?, version = ?, updated_at = now() WHERE id = ?",
                          [spec["title"], json.dumps(spec), version, app_id])
                aid = app_id
            else:
                aid, version = "app_" + uuid.uuid4().hex[:12], 1
                c.execute("INSERT INTO apps (id, title, spec, version, status, prompt, prompt_id, created_by) "
                          "VALUES (?, ?, ?, 1, 'active', ?, ?, ?)",
                          [aid, spec["title"], json.dumps(spec), (prompt or "")[:2000] or None, prompt_id, actor.user_id])
            c.execute("INSERT INTO app_versions (app_id, version, spec, depends, note, created_by) VALUES (?, ?, ?, ?, ?, ?)",
                      [aid, version, json.dumps(spec), json.dumps(depends), note[:300], actor.user_id])
            return aid, version

        aid, version = db.write(tx)
        self.guard.audit(actor, ws, "app.save", aid, "ok", {"version": version, "blocks": len(spec["blocks"])})
        asset = None
        if self.assets is not None:
            asset = self.assets.on_app(actor, ws, aid, spec["title"], depends, question=prompt, note=note)
        return {"app_id": aid, "version": version, "asset": asset}

    def list(self, actor: Principal, ws: str, *, include_retired: bool = False) -> list[dict[str, Any]]:
        self.guard.require(actor, ws, "workspace.read")
        rows = self._db(ws).query("SELECT id, title, version, status, created_by, created_at, updated_at FROM apps "
                                  + ("" if include_retired else "WHERE status = 'active' ") + "ORDER BY updated_at DESC")
        return rows

    def get(self, actor: Principal, ws: str, app_id: str, *, version: int | None = None) -> dict[str, Any]:
        self.guard.require(actor, ws, "workspace.read", app_id)
        rows = self._db(ws).query("SELECT id, title, version, status, prompt, created_by, created_at, updated_at FROM apps "
                                  "WHERE id = ?", [app_id])
        if not rows:
            raise KeyError(app_id)
        app = rows[0]
        v = version or app["version"]
        ver = self._db(ws).query("SELECT version, spec, depends, note, created_by, created_at FROM app_versions "
                                 "WHERE app_id = ? AND version = ?", [app_id, v])
        if not ver:
            raise KeyError(f"{app_id} v{v}")
        app["spec"] = json.loads(ver[0]["spec"])
        app["depends"] = json.loads(ver[0]["depends"])
        app["versions"] = [r["version"] for r in self._db(ws).query(
            "SELECT version FROM app_versions WHERE app_id = ? ORDER BY version DESC", [app_id])]
        return app

    def open(self, actor: Principal, ws: str, app_id: str) -> dict[str, Any]:
        """The app's current spec with every value read now (an app reads current data whenever it is opened)."""
        app = self.get(actor, ws, app_id)
        if app["status"] == "retired":
            raise AppInvalid("this app is retired")
        res = self.resolve(actor, ws, app["spec"])
        return {"app_id": app_id, "version": app["version"], "title": app["title"], **res}

    def retire(self, actor: Principal, ws: str, app_id: str) -> dict[str, Any]:
        self.guard.require(actor, ws, "task.control", app_id)
        if not self._db(ws).query("SELECT id FROM apps WHERE id = ?", [app_id]):
            raise KeyError(app_id)
        self._db(ws).execute("UPDATE apps SET status = 'retired', updated_at = now() WHERE id = ?", [app_id])
        self.guard.audit(actor, ws, "app.retire", app_id, "ok")
        return {"app_id": app_id, "status": "retired"}


__all__ = ["CATALOG", "MAX_BLOCKS", "AppInvalid", "AppService", "AppSpec", "Checker", "SpecReader"]
