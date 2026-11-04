# lyst-client

The Python client for a Lyst engine's HTTP API. One dependency (httpx); pandas is optional.

```python
from lyst_client import Lyst

lyst = Lyst("http://127.0.0.1:8770", token="...")           # or LYST_URL and LYST_TOKEN in the environment
ws = lyst.workspace("northwind")

plan = ws.files.inspect("regional.xlsx")                     # what is in the file, nothing loaded yet
for t in plan["tables"]:
    print(t["key"], t["proposed_name"], t["rows"], t["warnings"])
ws.files.commit(plan["id"], tables=[{"key": "P&L!A3:D8", "name": "regional_pnl"}])

answer = ws.ask("Why did operating margin fall last quarter?")   # waits for the analysis; retries are idempotent
print(answer["result"]["summary"])

frame = ws.sql("SELECT region, sum(revenue) FROM sales GROUP BY 1").to_pandas()

for event in ws.events.stream(types=["dataset.*", "task.completed"]):   # long-polls, resumes from a cursor
    print(event["type"], event["subject"])
```

Webhook receivers verify deliveries with `lyst_client.verify_signature(secret, request.headers["Lyst-Signature"], body)`.

Errors are typed: `AuthError` (401/403), `NotFound`, `Invalid` (400/422), `RateLimited` (429, with `retry_after`),
`ServerError`. Reads and idempotent writes retry on 429/502/503/504 with backoff that honours `Retry-After`.

The client checks the engine's `Lyst-API-Version` header and warns once when it is newer than the client knows.
