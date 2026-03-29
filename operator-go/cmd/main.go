package main

import (
	"os"

	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/runtime"
	"k8s.io/apimachinery/pkg/runtime/schema"
	utilruntime "k8s.io/apimachinery/pkg/util/runtime"
	clientgoscheme "k8s.io/client-go/kubernetes/scheme"
	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/healthz"
	"sigs.k8s.io/controller-runtime/pkg/log/zap"
	metricsserver "sigs.k8s.io/controller-runtime/pkg/metrics/server"

	herv1 "github.com/guangzhou/carher-admin/operator-go/api/v1alpha1"
	"github.com/guangzhou/carher-admin/operator-go/internal/controller"
	_ "github.com/guangzhou/carher-admin/operator-go/internal/metrics" // register metrics
)

var scheme = runtime.NewScheme()

func init() {
	utilruntime.Must(clientgoscheme.AddToScheme(scheme))

	// Register HerInstance CRD types with full scheme support
	gv := schema.GroupVersion{Group: "carher.io", Version: "v1alpha1"}
	scheme.AddKnownTypes(gv,
		&herv1.HerInstance{},
		&herv1.HerInstanceList{},
	)
	metav1.AddToGroupVersion(scheme, gv)
}

func main() {
	ctrl.SetLogger(zap.New(zap.UseDevMode(false)))
	logger := ctrl.Log.WithName("setup")

	mgr, err := ctrl.NewManager(ctrl.GetConfigOrDie(), ctrl.Options{
		Scheme:                 scheme,
		HealthProbeBindAddress: ":8081",
		Metrics:                metricsserver.Options{BindAddress: ":8080"},
		LeaderElection:         true,
		LeaderElectionID:       "carher-operator-leader",
	})
	if err != nil {
		logger.Error(err, "Unable to start manager")
		os.Exit(1)
	}

	// Initialize knownBots manager
	knownBots := controller.NewKnownBotsManager(mgr.GetClient())

	// Register reconciler
	if err := (&controller.HerInstanceReconciler{
		Client:    mgr.GetClient(),
		Scheme:    mgr.GetScheme(),
		KnownBots: knownBots,
	}).SetupWithManager(mgr); err != nil {
		logger.Error(err, "Unable to create controller")
		os.Exit(1)
	}

	// Register health checker as a Runnable (concurrent goroutine pool)
	if err := mgr.Add(&controller.HealthChecker{
		Client: mgr.GetClient(),
	}); err != nil {
		logger.Error(err, "Unable to add health checker")
		os.Exit(1)
	}

	// Register knownBots manager as a Runnable
	if err := mgr.Add(knownBots); err != nil {
		logger.Error(err, "Unable to add knownBots manager")
		os.Exit(1)
	}

	// Health/ready probes
	mgr.AddHealthzCheck("healthz", healthz.Ping)
	mgr.AddReadyzCheck("readyz", healthz.Ping)

	logger.Info("Starting CarHer operator",
		"health-workers", controller.HealthCheckWorkers,
		"health-interval", controller.HealthCheckInterval,
	)
	if err := mgr.Start(ctrl.SetupSignalHandler()); err != nil {
		logger.Error(err, "Problem running manager")
		os.Exit(1)
	}
}
