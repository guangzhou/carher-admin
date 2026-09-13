{{- define "litellm-proxy.name" -}}
{{- .Values.nameOverride -}}
{{- end -}}

{{- define "litellm-proxy.selectorLabels" -}}
{{- toYaml .Values.selectorLabels -}}
{{- end -}}

{{- define "litellm-proxy.labels" -}}
{{ include "litellm-proxy.selectorLabels" . }}
app.kubernetes.io/name: litellm-proxy
app.kubernetes.io/managed-by: {{ .Release.Service }}
helm.sh/chart: {{ printf "%s-%s" .Chart.Name .Chart.Version | quote }}
{{- end -}}

{{- define "litellm-proxy.configChecksum" -}}
{{- toYaml .Values.config.data | sha256sum | trunc 12 -}}
{{- end -}}

{{- define "litellm-proxy.callbacksChecksum" -}}
{{- toYaml .Values.callbacks.data | sha256sum | trunc 12 -}}
{{- end -}}

{{- define "litellm-proxy.callbacksEvidenceChecksum" -}}
{{- printf "sha256:%s" (sha256sum (toRawJson .Values.callbacks.data)) -}}
{{- end -}}

{{- define "litellm-proxy.runtimeChecksum" -}}
{{- $runtime := dict "args" .Values.args "command" .Values.command "extraEnv" .Values.extraEnv "secretRefs" .Values.secretRefs -}}
{{- printf "sha256:%s" (sha256sum (toRawJson $runtime)) -}}
{{- end -}}

{{- define "litellm-proxy.configName" -}}
{{- printf "%s-config-%s" .Release.Name (include "litellm-proxy.configChecksum" .) -}}
{{- end -}}

{{- define "litellm-proxy.callbacksName" -}}
{{- printf "%s-callbacks-%s" .Release.Name (include "litellm-proxy.callbacksChecksum" .) -}}
{{- end -}}

{{- define "litellm-proxy.snapshotName" -}}
{{- $checksum := toYaml .snapshot.data | sha256sum | trunc 12 -}}
{{- $prefix := printf "%s-%s" .root.Release.Name .snapshot.name | trunc 50 | trimSuffix "-" -}}
{{- printf "%s-%s" $prefix $checksum -}}
{{- end -}}

