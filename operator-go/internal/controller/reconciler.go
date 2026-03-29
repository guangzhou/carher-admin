package controller

import (
	"context"
	"crypto/md5"
	"encoding/base64"
	"encoding/json"
	"fmt"
	"strconv"
	"strings"
	"time"

	corev1 "k8s.io/api/core/v1"
	"k8s.io/apimachinery/pkg/api/errors"
	"k8s.io/apimachinery/pkg/api/resource"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/runtime"
	"k8s.io/apimachinery/pkg/types"
	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/log"

	herv1 "github.com/guangzhou/carher-admin/operator-go/api/v1alpha1"
	"github.com/guangzhou/carher-admin/operator-go/internal/metrics"
)

const (
	Namespace = "carher"
	ACR       = "cltx-her-ck-registry.ap-southeast-1.cr.aliyuncs.com/her/carher"
)

type HerInstanceReconciler struct {
	client.Client
	Scheme    *runtime.Scheme
	KnownBots *KnownBotsManager
}

func (r *HerInstanceReconciler) Reconcile(ctx context.Context, req ctrl.Request) (ctrl.Result, error) {
	logger := log.FromContext(ctx)
	start := time.Now()

	var her herv1.HerInstance
	if err := r.Get(ctx, req.NamespacedName, &her); err != nil {
		if errors.IsNotFound(err) {
			return ctrl.Result{}, nil
		}
		return ctrl.Result{}, err
	}

	uid := her.Spec.UserID
	action := "reconcile"

	defer func() {
		metrics.ReconcileDuration.WithLabelValues(action).Observe(time.Since(start).Seconds())
	}()

	// Handle deletion
	if !her.DeletionTimestamp.IsZero() {
		action = "delete"
		logger.Info("Deleting", "uid", uid)
		r.deletePod(ctx, uid)
		r.deleteConfigMap(ctx, uid)
		// PVC intentionally preserved
		r.KnownBots.MarkDirty()
		return ctrl.Result{}, nil
	}

	// Handle paused
	if her.Spec.Paused {
		if her.Status.Phase != "Paused" {
			r.deletePod(ctx, uid)
			her.Status.Phase = "Paused"
			if err := r.Status().Update(ctx, &her); err != nil {
				logger.V(1).Info("Status update failed", "uid", uid, "err", err)
			}
		}
		return ctrl.Result{}, nil
	}

	// Ensure PVC
	if err := r.ensurePVC(ctx, uid); err != nil {
		logger.Error(err, "Failed to ensure PVC", "uid", uid)
		return ctrl.Result{RequeueAfter: 30 * time.Second}, err
	}

	// Generate and apply config
	configHash, err := r.applyConfig(ctx, &her)
	if err != nil {
		logger.Error(err, "Failed to apply config", "uid", uid)
		return ctrl.Result{RequeueAfter: 15 * time.Second}, err
	}

	// Check if Pod needs recreation
	needRecreate := false
	pod, podErr := r.getPod(ctx, uid)

	if podErr != nil || pod == nil {
		needRecreate = true
		action = "create"
	} else {
		// Image changed?
		currentImage := ""
		if len(pod.Spec.Containers) > 0 {
			currentImage = pod.Spec.Containers[0].Image
		}
		desiredImage := fmt.Sprintf("%s:%s", ACR, her.Spec.Image)
		if currentImage != desiredImage {
			needRecreate = true
			action = "update-image"
		}
		// Config changed?
		if her.Status.ConfigHash != "" && her.Status.ConfigHash != configHash {
			needRecreate = true
			action = "update-config"
		}
	}

	if needRecreate {
		logger.Info("Recreating pod", "uid", uid, "action", action, "image", her.Spec.Image)
		if err := r.deletePod(ctx, uid); err != nil {
			logger.V(1).Info("Delete pod returned error (may be ok)", "uid", uid, "err", err)
		}
		if err := r.createPod(ctx, &her); err != nil {
			if errors.IsAlreadyExists(err) {
				// Pod still terminating, requeue quickly
				return ctrl.Result{RequeueAfter: 3 * time.Second}, nil
			}
			logger.Error(err, "Failed to create pod", "uid", uid)
			her.Status.Phase = "Failed"
			her.Status.Message = err.Error()
			if uerr := r.Status().Update(ctx, &her); uerr != nil {
				logger.V(1).Info("Status update failed", "uid", uid, "err", uerr)
			}
			return ctrl.Result{RequeueAfter: 30 * time.Second}, err
		}
		her.Status.Phase = "Pending"
	}

	her.Status.ConfigHash = configHash
	if err := r.Status().Update(ctx, &her); err != nil {
		logger.V(1).Info("Status update failed", "uid", uid, "err", err)
	}

	// Check if bot fields changed (triggers knownBots rebuild)
	r.KnownBots.MarkDirty()

	return ctrl.Result{RequeueAfter: 30 * time.Second}, nil
}

