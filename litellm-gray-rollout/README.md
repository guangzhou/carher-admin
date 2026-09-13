# LiteLLM 198 Gray Rollout

This directory is the single ownership boundary for the LiteLLM 198 gray
upgrade. Keep rollout code, tests, manifests, chart sources, fixtures, and
operator documentation here so a frozen source snapshot contains every
reviewed artifact.

## Layout

| Path | Purpose |
|---|---|
| `scripts/` | Routing transactions, evidence collectors, migration runners, and nginx fixtures |
| `tests/` | Offline pytest contract suite for all rollout artifacts |
| `k8s/` | Safe templates for clone qualification, migration, and release values |
| `chart/` | Shared Helm chart for prod, gray, and guarded-old releases |
| `docs/` | Design baseline and per-run frozen execution record template |

The templates are not directly deployable. They contain placeholder digests
and fail-closed markers that must be replaced only in a root-owned frozen run
directory. Never store credentials, database dumps, rendered Secrets, real
virtual keys, or unredacted runtime evidence in this repository.

This package targets the two-node 198 IDC K3s cluster, not ACK. Images use the
verified `127.0.0.1:5000` local-registry alias with immutable digests. Node 225
(`aiyjy-litellm-standby`) owns clone and no-traffic qualification work; node
198 remains the production/DB node until the traffic gates explicitly pass.
The K3s `local-path` provisioner must resolve to `/Data/rancher/storage` on both
nodes. The clone storage probe fails unless the mounted filesystem has at least
20 GiB available; never infer clone safety from the smaller root filesystem.

## Verify

Run the complete offline suite from the repository root:

```bash
python3 litellm-gray-rollout/scripts/verify-readiness.py \
  --output /root/litellm-gray-run/readiness.json
```

Production mode fails closed when Helm, stock nginx, ShellCheck, or kubeconform
is missing. `--developer-mode` may be used locally, but produces
`DEVELOPER_PASS`, never production readiness. The live gate additionally
requires server-side dry-run and allowed/attacker NetworkPolicy probes. See
`docs/litellm-198-gray-upgrade-plan.md` for the complete gate and
`docs/litellm-198-gray-rollout-runbook.md` for the frozen execution record.

## Entry Points

- Design and safety rules: `docs/litellm-198-gray-upgrade-plan.md`
- Operator manual: `docs/litellm-198-gray-operator-manual.md`
- Execution record template: `docs/litellm-198-gray-rollout-runbook.md`
- Manifest preparation: `k8s/README.md`
- Production nginx template preparation: `scripts/production-nginx/README.md`
- Nginx routing fixture: `scripts/fixtures/nginx/README.md`
- Readiness gate: `scripts/verify-readiness.py`
