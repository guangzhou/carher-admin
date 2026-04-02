package controller

import (
	"context"
	"encoding/json"
	"fmt"
	"net/http"
	"os"
	"strconv"
	"strings"
	"sync"
	"time"

	corev1 "k8s.io/api/core/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/log"

	herv1 "github.com/guangzhou/carher-admin/operator-go/api/v1alpha1"
	"github.com/guangzhou/carher-admin/operator-go/internal/metrics"
)

var (
	HealthCheckInterval = 30 * time.Second
	HealthCheckWorkers  = getEnvInt("HEALTH_CHECK_WORKERS", 50)
)

func getEnvInt(key string, fallback int) int {
	if v := os.Getenv(key); v != "" {
		if n, err := strconv.Atoi(v); err == nil && n > 0 {
			return n
		}
	}
	return fallback
}

// HealthChecker runs periodic concurrent health checks across all HerInstances.
type HealthChecker struct {
	Client client.Client
}

// Start runs the health check loop. Call from manager startup.
func (hc *HealthChecker) Start(ctx context.Context) error {
	logger := log.FromContext(ctx).WithName("health-checker")
	logger.Info("Starting concurrent health checker", "workers", HealthCheckWorkers, "interval", HealthCheckInterval)

	ticker := time.NewTicker(HealthCheckInterval)
	defer ticker.Stop()

	for {
		select {
		case <-ctx.Done():
			return nil
		case <-ticker.C:
			hc.runCycle(ctx)
		}
	}
}

func (hc *HealthChecker) runCycle(ctx context.Context) {
	logger := log.FromContext(ctx).WithName("health-checker")
	start := time.Now()

	// List all HerInstances
	var list herv1.HerInstanceList
	if err := hc.Client.List(ctx, &list, client.InNamespace(Namespace)); err != nil {
		logger.Error(err, "Failed to list HerInstances")
		return
	}

	// List all pods once (batch query, not per-instance)
	var podList corev1.PodList
	if err := hc.Client.List(ctx, &podList, client.InNamespace(Namespace), client.MatchingLabels{"app": "carher-user"}); err != nil {
		logger.Error(err, "Failed to list pods")
		return
	}
	podsByUID := make(map[int][]*corev1.Pod, len(podList.Items))
	for i := range podList.Items {
		p := &podList.Items[i]
		if uidStr, ok := p.Labels["user-id"]; ok {
			if uid, err := strconv.Atoi(uidStr); err == nil {
				podsByUID[uid] = append(podsByUID[uid], p)
			}
		}
	}

	// Concurrent health checks with worker pool
	type job struct {
		her  *herv1.HerInstance
		pods []*corev1.Pod
	}
	jobs := make(chan job, len(list.Items))
	var wg sync.WaitGroup

	// Collect metrics into a local map, then set gauges atomically after the cycle.
	// Avoids Reset() which causes Prometheus to see zero-valued metrics briefly.
	type phaseGroup struct{ phase, group string }
	phaseCounts := make(map[phaseGroup]float64)
	var phaseCountsMu sync.Mutex

	for w := 0; w < HealthCheckWorkers; w++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			for j := range jobs {
				phase, group := hc.checkOne(ctx, j.her, j.pods)
				phaseCountsMu.Lock()
				phaseCounts[phaseGroup{phase, group}]++
				phaseCountsMu.Unlock()
			}
		}()
	}

	for i := range list.Items {
		her := &list.Items[i]
		pods := podsByUID[her.Spec.UserID]
		jobs <- job{her: her, pods: pods}
	}
	close(jobs)
	wg.Wait()

	// Atomically replace gauge values (no Reset gap visible to Prometheus)
	metrics.InstancesTotal.Reset()
	for pg, count := range phaseCounts {
		metrics.InstancesTotal.WithLabelValues(pg.phase, pg.group).Set(count)
	}

	duration := time.Since(start)
	metrics.HealthCheckDuration.Observe(duration.Seconds())
	logger.Info("Health check cycle complete", "instances", len(list.Items), "duration", duration.Round(time.Millisecond))
}

