package metrics

import (
	"github.com/prometheus/client_golang/prometheus"
	"sigs.k8s.io/controller-runtime/pkg/metrics"
)

var (
	InstancesTotal = prometheus.NewGaugeVec(
		prometheus.GaugeOpts{
			Name: "carher_instances_total",
			Help: "Total number of HerInstance CRDs by phase",
		},
		[]string{"phase", "deploy_group"},
	)

	FeishuWSConnected = prometheus.NewGaugeVec(
		prometheus.GaugeOpts{
			Name: "carher_feishu_ws_connected",
			Help: "Whether the Feishu WebSocket is connected (1=yes, 0=no)",
		},
		[]string{"user_id", "name"},
	)

	PodRestarts = prometheus.NewGaugeVec(
		prometheus.GaugeOpts{
			Name: "carher_pod_restarts",
			Help: "Pod restart count per instance",
		},
		[]string{"user_id", "name"},
	)

	ReconcileDuration = prometheus.NewHistogramVec(
		prometheus.HistogramOpts{
			Name:    "carher_reconcile_duration_seconds",
			Help:    "Duration of reconcile operations",
			Buckets: prometheus.DefBuckets,
		},
		[]string{"action"},
	)

	HealthCheckDuration = prometheus.NewHistogram(
		prometheus.HistogramOpts{
			Name:    "carher_health_check_duration_seconds",
			Help:    "Duration of full health check cycle across all instances",
			Buckets: []float64{1, 5, 10, 30, 60, 120},
		},
	)

	KnownBotsCount = prometheus.NewGauge(
		prometheus.GaugeOpts{
			Name: "carher_known_bots_total",
			Help: "Total number of known bots in shared ConfigMap",
		},
	)

	DeployActive = prometheus.NewGauge(
		prometheus.GaugeOpts{
			Name: "carher_deploy_active",
			Help: "Whether a deploy is currently active (1=yes, 0=no)",
		},
	)

	SelfHealTotal = prometheus.NewCounter(
		prometheus.CounterOpts{
			Name: "carher_self_heal_total",
			Help: "Total number of self-healing Pod recreations",
		},
	)
)

func init() {
	metrics.Registry.MustRegister(
		InstancesTotal,
		FeishuWSConnected,
		PodRestarts,
		ReconcileDuration,
		HealthCheckDuration,
		KnownBotsCount,
		DeployActive,
		SelfHealTotal,
	)
}
