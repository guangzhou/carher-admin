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
	"sigs.k8s.io/controller-runtime/pkg/controller/controllerutil"
	"sigs.k8s.io/controller-runtime/pkg/log"

	herv1 "github.com/guangzhou/carher-admin/operator-go/api/v1alpha1"
	"github.com/guangzhou/carher-admin/operator-go/internal/metrics"
)

const (
	Namespace           = "carher"
	ACR                 = "cltx-her-ck-registry-vpc.ap-southeast-1.cr.aliyuncs.com/her/carher"
	UserPVCStorageClass = "alibabacloud-cnfs-nas"
	UserPVCStorageSize  = "20Gi"

	// FeishuWSReadinessGate is a custom pod condition set by the health checker.
	// K8s rolling update will NOT terminate the old pod until this gate is True on
	// the new pod — guaranteeing zero WebSocket disruption during rollouts.
	FeishuWSReadinessGate corev1.PodConditionType = "carher.io/feishu-ws-ready"

	// AnnotationExtraLitellmModels opts a specific HerInstance into additional
	// LiteLLM models beyond the default set. Value: comma-separated model ids
	// registered in extraLitellmModelRegistry (e.g. "anthropic.claude-opus-4-7").
	// Only takes effect when spec.provider=litellm. Unknown ids are ignored.
	AnnotationExtraLitellmModels   = "carher.io/extra-litellm-models"
	AnnotationFeishuHomeChannel    = "carher.io/feishu-home-channel"
	AnnotationHermesProvider       = "carher.io/hermes-provider"
	AnnotationHermesConfigTemplate = "carher.io/hermes-config-template"

	// AnnotationRuntimeProfile opts a single HerInstance into an alternate
	// runtime contract without changing the default operator path.
	AnnotationRuntimeProfile  = "carher.io/runtime-profile"
	RuntimeProfileH75Openclaw = "h75-openclaw"
	BaseConfigDefault         = "carher-base-config"
	BaseConfigCleanTest       = "carher-base-config-67ffa406-test"
	BaseConfigH75Openclaw     = "carher-base-config-h75"
	DifyBootstrapTokenSecret  = "carher-dify-bootstrap-token"
	DifyBootstrapTokenKey     = "token"
	InternalDifyBaseURL       = "http://dify-nginx.dify.svc.cluster.local"
	InternalDifyBootstrapURL  = "http://dify-bootstrap.dify.svc.cluster.local:5688/v1/bootstrap/carher-bot"
	InternalLiteLLMBaseURL    = "http://litellm-proxy.carher.svc.cluster.local:4000/v1"
	H75RuntimeSecret          = "carher-h75-runtime-secrets"
	H75ACPSecret              = "carher-h75-acp-secrets"
	H75RuntimePodSpecRevision = "a2a-acp-engine-v19-hermes-feishu-deps"
	HermesFeishuDepsPath      = "/data/.openclaw/local/hermes-python-packages"

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

func runtimeProfile(her *herv1.HerInstance) string {
	if her == nil || her.Annotations == nil {
		return ""
	}
	return her.Annotations[AnnotationRuntimeProfile]
}

func feishuHomeChannel(her *herv1.HerInstance) string {
	if her == nil || her.Annotations == nil {
		return ""
	}
	return her.Annotations[AnnotationFeishuHomeChannel]
}

func hermesProvider(her *herv1.HerInstance) string {
	if her == nil || her.Annotations == nil {
		return "chatgpt-pro"
	}
	if v := strings.TrimSpace(her.Annotations[AnnotationHermesProvider]); v != "" {
		return v
	}
	return "chatgpt-pro"
}

func hermesConfigTemplate(her *herv1.HerInstance) string {
	if her == nil || her.Annotations == nil {
		return "/opt/carher-runtime/templates/hermes-config.carher-pro.yaml"
	}
	if v := strings.TrimSpace(her.Annotations[AnnotationHermesConfigTemplate]); v != "" {
		return v
	}
	return "/opt/carher-runtime/templates/hermes-config.carher-pro.yaml"
}

func hermesPodSpecKey(her *herv1.HerInstance) string {
	if her == nil || her.Annotations == nil {
		return ""
	}
	if _, ok := her.Annotations[AnnotationHermesProvider]; !ok {
		if _, ok := her.Annotations[AnnotationHermesConfigTemplate]; !ok {
			return ""
		}
	}
	return "|hermes-provider=" + hermesProvider(her) + "|hermes-template=" + hermesConfigTemplate(her)
}

func runtimeProfilePodSpecKey(profile string) string {
	if profile == "" {
		return ""
	}
	if profile == RuntimeProfileH75Openclaw {
		return "|profile=" + profile + "|" + H75RuntimePodSpecRevision
	}
	return "|profile=" + profile
}

func baseConfigNameForRuntimeProfile(imageTag, profile string) string {
	if profile == RuntimeProfileH75Openclaw {
		return BaseConfigH75Openclaw
	}
	if imageTag == "67ffa406-clean" {
		return BaseConfigCleanTest
	}
	return BaseConfigDefault
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
		if controllerutil.RemoveFinalizer(&her, "carher.io/cleanup") {
			if err := r.Update(ctx, &her); err != nil {
				return ctrl.Result{}, err
			}
		}
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
	desiredPodSpecKey := fmt.Sprintf("%s|%s|%s|%s",
		resolveImage(her.Spec.Image), resolvePrefix(her.Spec.Prefix), secretName_, her.Spec.DeployGroup)
	if her.Spec.EnableLivenessProbe {
		desiredPodSpecKey += "|lp=1"
	}
	desiredPodSpecKey += runtimeProfilePodSpecKey(runtimeProfile(&her))
	if runtimeProfile(&her) == RuntimeProfileH75Openclaw {
		desiredPodSpecKey += hermesPodSpecKey(&her)
	}

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
	logger := log.FromContext(ctx)
	uid := her.Spec.UserID

	appSecret := ""
	secretName := her.Spec.AppSecretRef
	if secretName == "" {
		secretName = fmt.Sprintf("carher-%d-secret", uid)
	}
	var secret corev1.Secret
	if err := r.Get(ctx, types.NamespacedName{Name: secretName, Namespace: Namespace}, &secret); err != nil {
		if !errors.IsNotFound(err) {
			logger.Error(err, "Failed to read app secret, aborting config generation", "uid", uid, "secret", secretName)
			return "", fmt.Errorf("read secret %s: %w", secretName, err)
		}
	} else {
		if v, ok := secret.Data["app_secret"]; ok {
			appSecret = string(v)
		}
	}

	extraModels := parseExtraLitellmModels(her.Annotations[AnnotationExtraLitellmModels])

	configJSON := GenerateOpenclawJSON(ConfigInput{
		ID:                 uid,
		Name:               her.Spec.Name,
		Model:              her.Spec.Model,
		AppID:              her.Spec.AppID,
		AppSecret:          appSecret,
		Prefix:             her.Spec.Prefix,
		Owner:              her.Spec.Owner,
		Provider:           her.Spec.Provider,
		LitellmKey:         her.Spec.LitellmKey,
		LitellmUrl:         her.Spec.LitellmUrl,
		BotOpenID:          her.Spec.BotOpenID,
		OAuthRedirectUri:   her.Spec.OAuthRedirectUri,
		ExtraLitellmModels: extraModels,
		ContextTokens:      her.Spec.ContextTokens,
	})

	hash := fmt.Sprintf("%x", md5.Sum([]byte(configJSON)))[:12]
	cmName := fmt.Sprintf("carher-%d-user-config", uid)

	if her.Status.ConfigHash == hash {
		var existing corev1.ConfigMap
		err := r.Get(ctx, types.NamespacedName{Name: cmName, Namespace: Namespace}, &existing)
		if err == nil {
			return hash, nil
		}
		if err != nil && !errors.IsNotFound(err) {
			return hash, err
		}
	}

	isController := true
	blockOwnerDeletion := true
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

// herContainerEnv returns the env vars for the main carher container.
// When litellmKey is set, LITELLM_API_KEY is injected explicitly so it
// overrides the master key from the shared carher-env-keys Secret.
func herContainerEnv(uid int, pfx, litellmKey, profile, appID, secretName, feishuHomeChannel, hermesProviderName, hermesTemplatePath, larkStrictMode string) []corev1.EnvVar {
	env := []corev1.EnvVar{
		{Name: "HOME", Value: "/data"},
		{Name: "OPENCLAW_INSTANCE_ID", Value: fmt.Sprintf("carher-%d-k8s", uid)},
		{Name: "NODE_OPTIONS", Value: "--max-old-space-size=2304"},
		{Name: "GOOGLE_APPLICATION_CREDENTIALS", Value: "/gcloud/application_default_credentials.json"},
		{Name: "VOICE_FE_HOST", Value: fmt.Sprintf("%su%d-fe.carher.net", pfx, uid)},
		{Name: "VOICE_PROXY_HOST", Value: fmt.Sprintf("%su%d-proxy.carher.net", pfx, uid)},
		{Name: "REDIS_URL", Value: "redis://carher-redis.carher.svc:6379"},
	}
	if litellmKey != "" {
		env = append(env, corev1.EnvVar{Name: "LITELLM_API_KEY", Value: litellmKey})
	}
	if profile == RuntimeProfileH75Openclaw {
		botID := fmt.Sprintf("carher-%d", uid)
		env = append(env,
			corev1.EnvVar{Name: "FEISHU_APP_ID", Value: appID},
			corev1.EnvVar{
				Name: "FEISHU_APP_SECRET",
				ValueFrom: &corev1.EnvVarSource{
					SecretKeyRef: &corev1.SecretKeySelector{
						LocalObjectReference: corev1.LocalObjectReference{Name: secretName},
						Key:                  "app_secret",
					},
				},
			},
		)
		if litellmKey != "" {
			env = append(env, corev1.EnvVar{Name: "CARHER_PROD_KEY", Value: litellmKey})
		} else {
			env = append(env, corev1.EnvVar{
				Name: "CARHER_PROD_KEY",
				ValueFrom: &corev1.EnvVarSource{
					SecretKeyRef: &corev1.SecretKeySelector{
						LocalObjectReference: corev1.LocalObjectReference{Name: "carher-env-keys"},
						Key:                  "LITELLM_API_KEY",
					},
				},
			})
		}
		env = append(env,
			corev1.EnvVar{Name: "NODE_ENV", Value: "production"},
			corev1.EnvVar{Name: "PATH", Value: "/carher-fastbin:/opt/hermes/venv/bin:/opt/hermes/.venv/bin:/opt/node22/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"},
			corev1.EnvVar{Name: "NPM_CONFIG_PREFIX", Value: "/data/.openclaw/local"},
			corev1.EnvVar{Name: "OPENAI_BASE_URL", Value: InternalLiteLLMBaseURL},
			corev1.EnvVar{Name: "HERMES_HOME", Value: "/opt/data/.hermes"},
			corev1.EnvVar{Name: "HERMES_DATA_DIR", Value: "/opt/data"},
			corev1.EnvVar{Name: "HERMES_YOLO_MODE", Value: "1"},
			corev1.EnvVar{Name: "HERMES_ACCEPT_HOOKS", Value: "1"},
			corev1.EnvVar{Name: "HERMES_INFERENCE_PROVIDER", Value: hermesProviderName},
			corev1.EnvVar{Name: "PYTHONPATH", Value: HermesFeishuDepsPath},
			corev1.EnvVar{Name: "CARHER_REQUIRED_SECRET_ENVS", Value: "CARHER_PROD_KEY"},
			corev1.EnvVar{Name: "CARHER_HERMES_VOLUME_ROOT", Value: "/opt/data"},
			corev1.EnvVar{Name: "CARHER_HERMES_CONFIG_TEMPLATE", Value: hermesTemplatePath},
			corev1.EnvVar{Name: "OPENCLAW_LARK_VERSION", Value: "2026.4.10"},
			corev1.EnvVar{Name: "CARHER_RUNTIME_OPENCLAW_LARK_VERSION", Value: "2026.4.10"},
			corev1.EnvVar{Name: "CARHER_ACP_ENABLED", Value: "1"},
			corev1.EnvVar{Name: "CARHER_DIFY_ENABLED", Value: "1"},
			corev1.EnvVar{Name: "CARHER_DIFY_BOT_ID", Value: botID},
			corev1.EnvVar{Name: "HERMESTEST_CARHER_ID", Value: botID},
			corev1.EnvVar{Name: "CARHER_DIFY_BASE_URL", Value: InternalDifyBaseURL},
			corev1.EnvVar{Name: "CARHER_DIFY_BOOTSTRAP_URL", Value: InternalDifyBootstrapURL},
			corev1.EnvVar{Name: "CARHER_DIFY_WORKSPACE_SLUG", Value: botID},
			corev1.EnvVar{Name: "CARHER_DIFY_MODEL", Value: "chatgpt-gpt-5.5"},
			corev1.EnvVar{Name: "CARHER_DIFY_CODEX_BASE_URL", Value: InternalLiteLLMBaseURL},
			corev1.EnvVar{Name: "CARHER_DIFY_CODEX_KEY_ENV", Value: "CARHER_PROD_KEY"},
			corev1.EnvVar{Name: "CARHER_RUNTIME_PLUGINS_REFRESH", Value: "1"},
			corev1.EnvVar{Name: "FEISHU_ALLOW_ALL_USERS", Value: "true"},
			corev1.EnvVar{Name: "FEISHU_GROUP_POLICY", Value: "open"},
			corev1.EnvVar{Name: "CARHER_LAN_IP", Value: fmt.Sprintf("carher-%d-svc.%s.svc.cluster.local", uid, Namespace)},
			corev1.EnvVar{Name: "CARHER_A2A_PORT", Value: "18800"},
			corev1.EnvVar{Name: "HERMESTEST_A2A_ENABLED", Value: "1"},
			corev1.EnvVar{Name: "HERMESTEST_A2A_HOST", Value: "0.0.0.0"},
			corev1.EnvVar{Name: "HERMESTEST_A2A_PORT", Value: "18800"},
			corev1.EnvVar{Name: "HERMESTEST_A2A_PUBLIC_URL", Value: fmt.Sprintf("http://carher-%d-svc.%s.svc.cluster.local:18800", uid, Namespace)},
			corev1.EnvVar{Name: "HERMESTEST_A2A_AUTH", Value: "none"},
			corev1.EnvVar{Name: "HERMESTEST_A2A_REGISTER_REDIS", Value: "1"},
			corev1.EnvVar{Name: "CARHER_SERVER", Value: "ack"},
			corev1.EnvVar{
				Name: "CARHER_GATEWAY_TOKEN",
				ValueFrom: &corev1.EnvVarSource{
					SecretKeyRef: &corev1.SecretKeySelector{
						LocalObjectReference: corev1.LocalObjectReference{Name: H75RuntimeSecret},
						Key:                  "CARHER_GATEWAY_TOKEN",
					},
				},
			},
			corev1.EnvVar{
				Name: "ANTHROPIC_AUTH_TOKEN",
				ValueFrom: &corev1.EnvVarSource{
					SecretKeyRef: &corev1.SecretKeySelector{
						LocalObjectReference: corev1.LocalObjectReference{Name: H75ACPSecret},
						Key:                  "ANTHROPIC_AUTH_TOKEN",
					},
				},
			},
			corev1.EnvVar{
				Name: "ANTHROPIC_BASE_URL",
				ValueFrom: &corev1.EnvVarSource{
					SecretKeyRef: &corev1.SecretKeySelector{
						LocalObjectReference: corev1.LocalObjectReference{Name: H75ACPSecret},
						Key:                  "ANTHROPIC_BASE_URL",
					},
				},
			},
			corev1.EnvVar{
				Name: "CARHER_DIFY_BOOTSTRAP_TOKEN",
				ValueFrom: &corev1.EnvVarSource{
					SecretKeyRef: &corev1.SecretKeySelector{
						LocalObjectReference: corev1.LocalObjectReference{Name: DifyBootstrapTokenSecret},
						Key:                  DifyBootstrapTokenKey,
					},
				},
			},
		)
		if feishuHomeChannel != "" {
			env = append(env, corev1.EnvVar{Name: "FEISHU_HOME_CHANNEL", Value: feishuHomeChannel})
		}
		if larkStrictMode != "" {
			env = append(env, corev1.EnvVar{Name: "CARHER_LARK_CLI_STRICT_MODE", Value: larkStrictMode})
		}
	}
	return env
}

func runtimeProfileVolumeMounts(profile string) []corev1.VolumeMount {
	if profile != RuntimeProfileH75Openclaw {
		return nil
	}
	return []corev1.VolumeMount{
		{Name: "user-data", MountPath: "/opt/data"},
		{Name: "user-data", MountPath: "/data/.engine", SubPath: ".engine"},
		{Name: "h75-fastbin", MountPath: "/carher-fastbin", ReadOnly: true},
		{Name: "h75-agent-skills", MountPath: "/data/.agents/skills"},
		{Name: "h75-openclaw-local", MountPath: "/data/.openclaw/local"},
		{Name: "h75-runtime-plugins", MountPath: "/data/.openclaw/runtime-plugins"},
		{Name: "h75-openclaw-extensions", MountPath: "/data/.openclaw/extensions"},
		{Name: "h75-openclaw-skills", MountPath: "/data/.openclaw/skills"},
		{Name: "h75-hermes-skills", MountPath: "/opt/data/.hermes/skills"},
		{Name: "h75-hermes-opt-skills", MountPath: "/opt/data/skills"},
	}
}

func runtimeProfileVolumes(profile string) []corev1.Volume {
	if profile != RuntimeProfileH75Openclaw {
		return nil
	}
	emptyDir := func() corev1.VolumeSource {
		return corev1.VolumeSource{EmptyDir: &corev1.EmptyDirVolumeSource{}}
	}
	return []corev1.Volume{
		{Name: "h75-fastbin", VolumeSource: emptyDir()},
		{Name: "h75-agent-skills", VolumeSource: emptyDir()},
		{Name: "h75-openclaw-local", VolumeSource: emptyDir()},
		{Name: "h75-runtime-plugins", VolumeSource: emptyDir()},
		{Name: "h75-openclaw-extensions", VolumeSource: emptyDir()},
		{Name: "h75-openclaw-skills", VolumeSource: emptyDir()},
		{Name: "h75-hermes-skills", VolumeSource: emptyDir()},
		{Name: "h75-hermes-opt-skills", VolumeSource: emptyDir()},
	}
}

func runtimeProfileInitContainers(profile, image string) []corev1.Container {
	if profile != RuntimeProfileH75Openclaw {
		return nil
	}
	return []corev1.Container{{
		Name:    "copy-hermes-feishu-deps",
		Image:   image,
		Command: []string{"sh", "-lc", hermesFeishuDepsInitScript},
		VolumeMounts: []corev1.VolumeMount{{
			Name:      "h75-openclaw-local",
			MountPath: "/data/.openclaw/local",
		}},
	}}
}

const hermesFeishuDepsInitScript = `set -eu
DST=/data/.openclaw/local/hermes-python-packages
rm -rf "$DST"
mkdir -p "$DST"
uv pip install --target "$DST" --link-mode=copy "lark-oapi==1.6.7" "aiohttp-socks==0.11.0"
PYTHONPATH="$DST" /opt/hermes/.venv/bin/python3 -c 'import lark_oapi, aiohttp_socks; print("hermes-feishu-deps=ok")'
`

const h75FastbinInitScript = `
set -eu
mkdir -p /carher-fastbin
cat > /carher-fastbin/chown <<'EOF'
#!/bin/sh
real=/usr/bin/chown
[ -x "$real" ] || real=/bin/chown
stamp="${CARHER_FAST_CHOWN_STAMP:-/data/.engine/fast-chown-ready}"
if [ "$1" = "-R" ] && [ "$#" -eq 3 ] && [ -f "$stamp" ]; then
  case "$2 $3" in
    "hermes:hermes /data/.openclaw/workspace"|"hermes:hermes /data/.openclaw/agents"|"hermes:hermes /data/.openclaw/sessions"|"hermes:hermes /opt/data/.hermes"|"hermes:hermes /data/.agents"|"hermes:hermes /data/.openclaw/skills"|"hermes:hermes /data/.openclaw/local"|"hermes:hermes /data/.openclaw/runtime-plugins"|"hermes:hermes /data/.openclaw/extensions"|"hermes:hermes /opt/data/skills")
      "$real" "$2" "$3" 2>/dev/null || true
      exit 0
      ;;
    "0:0 /data/.openclaw/runtime-plugins")
      ready="$3/.carher-root-owned-ready"
      [ -f "$ready" ] && exit 0
      "$real" "$2" "$3" 2>/dev/null || true
      touch "$ready" 2>/dev/null || true
      exit 0
      ;;
  esac
fi
exec "$real" "$@"
EOF
cat > /carher-fastbin/chmod <<'EOF'
#!/bin/sh
real=/bin/chmod
if [ "$1" = "-R" ] && [ "$#" -eq 3 ] && [ "$2" = "go-w" ] && [ "$3" = "/data/.openclaw/runtime-plugins" ]; then
  ready="$3/.carher-chmod-ready"
  [ -f "$ready" ] && exit 0
  "$real" "$@"
  status=$?
  [ "$status" -eq 0 ] && touch "$ready" 2>/dev/null || true
  exit "$status"
fi
exec "$real" "$@"
EOF
cat > /carher-fastbin/rm <<'EOF'
#!/bin/sh
real=/bin/rm
if [ "$1" = "-rf" ] && [ "$#" -eq 2 ] && [ "$2" = "/data/.openclaw/runtime-plugins" ]; then
  [ -f "$2/.carher-prepared-v3" ] && exit 0
  find "$2" -mindepth 1 -maxdepth 1 -exec "$real" -rf {} +
  exit 0
fi
if [ "$1" = "-rf" ] && [ "$#" -eq 2 ]; then
  case "$2" in
    /data/.openclaw/local/lib/node_modules/*)
      exit 0
      ;;
    /data/.agents/skills/lark-*)
      [ -d "$2" ] && [ ! -L "$2" ] && [ -f "$2/.carher-fast-cache-ready" ] && exit 0
      ;;
    /opt/data/.hermes/skills/dogfood/*|/opt/data/skills/dogfood/*|/data/.openclaw/extensions/node_modules)
      base="$(basename "$2")"
      case "$base" in
        lark-*) ;;
        *) [ -d "$2" ] && [ ! -L "$2" ] && [ -f "$2/.carher-fast-cache-ready" ] && exit 0 ;;
      esac
      ;;
  esac
fi
exec "$real" "$@"
EOF
cat > /carher-fastbin/cp <<'EOF'
#!/bin/sh
real=/bin/cp
mark_ready() {
  [ -n "$1" ] && [ -d "$1" ] && touch "$1/.carher-fast-cache-ready" 2>/dev/null || true
}
if { [ "$1" = "-a" ] || [ "$1" = "-R" ]; } && [ "$#" -eq 3 ]; then
  src="$2"
  dst="$3"
  target=""
  case "$src $dst" in
    /opt/carher-acp/lib/node_modules/acpx\ /data/.openclaw/local/lib/node_modules/acpx)
      target="$dst"
      ;;
    /opt/carher-runtime/vendor/acp-adapters/node_modules/*\ /data/.openclaw/local/lib/node_modules/)
      base="$(basename "$src")"
      target="$dst/$base"
      ;;
    /opt/carher-lark-cli-skills/.agents/skills/lark-*\ /data/.agents/skills/*)
      target="$dst"
      ;;
    /opt/carher-shared-skills/*\ /opt/data/.hermes/skills/dogfood/*|/opt/carher-shared-skills/*\ /opt/data/skills/dogfood/*)
      target="$dst"
      ;;
    /opt/hermestest/skills/*\ /opt/data/.hermes/skills/dogfood/*|/opt/hermestest/skills/*\ /opt/data/skills/dogfood/*)
      target="$dst"
      ;;
    /opt/carher-runtime/openclaw-extensions/node_modules\ /data/.openclaw/extensions/node_modules)
      target="$dst"
      ;;
    /mounted-carher-plugins/*\ /data/.openclaw/runtime-plugins/*|/openclaw-plugins/carher-engine-swap\ /data/.openclaw/runtime-plugins/carher-engine-swap|/opt/carher-runtime/plugins/carher-engine-swap\ /data/.openclaw/runtime-plugins/carher-engine-swap)
      target="$dst"
      ;;
  esac
  if [ -n "$target" ] && [ -f "$target/.carher-fast-cache-ready" ]; then
    exit 0
  fi
  "$real" "$@"
  status=$?
  [ "$status" -eq 0 ] && mark_ready "$target"
  exit "$status"
fi
exec "$real" "$@"
EOF
chmod 0755 /carher-fastbin/chown /carher-fastbin/chmod /carher-fastbin/rm /carher-fastbin/cp
`

func runtimeProfileContainerPorts(profile string) []corev1.ContainerPort {
	if profile != RuntimeProfileH75Openclaw {
		return nil
	}
	return []corev1.ContainerPort{
		{ContainerPort: 18800, Name: "a2a-http"},
		{ContainerPort: 18801, Name: "a2a-grpc"},
	}
}

func servicePortsForRuntimeProfile(profile string) []corev1.ServicePort {
	ports := []corev1.ServicePort{
		{Name: "gateway", Port: 18789, TargetPort: intstr.FromInt(18789), Protocol: corev1.ProtocolTCP},
		{Name: "realtime", Port: 18790, TargetPort: intstr.FromInt(18790), Protocol: corev1.ProtocolTCP},
		{Name: "frontend", Port: 8000, TargetPort: intstr.FromInt(8000), Protocol: corev1.ProtocolTCP},
		{Name: "ws-proxy", Port: 8080, TargetPort: intstr.FromInt(8080), Protocol: corev1.ProtocolTCP},
		{Name: "oauth", Port: 18891, TargetPort: intstr.FromInt(18891), Protocol: corev1.ProtocolTCP},
		{Name: "a2a", Port: 18795, TargetPort: intstr.FromInt(18795), Protocol: corev1.ProtocolTCP},
	}
	if profile == RuntimeProfileH75Openclaw {
		ports = append(ports,
			corev1.ServicePort{Name: "a2a-http", Port: 18800, TargetPort: intstr.FromInt(18800), Protocol: corev1.ProtocolTCP},
			corev1.ServicePort{Name: "a2a-grpc", Port: 18801, TargetPort: intstr.FromInt(18801), Protocol: corev1.ProtocolTCP},
		)
	}
	return ports
}

func withoutVolumeMount(mounts []corev1.VolumeMount, name, mountPath string) []corev1.VolumeMount {
	filtered := mounts[:0]
	for _, mount := range mounts {
		if mount.Name == name && mount.MountPath == mountPath {
			continue
		}
		filtered = append(filtered, mount)
	}
	return filtered
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
	profile := runtimeProfile(her)

	secretName := her.Spec.AppSecretRef
	if secretName == "" {
		secretName = fmt.Sprintf("carher-%d-secret", uid)
	}

	isController := true
	blockOwnerDeletion := true
	replicas := int32(1)

	// CarHer-1000-style image (carher-14 同源, 67ffa406 commit) 需要：
	//   1) 走专属 base-config (兼容 2026.5.3 schema)
	//   2) 锁定 openclaw-lark 版本 + 强制重装
	//   3) 挂 carher-patches ConfigMap，含 R-9/R-10 footer & history-fill patch + 新 entrypoint
	// 默认 image (fix-compact-eb348941 等) 行为不变。
	cleanImage := imageTag == "67ffa406-clean"
	baseConfigName := baseConfigNameForRuntimeProfile(imageTag, profile)

	labels := map[string]string{
		"app":        "carher-user",
		"user-id":    uidStr,
		"managed-by": "carher-operator",
	}

	initContainers := append(runtimeProfileInitContainers(profile, image), corev1.Container{
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
	})

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
			InitContainers: initContainers,
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
				Env: herContainerEnv(uid, pfx, her.Spec.LitellmKey, profile, her.Spec.AppID, secretName, feishuHomeChannel(her), hermesProvider(her), hermesConfigTemplate(her), her.Spec.LarkStrictMode),
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
				{Name: "base-config", VolumeSource: corev1.VolumeSource{ConfigMap: &corev1.ConfigMapVolumeSource{LocalObjectReference: corev1.LocalObjectReference{Name: baseConfigName}}}},
				{Name: "gcloud-adc", VolumeSource: corev1.VolumeSource{Secret: &corev1.SecretVolumeSource{SecretName: "carher-gcloud-adc"}}},
				{Name: "shared-skills", VolumeSource: corev1.VolumeSource{PersistentVolumeClaim: &corev1.PersistentVolumeClaimVolumeSource{ClaimName: "carher-shared-skills", ReadOnly: true}}},
				{Name: "dept-skills", VolumeSource: corev1.VolumeSource{PersistentVolumeClaim: &corev1.PersistentVolumeClaimVolumeSource{ClaimName: "carher-dept-skills", ReadOnly: true}}},
				{Name: "user-sessions", VolumeSource: corev1.VolumeSource{PersistentVolumeClaim: &corev1.PersistentVolumeClaimVolumeSource{ClaimName: "carher-shared-sessions"}}},
			},
		},
	}
	podTemplate.Spec.Volumes = append(podTemplate.Spec.Volumes, runtimeProfileVolumes(profile)...)

	if profile == RuntimeProfileH75Openclaw {
		podTemplate.Spec.InitContainers = append(podTemplate.Spec.InitContainers, corev1.Container{
			Name:    "prepare-h75-runtime-dirs",
			Image:   image,
			Command: []string{"sh", "-lc", "mkdir -p /user-data/.engine"},
			VolumeMounts: []corev1.VolumeMount{
				{Name: "user-data", MountPath: "/user-data"},
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
		}, corev1.Container{
			Name:    "prepare-h75-fastbin",
			Image:   image,
			Command: []string{"sh", "-lc", h75FastbinInitScript},
			VolumeMounts: []corev1.VolumeMount{
				{Name: "h75-fastbin", MountPath: "/carher-fastbin"},
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
		})
	}

	// CarHer-1000-style additions: env + carher-patches volume + mount.
	if cleanImage {
		mode := int32(0o755)
		podTemplate.Spec.Volumes = append(podTemplate.Spec.Volumes, corev1.Volume{
			Name: "carher-patches",
			VolumeSource: corev1.VolumeSource{
				ConfigMap: &corev1.ConfigMapVolumeSource{
					LocalObjectReference: corev1.LocalObjectReference{Name: "carher-patches"},
					DefaultMode:          &mode,
				},
			},
		})
		podTemplate.Spec.Containers[0].VolumeMounts = append(
			podTemplate.Spec.Containers[0].VolumeMounts,
			corev1.VolumeMount{Name: "carher-patches", MountPath: "/carher-patches", ReadOnly: true},
			corev1.VolumeMount{Name: "carher-patches", MountPath: "/entrypoint.sh", SubPath: "carher-entrypoint.sh", ReadOnly: true},
		)
		podTemplate.Spec.Containers[0].Env = append(
			podTemplate.Spec.Containers[0].Env,
			corev1.EnvVar{Name: "CARHER_OPENCLAW_LARK_VERSION", Value: "2026.4.10"},
			corev1.EnvVar{Name: "CARHER_FORCE_PLUGIN_INSTALL", Value: "1"},
		)
	}

	if profile == RuntimeProfileH75Openclaw {
		podTemplate.Spec.Containers[0].VolumeMounts = withoutVolumeMount(
			podTemplate.Spec.Containers[0].VolumeMounts,
			"dept-skills",
			"/data/.agents/skills",
		)
		podTemplate.Spec.Containers[0].VolumeMounts = withoutVolumeMount(
			podTemplate.Spec.Containers[0].VolumeMounts,
			"shared-skills",
			"/data/.openclaw/skills",
		)
	}
	podTemplate.Spec.Containers[0].Ports = append(
		podTemplate.Spec.Containers[0].Ports,
		runtimeProfileContainerPorts(profile)...,
	)
	podTemplate.Spec.Containers[0].VolumeMounts = append(
		podTemplate.Spec.Containers[0].VolumeMounts,
		runtimeProfileVolumeMounts(profile)...,
	)

	// Opt-in livenessProbe (per HerInstance.spec.enableLivenessProbe). Default off.
	// /healthz on 18789 is served by OpenClaw gateway on the same Node event loop;
	// when the loop blocks (reindex storm, sync sqlite stall) the probe times out
	// and kubelet recycles the container via restartPolicy=Always.
	// Conservative thresholds: ≈ failureThreshold × (period + timeout) ≈ 240s of
	// continuous unresponsiveness before kill, to tolerate normal reindex bursts.
	if her.Spec.EnableLivenessProbe {
		podTemplate.Spec.Containers[0].LivenessProbe = &corev1.Probe{
			ProbeHandler: corev1.ProbeHandler{
				HTTPGet: &corev1.HTTPGetAction{
					Path:   "/healthz",
					Port:   intstr.FromInt(18789),
					Scheme: corev1.URISchemeHTTP,
				},
			},
			InitialDelaySeconds: 180,
			PeriodSeconds:       30,
			TimeoutSeconds:      10,
			FailureThreshold:    6,
			SuccessThreshold:    1,
		}
	}

	podSpecKeyVal := fmt.Sprintf("%s|%s|%s|%s", imageTag, prefix, secretName, her.Spec.DeployGroup)
	if her.Spec.EnableLivenessProbe {
		podSpecKeyVal += "|lp=1"
	}
	podSpecKeyVal += runtimeProfilePodSpecKey(profile)
	if profile == RuntimeProfileH75Openclaw {
		podSpecKeyVal += hermesPodSpecKey(her)
	}

	desired := &appsv1.Deployment{
		ObjectMeta: metav1.ObjectMeta{
			Name:      fmt.Sprintf("carher-%d", uid),
			Namespace: Namespace,
			Labels:    labels,
			Annotations: map[string]string{
				"carher.io/live-config-hash": configHash,
				"carher.io/pod-spec-key":     podSpecKeyVal,
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
		current := existing.Spec.Resources.Requests[corev1.ResourceStorage]
		target := resource.MustParse(UserPVCStorageSize)
		if current.Cmp(target) >= 0 {
			return nil
		}
		updated := existing.DeepCopy()
		if updated.Spec.Resources.Requests == nil {
			updated.Spec.Resources.Requests = corev1.ResourceList{}
		}
		updated.Spec.Resources.Requests[corev1.ResourceStorage] = target
		return r.Update(ctx, updated)
	}
	if !errors.IsNotFound(err) {
		return err
	}

	sc := UserPVCStorageClass
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
				Requests: corev1.ResourceList{corev1.ResourceStorage: resource.MustParse(UserPVCStorageSize)},
			},
		},
	}
	return r.Create(ctx, pvc)
}

