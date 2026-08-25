# LiteLLM Budget Fallback to GPT-5.3 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add an opt-in per-LiteLLM-key policy that switches a key to an isolated zero-cost GPT-5.3 product at 98% of its periodic monetary budget and restores its exact original routing when the budget period resets.

**Architecture:** A new SQLite-backed policy ledger stores snapshots, state, leases, and events. A focused controller batch-observes LiteLLM key spend, applies compare-before-write `/key/update` mutations, and runs from the existing FastAPI worker lifecycle. The existing React admin gains a budget-routing page; clients keep the same API key and requested model name throughout.

**Tech Stack:** Python 3.11+, FastAPI, SQLite, pytest, LiteLLM virtual-key API, Kubernetes ConfigMap manifest, React 19, Vite 6, Tailwind CSS, Node built-in test runner.

## Global Constraints

- Policy is opt-in per key and defaults to disabled.
- Version 1 uses fixed thresholds: warning at 90%, switch at 98%.
- Only keys with positive `max_budget`, non-empty `budget_duration`, and a valid `budget_reset_at` are eligible.
- Plaintext LiteLLM API keys must never be persisted, logged, returned by admin APIs, or rendered by the frontend.
- Fallback traffic must only reach the isolated `chatgpt-budget-fallback-gpt-5.3` group; that group has zero input, cached-input, and output cost and no paid fallback chain.
- Existing `chatgpt-gpt-5.3-codex-spark` pricing and routing remain unchanged.
- Any ambiguous controller failure remains cost-safe: keep the key on GPT-5.3 and expose the error.
- External changes to managed key fields cause `MANUAL_HOLD`; the controller does not overwrite them automatically.
- Do not modify or include the unrelated working-tree changes under `scripts/chatgpt-acct-reset-bank.sh` or `scripts/tests/` in commits.
- Write every production behavior after a focused failing test and observe the expected failure before implementation.

---

## File Structure

### New backend files

- `backend/budget_fallback_store.py`: policy/event persistence, JSON serialization, and worker leases.
- `backend/litellm_budget_client.py`: typed LiteLLM key/model read and update operations.
- `backend/budget_fallback.py`: pure state decisions, fingerprints, fallback payload construction, and orchestration service.
- `backend/budget_fallback_worker.py`: periodic scheduling, retry timing, Feishu edge alerts, and worker startup.
- `backend/budget_fallback_api.py`: authenticated FastAPI router for list/enable/disable/manual actions/events.

### New frontend files

- `frontend/src/components/BudgetFallbackPage.jsx`: budget-routing administration page.
- `frontend/src/components/budgetFallbackViewModel.js`: pure status, utilization, countdown, and presentation helpers.
- `frontend/src/components/budgetFallbackViewModel.test.js`: Node tests for frontend behavior.

### Modified files

- `k8s/litellm-proxy.yaml`: isolated zero-cost GPT-5.3 model group.
- `backend/database.py`: schema version 13 and new tables/indexes.
- `backend/litellm_ops.py`: shared authenticated GET helper only; existing key generation behavior remains unchanged.
- `backend/main.py`: include the new router and start the new worker.
- `backend/models.py`: request models for policy mutations.
- `frontend/src/api.js`: budget-fallback API methods.
- `frontend/src/App.jsx`: add the `预算路由` administration tab.
- `frontend/src/index.css`: monochrome page tokens and small colored state accents.
- `frontend/package.json`: add `node --test` script.

### New tests

- `backend/tests/test_budget_fallback_model.py`
- `backend/tests/test_budget_fallback_store.py`
- `backend/tests/test_litellm_budget_client.py`
- `backend/tests/test_budget_fallback.py`
- `backend/tests/test_budget_fallback_worker.py`
- `backend/tests/test_budget_fallback_api.py`

---

### Task 1: Isolated Zero-Cost GPT-5.3 Product

**Files:**
- Modify: `k8s/litellm-proxy.yaml:1552`
- Create: `backend/tests/test_budget_fallback_model.py`

**Interfaces:**
- Produces model group name constant: `chatgpt-budget-fallback-gpt-5.3`.
- Produces a group with the same internal Spark upstreams as the current subscription-backed pool, explicit zero costs, `mode: responses`, unique model IDs, and no entry in `router_settings.fallbacks`.
- Later tasks use this exact group name as `FALLBACK_MODEL_GROUP`.

- [ ] **Step 1: Write the failing manifest tests**

```python
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[2]
FALLBACK_GROUP = "chatgpt-budget-fallback-gpt-5.3"


def load_litellm_config() -> dict:
    outer = yaml.safe_load((ROOT / "k8s/litellm-proxy.yaml").read_text(encoding="utf-8"))
    configmap = next(
        item for item in outer["items"]
        if item.get("kind") == "ConfigMap" and item.get("metadata", {}).get("name") == "litellm-config"
    )
    return yaml.safe_load(configmap["data"]["config.yaml"])


def test_budget_fallback_group_is_zero_cost_and_responses_only():
    config = load_litellm_config()
    rows = [row for row in config["model_list"] if row["model_name"] == FALLBACK_GROUP]
    assert rows
    for row in rows:
        params = row["litellm_params"]
        assert params["model"] == "openai/gpt-5.3-codex-spark"
        assert params["input_cost_per_token"] == 0
        assert params["output_cost_per_token"] == 0
        assert params["cache_read_input_token_cost"] == 0
        assert row["model_info"]["mode"] == "responses"


def test_budget_fallback_group_has_no_paid_router_fallback():
    config = load_litellm_config()
    sources = {
        next(iter(item)): item[next(iter(item))]
        for item in config["router_settings"].get("fallbacks", [])
    }
    assert FALLBACK_GROUP not in sources
```

