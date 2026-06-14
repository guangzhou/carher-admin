package controller

import (
	"strings"
	"testing"

	herv1 "github.com/guangzhou/carher-admin/operator-go/api/v1alpha1"
	corev1 "k8s.io/api/core/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
)

func TestRuntimeProfileDefaultPath(t *testing.T) {
	if got := baseConfigNameForRuntimeProfile("fix-compact-eb348941", ""); got != BaseConfigDefault {
		t.Fatalf("default base config = %q, want %q", got, BaseConfigDefault)
	}
	env := herContainerEnv(266, "s1-", "sk-test", "", "cli_test", "carher-266-secret", "", "chatgpt-pro", "/opt/carher-runtime/templates/hermes-config.carher-pro.yaml", "")
	if hasEnv(env, "CARHER_DIFY_ENABLED") {
		t.Fatal("default profile must not inject Dify env")
	}
	if !hasEnv(env, "LITELLM_API_KEY") {
		t.Fatal("litellm key should still be injected on default profile")
	}
	if mounts := runtimeProfileVolumeMounts(""); len(mounts) != 0 {
		t.Fatalf("default profile mounts = %v, want none", mounts)
	}
	if initContainers := runtimeProfileInitContainers("", "image"); len(initContainers) != 0 {
		t.Fatalf("default profile init containers = %v, want none", initContainers)
	}
}

