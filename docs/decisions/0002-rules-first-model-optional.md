# 0002. Rules first, model optional, numbers never from a model

**Decision.** Planning and answer text work without a model. A configured model may pick among approved
metric names and reword findings; any number in its text that no tool produced is rejected.

**Why.** The core must run on open source with no inference service, and an analyst's numbers must be
reproducible. Ported from Acumane's `ruleIntent` and `unbackedClaims`.