- [ ] **Step 2: Run the tests and verify the missing group failure**

Run: `python -m pytest backend/tests/test_budget_fallback_model.py -v`

Expected: FAIL because no rows have `model_name == "chatgpt-budget-fallback-gpt-5.3"`.

- [ ] **Step 3: Add isolated model rows**

For every healthy `chatgpt-gpt-5.3-codex-spark` deployment currently represented in the manifest, add a sibling row with this shape and the same `api_base`:

```yaml
- model_name: chatgpt-budget-fallback-gpt-5.3
  litellm_params:
    model: openai/gpt-5.3-codex-spark
    api_base: http://chatgpt-acct-11.carher.svc:4000
    api_key: os.environ/CHATGPT_POOL_KEY
    input_cost_per_token: 0
    output_cost_per_token: 0
    cache_read_input_token_cost: 0
  model_info:
    mode: responses
    id: budget-fallback/chatgpt-acct-11/gpt-5.3-codex-spark
```

Use a unique `model_info.id` for every copied upstream. Do not add this group to `model_group_alias` or `fallbacks`.

- [ ] **Step 4: Run the manifest tests**

Run: `python -m pytest backend/tests/test_budget_fallback_model.py -v`

Expected: 2 tests PASS.

- [ ] **Step 5: Commit the isolated product**

```bash
git add k8s/litellm-proxy.yaml backend/tests/test_budget_fallback_model.py
git commit -m "feat(litellm): add zero-cost GPT-5.3 budget fallback group"
```

---

### Task 2: Policy Ledger, Events, and Leases

**Files:**
- Modify: `backend/database.py:25`
- Create: `backend/budget_fallback_store.py`
- Create: `backend/tests/test_budget_fallback_store.py`
- Modify: `backend/tests/test_database.py:20`

**Interfaces:**
- Produces `PolicyState = Literal["NORMAL", "FALLBACK_PENDING", "FALLBACK_5_3", "RESTORING", "MANUAL_HOLD"]`.
- Produces `BudgetFallbackStore` methods:
  - `list_policies() -> list[dict]`
  - `get_policy(key_id: str) -> dict | None`
  - `enable_policy(key_snapshot: dict, actor: str) -> dict`
  - `update_policy(key_id: str, **changes) -> dict`
  - `disable_policy(key_id: str, actor: str) -> dict`
  - `append_event(key_id: str, event_type: str, detail: dict, actor: str = "system") -> int`
  - `list_events(key_id: str, limit: int = 100) -> list[dict]`
  - `acquire_lease(key_id: str, owner: str, now: datetime, ttl_seconds: int = 30) -> bool`
  - `release_lease(key_id: str, owner: str) -> None`
- Stores JSON objects as canonical compact JSON with sorted keys.

- [ ] **Step 1: Write failing schema and store tests**

```python
import sqlite3
from datetime import UTC, datetime, timedelta

from backend.budget_fallback_store import BudgetFallbackStore


def snapshot() -> dict:
    return {
        "key_id": "hash-1",
        "key_alias": "cursor-alice",
        "models": ["gpt-5.5", "gpt-5.4"],
        "aliases": {"gpt-5.5": "chatgpt-pool-gpt-5.5"},
        "max_budget": 100.0,
        "budget_duration": "1d",
        "budget_reset_at": "2026-07-14T00:00:00+00:00",
        "blocked": False,
    }


def test_schema_contains_budget_fallback_tables(db):
    conn = sqlite3.connect(str(db.DB_PATH))
    tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"litellm_budget_fallback_policies", "litellm_budget_fallback_events"} <= tables


def test_enable_policy_persists_original_snapshot(db):
    store = BudgetFallbackStore(db)
    row = store.enable_policy(snapshot(), actor="admin")
    assert row["enabled"] is True
    assert row["state"] == "NORMAL"
    assert row["original_models"] == ["gpt-5.5", "gpt-5.4"]
    assert row["original_max_budget"] == 100.0


def test_lease_allows_only_one_owner_until_expiry(db):
    store = BudgetFallbackStore(db)
    store.enable_policy(snapshot(), actor="admin")
    now = datetime(2026, 7, 13, tzinfo=UTC)
    assert store.acquire_lease("hash-1", "worker-a", now, 30) is True
    assert store.acquire_lease("hash-1", "worker-b", now + timedelta(seconds=5), 30) is False
    assert store.acquire_lease("hash-1", "worker-b", now + timedelta(seconds=31), 30) is True


def test_events_redact_secret_shaped_fields(db):
    store = BudgetFallbackStore(db)
    store.enable_policy(snapshot(), actor="admin")
    store.append_event("hash-1", "switched", {"api_key": "sk-secret", "state": "FALLBACK_5_3"})
    event = store.list_events("hash-1")[0]
    assert "sk-secret" not in str(event)
    assert event["detail"]["state"] == "FALLBACK_5_3"
```