func TestRuntimeProfileH75Openclaw(t *testing.T) {
	if got := baseConfigNameForRuntimeProfile("h75-runtime-b600887-20260530", RuntimeProfileH75Openclaw); got != BaseConfigH75Openclaw {
		t.Fatalf("h75 base config = %q, want %q", got, BaseConfigH75Openclaw)
	}
	if got := runtimeProfilePodSpecKey(RuntimeProfileH75Openclaw); got != "|profile=h75-openclaw|a2a-acp-engine-v19-hermes-feishu-deps" {
		t.Fatalf("pod spec key suffix = %q", got)
	}

	env := herContainerEnv(266, "s1-", "sk-test", RuntimeProfileH75Openclaw, "cli_test", "carher-266-secret", "oc_home", "chatgpt-pro", "/opt/carher-runtime/templates/hermes-config.carher-pro.yaml", "")
	for _, name := range []string{
		"FEISHU_APP_ID",
		"FEISHU_APP_SECRET",
		"OPENAI_BASE_URL",
		"PATH",
		"CARHER_PROD_KEY",
		"CARHER_REQUIRED_SECRET_ENVS",
		"CARHER_DIFY_ENABLED",
		"CARHER_DIFY_BOT_ID",
		"CARHER_DIFY_BASE_URL",
		"CARHER_DIFY_BOOTSTRAP_URL",
		"CARHER_DIFY_WORKSPACE_SLUG",
		"CARHER_DIFY_MODEL",
		"CARHER_DIFY_CODEX_BASE_URL",
		"CARHER_DIFY_CODEX_KEY_ENV",
		"CARHER_RUNTIME_PLUGINS_REFRESH",
		"CARHER_ACP_ENABLED",
		"CARHER_LAN_IP",
		"CARHER_A2A_PORT",
		"HERMESTEST_A2A_ENABLED",
		"HERMESTEST_A2A_HOST",
		"HERMESTEST_A2A_PORT",
		"HERMESTEST_A2A_PUBLIC_URL",
		"HERMESTEST_A2A_AUTH",
		"HERMESTEST_A2A_REGISTER_REDIS",
		"CARHER_SERVER",
		"FEISHU_ALLOW_ALL_USERS",
		"FEISHU_GROUP_POLICY",
		"PYTHONPATH",
		"CARHER_GATEWAY_TOKEN",
		"ANTHROPIC_AUTH_TOKEN",
		"ANTHROPIC_BASE_URL",
		"CARHER_DIFY_BOOTSTRAP_TOKEN",
		"FEISHU_HOME_CHANNEL",
	} {
		if !hasEnv(env, name) {
			t.Fatalf("h75 profile missing env %s", name)
		}
	}
	prodKey := findEnv(env, "CARHER_PROD_KEY")
	if prodKey == nil || prodKey.Value != "sk-test" {
		t.Fatalf("CARHER_PROD_KEY = %#v, want literal LiteLLM key", prodKey)
	}
	appSecret := findEnv(env, "FEISHU_APP_SECRET")
	if appSecret == nil || appSecret.ValueFrom == nil || appSecret.ValueFrom.SecretKeyRef == nil {
		t.Fatal("FEISHU_APP_SECRET must come from the per-instance SecretKeyRef")
	}
	if appSecret.ValueFrom.SecretKeyRef.Name != "carher-266-secret" || appSecret.ValueFrom.SecretKeyRef.Key != "app_secret" {
		t.Fatalf("FEISHU_APP_SECRET ref = %#v", appSecret.ValueFrom.SecretKeyRef)
	}
	pathEnv := findEnv(env, "PATH")
	if pathEnv == nil || !strings.Contains(pathEnv.Value, "/carher-fastbin") || !strings.Contains(pathEnv.Value, "/opt/hermes/venv/bin") || !strings.Contains(pathEnv.Value, "/opt/hermes/.venv/bin") {
		t.Fatalf("PATH = %#v", pathEnv)
	}
	token := findEnv(env, "CARHER_DIFY_BOOTSTRAP_TOKEN")
	if token == nil || token.ValueFrom == nil || token.ValueFrom.SecretKeyRef == nil {
		t.Fatal("bootstrap token must come from a SecretKeyRef")
	}
	if token.ValueFrom.SecretKeyRef.Name != DifyBootstrapTokenSecret {
		t.Fatalf("token secret = %q, want %q", token.ValueFrom.SecretKeyRef.Name, DifyBootstrapTokenSecret)
	}
	if token.ValueFrom.SecretKeyRef.Key != DifyBootstrapTokenKey {
		t.Fatalf("token key = %q, want %q", token.ValueFrom.SecretKeyRef.Key, DifyBootstrapTokenKey)
	}
	gatewayToken := findEnv(env, "CARHER_GATEWAY_TOKEN")
	if gatewayToken == nil || gatewayToken.ValueFrom == nil || gatewayToken.ValueFrom.SecretKeyRef == nil {
		t.Fatal("gateway token must come from a SecretKeyRef")
	}
	if gatewayToken.ValueFrom.SecretKeyRef.Name != H75RuntimeSecret {
		t.Fatalf("gateway token secret = %q, want %q", gatewayToken.ValueFrom.SecretKeyRef.Name, H75RuntimeSecret)
	}
	if gatewayToken.ValueFrom.SecretKeyRef.Key != "CARHER_GATEWAY_TOKEN" {
		t.Fatalf("gateway token key = %q", gatewayToken.ValueFrom.SecretKeyRef.Key)
	}
	anthropicToken := findEnv(env, "ANTHROPIC_AUTH_TOKEN")
	if anthropicToken == nil || anthropicToken.ValueFrom == nil || anthropicToken.ValueFrom.SecretKeyRef == nil {
		t.Fatal("Anthropic token must come from a SecretKeyRef")
	}
	if anthropicToken.ValueFrom.SecretKeyRef.Name != H75ACPSecret {
		t.Fatalf("Anthropic token secret = %q, want %q", anthropicToken.ValueFrom.SecretKeyRef.Name, H75ACPSecret)
	}
	if anthropicToken.ValueFrom.SecretKeyRef.Key != "ANTHROPIC_AUTH_TOKEN" {
		t.Fatalf("Anthropic token key = %q", anthropicToken.ValueFrom.SecretKeyRef.Key)
	}
	lanIP := findEnv(env, "CARHER_LAN_IP")
	if lanIP == nil || lanIP.Value != "carher-266-svc.carher.svc.cluster.local" {
		t.Fatalf("CARHER_LAN_IP = %#v", lanIP)
	}
	a2aPort := findEnv(env, "CARHER_A2A_PORT")
	if a2aPort == nil || a2aPort.Value != "18800" {
		t.Fatalf("CARHER_A2A_PORT = %#v", a2aPort)
	}
	hermesA2AEnabled := findEnv(env, "HERMESTEST_A2A_ENABLED")
	if hermesA2AEnabled == nil || hermesA2AEnabled.Value != "1" {
		t.Fatalf("HERMESTEST_A2A_ENABLED = %#v", hermesA2AEnabled)
	}
	hermesA2AHost := findEnv(env, "HERMESTEST_A2A_HOST")
	if hermesA2AHost == nil || hermesA2AHost.Value != "0.0.0.0" {
		t.Fatalf("HERMESTEST_A2A_HOST = %#v", hermesA2AHost)
	}
	hermesA2APort := findEnv(env, "HERMESTEST_A2A_PORT")
	if hermesA2APort == nil || hermesA2APort.Value != "18800" {
		t.Fatalf("HERMESTEST_A2A_PORT = %#v", hermesA2APort)
	}
	hermesA2APublicURL := findEnv(env, "HERMESTEST_A2A_PUBLIC_URL")
	if hermesA2APublicURL == nil || hermesA2APublicURL.Value != "http://carher-266-svc.carher.svc.cluster.local:18800" {
		t.Fatalf("HERMESTEST_A2A_PUBLIC_URL = %#v", hermesA2APublicURL)
	}
	hermesA2AAuth := findEnv(env, "HERMESTEST_A2A_AUTH")
	if hermesA2AAuth == nil || hermesA2AAuth.Value != "none" {
		t.Fatalf("HERMESTEST_A2A_AUTH = %#v", hermesA2AAuth)
	}
	hermesA2ARegisterRedis := findEnv(env, "HERMESTEST_A2A_REGISTER_REDIS")
	if hermesA2ARegisterRedis == nil || hermesA2ARegisterRedis.Value != "1" {
		t.Fatalf("HERMESTEST_A2A_REGISTER_REDIS = %#v", hermesA2ARegisterRedis)
	}
	server := findEnv(env, "CARHER_SERVER")
	if server == nil || server.Value != "ack" {
		t.Fatalf("CARHER_SERVER = %#v", server)
	}
	homeChannel := findEnv(env, "FEISHU_HOME_CHANNEL")
	if homeChannel == nil || homeChannel.Value != "oc_home" {
		t.Fatalf("FEISHU_HOME_CHANNEL = %#v", homeChannel)
	}
	openAIBaseURL := findEnv(env, "OPENAI_BASE_URL")
	if openAIBaseURL == nil || openAIBaseURL.Value != InternalLiteLLMBaseURL {
		t.Fatalf("OPENAI_BASE_URL = %#v, want %q", openAIBaseURL, InternalLiteLLMBaseURL)
	}
	difyBaseURL := findEnv(env, "CARHER_DIFY_BASE_URL")
	if difyBaseURL == nil || difyBaseURL.Value != InternalDifyBaseURL {
		t.Fatalf("CARHER_DIFY_BASE_URL = %#v, want %q", difyBaseURL, InternalDifyBaseURL)
	}
	difyBootstrapURL := findEnv(env, "CARHER_DIFY_BOOTSTRAP_URL")
	if difyBootstrapURL == nil || difyBootstrapURL.Value != InternalDifyBootstrapURL {
		t.Fatalf("CARHER_DIFY_BOOTSTRAP_URL = %#v, want %q", difyBootstrapURL, InternalDifyBootstrapURL)
	}
	difyCodexBaseURL := findEnv(env, "CARHER_DIFY_CODEX_BASE_URL")
	if difyCodexBaseURL == nil || difyCodexBaseURL.Value != InternalLiteLLMBaseURL {
		t.Fatalf("CARHER_DIFY_CODEX_BASE_URL = %#v, want %q", difyCodexBaseURL, InternalLiteLLMBaseURL)
	}
	hermesProvider := findEnv(env, "HERMES_INFERENCE_PROVIDER")
	if hermesProvider == nil || hermesProvider.Value != "chatgpt-pro" {
		t.Fatalf("HERMES_INFERENCE_PROVIDER = %#v", hermesProvider)
	}
	hermesTemplate := findEnv(env, "CARHER_HERMES_CONFIG_TEMPLATE")
	if hermesTemplate == nil || hermesTemplate.Value != "/opt/carher-runtime/templates/hermes-config.carher-pro.yaml" {
		t.Fatalf("CARHER_HERMES_CONFIG_TEMPLATE = %#v", hermesTemplate)
	}
	pythonPath := findEnv(env, "PYTHONPATH")
	if pythonPath == nil || pythonPath.Value != HermesFeishuDepsPath {
		t.Fatalf("PYTHONPATH = %#v, want %q", pythonPath, HermesFeishuDepsPath)
	}
	allowAll := findEnv(env, "FEISHU_ALLOW_ALL_USERS")
	if allowAll == nil || allowAll.Value != "true" {
		t.Fatalf("FEISHU_ALLOW_ALL_USERS = %#v", allowAll)
	}
	groupPolicy := findEnv(env, "FEISHU_GROUP_POLICY")
	if groupPolicy == nil || groupPolicy.Value != "open" {
		t.Fatalf("FEISHU_GROUP_POLICY = %#v", groupPolicy)
	}
	pluginRefresh := findEnv(env, "CARHER_RUNTIME_PLUGINS_REFRESH")
	if pluginRefresh == nil || pluginRefresh.Value != "1" {
		t.Fatalf("CARHER_RUNTIME_PLUGINS_REFRESH = %#v", pluginRefresh)
	}

	mounts := runtimeProfileVolumeMounts(RuntimeProfileH75Openclaw)
	if !hasVolumeMount(mounts, "user-data", "/opt/data") {
		t.Fatal("h75 profile must mount user-data PVC at /opt/data for Hermes runtime data")
	}
	if !hasVolumeMountSubPath(mounts, "user-data", "/data/.engine", ".engine") {
		t.Fatal("h75 profile must persist /data/.engine on the user-data PVC for engine switching")
	}
	for _, item := range []struct {
		name string
		path string
	}{
		{"h75-fastbin", "/carher-fastbin"},
		{"h75-agent-skills", "/data/.agents/skills"},
		{"h75-openclaw-local", "/data/.openclaw/local"},
		{"h75-runtime-plugins", "/data/.openclaw/runtime-plugins"},
		{"h75-openclaw-extensions", "/data/.openclaw/extensions"},
		{"h75-openclaw-skills", "/data/.openclaw/skills"},
		{"h75-hermes-skills", "/opt/data/.hermes/skills"},
		{"h75-hermes-opt-skills", "/opt/data/skills"},
	} {
		if !hasVolumeMount(mounts, item.name, item.path) {
			t.Fatalf("h75 profile missing local-cache mount %s at %s", item.name, item.path)
		}
	}
	volumes := runtimeProfileVolumes(RuntimeProfileH75Openclaw)
	for _, name := range []string{
		"h75-fastbin",
		"h75-agent-skills",
		"h75-openclaw-local",
		"h75-runtime-plugins",
		"h75-openclaw-extensions",
		"h75-openclaw-skills",
		"h75-hermes-skills",
		"h75-hermes-opt-skills",
	} {
		if !hasEmptyDirVolume(volumes, name) {
			t.Fatalf("h75 profile missing emptyDir volume %s", name)
		}
	}
	initContainers := runtimeProfileInitContainers(RuntimeProfileH75Openclaw, "h75-image")
	if !hasInitContainer(initContainers, "copy-hermes-feishu-deps", "h75-image") {
		t.Fatal("h75 profile must install Hermes Feishu deps before the main container starts")
	}
	depsInit := findInitContainer(initContainers, "copy-hermes-feishu-deps")
	if depsInit == nil || !hasVolumeMount(depsInit.VolumeMounts, "h75-openclaw-local", "/data/.openclaw/local") {
		t.Fatal("copy-hermes-feishu-deps must write into h75-openclaw-local")
	}
	ports := runtimeProfileContainerPorts(RuntimeProfileH75Openclaw)
	if !hasContainerPort(ports, "a2a-http", 18800) {
		t.Fatal("h75 profile must expose a2a-gateway HTTP port 18800")
	}
	if !hasContainerPort(ports, "a2a-grpc", 18801) {
		t.Fatal("h75 profile must expose a2a-gateway gRPC port 18801")
	}
	servicePorts := servicePortsForRuntimeProfile(RuntimeProfileH75Openclaw)
	if !hasServicePort(servicePorts, "a2a-http", 18800, 18800) {
		t.Fatal("h75 Service must expose a2a-gateway HTTP port 18800")
	}
	if !hasServicePort(servicePorts, "a2a-grpc", 18801, 18801) {
		t.Fatal("h75 Service must expose a2a-gateway gRPC port 18801")
	}

	defaultMounts := []corev1.VolumeMount{
		{Name: "dept-skills", MountPath: "/data/.agents/skills", ReadOnly: true},
		{Name: "shared-skills", MountPath: "/data/.openclaw/skills", ReadOnly: true},
	}
	filtered := withoutVolumeMount(defaultMounts, "dept-skills", "/data/.agents/skills")
	filtered = withoutVolumeMount(filtered, "shared-skills", "/data/.openclaw/skills")
	if hasVolumeMount(filtered, "dept-skills", "/data/.agents/skills") {
		t.Fatal("h75 profile must remove read-only dept skills mount so runtime can write /data/.agents/skills")
	}
	if hasVolumeMount(filtered, "shared-skills", "/data/.openclaw/skills") {
		t.Fatal("h75 profile must remove read-only shared skills mount so runtime can manage /data/.openclaw/skills")
	}
}

