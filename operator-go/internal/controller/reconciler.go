package controller

import (
	"context"
	"crypto/md5"
	"fmt"
	"strconv"
	"strings"
	"time"

	appsv1 "k8s.io/api/apps/v1"
	corev1 "k8s.io/api/core/v1"
	"k8s.io/apimachinery/pkg/api/errors"
	"k8s.io/apimachinery/pkg/api/resource"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/runtime"
	"k8s.io/apimachinery/pkg/types"
	"k8s.io/apimachinery/pkg/util/intstr"
	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/log"

	herv1 "github.com/guangzhou/carher-admin/operator-go/api/v1alpha1"
	"github.com/guangzhou/carher-admin/operator-go/internal/metrics"
)

const (
	Namespace = "carher"
	ACR       = "cltx-her-ck-registry-vpc.ap-southeast-1.cr.aliyuncs.com/her/carher"

	// FeishuWSReadinessGate is a custom pod condition set by the health checker.
	// K8s rolling update will NOT terminate the old pod until this gate is True on
	// the new pod — guaranteeing zero WebSocket disruption during rollouts.
	FeishuWSReadinessGate corev1.PodConditionType = "carher.io/feishu-ws-ready"

	initScript = `
const fs = require('fs');
const cfg = JSON.parse(fs.readFileSync('/config-template/openclaw.json', 'utf8'));
const secret = process.env.FEISHU_APP_SECRET || '';
if (secret && cfg.channels && cfg.channels.feishu) {
  cfg.channels.feishu.appSecret = secret;
}
fs.writeFileSync('/merged-config/openclaw.json', JSON.stringify(cfg, null, 2));
console.log('config merged, secret-injected:', !!secret);
`

	// reloaderScript runs as a sidecar, watching ConfigMap volume for changes
	// and syncing merged config (with secret injected) to the EmptyDir.
	// K8s auto-propagates ConfigMap changes to non-SubPath mounts (~60s).
	// IMPORTANT: must use writeFileSync (same inode) instead of rename, because
	// the main container mounts this file via SubPath bind mount — rename would
	// create a new inode that the bind mount doesn't follow.
	reloaderScript = `
const fs=require('fs'),crypto=require('crypto');
const SRC='/config-watch/openclaw.json',DST='/merged-config/openclaw.json';
let lastHash='';
function sync(){try{const raw=fs.readFileSync(SRC,'utf8');
const h=crypto.createHash('md5').update(raw).digest('hex').slice(0,12);
if(h===lastHash)return;const cfg=JSON.parse(raw);
const s=process.env.FEISHU_APP_SECRET||'';
if(s&&cfg.channels&&cfg.channels.feishu)cfg.channels.feishu.appSecret=s;
fs.writeFileSync(DST,JSON.stringify(cfg,null,2));
lastHash=h;
console.log('[reloader] synced hash='+h);}catch(e){
if(lastHash)console.error('[reloader]',e.message);}}
sync();setInterval(sync,5000);
`
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

	if !her.DeletionTimestamp.IsZero() {
		action = "delete"
		logger.Info("Deleting", "uid", uid)
		r.deleteDeployment(ctx, uid)
		r.deleteConfigMap(ctx, uid)
		r.KnownBots.MarkDirty()
		uidStr := strconv.Itoa(uid)
		metrics.FeishuWSConnected.DeleteLabelValues(uidStr, her.Spec.Name)
		metrics.PodRestarts.DeleteLabelValues(uidStr, her.Spec.Name)
		return ctrl.Result{}, nil
	}

	if her.Spec.Paused {
		if her.Status.Phase != "Paused" {
			r.scaleDeployment(ctx, uid, 0)
			her.Status.Phase = "Paused"
			if err := r.Status().Update(ctx, &her); err != nil {
				logger.V(1).Info("Status update failed", "uid", uid, "err", err)
			}
		}
		return ctrl.Result{}, nil
	}

	if err := r.ensurePVC(ctx, uid); err != nil {
		logger.Error(err, "Failed to ensure PVC", "uid", uid)
		return ctrl.Result{RequeueAfter: 30 * time.Second}, err
	}

	if err := r.ensureService(ctx, &her); err != nil {
		logger.Error(err, "Failed to ensure Service", "uid", uid)
		return ctrl.Result{RequeueAfter: 15 * time.Second}, err
	}

	configHash, err := r.applyConfig(ctx, &her)
	if err != nil {
		logger.Error(err, "Failed to apply config", "uid", uid)
		return ctrl.Result{RequeueAfter: 15 * time.Second}, err
	}

	needRollout := false
	hotReload := false
	deploy, deployErr := r.getDeployment(ctx, uid)

	// pod-spec-key covers all fields that affect the Pod template beyond ConfigMap content.
	// If any of these change, a rolling update is required. If only ConfigMap changes,
	// the config-reloader sidecar handles it without pod restart.
	secretName_ := her.Spec.AppSecretRef
	if secretName_ == "" {
		secretName_ = fmt.Sprintf("carher-%d-secret", uid)
	}
	desiredPodSpecKey := fmt.Sprintf("%s|%s|%s",
		resolveImage(her.Spec.Image), resolvePrefix(her.Spec.Prefix), secretName_)

	if deployErr != nil || deploy == nil {
		needRollout = true
		action = "create"
	} else {
		currentPodSpecKey := ""
		if deploy.Annotations != nil {
			currentPodSpecKey = deploy.Annotations["carher.io/pod-spec-key"]
		}
		if currentPodSpecKey == "" {
			// Legacy: reconstruct from existing deployment for backward compat
			img := ""
			if len(deploy.Spec.Template.Spec.Containers) > 0 {
				img = deploy.Spec.Template.Spec.Containers[0].Image
			}
			currentPodSpecKey = img
		}
		if currentPodSpecKey != desiredPodSpecKey {
			needRollout = true
			action = "update-pod-spec"
		}

		// For config-only changes, use hot-reload via sidecar instead of rolling out.
		// live-config-hash tracks the latest config even when no rollout occurred.
		if !needRollout {
			liveHash := ""
			if deploy.Annotations != nil {
				liveHash = deploy.Annotations["carher.io/live-config-hash"]
			}
			if liveHash == "" {
				liveHash = deploy.Spec.Template.Annotations["carher.io/config-hash"]
			}
			if liveHash != configHash {
				hotReload = true
				action = "hot-reload-config"
			}
		}
	}

	if needRollout {
		logger.Info("Ensuring deployment", "uid", uid, "action", action, "image", resolveImage(her.Spec.Image))
		if err := r.ensureDeployment(ctx, &her, configHash); err != nil {
			logger.Error(err, "Failed to ensure deployment", "uid", uid)
			her.Status.Phase = "Failed"
			her.Status.Message = err.Error()
			if uerr := r.Status().Update(ctx, &her); uerr != nil {
				logger.V(1).Info("Status update failed", "uid", uid, "err", uerr)
			}
			return ctrl.Result{RequeueAfter: 30 * time.Second}, err
		}
		her.Status.Phase = "Pending"
	} else if hotReload {
		// Config-only change: ConfigMap already updated by applyConfig above.
		// The config-reloader sidecar will detect the ConfigMap volume change
		// and write the merged config to the shared EmptyDir — no pod restart.
		logger.Info("Hot config reload via sidecar (no pod restart)", "uid", uid, "configHash", configHash)
		if deploy.Annotations == nil {
			deploy.Annotations = map[string]string{}
		}
		deploy.Annotations["carher.io/live-config-hash"] = configHash
		if err := r.Update(ctx, deploy); err != nil {
			logger.Error(err, "Failed to update live-config-hash", "uid", uid)
		}
	} else if deploy != nil && deploy.Spec.Replicas != nil && *deploy.Spec.Replicas == 0 {
		// Instance was paused (scaled to 0) and has since been unpaused.
		// Neither pod-spec-key nor config changed, so scale back up explicitly.
		logger.Info("Scaling deployment back to 1 (unpause)", "uid", uid)
		if err := r.scaleDeployment(ctx, uid, 1); err != nil {
			logger.Error(err, "Failed to scale deployment", "uid", uid)
		}
	}

	prevHash := her.Status.ConfigHash
	statusChanged := needRollout || prevHash != configHash
	her.Status.ConfigHash = configHash
	if statusChanged {
		if err := r.Status().Update(ctx, &her); err != nil {
			logger.V(1).Info("Status update failed", "uid", uid, "err", err)
		}
		if prevHash != configHash && prevHash != "" {
			r.KnownBots.MarkDirty()
		}
	}

	return ctrl.Result{}, nil
}