func (r *HerInstanceReconciler) SetupWithManager(mgr ctrl.Manager) error {
	return ctrl.NewControllerManagedBy(mgr).
		For(&herv1.HerInstance{}).
		Complete(r)
}

// ── Config generation ──

func (r *HerInstanceReconciler) applyConfig(ctx context.Context, her *herv1.HerInstance) (string, error) {
	uid := her.Spec.UserID

	// Read appSecret from K8s Secret
	appSecret := ""
	secretName := her.Spec.AppSecretRef
	if secretName == "" {
		secretName = fmt.Sprintf("carher-%d-secret", uid)
	}
	var secret corev1.Secret
	if err := r.Get(ctx, types.NamespacedName{Name: secretName, Namespace: Namespace}, &secret); err == nil {
		if v, ok := secret.Data["app_secret"]; ok {
			appSecret = string(v)
		}
	}

	knownBots, knownBotOpenIDs := r.KnownBots.Get()

	configJSON := GenerateOpenclawJSON(ConfigInput{
		ID:              uid,
		Name:            her.Spec.Name,
		Model:           her.Spec.Model,
		AppID:           her.Spec.AppID,
		AppSecret:       appSecret,
		Prefix:          her.Spec.Prefix,
		Owner:           her.Spec.Owner,
		Provider:        her.Spec.Provider,
		BotOpenID:       her.Spec.BotOpenID,
		KnownBots:       knownBots,
		KnownBotOpenIDs: knownBotOpenIDs,
	})

	hash := fmt.Sprintf("%x", md5.Sum([]byte(configJSON)))[:12]

	// Apply ConfigMap
	cmName := fmt.Sprintf("carher-%d-user-config", uid)
	cm := &corev1.ConfigMap{
		ObjectMeta: metav1.ObjectMeta{
			Name:      cmName,
			Namespace: Namespace,
			Labels: map[string]string{
				"app":        "carher-user",
				"user-id":    strconv.Itoa(uid),
				"managed-by": "carher-operator",
			},
		},
		Data: map[string]string{"openclaw.json": configJSON},
	}

	var existing corev1.ConfigMap
	err := r.Get(ctx, types.NamespacedName{Name: cmName, Namespace: Namespace}, &existing)
	if errors.IsNotFound(err) {
		return hash, r.Create(ctx, cm)
	} else if err != nil {
		return hash, err
	}
	existing.Data = cm.Data
	existing.Labels = cm.Labels
	return hash, r.Update(ctx, &existing)
}

// ── Pod lifecycle ──

func (r *HerInstanceReconciler) getPod(ctx context.Context, uid int) (*corev1.Pod, error) {
	var pod corev1.Pod
	err := r.Get(ctx, types.NamespacedName{Name: fmt.Sprintf("carher-%d", uid), Namespace: Namespace}, &pod)
	if errors.IsNotFound(err) {
		return nil, nil
	}
	return &pod, err
}