func TestRuntimeProfileH75HermesOverrides(t *testing.T) {
	env := herContainerEnv(
		1000,
		"s1-",
		"sk-test",
		RuntimeProfileH75Openclaw,
		"cli_test",
		"carher-1000-secret",
		"",
		"litellm",
		"/opt/data/.hermes/config.yaml",
		"",
	)
	hermesProvider := findEnv(env, "HERMES_INFERENCE_PROVIDER")
	if hermesProvider == nil || hermesProvider.Value != "litellm" {
		t.Fatalf("HERMES_INFERENCE_PROVIDER = %#v", hermesProvider)
	}
	hermesTemplate := findEnv(env, "CARHER_HERMES_CONFIG_TEMPLATE")
	if hermesTemplate == nil || hermesTemplate.Value != "/opt/data/.hermes/config.yaml" {
		t.Fatalf("CARHER_HERMES_CONFIG_TEMPLATE = %#v", hermesTemplate)
	}
}

func TestHermesPodSpecKeyOnlyWhenAnnotated(t *testing.T) {
	if got := hermesPodSpecKey(nil); got != "" {
		t.Fatalf("nil Hermes pod spec key = %q, want empty", got)
	}
	plain := &herv1.HerInstance{}
	if got := hermesPodSpecKey(plain); got != "" {
		t.Fatalf("plain Hermes pod spec key = %q, want empty", got)
	}
	annotated := &herv1.HerInstance{ObjectMeta: metav1.ObjectMeta{Annotations: map[string]string{
		AnnotationHermesProvider:       "litellm",
		AnnotationHermesConfigTemplate: "/opt/data/.hermes/config-litellm.yaml",
	}}}
	want := "|hermes-provider=litellm|hermes-template=/opt/data/.hermes/config-litellm.yaml"
	if got := hermesPodSpecKey(annotated); got != want {
		t.Fatalf("annotated Hermes pod spec key = %q, want %q", got, want)
	}
}