- [ ] **Step 2: Run the store tests and verify import/schema failures**

Run: `python -m pytest backend/tests/test_budget_fallback_store.py backend/tests/test_database.py::TestInitDb::test_creates_all_tables -v`

Expected: FAIL because the store module and schema tables do not exist.

- [ ] **Step 3: Add schema version 13**

Increment `SCHEMA_VERSION` to `13`. Add both tables to `SCHEMA_SQL` and migration 13:

```sql
CREATE TABLE IF NOT EXISTS litellm_budget_fallback_policies (
    key_id TEXT PRIMARY KEY,
    key_alias TEXT NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 0,
    state TEXT NOT NULL DEFAULT 'NORMAL',
    threshold_percent REAL NOT NULL DEFAULT 98,
    original_models TEXT NOT NULL DEFAULT '[]',
    original_aliases TEXT NOT NULL DEFAULT '{}',
    original_max_budget REAL,
    original_budget_duration TEXT NOT NULL DEFAULT '',
    original_budget_reset_at TEXT NOT NULL DEFAULT '',
    original_config_fingerprint TEXT NOT NULL DEFAULT '',
    fallback_config_fingerprint TEXT NOT NULL DEFAULT '',
    fallback_entered_at TEXT NOT NULL DEFAULT '',
    last_observed_spend REAL NOT NULL DEFAULT 0,
    last_observed_at TEXT NOT NULL DEFAULT '',
    last_error TEXT NOT NULL DEFAULT '',
    automation_paused INTEGER NOT NULL DEFAULT 0,
    lease_owner TEXT NOT NULL DEFAULT '',
    lease_expires_at TEXT NOT NULL DEFAULT '',
    created_by TEXT NOT NULL DEFAULT '',
    updated_by TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS litellm_budget_fallback_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    key_id TEXT NOT NULL,
    event_type TEXT NOT NULL,
    actor TEXT NOT NULL DEFAULT 'system',
    detail TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY(key_id) REFERENCES litellm_budget_fallback_policies(key_id)
);

CREATE INDEX IF NOT EXISTS idx_budget_fallback_state
ON litellm_budget_fallback_policies(enabled, state);

CREATE INDEX IF NOT EXISTS idx_budget_fallback_events_key
ON litellm_budget_fallback_events(key_id, id DESC);
```

- [ ] **Step 4: Implement `BudgetFallbackStore`**

Use `db.get_db()` for every operation, convert SQLite integers to booleans, decode JSON columns on reads, encode them canonically on writes, call `db.backup_to_nas()` after mutations, and recursively redact keys named `api_key`, `key`, `token`, `authorization`, `secret`, and `password` from event details.

Lease acquisition must be one conditional `UPDATE` whose predicate permits an empty lease, the same owner, or an expired ISO-8601 timestamp. Return `cursor.rowcount == 1`.

- [ ] **Step 5: Update the database table assertion and run tests**

Add the two new table names to `required` in `TestInitDb.test_creates_all_tables`.

Run: `python -m pytest backend/tests/test_budget_fallback_store.py backend/tests/test_database.py -v`

Expected: all store and database tests PASS.

- [ ] **Step 6: Commit the ledger**

```bash
git add backend/database.py backend/budget_fallback_store.py backend/tests/test_budget_fallback_store.py backend/tests/test_database.py
git commit -m "feat: persist LiteLLM budget fallback policies"
```

---

### Task 3: Typed LiteLLM Budget Client

**Files:**
- Modify: `backend/litellm_ops.py:125`
- Create: `backend/litellm_budget_client.py`
- Create: `backend/tests/test_litellm_budget_client.py`

**Interfaces:**
- Produces immutable `KeySnapshot` dataclass fields: `key_id`, `key_alias`, `models`, `aliases`, `max_budget`, `budget_duration`, `budget_reset_at`, `spend`, `blocked`.
- Produces `ModelHealth` dataclass fields: `available`, `zero_cost`, `deployment_count`, `error`.
- Produces `LiteLLMBudgetClient` methods:
  - `list_budgeted_keys(limit: int = 2000) -> list[KeySnapshot]`
  - `get_key(key_id: str) -> KeySnapshot`
  - `update_key(key_id: str, *, models: list[str], aliases: dict[str, str], max_budget: float | None, budget_duration: str | None, spend: float | None = None, blocked: bool | None = None) -> KeySnapshot`
  - `check_fallback_model() -> ModelHealth`
- Raises `LiteLLMBudgetError` with sanitized status and response text.

- [ ] **Step 1: Write failing client tests with a fake transport**

