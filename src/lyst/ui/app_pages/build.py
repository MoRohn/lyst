import pandas as pd
import streamlit as st

from lyst.ui.client import client

api = client()
st.markdown("## Build")
st.caption("The capabilities Lyst can plan with. New capabilities arrive as reviewed skills: proposed, tested in a "
           "sandbox, approved by an owner who did not propose them, then registered. Skill authoring is the next "
           "build phase; this view lists what is registered today.")
caps = pd.DataFrame(api.get("/v1/capabilities"))
st.dataframe(caps, hide_index=True, width="stretch",
             column_config={"mutating": st.column_config.CheckboxColumn(),
                            "requires_approval": st.column_config.CheckboxColumn()})
