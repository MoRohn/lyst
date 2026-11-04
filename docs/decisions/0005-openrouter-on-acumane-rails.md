# 0005. OpenRouter on Acumane's rails

**Decision (Rohn, 2026-11-04).** Lyst uses OpenRouter for inference, through a Python port of Acumane's
gateway (pools, admission, adaptive limits, retries, workload routing, tiers). Local OpenAI-compatible
models stay supported as the alternative.

**Fit with the open-source policy.** OpenRouter is an external service the operator chooses by setting a
key; the core still installs and runs without it (rules and templates, or a local model). Workspace owners
can switch external providers off, and models never receive dataset rows.