func hasEnv(envs []corev1.EnvVar, name string) bool {
	return findEnv(envs, name) != nil
}

func findEnv(envs []corev1.EnvVar, name string) *corev1.EnvVar {
	for i := range envs {
		if envs[i].Name == name {
			return &envs[i]
		}
	}
	return nil
}

func hasInitContainer(containers []corev1.Container, name, image string) bool {
	container := findInitContainer(containers, name)
	return container != nil && container.Image == image
}

func findInitContainer(containers []corev1.Container, name string) *corev1.Container {
	for i := range containers {
		if containers[i].Name == name {
			return &containers[i]
		}
	}
	return nil
}

func hasVolumeMount(mounts []corev1.VolumeMount, name, mountPath string) bool {
	for _, mount := range mounts {
		if mount.Name == name && mount.MountPath == mountPath {
			return true
		}
	}
	return false
}

func hasVolumeMountSubPath(mounts []corev1.VolumeMount, name, mountPath, subPath string) bool {
	for _, mount := range mounts {
		if mount.Name == name && mount.MountPath == mountPath && mount.SubPath == subPath {
			return true
		}
	}
	return false
}

func hasContainerPort(ports []corev1.ContainerPort, name string, port int32) bool {
	for _, item := range ports {
		if item.Name == name && item.ContainerPort == port {
			return true
		}
	}
	return false
}

func hasEmptyDirVolume(volumes []corev1.Volume, name string) bool {
	for _, volume := range volumes {
		if volume.Name == name && volume.EmptyDir != nil {
			return true
		}
	}
	return false
}

func hasServicePort(ports []corev1.ServicePort, name string, port int32, targetPort int) bool {
	for _, item := range ports {
		if item.Name == name && item.Port == port && item.TargetPort.IntValue() == targetPort {
			return true
		}
	}
	return false
}
