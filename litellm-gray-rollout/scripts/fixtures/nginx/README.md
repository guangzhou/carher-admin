# LiteLLM 198 nginx gray fixture

This fixture contains fake keys only. It serves two purposes:

1. `route_model.py` verifies the routing state machine without requiring nginx.
2. `run_fixture.py` renders the full candidate config and, when nginx 1.18 is
   available, starts three fake upstreams and sends real HTTP requests through
   nginx to prove prod/gray/guarded-old routing and `/pro/` rewrite behavior.

Run the offline matrix:

```bash
.venv/bin/python litellm-gray-rollout/scripts/fixtures/nginx/run_fixture.py --model-only
```

Run the required runtime gate with an installed nginx binary. This starts real
prod/gray/guarded-old HTTP stubs, launches nginx in the foreground, and sends
every case in `cases.json` through nginx. A successful `nginx -t` alone cannot
pass this command:

```bash
.venv/bin/python litellm-gray-rollout/scripts/fixtures/nginx/run_fixture.py
```

If nginx is unavailable, the default command returns JSON `status=FAIL` and
exit code `2`. Environments where absence is an expected test skip may opt in
to a machine-readable skip (JSON `status=SKIP`, exit code `77`):

```bash
.venv/bin/python litellm-gray-rollout/scripts/fixtures/nginx/run_fixture.py \
  --skip-if-missing
```

Neither missing-nginx outcome is a runtime PASS. The offline `--model-only`
matrix is useful for fast feedback but does not satisfy the artifact readiness
gate.

For production, render `@@GENERATION_DIR@@` as the root-only active symlink
managed by the rollout scripts. Never put real virtual keys in this directory.
