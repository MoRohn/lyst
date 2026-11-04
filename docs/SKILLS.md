# Governed skills

A skill is how Lyst gains a capability it did not ship with, without anyone editing Lyst. It is a Python
function `run(df, **params) -> dict` with a purpose, a governed input query, trigger phrases and tests.
Scenario F (`tests/test_skills.py::test_f_propose_test_approve_register_and_use`) walks the whole
lifecycle.

## Lifecycle

| Step | Who | What happens |
|---|---|---|
| Propose | editor or above | `static_check` parses the source and refuses imports outside the allow-list (math, statistics, numpy, pandas, scipy, decimal, …), file and process access, dunder attributes, pandas readers and writers, and global state. The input query must pass the SQL guard for the proposer's role. Stored as `proposed` (or `rejected`). |
| Test | editor or above | Runs in the sandbox: the author's cases (expected output compared with tolerance) plus generated adversarial cases (empty input, all nulls, a single row), which must return a result or a clear error. Stored as `tested` or `failed` with the report. |
| Request registration | proposer | Only a `tested` version. Opens a `skill.register` approval. |
| Approve | owner, not the proposer | `TaskRuntime.decide` requires `skill.approve` and refuses the proposer. Earlier approved versions become `superseded`. |
| Use | anyone who may query | When a request contains a trigger phrase, the planner adds `run_skill`: the input query runs through the guard as the asking user (roles and column policies apply), the frame goes to the sandbox, and the result becomes the answer with the query as evidence. |
| Retire | owner | Takes the skill out of plans; versions stay for audit. |

## The sandbox

`lyst.skills.runner` runs in a separate `python -I` process with an empty environment, a temporary
working directory, CPU time and file-size limits (`RLIMIT_CPU`, `RLIMIT_FSIZE=0`), a wall-clock timeout,
an import hook that admits only the allow-list, and builtins without `open`, `exec`, `eval`, `compile`,
`input` or `__import__`. The skill receives a DataFrame and returns JSON.

What this is not: a kernel sandbox. A determined author could still find a path through a C extension's
own file access. For hostile multi-tenant use, run the engine itself in a container with no network and a
read-only filesystem (the Compose file does this), and keep `skill.propose` limited to trusted roles.

## Writing a skill

```python
def run(df, top=1):
    if df.empty:
        return {"summary": "No rows.", "table": []}
    total = float(df["revenue"].sum())
    ranked = df.sort_values("revenue", ascending=False)
    share = float(ranked["revenue"].head(top).sum()) / total if total else None
    return {"summary": f"The top {top} region(s) earn {share:.1%} of revenue.",
            "findings": [...], "table": [{"region": r, "revenue": float(v)} for r, v in zip(ranked.region, ranked.revenue)]}
```

`summary` becomes the answer, `findings` the evidence, `table` (a list of dicts) a table. Tests are JSON:
`{"name", "columns", "rows", "params", "expect", "tolerance"}`; `expect_error: true` for a case that must raise.

## The Foundry: Lyst proposing its own skills

When a request names a method Lyst has neither a capability nor an approved skill for (`detect_gap` in
`analyst/foundry.py`), the plan runs `propose_capability` instead of improvising a number. Templates exist
for the Herfindahl index, the Gini coefficient and the Pareto share; a configured model may draft other
methods against the contract `run(df[member, value], **params) -> dict`. Every draft goes through the same
static check, sandbox, adversarial cases and benchmark as a human proposal, is stored with its test
report, and waits for an owner who did not propose it. The answer to the original request says exactly
that and produces no number. After approval, the same request runs the skill on governed data.