```python
from backend.litellm_budget_client import FALLBACK_MODEL_GROUP, LiteLLMBudgetClient


class FakeTransport:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def request_json(self, method, path, payload=None, timeout=15):
        self.calls.append((method, path, payload))
        return self.responses.pop(0)


def test_list_budgeted_keys_filters_periodic_positive_budgets():
    transport = FakeTransport([[{
        "token": "hash-1", "key_alias": "cursor-alice", "models": ["gpt-5.5"],
        "aliases": {}, "max_budget": 100, "budget_duration": "1d",
        "budget_reset_at": "2026-07-14T00:00:00Z", "spend": 20, "blocked": False,
    }, {
        "token": "hash-2", "key_alias": "unlimited", "max_budget": None,
    }]])
    client = LiteLLMBudgetClient(transport)
    rows = client.list_budgeted_keys()
    assert [row.key_id for row in rows] == ["hash-1"]


def test_update_key_posts_all_managed_fields_and_rereads():
    updated = {
        "info": {"token": "hash-1", "key_alias": "cursor-alice", "models": [FALLBACK_MODEL_GROUP],
                 "aliases": {"gpt-5.5": FALLBACK_MODEL_GROUP}, "max_budget": None,
                 "budget_duration": None, "budget_reset_at": None, "spend": 98, "blocked": False}
    }
    transport = FakeTransport([{"status": "ok"}, updated])
    client = LiteLLMBudgetClient(transport)
    row = client.update_key(
        "hash-1", models=[FALLBACK_MODEL_GROUP], aliases={"gpt-5.5": FALLBACK_MODEL_GROUP},
        max_budget=None, budget_duration=None,
    )
    assert transport.calls[0][1] == "/key/update"
    assert transport.calls[0][2]["key"] == "hash-1"
    assert row.max_budget is None


def test_fallback_health_requires_every_cost_field_to_be_zero():
    transport = FakeTransport([{"data": [{
        "model_name": FALLBACK_MODEL_GROUP,
        "litellm_params": {"input_cost_per_token": 0, "output_cost_per_token": 0,
                           "cache_read_input_token_cost": 0},
    }]}])
    health = LiteLLMBudgetClient(transport).check_fallback_model()
    assert health.available is True
    assert health.zero_cost is True
```

- [ ] **Step 2: Run tests and verify the module import failure**

Run: `python -m pytest backend/tests/test_litellm_budget_client.py -v`

Expected: FAIL because `backend.litellm_budget_client` does not exist.

- [ ] **Step 3: Add a shared authenticated request helper**

In `backend/litellm_ops.py`, add:

```python
def request_json(method: str, path: str, payload: dict | None = None, timeout: int = 15) -> Any:
    if not LITELLM_MASTER_KEY:
        raise RuntimeError("LITELLM_MASTER_KEY is not configured")
    body = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(
        f"{LITELLM_PROXY_URL}{path}", data=body, method=method,
        headers={"Authorization": f"Bearer {LITELLM_MASTER_KEY}", "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read()
    return json.loads(raw) if raw else {}
```

Keep `_post_json` delegating to this helper so existing callers preserve behavior.

- [ ] **Step 4: Implement the typed client**

Normalize `budget_reset_at` to an ISO-8601 UTC string, coerce missing aliases/models to empty collections, and never include raw authorization values in exceptions. `list_budgeted_keys` calls `/spend/keys?limit=2000`; `get_key` calls `/key/info?key=<urlencoded hash>`; `check_fallback_model` calls `/v1/model/info` and filters the isolated group.

- [ ] **Step 5: Run client and existing key tests**

Run: `python -m pytest backend/tests/test_litellm_budget_client.py backend/tests/test_litellm_ops.py -v`

Expected: all tests PASS.

- [ ] **Step 6: Commit the client**

```bash
git add backend/litellm_ops.py backend/litellm_budget_client.py backend/tests/test_litellm_budget_client.py
git commit -m "feat: add LiteLLM budget key client"
```

---

### Task 4: Pure State Machine and Cost-Safe Controller

**Files:**
- Create: `backend/budget_fallback.py`
- Create: `backend/tests/test_budget_fallback.py`

**Interfaces:**
- Consumes `BudgetFallbackStore`, `KeySnapshot`, `LiteLLMBudgetClient`, and `FALLBACK_MODEL_GROUP`.
- Produces:
  - `managed_fingerprint(snapshot_or_fields: dict) -> str`
  - `utilization_percent(snapshot: KeySnapshot) -> float`
  - `fallback_fields(snapshot: KeySnapshot) -> dict`
  - `BudgetFallbackController.run_policy(key_id: str, now: datetime) -> TransitionResult`
  - `BudgetFallbackController.force_fallback(key_id: str, actor: str, now: datetime) -> TransitionResult`
  - `BudgetFallbackController.force_restore(key_id: str, actor: str, now: datetime) -> TransitionResult`
  - `BudgetFallbackController.recapture(key_id: str, actor: str) -> dict`
- `TransitionResult` contains `key_id`, `from_state`, `to_state`, `changed`, `event_type`, and `error`.

- [ ] **Step 1: Write failing pure behavior tests**

