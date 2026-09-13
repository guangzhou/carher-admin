# LiteLLM 198 gray rollout manifests

These files are safe templates for the artifact-readiness gate. They do not
contain credentials and set `artifactTemplate: true`, which makes Helm refuse
to render them unless the frozen run copy explicitly sets it to `false`.
Do not apply the checked-in examples as-is. The repeated `2`/`3` image digests
are non-secret examples, not published image identities.

Before a run:

1. Mirror every image into the registry appropriate for the target cluster and
   replace each placeholder with the audited immutable digest. ACK workloads
   use the ACR VPC registry. The 198 IDC K3s rollout uses its verified local
   registry alias `127.0.0.1:5000`; on node 225 this alias is runtime-mapped to
   the registry hosted by node 198. Do not use the ACR VPC hostname on 198/225:
   the IDC nodes cannot resolve it. Public registries and mutable tags remain
   forbidden for rollout manifests.
2. Export the live config and callbacks into a root-only values file. The chart
   renders each snapshot as a release-scoped, checksum-named immutable
   ConfigMap. Use `litellm-gray-rollout/scripts/prepare-values.py`; never hand
   edit `artifactTemplate` or reuse the example digests. The scheduler evidence
   passed to that tool must be a fresh clone/direct observation bound to the
   exact image digest, config SHA-256, and callbacks SHA-256. It records zero
   duplicate scheduler runs, duplicate background jobs, and unexpected control
   writes. LiteLLM has no verified universal environment switch for these
   processes; the frozen runtime shape and observation evidence are the gate.
   Scheduler evidence must include `runtime_sha256` over the effective
   command/args/extraEnv/secretRefs. `prepare-values.py` merges chart defaults
   with the selected profile before applying the live Deployment snapshot and
   rejects evidence captured for a different runtime shape.
   Stop if the 198 clone/direct observation cannot prove the non-prod task path
   is quiet.
3. Copy `clone-secret-templates.yaml` into the root-only run directory, replace
   every placeholder, and apply only that protected copy. Never commit rendered
   Secret manifests or dumps.
   Before resuming any dump/restore Job, verify the live `local-path-config`
   points at `/Data/rancher/storage`, the bound PV paths are under `/Data`, and
   `clone-storage-probe-job.yaml` reports at least 20 GiB available. A healthy
   `/Data` filesystem does not excuse a nearly-full node root filesystem; image
   and containerd storage must be checked separately.
4. Freeze the clone gate manifests before applying them:
   - deploy `clone-redis.yaml` and point qualification workloads at
     `litellm-clone-redis`; it is ephemeral and must never copy production
     Redis keys;
   - run the suspended metadata-dump Job in `litellm-clone`, restore clone A, then
     run the clone migration Job;
   - create the post-migration dump from A and restore that exact artifact into
     B and C before the isolated old/new and concurrent compatibility tests.
5. Package `../chart`, record its SHA-256, and deploy prod, gray,
   and guarded-old from that same frozen package.
6. Verify the allowed network probe resolves/connects and the unlabelled
   attacker probe can neither resolve nor connect. Only labelled dump,
   migration, restore, version-test, and network-probe Pods receive DNS egress.
   Stop if the CNI does not enforce NetworkPolicy.

The migration and version-test Jobs fail closed. `migration-job.yaml` contains
both clone and production templates, but the renderer emits exactly one: clone
is the default; production requires explicit `--migration-target prod` after
clone qualification. Render them with
`litellm-gray-rollout/scripts/prepare-migration-run.py` into the protected run
directory; the renderer replaces example digests/commands, verifies the DDL
ledger and clone-test command files, and preserves `suspend: true`. Review the
rendered output before an approved operator removes `suspend`.

The transactional migration runner accepts only approved
`ALTER TABLE ... ADD COLUMN` ledger entries and runs each entry in its own
transaction. It rejects indexes, constraints, and other `ALTER` forms. The
v1.95 `LiteLLM_SpendLogToolIndex_start_time_idx` is handled separately by the
fixed-purpose `v195-concurrent-index-runner.sh` and the suspended
`v195-concurrent-index-job.yaml`; never add it to the column ledger or run the
ordinary Prisma-generated `CREATE INDEX` against production.

After the clone Jobs finish, copy the root-only runner results and normalized
schema artifacts out of the Pods, then use
`litellm-gray-rollout/scripts/collect-migration-evidence.py`. Do not hand-build
the `check-migration.py` JSON. The collector binds the ledger and immutable
stable/target images, verifies the A/B/C runner result checksums, merges the
reviewed per-DDL lock/rewrite observations, and writes a new mode `0600`
evidence file. Only a structured `check-migration.py` PASS from that file may
qualify the production migration render.
