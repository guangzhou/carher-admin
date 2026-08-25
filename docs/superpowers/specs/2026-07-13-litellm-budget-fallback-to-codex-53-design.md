# LiteLLM Key Budget Fallback to GPT-5.3 Design

## Status

- Date: 2026-07-13
- Scope: `carher-admin` and the existing `litellm-product` deployment
- Decision: approved for implementation planning
- Rollout: one-key canary before broader enablement

## Problem

LiteLLM virtual keys can have a monetary `max_budget` and a periodic
`budget_duration`. When a key exhausts its budget, LiteLLM rejects the request
before model routing, so router-level fallbacks do not run. The desired behavior
is to keep the same client API key and requested model name working by routing
the key to `chatgpt-gpt-5.3-codex-spark`, which is treated as zero monetary cost
for this policy. When the original budget period resets, the key must return to
its original paid-model configuration automatically.

The policy must be opt-in per key. It must not change the behavior of existing
keys unless an administrator explicitly enables it.

## Goals

1. Switch an opted-in key to GPT-5.3 before LiteLLM rejects it for exceeding its
   monetary budget.
2. Keep the client API key and client model name unchanged during fallback.
3. Ensure fallback traffic cannot reach paid models through alternate model
   names.
4. Continue recording requests, tokens, and selected models while recording zero
   monetary spend for the dedicated GPT-5.3 fallback product.
5. Restore the exact original key routing and budget at the next periodic reset.
6. Survive service restarts without losing the original configuration or reset
   deadline.
7. Prefer a cost-safe failure mode: a failed controller must leave a fallback key
   on GPT-5.3 rather than accidentally restoring paid traffic.

## Non-Goals

- This policy does not respond to upstream ChatGPT account 5-hour or weekly
  quota exhaustion.
- It does not apply to keys with a lifetime budget and no `budget_duration`.
- It does not modify LiteLLM's core authentication or budget-enforcement code.
- It does not introduce a second public gateway or require clients to rotate API
  keys.
- It does not automatically enable the policy for all existing budgeted keys.

## Considered Approaches

### 1. Per-key budget fallback controller (selected)

A controller observes opted-in keys and uses LiteLLM's existing `/key/update`
API to atomically change each key's model allowlist, aliases, and budget fields.
The original configuration is stored in the admin database for restoration.

Advantages:

- Reuses existing LiteLLM virtual-key and per-key alias behavior.
- Requires no client changes and no LiteLLM restart.
- Is isolated to explicitly enabled keys.
- Can be implemented and tested independently of LiteLLM internals.

Trade-off: switching is asynchronous, so the controller must switch before 100%
and use a tighter polling interval near the threshold.

### 2. Dual-key request gateway

A new gateway would authenticate one external key and select between a paid
internal key and a GPT-5.3 internal key.

This provides strong separation but adds a new streaming proxy, authentication
layer, deployment, and operational failure domain. It is not selected for the
first version.

### 3. Patch LiteLLM budget authentication

LiteLLM could be modified to permit over-budget requests and rewrite them to
GPT-5.3.

This is not selected because it couples the feature to third-party internals and
raises upgrade and security risk.

## Architecture

The feature consists of four bounded units:

1. **Policy repository**: persists opt-in settings, the original LiteLLM key
   snapshot, state, deadlines, configuration fingerprints, and audit events.
2. **Budget observer**: batch-reads current key spend, maximum budget, reset time,
   blocked status, models, and aliases from LiteLLM.
3. **State controller**: makes deterministic state transitions and applies
   `/key/update` mutations.
4. **Admin API and UI**: exposes policy status and deliberate operator actions;
   the browser never writes directly to LiteLLM.

The implementation should follow existing `backend/litellm_ops.py` patterns for
LiteLLM API access and the existing background-worker patterns in the backend.
The UI is added to the current key/account-pool administration surface rather
than creating another application.

## Policy Data Model

The admin database stores a policy row keyed by the LiteLLM verification-token
hash or stable token identifier. Plaintext API keys must not be stored.