```python
from datetime import UTC, datetime, timedelta

from backend.budget_fallback import fallback_fields, utilization_percent
from backend.litellm_budget_client import FALLBACK_MODEL_GROUP, KeySnapshot


def key(spend=20, budget=100, reset="2026-07-14T00:00:00+00:00", blocked=False):
    return KeySnapshot(
        key_id="hash-1", key_alias="cursor-alice",
        models=("gpt-5.5", "gpt-5.4", "wangsu-gpt-5.5", "BAAI/bge-m3"),
        aliases={"gpt-5.5": "chatgpt-pool-gpt-5.5"}, max_budget=budget,
        budget_duration="1d", budget_reset_at=reset, spend=spend, blocked=blocked,
    )


def test_utilization_is_clamped_and_division_safe():
    assert utilization_percent(key(spend=98)) == 98
    assert utilization_percent(key(spend=120)) == 120
    assert utilization_percent(key(budget=0)) == 0


def test_fallback_fields_preserve_public_names_but_remove_internal_and_embedding_routes():
    fields = fallback_fields(key())
    assert fields["models"] == ["gpt-5.5", "gpt-5.4", FALLBACK_MODEL_GROUP]
    assert fields["aliases"] == {
        "gpt-5.5": FALLBACK_MODEL_GROUP,
        "gpt-5.4": FALLBACK_MODEL_GROUP,
    }
    assert fields["max_budget"] is None
    assert fields["budget_duration"] is None
```

- [ ] **Step 2: Write failing controller transition tests**

Use in-memory fake store/client objects and cover these exact behaviors:

```python
def test_98_percent_switches_and_verifies_fallback(): ...
def test_90_percent_only_records_observation(): ...
def test_blocked_key_enters_manual_hold_without_update(): ...
def test_fallback_waits_until_saved_reset_deadline(): ...
def test_restore_sets_spend_zero_and_original_fields(): ...
def test_mismatched_fallback_fingerprint_enters_manual_hold(): ...
def test_write_timeout_rereads_before_retrying(): ...
def test_failed_restore_keeps_fallback_state(): ...
```

Each fake client must record update payloads so assertions verify no paid internal model remains reachable.

- [ ] **Step 3: Run tests and verify missing behavior failures**

Run: `python -m pytest backend/tests/test_budget_fallback.py -v`

Expected: FAIL because the state-machine module does not exist.

- [ ] **Step 4: Implement pure helpers**

Use canonical JSON plus SHA-256 over only `models`, `aliases`, `max_budget`, `budget_duration`, and `blocked`. Define public generation models as names from the original allowlist that are not the isolated target, do not equal `BAAI/bge-m3`, and do not start with `wangsu-`, `openrouter-`, `chatgpt-pool-`, `anthropic.`, or `local-`. Require at least one public generation model or reject enablement.

- [ ] **Step 5: Implement compare-before-write controller transitions**

The controller sequence for a switch is:

```python
current = client.get_key(key_id)
assert managed_fingerprint(current) == policy["original_config_fingerprint"]
assert client.check_fallback_model().zero_cost
store.update_policy(key_id, state="FALLBACK_PENDING", ...)
updated = client.update_key(key_id, **fallback_fields(current))
assert managed_fingerprint(updated) == expected_fallback_fingerprint
store.update_policy(key_id, state="FALLBACK_5_3", ...)
store.append_event(key_id, "automatic_switch", before_after_detail)
```

The restore sequence compares the live fields with `fallback_config_fingerprint`, writes the exact original fields plus `spend=0`, verifies a new future reset time, and returns to `NORMAL`. On timeout, re-read first. On a conflict, write `MANUAL_HOLD` and do not call update again.

- [ ] **Step 6: Run state tests**

Run: `python -m pytest backend/tests/test_budget_fallback.py -v`

Expected: all tests PASS.

- [ ] **Step 7: Commit the controller**

```bash
git add backend/budget_fallback.py backend/tests/test_budget_fallback.py
git commit -m "feat: add budget fallback state controller"
```

---

### Task 5: Background Worker, Alerts, and Admin API

**Files:**
- Create: `backend/budget_fallback_worker.py`
- Create: `backend/budget_fallback_api.py`
- Modify: `backend/models.py`
- Modify: `backend/main.py:34`
- Create: `backend/tests/test_budget_fallback_worker.py`
- Create: `backend/tests/test_budget_fallback_api.py`

**Interfaces:**
- Consumes `BudgetFallbackController` and `BudgetFallbackStore`.
- Produces worker functions:
  - `start_budget_fallback_worker() -> asyncio.Task`
  - `run_budget_fallback_cycle(now: datetime | None = None) -> list[TransitionResult]`
  - `notify_transition(result: TransitionResult) -> None`
- Produces API routes under `/api/litellm/budget-fallback`:
  - `GET /keys`
  - `GET /keys/{key_id}/events`
  - `POST /keys/{key_id}/enable`
  - `POST /keys/{key_id}/disable`
  - `POST /keys/{key_id}/fallback`
  - `POST /keys/{key_id}/restore`
  - `POST /keys/{key_id}/recapture`
  - `POST /keys/{key_id}/pause`
  - `POST /keys/{key_id}/resume`

- [ ] **Step 1: Add request models**

