# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Rohn Springfield
"""U1: the interface lent its shared token (LYST_TOKEN or the dev-token file) to every browser that reached it.
`lyst ui` listened on every interface (Streamlit's default), so anyone on the network was signed in as the owner."""

from lyst.cli import ui_address
from lyst.ui.client import shared_identity_allowed


def test_the_shared_token_is_lent_only_to_a_browser_on_the_same_machine_of_a_loopback_interface():
    local = {"LYST_UI_LOCAL": "1"}
    assert shared_identity_allowed("127.0.0.1", local) and shared_identity_allowed("::1", local)
    assert not shared_identity_allowed("192.168.68.20", local)          # another machine on the LAN
    assert shared_identity_allowed(None, local)                         # Streamlit's way of saying loopback
    assert not shared_identity_allowed(None, local, proxied=True)       # a proxy on this machine forwarded it
    assert not shared_identity_allowed("127.0.0.1", {"LYST_UI_LOCAL": "0"})  # bound to the network (a local proxy)
    assert not shared_identity_allowed("127.0.0.1", {})


def test_lyst_ui_listens_on_localhost_unless_a_deployment_asks_for_the_network():
    assert ui_address({}) == ("localhost", True)
    assert ui_address({"STREAMLIT_SERVER_ADDRESS": "0.0.0.0"}) == ("0.0.0.0", False)  # noqa: S104 - Compose