// ── Service ──

func (r *HerInstanceReconciler) ensureService(ctx context.Context, her *herv1.HerInstance) error {
	uid := her.Spec.UserID
	svcName := fmt.Sprintf("carher-%d-svc", uid)
	profile := runtimeProfile(her)

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
			Ports: servicePortsForRuntimeProfile(profile),
		},
	}

	var existing corev1.Service
	err := r.Get(ctx, types.NamespacedName{Name: svcName, Namespace: Namespace}, &existing)
	if errors.IsNotFound(err) {
		return r.Create(ctx, svc)
	} else if err != nil {
		return err
	}

	existing.Labels = svc.Labels
	existing.OwnerReferences = svc.OwnerReferences
	existing.Spec.Selector = svc.Spec.Selector
	existing.Spec.Ports = svc.Spec.Ports
	return r.Update(ctx, &existing)
}

// ── helpers ──

func resolveImage(specImage string) string {
	if specImage == "" {
		return "fix-compact-eb348941"
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

// parseExtraLitellmModels splits a comma-separated annotation value into a
// clean slice of non-empty trimmed ids. Empty input returns nil.
func parseExtraLitellmModels(s string) []string {
	if s == "" {
		return nil
	}
	var result []string
	for _, m := range strings.Split(s, ",") {
		m = strings.TrimSpace(m)
		if m != "" {
			result = append(result, m)
		}
	}
	return result
}

func intstrPtr(val int) *intstr.IntOrString {
	v := intstr.FromInt(val)
	return &v
}