Add to `backend/models.py`:

```python
class BudgetFallbackEnableRequest(BaseModel):
    key_id: str = Field(..., min_length=1)


class BudgetFallbackDisableRequest(BaseModel):
    restore: bool = Field(..., description="Restore saved routing before disabling")


class BudgetFallbackActionRequest(BaseModel):
    reason: str = Field("", max_length=500)
```

- [ ] **Step 2: Write failing worker tests**

```python
import asyncio
from datetime import UTC, datetime

from backend import budget_fallback_worker as worker


def test_cycle_acquires_lease_before_running_policy(monkeypatch):
    calls = []
    monkeypatch.setattr(worker, "list_enabled_policy_ids", lambda: ["hash-1"])
    monkeypatch.setattr(worker, "acquire_policy_lease", lambda key_id, owner, now: calls.append("lease") or True)
    monkeypatch.setattr(worker, "run_one_policy", lambda key_id, now: calls.append("run") or None)
    asyncio.run(worker.run_budget_fallback_cycle(datetime(2026, 7, 13, tzinfo=UTC)))
    assert calls == ["lease", "run"]


def test_notification_only_fires_for_edge_events(monkeypatch):
    sent = []
    monkeypatch.setattr(worker, "post_feishu_text", sent.append)
    worker.notify_transition(make_result(event_type="near_limit", changed=False))
    worker.notify_transition(make_result(event_type="automatic_switch", changed=True))
    assert len(sent) == 1
```

- [ ] **Step 3: Write failing API tests**

Test router endpoint functions with dependency-injected fake service/store. Verify:

```python
def test_list_never_returns_plaintext_token(): ...
def test_enable_rejects_non_periodic_key_with_422(): ...
def test_enable_rejects_non_zero_cost_fallback_model_with_409(): ...
def test_manual_fallback_returns_resulting_state(): ...
def test_disable_requires_explicit_restore_boolean(): ...
def test_event_detail_is_redacted(): ...
```

- [ ] **Step 4: Run tests and observe missing module failures**

Run: `python -m pytest backend/tests/test_budget_fallback_worker.py backend/tests/test_budget_fallback_api.py -v`

Expected: FAIL because worker and API modules do not exist.

- [ ] **Step 5: Implement worker scheduling and Feishu alerts**

Use one background loop with a 5-second tick. For each policy, decide whether it is due using `last_observed_at`, state, and utilization: 30 seconds normally, 5 seconds near limit, 60 seconds in fallback. Run blocking LiteLLM operations via `asyncio.to_thread`. Use a stable worker ID containing hostname and process ID. Always release leases in `finally`.

Send Feishu text messages only for `automatic_switch`, `switch_failed`, `automatic_restore`, `restore_failed`, `manual_hold`, and `fallback_unhealthy`. Read the webhook from `db.get_setting("feishu_webhook")` with environment fallback. Sanitize aliases and errors before posting.

- [ ] **Step 6: Implement API router and service dependencies**

The list endpoint merges live `list_budgeted_keys()` snapshots with policy rows and returns only hashes/aliases, budget fields, state, utilization, reset time, countdown source time, error, and eligibility reason. Mutations derive actor from `request.state.auth` when available or the authenticated JWT subject helper; they return the resulting policy/live state.

Enablement performs these checks in order: live key exists, positive periodic budget, future/parseable reset time, not blocked, at least one supported public generation model, fallback model available and zero-cost. It then captures the original fingerprint and persists the policy.

- [ ] **Step 7: Register router and worker**

In `backend/main.py`:

```python
from .budget_fallback_api import router as budget_fallback_router
from .budget_fallback_worker import start_budget_fallback_worker

# after app construction
app.include_router(budget_fallback_router)

# in lifespan after db.init_db()
start_budget_fallback_worker()
```

The worker startup function must be idempotent in a process so test reloads do not create duplicate loops.

- [ ] **Step 8: Run worker/API and full backend tests**

Run: `python -m pytest backend/tests/test_budget_fallback_worker.py backend/tests/test_budget_fallback_api.py -v`

Expected: focused tests PASS.

Run: `python -m pytest backend/tests/ -v`

Expected: all backend tests PASS.

- [ ] **Step 9: Commit worker and API**

```bash
git add backend/budget_fallback_worker.py backend/budget_fallback_api.py backend/models.py backend/main.py backend/tests/test_budget_fallback_worker.py backend/tests/test_budget_fallback_api.py
git commit -m "feat: expose and schedule budget fallback policies"
```

---

### Task 6: Budget Routing Administration Page

**Files:**
- Create: `frontend/src/components/BudgetFallbackPage.jsx`
- Create: `frontend/src/components/budgetFallbackViewModel.js`
- Create: `frontend/src/components/budgetFallbackViewModel.test.js`
- Modify: `frontend/src/api.js`
- Modify: `frontend/src/App.jsx`
- Modify: `frontend/src/index.css`
- Modify: `frontend/package.json`

**Interfaces:**
- Consumes the Task 5 API.
- Produces pure helpers:
  - `budgetPercent(row) -> number`
  - `statusPresentation(row) -> {label, tone, dot}`
  - `formatResetCountdown(resetAt, now) -> string`
  - `canEnable(row) -> boolean`
