---
name: carher-ack-add-existing-ecs-node
description: Use when adding an existing Alibaba Cloud ECS host to the CarHer ACK Kubernetes cluster, especially when ACK console or OpenAPI credentials are unavailable but an existing worker node can provide the ACK attach cloud-init script.
metadata:
  requires:
    bins: ["kubectl", "scripts/jms"]
---

# CarHer ACK Add Existing ECS Node

> Add an existing ECS to the CarHer ACK node pool by reusing ACK's own attach cloud-init script from a known-good worker. Do not copy kubelet certs or hand-roll `kubeadm join`.

## When to Use

- User asks to add an ECS/public IP to the Alibaba Cloud ACK/K8s cluster.
- The ECS is reachable via JumpServer (`scripts/jms ssh <asset>`) and should become a dedicated worker.
- ACK Console/OpenAPI credentials are missing or insufficient, but an existing worker was originally joined by ACK.

Do not use this for deleting/draining nodes, changing node pool config, or joining non-CarHer clusters.

## Safety Rules

- Never paste or store full `--use-auth-token`, `--auth-audience`, AK/SK, cookies, or login links in docs or chat.
- Never copy `/etc/kubernetes/kubelet.conf`, `/etc/kubernetes/pki`, bootstrap kubeconfig, or `/var/lib/kubelet/pki` from another node.
- Do not run `attach_node.sh --help`: ACK's script starts real initialization even with `--help`.
- Prefer ACK Console/OpenAPI `AttachInstancesToNodePool` when valid cloud credentials are available; this fallback exists for the no-credential case.

## Inputs to Discover

```bash
# Existing cluster state
kubectl --kubeconfig ~/.kube/config get nodes -o wide
kubectl --kubeconfig ~/.kube/config get nodes \
  -L alibabacloud.com/nodepool-id,alibabacloud.com/ecs-instance-id,node.kubernetes.io/instance-type

# Target ECS facts
scripts/jms list | grep -i '<asset-or-ip>'
scripts/jms ssh <target-asset> 'hostname; ip -br addr; swapon --show || true'
scripts/jms ssh <target-asset> \
  'curl -s --max-time 2 http://100.100.100.200/latest/meta-data/instance-id; echo'
```

Expected target preflight:
- Target is not already in `kubectl get nodes`.
- `kubelet`, `containerd`, and Docker are inactive or disposable.
- swap is off.
- Target can reach ACK internals:

```bash
scripts/jms ssh <target-asset> 'set -e
curl -I --max-time 5 http://aliacs-k8s-ap-southeast-1.oss-ap-southeast-1-internal.aliyuncs.com/public/pkg/run/attach/1.35/attach_node.sh | head
timeout 5 bash -lc "</dev/tcp/apiserver.<cluster-id>.ap-southeast-1.cs.aliyuncs.com/6443" && echo apiserver-ok
timeout 5 bash -lc "</dev/tcp/registry-ap-southeast-1-vpc.ack.aliyuncs.com/443" && echo registry-ok'
```

## Core Workflow

1. **Find a source worker with ACK cloud-init.**

   Use a Ready node in the same node pool, usually `k8s-work-227`.

   ```bash
   scripts/jms ssh k8s-work-227 \
     'ls -la /var/lib/cloud/instances/*/scripts/part-001 /usr/local/bin/ack-tool /etc/acknode 2>/dev/null'
   scripts/jms ssh k8s-work-227 \
     'systemctl cat kubelet | sed -n "1,120p"'
   ```

   Confirm the kubelet labels include the expected cluster and node pool. The current CarHer production values are:
   - cluster: `c215e116fb0a7414287f4be1c31bb4ebc`
   - node pool: `np736bf80c764047449d05ce8840500edf`

2. **Copy only the ACK attach cloud-init script to the target.**

   ```bash
   rm -f /tmp/ack-attach-target.sh
   scripts/jms scp k8s-work-227:/var/lib/cloud/instances/<source-instance-id>/scripts/part-001 /tmp/ack-attach-target.sh
   chmod 600 /tmp/ack-attach-target.sh
   scripts/jms scp /tmp/ack-attach-target.sh <target-asset>:/tmp/ack-attach-target.sh
   rm -f /tmp/ack-attach-target.sh
   ```

   The script is reusable because ACK's `attach_node.sh` reads the target machine's own ECS metadata for instance ID, IP, image, and region.