func (r *HerInstanceReconciler) SetupWithManager(mgr ctrl.Manager) error {
	return ctrl.NewControllerManagedBy(mgr).
		For(&herv1.HerInstance{}).
		Owns(&appsv1.Deployment{}).
		Owns(&corev1.Service{}).
		Complete(r)
}

// ── Config generation ──

func (r *HerInstanceReconciler) applyConfig(ctx context.Context, her *herv1.HerInstance) (string, error) {
	uid := her.Spec.UserID

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

	configJSON := GenerateOpenclawJSON(ConfigInput{
		ID:               uid,
		Name:             her.Spec.Name,
		Model:            her.Spec.Model,
		AppID:            her.Spec.AppID,
		AppSecret:        appSecret,
		Prefix:           her.Spec.Prefix,
		Owner:            her.Spec.Owner,
		Provider:         her.Spec.Provider,
		BotOpenID:        her.Spec.BotOpenID,
		OAuthRedirectUri: her.Spec.OAuthRedirectUri,
	})

	hash := fmt.Sprintf("%x", md5.Sum([]byte(configJSON)))[:12]

	if her.Status.ConfigHash == hash {
		return hash, nil
	}

	isController := true
	blockOwnerDeletion := true
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
			OwnerReferences: []metav1.OwnerReference{{
				APIVersion:         "carher.io/v1alpha1",
				Kind:               "HerInstance",
				Name:               her.Name,
				UID:                her.UID,
				Controller:         &isController,
				BlockOwnerDeletion: &blockOwnerDeletion,
			}},
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
	existing.OwnerReferences = cm.OwnerReferences
	return hash, r.Update(ctx, &existing)
}