- Produces one responsive page with list, filters, detail drawer, opt-in switch, manual actions, and event timeline.

- [ ] **Step 1: Add failing view-model tests**

```javascript
import test from "node:test";
import assert from "node:assert/strict";
import { budgetPercent, canEnable, formatResetCountdown, statusPresentation } from "./budgetFallbackViewModel.js";

test("budgetPercent handles missing and over-budget values", () => {
  assert.equal(budgetPercent({ spend: 98, max_budget: 100 }), 98);
  assert.equal(budgetPercent({ spend: 120, max_budget: 100 }), 120);
  assert.equal(budgetPercent({ spend: 10, max_budget: null }), 0);
});

test("near-limit normal keys use the warning presentation", () => {
  assert.equal(statusPresentation({ state: "NORMAL", utilization_percent: 93 }).label, "接近限额");
  assert.equal(statusPresentation({ state: "FALLBACK_5_3" }).label, "5.3 兜底");
});

test("enablement requires backend eligibility and disabled policy", () => {
  assert.equal(canEnable({ eligible: true, enabled: false }), true);
  assert.equal(canEnable({ eligible: false, enabled: false }), false);
});

test("countdown never renders a negative duration", () => {
  assert.equal(formatResetCountdown("2026-07-13T00:00:00Z", new Date("2026-07-14T00:00:00Z")), "等待恢复");
});
```

- [ ] **Step 2: Add frontend test command and verify failure**

Set:

```json
"scripts": {
  "dev": "vite",
  "build": "vite build",
  "preview": "vite preview",
  "test": "node --test"
}
```

Run: `npm test --prefix frontend`

Expected: FAIL because the helper module does not exist.

- [ ] **Step 3: Implement view-model helpers**

Return these exact labels: `正常`, `接近限额`, `切换中`, `5.3 兜底`, `恢复中`, `人工检查`. Use neutral black/gray surfaces, with green, amber, cyan, blue, and red only for dots, bars, badges, and action feedback.

- [ ] **Step 4: Extend the API client**

Add:

```javascript
listBudgetFallbackKeys: () => request("/litellm/budget-fallback/keys"),
getBudgetFallbackEvents: (keyId) => request(`/litellm/budget-fallback/keys/${encodeURIComponent(keyId)}/events`),
enableBudgetFallback: (keyId) => request(`/litellm/budget-fallback/keys/${encodeURIComponent(keyId)}/enable`, { method: "POST", body: JSON.stringify({ key_id: keyId }) }),
disableBudgetFallback: (keyId, restore) => request(`/litellm/budget-fallback/keys/${encodeURIComponent(keyId)}/disable`, { method: "POST", body: JSON.stringify({ restore }) }),
forceBudgetFallback: (keyId, reason = "") => request(`/litellm/budget-fallback/keys/${encodeURIComponent(keyId)}/fallback`, { method: "POST", body: JSON.stringify({ reason }) }),
restoreBudgetFallback: (keyId, reason = "") => request(`/litellm/budget-fallback/keys/${encodeURIComponent(keyId)}/restore`, { method: "POST", body: JSON.stringify({ reason }) }),
recaptureBudgetFallback: (keyId, reason = "") => request(`/litellm/budget-fallback/keys/${encodeURIComponent(keyId)}/recapture`, { method: "POST", body: JSON.stringify({ reason }) }),
pauseBudgetFallback: (keyId) => request(`/litellm/budget-fallback/keys/${encodeURIComponent(keyId)}/pause`, { method: "POST" }),
resumeBudgetFallback: (keyId) => request(`/litellm/budget-fallback/keys/${encodeURIComponent(keyId)}/resume`, { method: "POST" }),
```

- [ ] **Step 5: Build the page**

The page layout is one focused system, not multiple dashboard concepts:

- Header: `预算路由` and one-line explanation.
- Summary strip: eligible, enabled, near limit, on fallback, manual hold.
- Toolbar: search by alias, state filter, refresh button.
- Main table/cards: alias, state, spend bar, reset countdown, policy switch.
- Right-side drawer on desktop and full-screen sheet on mobile: live fields, eligibility explanation, original/fallback routing summary, actions, and event timeline.
- Confirmation is required for manual fallback, manual restore, recapture, and disable-without-restore.
- Poll every 15 seconds while visible; preserve the selected key across refreshes.
- Never show key tokens, hashes beyond a short suffix, or raw request errors containing secrets.

- [ ] **Step 6: Register the page**

In `frontend/src/App.jsx`, add:

```javascript
import BudgetFallbackPage from "./components/BudgetFallbackPage";

{ id: "budget-routing", label: "预算路由", icon: "↳" }

{tab === "budget-routing" && <BudgetFallbackPage />}
```

Keep the existing navigation visual language; do not rework unrelated pages.

- [ ] **Step 7: Add focused styling**

Add CSS variables scoped to `.budget-fallback-page` for graphite surfaces, white text, gray borders, and restrained state colors. Use an expressive existing-safe font stack only within this page if no bundled font asset exists; do not add a remote font dependency. Ensure touch targets are at least 40px and the table becomes stacked cards below 768px.