func (hc *HealthChecker) checkOne(ctx context.Context, her *herv1.HerInstance, pods []*corev1.Pod) (string, string) {
	uid := her.Spec.UserID
	uidStr := strconv.Itoa(uid)
	logger := log.FromContext(ctx).WithName("health").WithValues("uid", uid)

	status := her.Status.DeepCopy()
	status.LastHealthCheck = time.Now().UTC().Format(time.RFC3339)

	retPhase := func() (string, string) {
		phase := status.Phase
		if phase == "" {
			phase = "Unknown"
		}
		group := her.Spec.DeployGroup
		if group == "" {
			group = "stable"
		}
		return phase, group
	}

	if her.Spec.Paused {
		status.Phase = "Paused"
		hc.updateStatus(ctx, her, status)
		return retPhase()
	}

	if len(pods) == 0 {
		if her.Status.Phase != "Pending" {
			logger.Info("Pod missing, triggering self-heal")
			metrics.SelfHealTotal.Inc()
		}
		status.Phase = "Pending"
		status.Message = "Pod missing, self-heal triggered"
		status.FeishuWS = "Unknown"
		hc.updateStatus(ctx, her, status)
		return retPhase()
	}

	// During rolling updates, multiple pods may exist for the same uid.
	// Pick the newest Running pod for CRD status, but check ReadinessGate on ALL.
	var primary *corev1.Pod
	for _, p := range pods {
		if string(p.Status.Phase) == "Running" {
			if primary == nil || p.CreationTimestamp.After(primary.CreationTimestamp.Time) {
				primary = p
			}
		}
	}
	if primary == nil {
		primary = pods[0]
	}

	phase := string(primary.Status.Phase)
	status.Phase = phase
	status.PodIP = primary.Status.PodIP
	status.Node = primary.Spec.NodeName

	// Container status (from primary pod)
	for _, cs := range primary.Status.ContainerStatuses {
		if cs.Name == "carher" {
			status.Restarts = cs.RestartCount
			metrics.PodRestarts.WithLabelValues(uidStr, her.Spec.Name).Set(float64(cs.RestartCount))
			if cs.State.Waiting != nil && strings.Contains(cs.State.Waiting.Reason, "CrashLoopBackOff") {
				status.Phase = "Failed"
				status.Message = fmt.Sprintf("CrashLoopBackOff (restarts: %d)", cs.RestartCount)
			}
			break
		}
	}

	// Set ReadinessGate on EVERY Running pod (critical for rolling updates:
	// the new pod must get its gate set, or the rollout stalls forever).
	anyConnected := false
	for _, p := range pods {
		if string(p.Status.Phase) != "Running" {
			continue
		}
		wsConnected := hc.checkFeishuWS(ctx, p)
		hc.setReadinessGate(ctx, p, wsConnected)
		if wsConnected {
			anyConnected = true
		}
	}

	if anyConnected {
		status.FeishuWS = "Connected"
		metrics.FeishuWSConnected.WithLabelValues(uidStr, her.Spec.Name).Set(1)
	} else if phase == "Running" {
		status.FeishuWS = "Disconnected"
		metrics.FeishuWSConnected.WithLabelValues(uidStr, her.Spec.Name).Set(0)
	}

	hc.updateStatus(ctx, her, status)
	return retPhase()
}

var healthClient = &http.Client{Timeout: 3 * time.Second}

func (hc *HealthChecker) checkFeishuWS(ctx context.Context, pod *corev1.Pod) bool {
	if pod == nil || pod.Status.PodIP == "" {
		return false
	}

	// Try the pod's /healthz endpoint for accurate Feishu WS status.
	url := fmt.Sprintf("http://%s:18789/healthz", pod.Status.PodIP)
	resp, err := healthClient.Get(url)
	if err == nil {
		defer resp.Body.Close()
		if resp.StatusCode == http.StatusOK {
			var health struct {
				FeishuWS string `json:"feishuWS"`
				OK       bool   `json:"ok"`
			}
			if json.NewDecoder(resp.Body).Decode(&health) == nil {
				if health.FeishuWS == "connected" || health.FeishuWS == "Connected" {
					return true
				}
				if health.FeishuWS != "" {
					return false
				}
				return health.OK
			}
			return true
		}
		return false
	}

	// Fallback: container ready + minimum uptime (for pods without /healthz).
	// TCP ready alone isn't sufficient — Feishu WS typically takes 5-15s after
	// container start. Require 15s uptime to avoid setting ReadinessGate prematurely.
	for _, cs := range pod.Status.ContainerStatuses {
		if cs.Name == "carher" && cs.Ready && cs.State.Running != nil {
			uptime := time.Since(cs.State.Running.StartedAt.Time)
			return uptime >= 15*time.Second
		}
	}
	return false
}

// setReadinessGate patches the pod's status conditions to satisfy the ReadinessGate.
// K8s rolling update will not terminate the old pod until the new pod's gate is True.
func (hc *HealthChecker) setReadinessGate(ctx context.Context, pod *corev1.Pod, ready bool) {
	if pod == nil {
		return
	}

	status := corev1.ConditionFalse
	if ready {
		status = corev1.ConditionTrue
	}

	for _, c := range pod.Status.Conditions {
		if c.Type == FeishuWSReadinessGate && c.Status == status {
			return
		}
	}

	patch := client.MergeFrom(pod.DeepCopy())
	condition := corev1.PodCondition{
		Type:               FeishuWSReadinessGate,
		Status:             status,
		LastTransitionTime: metav1.Now(),
	}

	found := false
	for i, c := range pod.Status.Conditions {
		if c.Type == FeishuWSReadinessGate {
			pod.Status.Conditions[i] = condition
			found = true
			break
		}
	}
	if !found {
		pod.Status.Conditions = append(pod.Status.Conditions, condition)
	}

	if err := hc.Client.Status().Patch(ctx, pod, patch); err != nil {
		log.FromContext(ctx).V(1).Info("Failed to set readiness gate", "pod", pod.Name, "ready", ready, "err", err)
	}
}

func (hc *HealthChecker) updateStatus(ctx context.Context, her *herv1.HerInstance, status *herv1.HerInstanceStatus) {
	patch := client.MergeFrom(her.DeepCopyObject().(client.Object))
	her.Status = *status
	if err := hc.Client.Status().Patch(ctx, her, patch); err != nil {
		log.FromContext(ctx).V(1).Info("Status patch failed", "uid", her.Spec.UserID, "err", err)
	}
}