// ── Deployment lifecycle ──

func (r *HerInstanceReconciler) getDeployment(ctx context.Context, uid int) (*appsv1.Deployment, error) {
	var deploy appsv1.Deployment
	err := r.Get(ctx, types.NamespacedName{Name: fmt.Sprintf("carher-%d", uid), Namespace: Namespace}, &deploy)
	if errors.IsNotFound(err) {
		return nil, nil
	}
	return &deploy, err
}

func (r *HerInstanceReconciler) ensureDeployment(ctx context.Context, her *herv1.HerInstance, configHash string) error {
	uid := her.Spec.UserID
	imageTag := resolveImage(her.Spec.Image)
	image := fmt.Sprintf("%s:%s", ACR, imageTag)
	prefix := resolvePrefix(her.Spec.Prefix)
	pfx := prefix + "-"
	uidStr := strconv.Itoa(uid)

	secretName := her.Spec.AppSecretRef
	if secretName == "" {
		secretName = fmt.Sprintf("carher-%d-secret", uid)
	}

	isController := true
	blockOwnerDeletion := true
	replicas := int32(1)

	labels := map[string]string{
		"app":        "carher-user",
		"user-id":    uidStr,
		"managed-by": "carher-operator",
	}

	podTemplate := corev1.PodTemplateSpec{
		ObjectMeta: metav1.ObjectMeta{
			Labels: labels,
			Annotations: map[string]string{
				"carher.io/config-hash":  configHash,
				"carher.io/deploy-group": her.Spec.DeployGroup,
			},
		},
		Spec: corev1.PodSpec{
			ImagePullSecrets: []corev1.LocalObjectReference{
				{Name: "acr-secret"},
				{Name: "acr-vpc-secret"},
			},
			ReadinessGates: []corev1.PodReadinessGate{{
				ConditionType: FeishuWSReadinessGate,
			}},
			InitContainers: []corev1.Container{{
				Name:    "inject-secret",
				Image:   image,
				Command: []string{"node", "-e", initScript},
				Env: []corev1.EnvVar{{
					Name: "FEISHU_APP_SECRET",
					ValueFrom: &corev1.EnvVarSource{
						SecretKeyRef: &corev1.SecretKeySelector{
							LocalObjectReference: corev1.LocalObjectReference{Name: secretName},
							Key:                  "app_secret",
						},
					},
				}},
				VolumeMounts: []corev1.VolumeMount{
					{Name: "user-config-template", MountPath: "/config-template"},
					{Name: "merged-config", MountPath: "/merged-config"},
				},
			}},
			Containers: []corev1.Container{{
				Name:  "carher",
				Image: image,
				Ports: []corev1.ContainerPort{
					{ContainerPort: 18789, Name: "gateway"},
					{ContainerPort: 18790, Name: "realtime"},
					{ContainerPort: 8000, Name: "frontend"},
					{ContainerPort: 8080, Name: "ws-proxy"},
					{ContainerPort: 18891, Name: "oauth"},
					{ContainerPort: 18795, Name: "a2a"},
				},
				Env: []corev1.EnvVar{
					{Name: "HOME", Value: "/data"},
					{Name: "OPENCLAW_INSTANCE_ID", Value: fmt.Sprintf("carher-%d-k8s", uid)},
					{Name: "NODE_OPTIONS", Value: "--max-old-space-size=2304"},
					{Name: "GOOGLE_APPLICATION_CREDENTIALS", Value: "/gcloud/application_default_credentials.json"},
					{Name: "VOICE_FE_HOST", Value: fmt.Sprintf("%su%d-fe.carher.net", pfx, uid)},
					{Name: "VOICE_PROXY_HOST", Value: fmt.Sprintf("%su%d-proxy.carher.net", pfx, uid)},
					{Name: "REDIS_URL", Value: "redis://carher-redis.carher.svc:6379"},
				},
				EnvFrom: []corev1.EnvFromSource{{
					SecretRef: &corev1.SecretEnvSource{LocalObjectReference: corev1.LocalObjectReference{Name: "carher-env-keys"}},
				}},
				Resources: corev1.ResourceRequirements{
					Requests: corev1.ResourceList{
						corev1.ResourceCPU:    resource.MustParse("300m"),
						corev1.ResourceMemory: resource.MustParse("1Gi"),
					},
					Limits: corev1.ResourceList{
						corev1.ResourceCPU:    resource.MustParse("3"),
						corev1.ResourceMemory: resource.MustParse("3Gi"),
					},
				},
				Lifecycle: &corev1.Lifecycle{
					PreStop: &corev1.LifecycleHandler{
						Exec: &corev1.ExecAction{
							Command: []string{"sh", "-c", "sleep 15"},
						},
					},
				},
				VolumeMounts: []corev1.VolumeMount{
					{Name: "user-data", MountPath: "/data/.openclaw"},
					{Name: "merged-config", MountPath: "/data/.openclaw/openclaw.json", SubPath: "openclaw.json"},
					{Name: "base-config", MountPath: "/data/.openclaw/carher-config.json", SubPath: "carher-config.json"},
					{Name: "base-config", MountPath: "/data/.openclaw/shared-config.json5", SubPath: "shared-config.json5"},
					{Name: "gcloud-adc", MountPath: "/gcloud/application_default_credentials.json", SubPath: "application_default_credentials.json", ReadOnly: true},
					{Name: "shared-skills", MountPath: "/data/.openclaw/skills", ReadOnly: true},
					{Name: "dept-skills", MountPath: "/data/.agents/skills", ReadOnly: true},
					{Name: "user-sessions", MountPath: "/data/.openclaw/sessions", SubPath: fmt.Sprintf("sessions/%d", uid)},
				},
			}, {
				Name:    "config-reloader",
				Image:   image,
				Command: []string{"node", "-e", reloaderScript},
				Env: []corev1.EnvVar{{
					Name: "FEISHU_APP_SECRET",
					ValueFrom: &corev1.EnvVarSource{
						SecretKeyRef: &corev1.SecretKeySelector{
							LocalObjectReference: corev1.LocalObjectReference{Name: secretName},
							Key:                  "app_secret",
						},
					},
				}},
				VolumeMounts: []corev1.VolumeMount{
					{Name: "user-config-template", MountPath: "/config-watch", ReadOnly: true},
					{Name: "merged-config", MountPath: "/merged-config"},
				},
				Resources: corev1.ResourceRequirements{
					Requests: corev1.ResourceList{
						corev1.ResourceCPU:    resource.MustParse("10m"),
						corev1.ResourceMemory: resource.MustParse("32Mi"),
					},
					Limits: corev1.ResourceList{
						corev1.ResourceCPU:    resource.MustParse("50m"),
						corev1.ResourceMemory: resource.MustParse("64Mi"),
					},
				},
			}},
			TerminationGracePeriodSeconds: func() *int64 { v := int64(30); return &v }(),
			Volumes: []corev1.Volume{
				{Name: "user-data", VolumeSource: corev1.VolumeSource{PersistentVolumeClaim: &corev1.PersistentVolumeClaimVolumeSource{ClaimName: fmt.Sprintf("carher-%d-data", uid)}}},
				{Name: "user-config-template", VolumeSource: corev1.VolumeSource{ConfigMap: &corev1.ConfigMapVolumeSource{LocalObjectReference: corev1.LocalObjectReference{Name: fmt.Sprintf("carher-%d-user-config", uid)}}}},
				{Name: "merged-config", VolumeSource: corev1.VolumeSource{EmptyDir: &corev1.EmptyDirVolumeSource{}}},
				{Name: "base-config", VolumeSource: corev1.VolumeSource{ConfigMap: &corev1.ConfigMapVolumeSource{LocalObjectReference: corev1.LocalObjectReference{Name: "carher-base-config"}}}},
				{Name: "gcloud-adc", VolumeSource: corev1.VolumeSource{Secret: &corev1.SecretVolumeSource{SecretName: "carher-gcloud-adc"}}},
				{Name: "shared-skills", VolumeSource: corev1.VolumeSource{PersistentVolumeClaim: &corev1.PersistentVolumeClaimVolumeSource{ClaimName: "carher-shared-skills", ReadOnly: true}}},
				{Name: "dept-skills", VolumeSource: corev1.VolumeSource{PersistentVolumeClaim: &corev1.PersistentVolumeClaimVolumeSource{ClaimName: "carher-dept-skills", ReadOnly: true}}},
				{Name: "user-sessions", VolumeSource: corev1.VolumeSource{PersistentVolumeClaim: &corev1.PersistentVolumeClaimVolumeSource{ClaimName: "carher-shared-sessions"}}},
			},
		},
	}

	desired := &appsv1.Deployment{
		ObjectMeta: metav1.ObjectMeta{
			Name:      fmt.Sprintf("carher-%d", uid),
			Namespace: Namespace,
			Labels:    labels,
			Annotations: map[string]string{
				"carher.io/live-config-hash": configHash,
				"carher.io/pod-spec-key":     fmt.Sprintf("%s|%s|%s", imageTag, prefix, secretName),
			},
			OwnerReferences: []metav1.OwnerReference{{
				APIVersion:         "carher.io/v1alpha1",
				Kind:               "HerInstance",
				Name:               her.Name,
				UID:                her.UID,
				Controller:         &isController,
				BlockOwnerDeletion: &blockOwnerDeletion,
			}},
		},
		Spec: appsv1.DeploymentSpec{
			Replicas: &replicas,
			Selector: &metav1.LabelSelector{MatchLabels: map[string]string{
				"app":     "carher-user",
				"user-id": uidStr,
			}},
			Strategy: appsv1.DeploymentStrategy{
				Type: appsv1.RollingUpdateDeploymentStrategyType,
				RollingUpdate: &appsv1.RollingUpdateDeployment{
					MaxSurge:       intstrPtr(1),
					MaxUnavailable: intstrPtr(0),
				},
			},
			Template: podTemplate,
		},
	}

	var existing appsv1.Deployment
	err := r.Get(ctx, types.NamespacedName{Name: desired.Name, Namespace: Namespace}, &existing)
	if errors.IsNotFound(err) {
		return r.Create(ctx, desired)
	} else if err != nil {
		return err
	}

	existing.Spec.Template = desired.Spec.Template
	existing.Spec.Strategy = desired.Spec.Strategy
	existing.Spec.Replicas = desired.Spec.Replicas
	existing.Labels = desired.Labels
	if existing.Annotations == nil {
		existing.Annotations = map[string]string{}
	}
	for k, v := range desired.Annotations {
		existing.Annotations[k] = v
	}
	return r.Update(ctx, &existing)
}