- [ ] **Step 8: Run frontend tests and build**

Run: `npm test --prefix frontend`

Expected: all Node tests PASS.

Run: `npm run build --prefix frontend`

Expected: Vite production build succeeds with no syntax errors.

- [ ] **Step 9: Commit the page**

```bash
git add frontend/src/components/BudgetFallbackPage.jsx frontend/src/components/budgetFallbackViewModel.js frontend/src/components/budgetFallbackViewModel.test.js frontend/src/api.js frontend/src/App.jsx frontend/src/index.css frontend/package.json
git commit -m "feat(frontend): manage budget fallback policies"
```

---

### Task 7: End-to-End Verification and Canary Gate

**Files:**
- Create: `backend/tests/test_budget_fallback_integration.py`
- Create: `docs/runbooks/litellm-budget-fallback.md`

**Interfaces:**
- Uses a local fake LiteLLM HTTP server for repeatable integration coverage.
- Documents production inspection and one-key canary commands without embedding secrets.

- [ ] **Step 1: Write the failing integration test**

Build a local threaded HTTP fake implementing `/spend/keys`, `/key/info`, `/key/update`, and `/v1/model/info`. The test must execute this sequence through real client/store/controller objects:

```python
def test_full_budget_switch_and_restore_cycle(tmp_path, monkeypatch):
    # key starts at spend=98/max_budget=100 with gpt-5.5
    # enable policy and run cycle -> FALLBACK_5_3
    # same public model remains authorized and aliases to isolated GPT-5.3
    # fake usage increments token audit counters but leaves monetary spend unchanged
    # advance now beyond saved reset -> NORMAL
    # original models/aliases/budget restored and spend reset to zero
    ...
```

- [ ] **Step 2: Run and observe the first integration failure**

Run: `python -m pytest backend/tests/test_budget_fallback_integration.py -v`

Expected: FAIL on the first missing integration seam or incorrect transition; fix production code, not the assertion, unless the assertion contradicts the approved design.

- [ ] **Step 3: Complete the integration seam and rerun**

Run: `python -m pytest backend/tests/test_budget_fallback_integration.py -v`

Expected: PASS.

- [ ] **Step 4: Write the production runbook**

Document:

1. How to inspect the isolated model group in `/v1/model/info` and confirm all cost fields are zero.
2. How to verify the group has no router fallback.
3. How to deploy `carher-admin` using the repository's build-server-only procedure.
4. How to select one disposable or designated periodic-budget key.
5. How to enable the policy from the UI/API.
6. How to observe state, SpendLogs, selected model, alerts, and reset restoration.
7. How to disable with restore.
8. How to recover a `MANUAL_HOLD` without overwriting external changes.
9. Rollback: disable policies, restore saved configurations, then roll back admin and LiteLLM manifests through their independent deployment pipelines.

- [ ] **Step 5: Run the complete verification suite**

Run: `python -m pytest backend/tests/ -v`

Expected: all backend tests PASS.

Run: `npm test --prefix frontend`

Expected: all frontend tests PASS.

Run: `npm run build --prefix frontend`

Expected: production build succeeds.

Run: `git diff --check HEAD`

Expected: no whitespace errors.

- [ ] **Step 6: Confirm unrelated dirty files remain untouched**

Run: `git status --short`

Expected: the pre-existing `scripts/chatgpt-acct-reset-bank.sh` and `scripts/tests/` changes remain outside this feature's staged set.

- [ ] **Step 7: Commit integration evidence and runbook**

```bash
git add backend/tests/test_budget_fallback_integration.py docs/runbooks/litellm-budget-fallback.md
git commit -m "test: verify LiteLLM budget fallback lifecycle"
```

- [ ] **Step 8: Stop at the production canary gate if live prerequisites are unavailable**

Do not claim production activation unless all of these live checks are observed:

- the isolated model group is present in the running LiteLLM deployment;
- every live deployment row reports zero costs;
- the group has no paid fallback;
- a designated test key can be safely used;
- the admin backend has its database migration and worker running;
- the test key completes one switch and restore cycle.

If any prerequisite is unavailable, report the exact missing check and leave all policies disabled.

---

## Plan Self-Review

- **Spec coverage:** Tasks cover isolated zero-cost routing, persistent snapshots/events/leases, eligibility, 90%/98% scheduling, compare-before-write conflicts, manual hold, automatic restoration, alerts, APIs, UI, audits, restart safety, and one-key canary.
- **Discovery adjustment:** The current `chatgpt-gpt-5.3-codex-spark` group has non-zero pricing and a paid fallback. The plan therefore creates `chatgpt-budget-fallback-gpt-5.3` instead of changing the existing group, preserving current users and satisfying the zero-spend requirement.
- **Type consistency:** `key_id`, state names, fingerprints, `FALLBACK_MODEL_GROUP`, API paths, and frontend fields use the same names in every task.
- **Scope:** The work is confined to LiteLLM budget fallback behavior and its management surface; upstream account quota automation and reset-card work remain separate.
