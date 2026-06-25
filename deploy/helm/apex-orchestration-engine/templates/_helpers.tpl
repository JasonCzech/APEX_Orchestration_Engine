{{/*
Expand the name of the chart.
*/}}
{{- define "apex.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" }}
{{- end }}

{{/*
Fully qualified app name (release-name aware, 63-char DNS limit).
*/}}
{{- define "apex.fullname" -}}
{{- if .Values.fullnameOverride }}
{{- .Values.fullnameOverride | trunc 63 | trimSuffix "-" }}
{{- else }}
{{- $name := default .Chart.Name .Values.nameOverride }}
{{- if contains $name .Release.Name }}
{{- .Release.Name | trunc 63 | trimSuffix "-" }}
{{- else }}
{{- printf "%s-%s" .Release.Name $name | trunc 63 | trimSuffix "-" }}
{{- end }}
{{- end }}
{{- end }}

{{/*
Chart name and version for the helm.sh/chart label.
*/}}
{{- define "apex.chart" -}}
{{- printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" }}
{{- end }}

{{/*
Common labels.
*/}}
{{- define "apex.labels" -}}
helm.sh/chart: {{ include "apex.chart" . }}
{{ include "apex.selectorLabels" . }}
app.kubernetes.io/version: {{ .Values.image.tag | default .Chart.AppVersion | quote }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end }}

{{/*
Selector labels.
*/}}
{{- define "apex.selectorLabels" -}}
app.kubernetes.io/name: {{ include "apex.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end }}

{{/*
ServiceAccount name to use.
*/}}
{{- define "apex.serviceAccountName" -}}
{{- if .Values.serviceAccount.create -}}
{{- default (include "apex.fullname" .) .Values.serviceAccount.name }}
{{- else -}}
{{- default "default" .Values.serviceAccount.name }}
{{- end }}
{{- end }}

{{/*
Workload-identity client id (explicit serviceAccount annotation wins; else the
workloadIdentity convenience value).
*/}}
{{- define "apex.workloadIdentityClientId" -}}
{{- .Values.workloadIdentity.clientId | default .Values.secretBackend.csi.clientId -}}
{{- end }}

{{/*
Soft pod anti-affinity: spread replicas across hosts without blocking on small
clusters. Used only when .Values.affinity is empty.
*/}}
{{- define "apex.podAntiAffinity" -}}
podAntiAffinity:
  preferredDuringSchedulingIgnoredDuringExecution:
    - weight: 100
      podAffinityTerm:
        labelSelector:
          matchLabels:
            {{- include "apex.selectorLabels" . | nindent 12 }}
        topologyKey: kubernetes.io/hostname
{{- end }}

{{/*
Default soft topology spread (zone) used when topologySpreadConstraints is empty.
*/}}
{{- define "apex.topologySpread" -}}
- maxSkew: {{ .Values.spreadConstraints.maxSkew }}
  topologyKey: {{ .Values.spreadConstraints.topologyKey }}
  whenUnsatisfiable: {{ .Values.spreadConstraints.whenUnsatisfiable }}
  labelSelector:
    matchLabels:
      {{- include "apex.selectorLabels" . | nindent 6 }}
{{- end }}

{{/*
Azure Key Vault SecretProviderClass name.
*/}}
{{- define "apex.akvSecretProviderClassName" -}}
{{- default (printf "%s-akv" (include "apex.fullname" .)) .Values.secretBackend.csi.secretProviderClassName -}}
{{- end }}

{{/*
Dashboard names/labels. A distinct app.kubernetes.io/name (-dashboard suffix)
keeps the server Service selector from matching dashboard pods.
*/}}
{{- define "apex.dashboard.fullname" -}}
{{- printf "%s-dashboard" (include "apex.fullname" .) | trunc 63 | trimSuffix "-" -}}
{{- end }}

{{- define "apex.dashboard.selectorLabels" -}}
app.kubernetes.io/name: {{ include "apex.name" . }}-dashboard
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end }}

{{- define "apex.dashboard.labels" -}}
helm.sh/chart: {{ include "apex.chart" . }}
{{ include "apex.dashboard.selectorLabels" . }}
app.kubernetes.io/version: {{ .Values.dashboard.image.tag | default .Chart.AppVersion | quote }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
app.kubernetes.io/component: dashboard
{{- end }}

{{/*
Container env shared by the server and the migration/bootstrap Jobs: the DB URIs
(+ optional license). Kept in one place so the Jobs run with the same config.
*/}}
{{- define "apex.dbEnv" -}}
- name: DATABASE_URI
  valueFrom:
    secretKeyRef:
      name: {{ .Values.database.existingSecret | quote }}
      key: {{ .Values.database.uriKey | quote }}
- name: APEX_DATABASE__URI
  valueFrom:
    secretKeyRef:
      name: {{ .Values.database.existingSecret | quote }}
      key: {{ required "database.apexUriKey is required" .Values.database.apexUriKey | quote }}
{{- end }}
