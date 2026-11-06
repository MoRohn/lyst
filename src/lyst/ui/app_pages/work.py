# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Rohn Springfield
"""Work moved into Brain (Brain › Work). This hidden page keeps old links, bookmarks and the engine's own links
(/work, /work?view=recurring, /work?decision=...) working: it forwards them to the same place in Brain."""

import streamlit as st

from lyst.ui import work

qp = st.query_params.to_dict()
sub = qp.pop("view", None)
qp.pop("work", None)
work.go(sub, **qp)