func (r *HerInstanceReconciler) createPod(ctx context.Context, her *herv1.HerInstance) error {
	uid := her.Spec.UserID
	imageTag := her.Spec.Image
	if imageTag == "" {
		imageTag = "v20260328"
	}
	prefix := her.Spec.Prefix
	if prefix == "" {
		prefix = "s1"
	}
	pfx := prefix + "-"

	pod := &corev1.Pod{
		ObjectMeta: metav1.ObjectMeta{
			Name:      fmt.Sprintf("carher-%d", uid),
			Namespace: Namespace,
			Labels: map[string]string{
				"app":        "carher-user",
				"user-id":    strconv.Itoa(uid),
				"managed-by": "carher-operator",
			},
			Annotations: map[string]string{
				"carher.io/deploy-group": her.Spec.DeployGroup,
			},
		},
		Spec: corev1.PodSpec{
			ImagePullSecrets: []corev1.LocalObjectReference{{Name: "acr-secret"}},
			RestartPolicy:    corev1.RestartPolicyAlways,
			Containers: []corev1.Container{{
				Name:  "carher",
				Image: fmt.Sprintf("%s:%s", ACR, imageTag),
				Ports: []corev1.ContainerPort{
					{ContainerPort: 18789, Name: "gateway"},
					{ContainerPort: 18790, Name: "realtime"},
					{ContainerPort: 8000, Name: "frontend"},
					{ContainerPort: 8080, Name: "ws-proxy"},
					{ContainerPort: 18891, Name: "oauth"},
				},
				Env: []corev1.EnvVar{
					{Name: "HOME", Value: "/data"},
					{Name: "OPENCLAW_INSTANCE_ID", Value: fmt.Sprintf("carher-%d-k8s", uid)},
					{Name: "NODE_OPTIONS", Value: "--max-old-space-size=1536"},
					{Name: "GOOGLE_APPLICATION_CREDENTIALS", Value: "/gcloud/application_default_credentials.json"},
					{Name: "VOICE_FE_HOST", Value: fmt.Sprintf("%su%d-fe.carher.net", pfx, uid)},
					{Name: "VOICE_PROXY_HOST", Value: fmt.Sprintf("%su%d-proxy.carher.net", pfx, uid)},
				},
				EnvFrom: []corev1.EnvFromSource{{
					SecretRef: &corev1.SecretEnvSource{LocalObjectReference: corev1.LocalObjectReference{Name: "carher-env-keys"}},
				}},
				Resources: corev1.ResourceRequirements{
					Requests: corev1.ResourceList{
						corev1.ResourceCPU:    resource.MustParse("500m"),
						corev1.ResourceMemory: resource.MustParse("1Gi"),
					},
					Limits: corev1.ResourceList{
						corev1.ResourceCPU:    resource.MustParse("2"),
						corev1.ResourceMemory: resource.MustParse("2Gi"),
					},
				},
				VolumeMounts: []corev1.VolumeMount{
					{Name: "user-data", MountPath: "/data/.openclaw"},
					{Name: "user-config", MountPath: "/data/.openclaw/openclaw.json", SubPath: "openclaw.json"},
					{Name: "base-config", MountPath: "/data/.openclaw/carher-config.json", SubPath: "carher-config.json"},
					{Name: "base-config", MountPath: "/data/.openclaw/shared-config.json5", SubPath: "shared-config.json5"},
					{Name: "gcloud-adc", MountPath: "/gcloud/application_default_credentials.json", SubPath: "application_default_credentials.json", ReadOnly: true},
					{Name: "shared-skills", MountPath: "/data/.openclaw/skills", ReadOnly: true},
					{Name: "dept-skills", MountPath: "/data/.agents/skills", ReadOnly: true},
					{Name: "user-sessions", MountPath: "/data/.openclaw/sessions", SubPath: fmt.Sprintf("sessions/%d", uid)},
				},
			}},
			Volumes: []corev1.Volume{
				{Name: "user-data", VolumeSource: corev1.VolumeSource{PersistentVolumeClaim: &corev1.PersistentVolumeClaimVolumeSource{ClaimName: fmt.Sprintf("carher-%d-data", uid)}}},
				{Name: "user-config", VolumeSource: corev1.VolumeSource{ConfigMap: &corev1.ConfigMapVolumeSource{LocalObjectReference: corev1.LocalObjectReference{Name: fmt.Sprintf("carher-%d-user-config", uid)}}}},
				{Name: "base-config", VolumeSource: corev1.VolumeSource{ConfigMap: &corev1.ConfigMapVolumeSource{LocalObjectReference: corev1.LocalObjectReference{Name: "carher-base-config"}}}},
				{Name: "gcloud-adc", VolumeSource: corev1.VolumeSource{Secret: &corev1.SecretVolumeSource{SecretName: "carher-gcloud-adc"}}},
				{Name: "shared-skills", VolumeSource: corev1.VolumeSource{PersistentVolumeClaim: &corev1.PersistentVolumeClaimVolumeSource{ClaimName: "carher-shared-skills", ReadOnly: true}}},
				{Name: "dept-skills", VolumeSource: corev1.VolumeSource{PersistentVolumeClaim: &corev1.PersistentVolumeClaimVolumeSource{ClaimName: "carher-dept-skills", ReadOnly: true}}},
				{Name: "user-sessions", VolumeSource: corev1.VolumeSource{PersistentVolumeClaim: &corev1.PersistentVolumeClaimVolumeSource{ClaimName: "carher-shared-sessions"}}},
			},
		},
	}
	return r.Create(ctx, pod)
}

