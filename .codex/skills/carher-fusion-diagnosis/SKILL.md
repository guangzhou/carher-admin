---
name: carher-fusion-diagnosis
version: 1.0.0
description: "Generate and verify CarHer Her fusion self-diagnosis evidence. Use when producing Her 自诊断 / 融合体月度自诊断 reports, reconciling Her self-scores with K8s/PVC evidence, or updating the Feishu Base score table."
metadata:
  requires:
    bins: ["kubectl", "lark-cli", "python3"]
---

# CarHer Fusion Diagnosis

> Build a report from Her self-check plus reproducible backend evidence; report shape may vary, but data counts must reconcile.

## Non-Negotiables

- Do not change the scoring template fields unless the user explicitly asks: A/B/C/D/E/F and their sub-scores must remain aligned with the source guide.
- Never output raw private chat content, secrets, full payloads, tokens, open IDs, message IDs, or full request IDs in the report.
- Keep separate scores when evidence layers differ:
  - `Her 自检分`: semantic self-check by the Her.
  - `系统证据分`: conservative K8s/PVC/LiteLLM evidence score.
  - `最终确认分`: owner-reviewed score when available.
- Treat file mtimes, message timestamps, and memory DB timestamps as different evidence types; do not mix them without labeling.

## Standard Workflow

1. **Confirm the period**
   - Use exact dates, for example `2026-06-07 00:00:00` to `2026-06-13 00:00:00` for an exclusive end.
   - State whether it is a weekly snapshot or a monthly formal report.

2. **Collect backend stats**
   ```bash
   scripts/carher_fusion_diagnosis_stats.py \
     --uid 3 \
     --start "2026-06-07 00:00:00" \
     --end "2026-06-13 00:00:00" \
     --owner-alias "<owner display alias>" \
     --bot-alias "<bot display alias>" \
     --pretty > /tmp/carher-3-fusion-stats.json
   ```
   Capture:
   - `files.recent_count` and `files.recent_by_area`
   - `keyword_recent_file_hits`
   - `memory_db.tables`
   - `memory_db.keyword_fts_hits`
   - `feishu_group_cache.total_recent_messages`
   - `scoring_evidence` as the primary model input for A/B/C/D scoring
   - `scoring_evidence.coverage_summary.bot_aliases_configured`; when false,
     do not interpret `bot_mentions=0` as no one mentioned the Her.

   Prefer `scoring_evidence` for report evaluation. Treat lower-level `files`,
   `feishu_group_cache`, `memory_db`, and `keyword_recent_file_hits` as audit
   details or debugging data, not as something the model should reinterpret
   from scratch each time.

   For batch checks, resolve each Her's owner and bot display aliases from CRD,
   config, or owner mapping before running the script. If aliases are unknown,
   group message counts remain valid but owner-message and bot-mention counts
   are intentionally marked unavailable.

3. **Reconcile key report claims**
   - A1/A3: use Her self-check plus session/memory activity. If exact chat count is unavailable, say it is estimated.
   - A2/A5: use Feishu group cache by message `ts`, not file mtime. If cache has zero recent messages, do not claim high group frequency.
   - B1/B2/B3/B4: use topic evidence from recent files, memory daily notes, and memory FTS hits.
   - C1/C2/C3: distinguish local output, group discussion, formal adoption, and reuse by other Hers.
   - D1/D2: cite remembered rules, corrected failures, and whether the same failure recurred.
   - For batch checks, first compare `scoring_evidence.coverage_summary` and the
     relevant A/B/C/D subsection across Hers. Do not re-open raw PVC files unless
     the aggregate evidence is missing, contradictory, or clearly suspicious.

### Batch evidence extraction

For fleet-wide evaluation, do one evidence extraction pass and feed only the
JSONL/summary to the scorer and report generator:

```bash
scripts/carher_fusion_diagnosis_batch.py \
  --uids 1-500 \
  --start "2026-05-01 00:00:00" \
  --end "2026-06-13 00:00:00" \
  --owner-bot-map /tmp/her-owner-bot-map.json \
  --output /tmp/fusion-evidence-20260501-20260612.jsonl \
  --summary-output /tmp/fusion-evidence-20260501-20260612-summary.json
```

`owner-bot-map` may be either an object keyed by uid or a list of objects:

```json
{
  "3": {
    "her_id": "carher-3",
    "owner_aliases": ["owner display alias"],
    "bot_aliases": ["bot display alias"]
  }
}
```

Use the JSONL rows as compact per-Her model input. Use the summary JSON for
cross-Her baseline metrics (`p50/p75/p90`) before assigning system evidence
scores. Only re-run a single Her or inspect lower-level fields when a JSONL row
has `status=error`, missing aliases, or contradictory evidence.

Each JSONL row now has three layers; later scoring/report steps should prefer
them in this order:

1. `base_metrics`: stable flat evidence row for Feishu Base and report inputs
   (`group_recent_messages`, `group_active_days`, `files_recent_count`,
   `workspace_recent_files`, `memory_chunks`, `topic_summary`, warnings, etc.).
2. `relative_metrics`: per-metric percentile position against the current batch
   (`percentile_rank` and `tier`: `below_p50`, `p50_p75`, `p75_p90`,
   `p90_plus`, or `no_signal`). Use this for cross-Her differentiation.
3. `scoring_evidence`: richer A/B/C/D evidence for a single Her when the model
   needs context for report prose. Do not reopen raw PVC files unless this layer
   is missing or contradictory.

Final report scoring must wait until the batch `summary-output` exists. A single
Her demo score is only a loop validation score, not a final comparable score.

4. **Update Feishu report and Base**
   - Report content can be adapted to the audience, but every numeric field in Base must match the report summary.
   - For Base report links, use a URL-style text field and write the doc URL directly.
   - If K8s verification later changes a prior report, update both the report and Base `数据源摘要` / `备注`.

## Evidence Rules

| Evidence | Use for | Caveat |
|---|---|---|
| Her self-check | Semantic judgment, B/C/D subjective dimensions | Must be owner-reviewable |
| `/data/.openclaw/workspace` mtime | Recent file and output activity | File count is not interaction count |
| `/data/.openclaw/workspace/memory/YYYY-MM-DD.md` | Daily topic memory and correction rules | May include summarized context, not full source |
| `memory/main.sqlite` FTS | Topic existence and indexed session/memory evidence | FTS hits are evidence of topic presence, not score by themselves |
| `feishu-groups/*.jsonl` message `ts` | Group activity, mentions, C2 propagation | Do not use file mtime as message date |
| LiteLLM CSV/API | Model calls, tokens, success rate | Not enough to judge cognitive contribution |

## K8s Access Check

Use the project JumpServer wrapper. If SSH fails, first verify `~/.config/jms/key.json` uses the internal KoKo endpoint:

```text
ssh_host: 10.68.13.189
ssh_port: 2222
```

Then:

```bash
scripts/jms ssh laoyang 'echo ok'
scripts/jms proxy laoyang 16443 172.16.1.163 6443
kubectl --kubeconfig ~/.kube/config -n carher get pods -l app=carher-user,user-id=3 -o wide
```

Close the foreground proxy with `Ctrl-C` when done.

## Failure Modes

- **Her score and system score differ**: do not average silently. Explain which evidence layer sees what and choose the score the user asked for.
- **Group cache has no recent messages**: keep A5/C2 conservative, even if workspace output is strong.
- **Large historical keyword counts**: narrow to the period first. Use all-time memory FTS only as background evidence.
- **K8s unavailable**: mark system evidence unavailable and do not present guessed backend counts.