3. **Run the attach script on the target in the background.**

   ```bash
   scripts/jms ssh <target-asset> 'set -e
   chmod 700 /tmp/ack-attach-target.sh
   mkdir -p /var/log/acs
   nohup bash /tmp/ack-attach-target.sh > /var/log/acs/init-from-codex.log 2>&1 &
   echo $! > /tmp/ack-attach-target.pid
   echo STARTED_PID=$(cat /tmp/ack-attach-target.pid)'
   ```

4. **Watch local install and cluster admission.**

   ```bash
   scripts/jms ssh <target-asset> \
     'ps -p $(cat /tmp/ack-attach-target.pid) -o pid,etime,cmd || true; tail -120 /var/log/acs/init-from-codex.log'

   kubectl --kubeconfig ~/.kube/config get nodes -o wide
   kubectl --kubeconfig ~/.kube/config get pods -A -o wide \
     --field-selector spec.nodeName=<new-node-name>
   ```

   Normal progression:
   - target installs/configures `containerd`;
   - node appears as `NotReady`;
   - `terway`, `csi-plugin`, `kube-proxy`, `node-local-dns`, `node-exporter`, `loongcollector`, and `ack-node-problem-detector` start;
   - node flips to `Ready`;
   - attach log ends with `Worker node joined successfully`.

5. **Verify business scheduling.**

   ```bash
   kubectl --kubeconfig ~/.kube/config get node <new-node-name> \
     -L alibabacloud.com/nodepool-id,alibabacloud.com/ecs-instance-id,node.kubernetes.io/instance-type -o wide

   kubectl --kubeconfig ~/.kube/config get pods -A -o wide \
     --field-selector spec.nodeName=<new-node-name>

   kubectl --kubeconfig ~/.kube/config -n carher get pods \
     --field-selector=status.phase=Pending -o wide

   kubectl --kubeconfig ~/.kube/config top node <new-node-name>
   ```

   A real CarHer pod reaching `Running` on the node verifies CNI, ACR VPC image pulls, PVC/NAS mounts, and kubelet runtime wiring. A CarHer readiness gate such as `carher.io/feishu-ws-ready` can remain false for business reasons and is not a node-join failure by itself.

6. **Clean temporary files on the target.**

   ```bash
   scripts/jms ssh <target-asset> \
     'rm -f /tmp/ack-attach-target.sh /tmp/ack-attach-target.pid; systemctl is-active kubelet; systemctl is-active containerd'
   ```

   Keep `/var/log/acs/` for audit.

## Known-Good Example

On 2026-06-04, ECS `47.84.85.100` / JumpServer asset `dify` joined successfully:

- ECS instance: `i-t4n8gmyjm5ht66ztiqk6`
- node: `ap-southeast-1.172.16.16.122`
- internal IP: `172.16.16.122`
- node pool: `np736bf80c764047449d05ce8840500edf`
- instance type: `ecs.c9i.8xlarge`
- result: node `Ready`, system DaemonSets Running, a CarHer pod reached `2/2 Running`

## Edge Cases

- **ACK OpenAPI 403 from worker RAM role**: existing workers may have only `KubernetesWorkerRole-*`; this can fail even for `DescribeClusterNodePoolDetail`. Use Console/OpenAPI credentials or this cloud-init fallback.
- **`attach_node.sh --help` starts initialization**: kill the process immediately if this happens and verify the source node remains Ready.
- **Node appears `NotReady` first**: wait for terway and CSI. Early `NetworkPluginNotReady` and transient ConfigMap/Secret mount warnings are expected during the first minute.
- **Large image pull looks stuck**: new nodes have empty image cache. H75 runtime images can be several GB; watch events for `Successfully pulled` rather than assuming failure.
- **JMS token permission denied**: retry the same `scripts/jms ssh` command; token acquisition can fail transiently. Do not switch back to old direct SSH paths.
