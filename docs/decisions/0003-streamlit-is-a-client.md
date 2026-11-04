# 0003. Streamlit is a client of the API

**Decision.** The UI calls FastAPI over HTTP and never opens a database. Long-running work belongs to the
engine's runtime, which survives browser sessions and resumes from checkpoints.

**Why.** Streamlit reruns scripts per interaction and loses session state with the tab. Work that matters
cannot live there.