Required fields:

| Field | Purpose |
|---|---|
| `key_id` | Stable LiteLLM key hash/identifier |
| `key_alias` | Operator-readable key name |
| `enabled` | Per-key opt-in switch; defaults to false |
| `state` | Current controller state |
| `threshold_percent` | Switch threshold; version 1 fixes this at 98 |
| `original_models` | Exact pre-fallback model allowlist |
| `original_aliases` | Exact pre-fallback per-key aliases |
| `original_max_budget` | Original periodic monetary limit |
| `original_budget_duration` | Original reset duration |
| `original_budget_reset_at` | Reset deadline captured before fallback |
| `original_config_fingerprint` | Hash of the original managed fields |
| `fallback_config_fingerprint` | Hash of the expected fallback fields |
| `fallback_entered_at` | Time of successful switch |
| `last_observed_spend` | Last current-cycle spend value |
| `last_observed_at` | Last successful observation time |
| `last_error` | Most recent controller failure, if any |
| `created_by` / `updated_by` | Administrative actor identifiers |
| `created_at` / `updated_at` | Audit timestamps |

An append-only event table records policy enablement, threshold crossings,
switches, restore attempts, conflicts, failures, manual operations, and the
before/after managed configuration. Events must never contain plaintext API
keys or secrets.

## State Machine

### `NORMAL`

The key uses its original paid-model configuration and periodic budget.

- Poll every 30 seconds while utilization is below 90%.
- Poll every 5 seconds once utilization is at least 90%.
- At utilization of at least 98%, transition to `FALLBACK_PENDING`.
- A manually blocked or invalid key transitions to `MANUAL_HOLD`, not fallback.

Utilization is calculated as:

```text
utilization_percent = current_cycle_spend / max_budget * 100
```

The current-cycle `spend` and `budget_reset_at` reported by LiteLLM are the
authoritative inputs. Prompt length, token estimates, or request counts are not
used to predict spend.

### `FALLBACK_PENDING`

The controller snapshots and fingerprints the original managed configuration,
then applies the fallback configuration in one `/key/update` request.

The fallback configuration must:

- Preserve the public model names the client is allowed to request.
- Map every preserved paid public model name to
  `chatgpt-gpt-5.3-codex-spark`.
- Restrict the allowlist so direct internal paid-model names cannot bypass the
  aliases.
- Include `chatgpt-gpt-5.3-codex-spark` as the only reachable internal
  generation target.
- Temporarily remove the key-level monetary hard limit that would otherwise
  reject the request before routing.
- Preserve manual `blocked=true`; a blocked key must not be made usable.

After applying the update, the controller re-reads the key. It transitions to
`FALLBACK_5_3` only if the observed configuration matches the expected fallback
fingerprint. Otherwise it records an error and retries without claiming success.

### `FALLBACK_5_3`

The same API key and public model names now resolve only to GPT-5.3. The
controller keeps the captured original reset deadline; it does not infer a new
deadline from the now-unlimited fallback key.

- Do not restore before `original_budget_reset_at`.
- Periodically verify that the fallback fingerprint still matches.
- If an administrator changes managed fields outside this feature, transition
  to `MANUAL_HOLD` instead of overwriting the change.
- If the GPT-5.3 upstream is unhealthy, remain in this state, expose the
  upstream error, retry health checks, and alert. Do not restore paid routing as
  an implicit availability fallback.

### `RESTORING`

At or after the captured reset deadline, the controller applies the exact
original models, aliases, maximum budget, and budget duration. It explicitly
sets current-cycle `spend` to zero because the key's native budget reset may not
have run while the monetary limit was temporarily absent.

After the update, it re-reads `/key/info` and verifies:

- the original managed configuration has been restored;
- `spend` is zero or within a small serialization tolerance;
- a new future `budget_reset_at` exists;
- the key is not unexpectedly blocked.

Only then does the state return to `NORMAL`. Failed verification leaves the key
cost-safe on fallback and schedules another restore attempt.

### `MANUAL_HOLD`