func (r *HerInstanceReconciler) deletePod(ctx context.Context, uid int) error {
	pod := &corev1.Pod{ObjectMeta: metav1.ObjectMeta{Name: fmt.Sprintf("carher-%d", uid), Namespace: Namespace}}
	if err := r.Delete(ctx, pod); err != nil && !errors.IsNotFound(err) {
		return err
	}
	return nil
}

func (r *HerInstanceReconciler) deleteConfigMap(ctx context.Context, uid int) error {
	cm := &corev1.ConfigMap{ObjectMeta: metav1.ObjectMeta{Name: fmt.Sprintf("carher-%d-user-config", uid), Namespace: Namespace}}
	if err := r.Delete(ctx, cm); err != nil && !errors.IsNotFound(err) {
		return err
	}
	return nil
}

// ── PVC ──

func (r *HerInstanceReconciler) ensurePVC(ctx context.Context, uid int) error {
	pvcName := fmt.Sprintf("carher-%d-data", uid)
	var existing corev1.PersistentVolumeClaim
	err := r.Get(ctx, types.NamespacedName{Name: pvcName, Namespace: Namespace}, &existing)
	if err == nil {
		return nil
	}
	if !errors.IsNotFound(err) {
		return err
	}

	sc := "alibabacloud-cnfs-nas"
	pvc := &corev1.PersistentVolumeClaim{
		ObjectMeta: metav1.ObjectMeta{
			Name:      pvcName,
			Namespace: Namespace,
			Labels:    map[string]string{"managed-by": "carher-operator"},
		},
		Spec: corev1.PersistentVolumeClaimSpec{
			AccessModes:      []corev1.PersistentVolumeAccessMode{corev1.ReadWriteMany},
			StorageClassName: &sc,
			Resources: corev1.VolumeResourceRequirements{
				Requests: corev1.ResourceList{corev1.ResourceStorage: resource.MustParse("5Gi")},
			},
		},
	}
	return r.Create(ctx, pvc)
}

// base64Decode is unused but kept for future Secret reading in non-K8s client contexts
func base64Decode(s string) string {
	b, err := base64.StdEncoding.DecodeString(s)
	if err != nil {
		return s
	}
	return string(b)
}

// ── helpers ──

func jsonMarshal(v interface{}) string {
	b, _ := json.MarshalIndent(v, "", "  ")
	return string(b)
}

// splitOwners splits a pipe-separated owner string into a slice.
func splitOwners(s string) []string {
	var result []string
	for _, o := range strings.Split(s, "|") {
		o = strings.TrimSpace(o)
		if o != "" {
			result = append(result, o)
		}
	}
	return result
}