{{- define "litellm-proxy.validate" -}}
{{- if and .Values.artifactTemplate (not .Values.allowTemplateRender) -}}
{{- fail "artifactTemplate=true is an example only; render a frozen run values file with artifactTemplate=false" -}}
{{- end -}}
{{- $expectedPort := dict "litellm-proxy" 30402 "litellm-proxy-gray" 30405 "litellm-proxy-guarded-old" 30406 -}}
{{- $name := include "litellm-proxy.name" . -}}
{{- if ne .Values.selectorLabels.app $name -}}
{{- fail "selectorLabels.app must exactly match nameOverride" -}}
{{- end -}}
{{- if ne (int .Values.service.nodePort) (int (get $expectedPort $name)) -}}
{{- fail (printf "%s must use NodePort %v" $name (get $expectedPort $name)) -}}
{{- end -}}
{{- if and (eq $name "litellm-proxy") (not .Values.productionRouteEnabled) -}}
{{- fail "litellm-proxy must enable the production-route label" -}}
{{- end -}}
{{- if and (ne $name "litellm-proxy") .Values.productionRouteEnabled -}}
{{- fail "only litellm-proxy may enable the production-route label" -}}
{{- end -}}
{{- if .Values.schemaUpdateEnabled -}}
{{- fail "schemaUpdateEnabled must remain false; migrations run in the independent Job" -}}
{{- end -}}
{{- $configSha := printf "sha256:%s" (sha256sum (get .Values.config.data "config.yaml")) -}}
{{- if ne .Values.schedulerSafety.configSha256 $configSha -}}
{{- fail "schedulerSafety.configSha256 must match the frozen config.yaml snapshot" -}}
{{- end -}}
{{- if ne .Values.schedulerSafety.callbacksSha256 (include "litellm-proxy.callbacksEvidenceChecksum" .) -}}
{{- fail "schedulerSafety.callbacksSha256 must match the frozen callbacks snapshot" -}}
{{- end -}}
{{- if ne .Values.schedulerSafety.runtimeSha256 (include "litellm-proxy.runtimeChecksum" .) -}}
{{- fail "schedulerSafety.runtimeSha256 must match the frozen runtime shape" -}}
{{- end -}}
{{- if or (eq .Values.schedulerSafety.evidenceSha256 "sha256:0000000000000000000000000000000000000000000000000000000000000000") (eq .Values.schedulerSafety.evidenceSha256 "sha256:1111111111111111111111111111111111111111111111111111111111111111") -}}
{{- fail "schedulerSafety evidence checksum is a placeholder" -}}
{{- end -}}
{{- if or (eq .Values.schedulerSafety.sourcePayloadSha256 "sha256:0000000000000000000000000000000000000000000000000000000000000000") (eq .Values.schedulerSafety.sourcePayloadSha256 "sha256:1111111111111111111111111111111111111111111111111111111111111111") -}}
{{- fail "schedulerSafety source payload checksum is a placeholder" -}}
{{- end -}}
{{- if eq $name "litellm-proxy" -}}
{{- if ne .Values.schedulerSafety.mode "primary" -}}
{{- fail "prod schedulerSafety.mode must be primary" -}}
{{- end -}}
{{- if not .Values.backgroundTasks.enabled -}}
{{- fail "prod backgroundTasks.enabled must be true" -}}
{{- end -}}
{{- else if ne .Values.schedulerSafety.mode "disabled" -}}
{{- fail "gray and guarded-old schedulerSafety.mode must be disabled" -}}
{{- else if .Values.backgroundTasks.enabled -}}
{{- fail "gray and guarded-old backgroundTasks.enabled must be false" -}}
{{- end -}}
{{- if ne (toJson .Values.workloadContract.containers) (toJson (list "litellm")) -}}
{{- fail "workloadContract.containers must exactly match the rendered container set" -}}
{{- end -}}
{{- if ne (toJson .Values.workloadContract.managedInitContainers) (toJson (list)) -}}
{{- fail "workloadContract.managedInitContainers must exactly match the chart-managed initContainer set" -}}
{{- end -}}
{{- if ne (toJson .Values.workloadContract.optionalInitContainers) (toJson (list "config-check")) -}}
{{- fail "workloadContract.optionalInitContainers must exactly match the approved optional initContainer set" -}}
{{- end -}}
{{- $approved := sortAlpha (deepCopy .Values.initContainers.approvedNames) -}}
{{- $actual := list -}}
{{- range .Values.initContainers.items -}}
{{- $actual = append $actual .name -}}
{{- end -}}
{{- $actual = sortAlpha $actual -}}
{{- if ne (toJson $approved) (toJson $actual) -}}
{{- fail "initContainers.items names must exactly match initContainers.approvedNames" -}}
{{- end -}}
{{- $snapshotNames := dict -}}
{{- $snapshotMounts := dict -}}
{{- range .Values.additionalSnapshots -}}
{{- if hasKey $snapshotNames .name -}}
{{- fail (printf "duplicate additionalSnapshots name: %s" .name) -}}
{{- end -}}
{{- if hasKey $snapshotMounts .mountPath -}}
{{- fail (printf "duplicate additionalSnapshots mountPath: %s" .mountPath) -}}
{{- end -}}
{{- if or (eq .mountPath "/app/config.yaml") (eq .mountPath "/app") -}}
{{- fail (printf "additionalSnapshots mountPath conflicts with LiteLLM config: %s" .mountPath) -}}
{{- end -}}
{{- $_ := set $snapshotNames .name true -}}
{{- $_ := set $snapshotMounts .mountPath true -}}
{{- end -}}
{{/* Drain budget is one derivation, not three independent knobs:
     grace >= preStop + streamDrain, and nginx proxy_read_timeout == streamDrain.
     A grace period shorter than the window nginx is willing to wait turns a
     clean 504 into a silent mid-stream SSE truncation. */}}
{{- $preStop := int .Values.drain.preStopSeconds -}}
{{- $streamDrain := int .Values.drain.streamDrainSeconds -}}
{{- $grace := int .Values.terminationGracePeriodSeconds -}}
{{- if lt $grace (add $preStop $streamDrain) -}}
{{- fail (printf "terminationGracePeriodSeconds=%d is below drain.preStopSeconds+drain.streamDrainSeconds=%d; raise the grace period or shorten the drain budget" $grace (add $preStop $streamDrain)) -}}
{{- end -}}
{{- $expectedPreStop := list "sh" "-c" (printf "sleep %d" $preStop) -}}
{{- if ne (toJson .Values.lifecycle.preStop.exec.command) (toJson $expectedPreStop) -}}
{{- fail (printf "lifecycle.preStop.exec.command must be exactly %s so it cannot drift from drain.preStopSeconds" (toJson $expectedPreStop)) -}}
{{- end -}}
{{- $envNames := dict "DISABLE_SCHEMA_UPDATE" true -}}
{{- range .Values.extraEnv -}}
{{- if hasKey $envNames .name -}}
{{- fail (printf "duplicate or reserved environment variable: %s" .name) -}}
{{- end -}}
{{- $_ := set $envNames .name true -}}
{{- end -}}
{{- end -}}
