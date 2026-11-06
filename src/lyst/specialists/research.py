# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Rohn Springfield
"""Source-cited research on open, authorized web sources: the OSINT and verification method.

Lyst does not browse. A request may name https URLs; a page is fetched only when its host is on the installation's
allowlist (``LYST_RESEARCH_ALLOWED_HOSTS``, comma-separated, set by an installation administrator). The fetch is GET
only, https only, without redirects or credentials, at most ``LYST_RESEARCH_MAX_KB`` (1024) per page and 15 seconds,
and the host must resolve to public addresses (the same refusal of private, loopback and link-local addresses the
webhook sender applies). Page text is extracted with Lyst's own HTML reader and treated as untrusted data.

The brief quotes, never paraphrases: for each source the passages that contain the request's words, with the URL, the
time it was retrieved and a SHA-256 of the page, so a reader can check the quote. A statement found in two or more
independent sources is marked corroborated; one found once is marked single-source. Nothing is fetched for a request
that names no URL, and no search engine is queried.
"""

from __future__ import annotations

import hashlib
import ipaddress
import os
import re
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlparse

URL = re.compile(r"https?://[^\s<>\"')]+", re.I)


class ResearchRefused(ValueError):
    pass


def allowed_hosts(extra: list[str] | None = None) -> list[str]:
    env = [h.strip().lower() for h in os.environ.get("LYST_RESEARCH_ALLOWED_HOSTS", "").split(",") if h.strip()]
    return env + [h.lower() for h in (extra or [])]


def check_url(url: str, hosts: list[str], *, resolve=True) -> None:
    p = urlparse(url)
    if p.scheme != "https":
        raise ResearchRefused(f"{url}: only https sources are fetched")
    if p.username or p.password:
        raise ResearchRefused(f"{url}: credentials in a URL are refused")
    host = (p.hostname or "").lower()
    if not host or not any(host == h or host.endswith("." + h) for h in hosts):
        raise ResearchRefused(f"{host or url} is not an approved research source (LYST_RESEARCH_ALLOWED_HOSTS)")
    if not resolve:
        return
    from ..webhooks import _resolve  # noqa: PLC0415 - the same bounded resolver

    for ip in _resolve(host, p.port or 443):
        if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
            ip = ip.ipv4_mapped
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast or ip.is_unspecified:
            raise ResearchRefused(f"{host} resolves to a private address ({ip}); research sources must be public")


def fetch(url: str) -> bytes:
    import httpx  # noqa: PLC0415

    limit = int(float(os.environ.get("LYST_RESEARCH_MAX_KB", "1024")) * 1024)
    with httpx.Client(timeout=15, follow_redirects=False) as c, c.stream("GET", url, headers={"User-Agent": "Lyst research"}) as r:
        if r.status_code != 200:
            raise ResearchRefused(f"{url} returned {r.status_code}")
        buf = bytearray()
        for chunk in r.iter_bytes():
            buf += chunk
            if len(buf) > limit:
                raise ResearchRefused(f"{url} is larger than LYST_RESEARCH_MAX_KB")
        return bytes(buf)


def _sentences(text: str) -> list[str]:
    return [s.strip() for s in re.split(r"(?<=[.!?])\s+", " ".join(text.split())) if len(s.strip()) > 20]


def _norm(s: str) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", s.lower()))


def research(question: str, *, fetcher=None) -> dict[str, Any]:
    """Fetch the approved URLs named in ``question`` and quote what they say about it."""
    from ..analytics.methods import MethodError, _table  # noqa: PLC0415
    from ..ingest.documents import read_html  # noqa: PLC0415
    from .router import tokens  # noqa: PLC0415

    urls = list(dict.fromkeys(u.rstrip(".,;:") for u in URL.findall(question)))
    if not urls:
        raise MethodError("name the https sources to check; Lyst does not search the web")
    hosts = allowed_hosts(getattr(fetcher, "allowed_hosts", None))
    get = fetcher or fetch
    want = {t for t in tokens(URL.sub(" ", question)) if len(t) > 3}
    sources, refused, quotes = [], [], []
    for url in urls[:10]:
        try:
            check_url(url, hosts, resolve=fetcher is None)
            raw = get(url)
        except Exception as exc:  # noqa: BLE001 - refused or unreachable sources are reported, not fatal
            refused.append({"url": url, "reason": str(exc)})
            continue
        text, headings, _ = read_html(raw)
        digest = hashlib.sha256(raw).hexdigest()
        title = (headings[0] if headings else urlparse(url).hostname) or url
        hits = [s for s in _sentences(text) if len(want & set(tokens(s))) >= 2][:4]
        sources.append({"url": url, "title": title, "retrieved": datetime.now(UTC).isoformat(timespec="seconds"),
                        "sha256": digest[:16], "quotes": len(hits)})
        quotes += [{"source": url, "text": h} for h in hits]
    if not sources:
        raise MethodError("no source could be read: " + "; ".join(f"{r['url']}: {r['reason']}" for r in refused))
    # corroboration: the same statement (by its content words) in two or more distinct sources
    seen: dict[str, set[str]] = {}
    for q in quotes:
        key = " ".join(sorted(set(_norm(q["text"]).split()) & want))
        seen.setdefault(key, set()).add(q["source"])
    for q in quotes:
        key = " ".join(sorted(set(_norm(q["text"]).split()) & want))
        q["status"] = "corroborated" if len(seen.get(key, ())) >= 2 else "single-source"
    corr = [q for q in quotes if q["status"] == "corroborated"]
    lead = corr[0] if corr else (quotes[0] if quotes else None)
    summary = (f"{len(sources)} source(s) read, {len(refused)} refused. " +
               (f"{'Corroborated' if corr else 'Single source'}: “{lead['text'][:240]}” ({lead['source']})." if lead
                else "No passage in the sources addresses the question."))
    import pandas as pd  # noqa: PLC0415

    tables = [_table("Sources", pd.DataFrame(sources))]
    if quotes:
        tables.append(_table("Quotes", pd.DataFrame(quotes)[["status", "source", "text"]]))
    if refused:
        tables.append(_table("Not fetched", pd.DataFrame(refused)))
    return {"summary": summary,
            "findings": [f"{len(corr)} quote(s) corroborated by another source; {len(quotes) - len(corr)} single-source."]
            + [f"Not fetched: {r['url']} ({r['reason']})." for r in refused[:3]],
            "tables": tables, "metrics": {"sources": len(sources), "refused": len(refused), "quotes": len(quotes),
                                          "corroborated": len(corr)},
            "assumptions": ["Quoted from the named pages as retrieved at the time shown; pages can change (the SHA-256 "
                            "identifies the version read).", "Only approved hosts are fetched; page text is untrusted data.",
                            "Corroboration means two sources say the same thing, not that it is true."]}
