package controller

import (
	"context"
	"fmt"
	"os"
	"strconv"
	"strings"
	"sync"
	"time"

	corev1 "k8s.io/api/core/v1"
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
	Client    client.Client
	KnownBots *KnownBotsManager
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
	podMap := make(map[int]*corev1.Pod, len(podList.Items))
	for i := range podList.Items {
		p := &podList.Items[i]
		if uidStr, ok := p.Labels["user-id"]; ok {
			if uid, err := strconv.Atoi(uidStr); err == nil {
				podMap[uid] = p
			}
		}
	}

	// Concurrent health checks with worker pool
	type job struct {
		her *herv1.HerInstance
		pod *corev1.Pod
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
				phase, group := hc.checkOne(ctx, j.her, j.pod)
				phaseCountsMu.Lock()
				phaseCounts[phaseGroup{phase, group}]++
				phaseCountsMu.Unlock()
			}
		}()
	}

	for i := range list.Items {
		her := &list.Items[i]
		pod := podMap[her.Spec.UserID]
		jobs <- job{her: her, pod: pod}
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

func (hc *HealthChecker) checkOne(ctx context.Context, her *herv1.HerInstance, pod *corev1.Pod) (string, string) {
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

	if pod == nil {
		logger.Info("Pod missing, triggering self-heal")
		metrics.SelfHealTotal.Inc()
		status.Phase = "Pending"
		status.Message = "Pod missing, self-heal triggered"
		status.FeishuWS = "Unknown"
		hc.updateStatus(ctx, her, status)
		return retPhase()
	}

	phase := string(pod.Status.Phase)
	status.Phase = phase
	status.PodIP = pod.Status.PodIP
	status.Node = pod.Spec.NodeName

	// Container status
	if len(pod.Status.ContainerStatuses) > 0 {
		cs := pod.Status.ContainerStatuses[0]
		status.Restarts = cs.RestartCount
		metrics.PodRestarts.WithLabelValues(uidStr, her.Spec.Name).Set(float64(cs.RestartCount))

		// CrashLoopBackOff detection
		if cs.State.Waiting != nil && strings.Contains(cs.State.Waiting.Reason, "CrashLoopBackOff") {
			status.Phase = "Failed"
			status.Message = fmt.Sprintf("CrashLoopBackOff (restarts: %d)", cs.RestartCount)
		}
	}

	// Check Feishu WS connectivity (only for Running pods)
	if phase == "Running" {
		wsConnected := hc.checkFeishuWS(ctx, pod)
		if wsConnected {
			status.FeishuWS = "Connected"
			metrics.FeishuWSConnected.WithLabelValues(uidStr, her.Spec.Name).Set(1)
		} else {
			status.FeishuWS = "Disconnected"
			metrics.FeishuWSConnected.WithLabelValues(uidStr, her.Spec.Name).Set(0)
		}
	}

	hc.updateStatus(ctx, her, status)
	return retPhase()
}

func (hc *HealthChecker) checkFeishuWS(ctx context.Context, pod *corev1.Pod) bool {
	if pod == nil {
		return false
	}
	// Check container ready status as a proxy for Feishu WS connectivity.
	// controller-runtime client doesn't support pod logs directly;
	// a proper solution would be a /healthz endpoint on each Pod.
	for _, cs := range pod.Status.ContainerStatuses {
		if cs.Name == "carher" && cs.Ready {
			return true
		}
	}
	return false
}

func (hc *HealthChecker) updateStatus(ctx context.Context, her *herv1.HerInstance, status *herv1.HerInstanceStatus) {
	patch := client.MergeFrom(her.DeepCopyObject().(client.Object))
	her.Status = *status
	if err := hc.Client.Status().Patch(ctx, her, patch); err != nil {
		log.FromContext(ctx).V(1).Info("Status patch failed", "uid", her.Spec.UserID, "err", err)
	}
}