The controller takes no automatic routing action in this state. Causes include:

- the key was manually blocked or disabled;
- authentication/token state is invalid;
- the key lacks a periodic reset duration;
- managed fields changed externally during fallback;
- the administrator explicitly paused automation.

The UI must explain the cause and require an administrator to choose whether to
recapture the current configuration, restore the saved configuration, or leave
the key untouched.

## Eligibility and Safety Rules

The feature may be enabled only when all of the following are true:

1. `max_budget` is a positive number.
2. `budget_duration` is present and supported by LiteLLM.
3. `budget_reset_at` can be read or deterministically initialized by LiteLLM.
4. The key is not blocked and is not an administrative/master key.
5. The GPT-5.3 fallback product is present and passes a lightweight health
   probe.
6. The key configuration can be read and fingerprinted.

The controller must use compare-before-write behavior. A write is allowed only
when the currently observed managed fields match the state the controller
expects. This prevents stale workers from overwriting operator changes.

Only one worker may transition a policy at a time. Use a database lease or row
lock with a short expiry so multiple backend replicas cannot apply competing
updates.

## Zero-Cost GPT-5.3 Product

`chatgpt-gpt-5.3-codex-spark` is the dedicated fallback target. For this feature,
its LiteLLM accounting configuration must record zero input, cached-input, and
output monetary cost. SpendLogs must still retain model, token, latency, status,
and key attribution.

The implementation must verify the live model accounting configuration before
enabling policies. If the model is not verifiably zero-cost, automatic fallback
must fail closed and the UI must prevent enablement.

This accounting rule is specific to the internal subscription-backed product;
it must not change similarly named paid provider products such as Wangsu or
OpenRouter GPT-5.3 routes.

## Controller Scheduling

- Default `NORMAL` interval: 30 seconds.
- Elevated `NORMAL` interval at 90% or more: 5 seconds.
- Fallback verification interval: 60 seconds.
- Failed mutations: exponential retry beginning at 5 seconds and capped at 5
  minutes.
- Restore attempts begin at the captured reset time and use the same bounded
  retry policy.

The first version uses 98% as a fixed safety threshold. The UI may display it,
but it is not user-configurable until production data demonstrates a need.

Scanning must batch-read eligible keys instead of issuing one serial network
request per key. A slow or unavailable LiteLLM API must not block unrelated
admin requests.

## Admin API

The backend exposes authenticated administrative endpoints for:

- listing policy and live budget status;
- enabling or disabling a key policy;
- manually switching a key to GPT-5.3;
- manually restoring the saved configuration;
- accepting the current LiteLLM configuration as a new baseline;
- pausing/resuming automation;
- retrieving policy event history.

All mutating endpoints must be idempotent and must return the resulting state,
not only an acknowledgement. Disabling a policy while it is in fallback requires
an explicit choice to restore the saved configuration or leave the key on its
current routing; there must be no ambiguous implicit behavior.

## Admin UI

The feature is embedded in the existing key/account-pool administration area.

Each eligible key has an opt-in switch labelled `预算用尽后切换 5.3`, disabled by
default. Before enabling it, the UI shows:

- current spend and maximum budget;
- budget duration and next reset time;
- the fixed 98% switch point;
- the fallback product name;
- a warning that paid models will be unavailable during fallback.

The list adds a `预算路由` status with these user-facing states:

| State | Label |
|---|---|
| `NORMAL` | 正常 |
| `NORMAL` at >=90% | 接近限额 |
| `FALLBACK_PENDING` | 切换中 |
| `FALLBACK_5_3` | 5.3 兜底 |
| `RESTORING` | 恢复中 |
| `MANUAL_HOLD` | 人工检查 |

The row also shows `spend / max_budget`, utilization, and the reset countdown.

The details drawer provides deliberate operations:

- `立即切到 5.3`;
- `立即恢复主模型`;
- `重新采集当前配置`;
- policy pause/resume;
- event history.

The frontend never receives or displays a plaintext API key.

## Alerts and Observability

Use the existing Feishu alert channel for:

