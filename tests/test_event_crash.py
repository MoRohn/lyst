# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Rohn Springfield
"""Scenario I: the dispatcher process dies in the middle of handling an event. A real process is killed (os._exit,
no clean shutdown, no finally blocks); a fresh process on the same home must redeliver the event and act on it
exactly once."""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from lyst.app import Lyst
from lyst.config import load_settings

_SETUP = textwrap.dedent("""
    import os, sys
    from lyst import demo
    from lyst.app import Lyst
    from lyst.autonomy import TEMPLATES
    from lyst.config import load_settings
    sys.path.insert(0, {tests!r})
    from test_autonomy import _reingest

    lyst = Lyst.build(load_settings({home!r}))
    owner = lyst.guard.create_user("avery@northwind.test", "Avery")
    demo.seed(lyst, owner)
    r = lyst.autonomy.save(owner, "northwind", TEMPLATES["schema_change"])
    lyst.autonomy.activate(owner, "northwind", r["id"])
    lyst.events.dispatch()  # every consumer's cursor now sits at the end of the stream
    c = lyst.events.consumers["autonomy.rules"]
    inner = c.handler

    def dying(ev):
        if ev["type"] == "dataset.schema_changed":
            if {when!r} == "before":
                os._exit(9)
            inner(ev)  # the activity is written ...
            os._exit(9)  # ... and the process dies before the inbox records it or the cursor moves
        return inner(ev)

    c.handler = dying
    _reingest(lyst, owner, "pnl", extra_col="channel")
    lyst.events.dispatch()
    print("the process was expected to die", flush=True)
""")


@pytest.mark.parametrize("when", ["before", "after"])
def test_a_dispatcher_killed_mid_event_redelivers_it_and_acts_once(tmp_path, monkeypatch, when):
    home = tmp_path / "home"
    script = _SETUP.format(home=str(home), when=when, tests=str(Path(__file__).parent))
    env = os.environ | {"LYST_MASTER_KEY": "ab" * 32, "LYST_KEYRING": "0", "LYST_CATALOG_AUTO": "0",
                        "LYST_SECRETS_DIR": str(tmp_path / "no-secrets")}
    for k in ("LYST_LLM_BASE_URL", "LYST_LLM_MODEL", "LYST_LLM_EXTERNAL", "LYST_OPENROUTER_API_KEY"):
        env.pop(k, None)
    p = subprocess.run([sys.executable, "-c", script], env=env, capture_output=True, text=True, timeout=240)
    assert p.returncode == 9, p.stdout + p.stderr  # it really died, inside the handler

    for k in ("LYST_MASTER_KEY", "LYST_KEYRING", "LYST_CATALOG_AUTO", "LYST_SECRETS_DIR"):
        monkeypatch.setenv(k, env[k])
    lyst = Lyst.build(load_settings(home))  # the restarted process
    try:
        ctl = lyst.stores.control
        seq = ctl.scalar("SELECT seq FROM platform_events WHERE type = 'dataset.schema_changed' "
                         "AND subject_id = 'pnl'")
        if seq is None:  # the event may still be in the workspace outbox if the process died before publishing
            lyst.events.collect()
            seq = ctl.scalar("SELECT seq FROM platform_events WHERE type = 'dataset.schema_changed' "
                             "AND subject_id = 'pnl'")
        assert seq is not None
        ws = lyst.stores.workspace("northwind")
        before = ws.scalar("SELECT count(*) FROM activities WHERE subject_kind = 'dataset' AND subject_id = 'pnl'")
        assert before == (1 if when == "after" else 0)  # the crash came after the side effect, or before it
        assert ctl.scalar("SELECT position FROM event_consumers WHERE name = 'autonomy.rules'") < seq  # not yet handled
        for _ in range(3):
            lyst.events.dispatch()
        acts = ws.query("SELECT status FROM activities WHERE subject_kind = 'dataset' AND subject_id = 'pnl'")
        assert len(acts) == 1, acts  # redelivered, acted on exactly once, whether the crash came before or after
        assert acts[0]["status"] == "completed"
        inbox = ctl.query("SELECT status, attempts FROM event_inbox WHERE consumer = 'autonomy.rules' AND seq = ?", [seq])
        assert inbox and inbox[0]["status"] == "done"
        assert ctl.scalar("SELECT position FROM event_consumers WHERE name = 'autonomy.rules'") >= seq
    finally:
        lyst.close()