func (r *HerInstanceReconciler) deleteDeployment(ctx context.Context, uid int) error {
	deploy := &appsv1.Deployment{ObjectMeta: metav1.ObjectMeta{Name: fmt.Sprintf("carher-%d", uid), Namespace: Namespace}}
	if err := r.Delete(ctx, deploy); err != nil && !errors.IsNotFound(err) {
		return err
	}
	return nil
}

func (r *HerInstanceReconciler) scaleDeployment(ctx context.Context, uid int, replicas int32) error {
	var deploy appsv1.Deployment
	name := fmt.Sprintf("carher-%d", uid)
	if err := r.Get(ctx, types.NamespacedName{Name: name, Namespace: Namespace}, &deploy); err != nil {
		if errors.IsNotFound(err) {
			return nil
		}
		return err
	}
	deploy.Spec.Replicas = &replicas
	return r.Update(ctx, &deploy)
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

// ── Service ──

func (r *HerInstanceReconciler) ensureService(ctx context.Context, her *herv1.HerInstance) error {
	uid := her.Spec.UserID
	svcName := fmt.Sprintf("carher-%d-svc", uid)

	var existing corev1.Service
	if err := r.Get(ctx, types.NamespacedName{Name: svcName, Namespace: Namespace}, &existing); err == nil {
		return nil
	} else if !errors.IsNotFound(err) {
		return err
	}

	isController := true
	blockOwnerDeletion := true
	svc := &corev1.Service{
		ObjectMeta: metav1.ObjectMeta{
			Name:      svcName,
			Namespace: Namespace,
			Labels: map[string]string{
				"app":        "carher-user",
				"user-id":    strconv.Itoa(uid),
				"managed-by": "carher-operator",
			},
			OwnerReferences: []metav1.OwnerReference{{
				APIVersion:         "carher.io/v1alpha1",
				Kind:               "HerInstance",
				Name:               her.Name,
				UID:                her.UID,
				Controller:         &isController,
				BlockOwnerDeletion: &blockOwnerDeletion,
			}},
		},
		Spec: corev1.ServiceSpec{
			Type: corev1.ServiceTypeClusterIP,
			Selector: map[string]string{
				"app":     "carher-user",
				"user-id": strconv.Itoa(uid),
			},
			Ports: []corev1.ServicePort{
				{Name: "gateway", Port: 18789, TargetPort: intstr.FromInt(18789), Protocol: corev1.ProtocolTCP},
				{Name: "realtime", Port: 18790, TargetPort: intstr.FromInt(18790), Protocol: corev1.ProtocolTCP},
				{Name: "frontend", Port: 8000, TargetPort: intstr.FromInt(8000), Protocol: corev1.ProtocolTCP},
				{Name: "ws-proxy", Port: 8080, TargetPort: intstr.FromInt(8080), Protocol: corev1.ProtocolTCP},
				{Name: "oauth", Port: 18891, TargetPort: intstr.FromInt(18891), Protocol: corev1.ProtocolTCP},
				{Name: "a2a", Port: 18795, TargetPort: intstr.FromInt(18795), Protocol: corev1.ProtocolTCP},
			},
		},
	}
	return r.Create(ctx, svc)
}

// ── helpers ──

func resolveImage(specImage string) string {
	if specImage == "" {
		return "v20260328"
	}
	return specImage
}

func resolvePrefix(specPrefix string) string {
	if specPrefix == "" {
		return "s1"
	}
	return specPrefix
}

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

func intstrPtr(val int) *intstr.IntOrString {
	v := intstr.FromInt(val)
	return &v
}