- successful automatic switch;
- failed switch after bounded retries;
- successful automatic restore;
- failed restore after bounded retries;
- configuration conflict/manual hold;
- unhealthy GPT-5.3 fallback upstream.

Crossing 90% is visible in the UI and metrics but does not send a Feishu alert.

Metrics should include policy counts by state, transition totals, mutation
failures, time spent in fallback, restore delay after reset, and GPT-5.3 health.
Logs must use key aliases or hashes and redact all tokens and credentials.

## Failure Handling

- **Controller unavailable:** existing fallback keys remain on GPT-5.3; normal
  keys continue under LiteLLM's native budget enforcement.
- **LiteLLM read failure:** do not make state transitions based on stale spend.
- **LiteLLM write timeout:** re-read before retrying because the prior write may
  have succeeded.
- **Partial or mismatched update:** remain in a transitional/error state and do
  not report successful fallback or restore.
- **GPT-5.3 unavailable:** remain cost-safe, alert, and retry; do not silently
  route to paid fallback providers.
- **External configuration change:** enter `MANUAL_HOLD` and require an explicit
  administrator decision.
- **Missing reset time:** reject enablement or enter `MANUAL_HOLD`; never guess a
  paid-model restore time.

## Testing Strategy

### Unit tests

Cover deterministic transitions and mutation payloads for:

- utilization below 90%;
- utilization at 90%;
- utilization at 98%;
- unavailable or zero maximum budget;
- missing budget duration/reset time;
- manually blocked key;
- snapshot and fingerprint creation;
- successful fallback verification;
- fallback configuration conflict;
- reset deadline not reached;
- successful restore and new reset deadline;
- restore failure and cost-safe retention;
- worker restart from every nonterminal state;
- lease contention between workers.

### Integration tests

Against a disposable LiteLLM environment:

1. Create a temporary key with a very small periodic budget.
2. Enable the policy and generate paid-model spend.
3. Verify the controller switches at the configured threshold.
4. Continue using the same API key and original public model name.
5. Verify the actual selected model is GPT-5.3 and monetary spend no longer
   increases while token/request audit data does.
6. Verify direct paid internal model names are rejected or resolve only to the
   fallback target.
7. Simulate or advance the reset deadline.
8. Verify the original configuration and budget are restored and the next
   paid-model request succeeds.
9. Delete the temporary key and policy records.

### Regression tests

- Keys without the opt-in policy retain native LiteLLM budget rejection.
- Existing per-key aliases and model allowlists are unchanged before activation
  and exactly restored afterward.
- Master/admin keys are never eligible.
- Existing upstream-quota account-pool automation remains independent.

## Rollout and Acceptance

1. Deploy schema, controller, API, and read-only UI status with all policies off.
2. Verify the live GPT-5.3 product is healthy and zero-cost.
3. Enable one disposable or designated test key.
4. Run the full switch-and-restore integration scenario.
5. Observe one complete real budget period for routing, spend, errors, and
   restore timing.
6. Enable additional keys individually; bulk enablement is a later operation,
   not a default behavior.

Acceptance criteria:

- The client does not change its API key or model configuration.
- Automatic switch occurs before native budget rejection in the tested load
  envelope.
- During fallback, all reachable generation routes for that key resolve only to
  the zero-cost GPT-5.3 product.
- Monetary spend does not increase during fallback, while usage audit data is
  retained.
- Original routing and periodic budget are restored after reset.
- A restart at any point does not lose state or restore paid traffic early.
- Any ambiguous failure remains cost-safe and visible to administrators.

## Implementation Boundary

The implementation plan should keep changes surgical:

- add database migration and repository methods in the existing backend data
  layer;
- extend LiteLLM operations with explicit read/update helpers;
- add a focused budget-fallback controller rather than expanding unrelated
  account-quota scripts;
- add authenticated admin endpoints in the existing backend;
- extend the existing account/key administration UI;
- add tests before production logic;
- avoid changes to the unrelated reset-bank probe work currently present in the
  branch.
