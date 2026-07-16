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
Build a derived Kubernetes name without truncating away its distinguishing
suffix. Callers pass `base` and a suffix without a leading dash.
*/}}
{{- define "apex.suffixedName" -}}
{{- $suffix := .suffix | trimPrefix "-" -}}
{{- if or (empty $suffix) (gt (len $suffix) 61) -}}
{{- fail "apex.suffixedName requires a suffix between 1 and 61 characters" -}}
{{- end -}}
{{- $maxBaseLength := sub 62 (len $suffix) | int -}}
{{- printf "%s-%s" (.base | trunc $maxBaseLength | trimSuffix "-") $suffix -}}
{{- end }}

{{/*
Chart name and version for the helm.sh/chart label.
*/}}
{{- define "apex.chart" -}}
{{- printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" }}
{{- end }}

{{/* Resolve an immutable digest when supplied, otherwise the chart/tag default. */}}
{{- define "apex.image" -}}
{{- if .Values.image.digest -}}
{{- printf "%s@%s" .Values.image.repository .Values.image.digest -}}
{{- else -}}
{{- printf "%s:%s" .Values.image.repository (.Values.image.tag | default .Chart.AppVersion) -}}
{{- end -}}
{{- end }}

{{- define "apex.dashboard.image" -}}
{{- if .Values.dashboard.image.digest -}}
{{- printf "%s@%s" .Values.dashboard.image.repository .Values.dashboard.image.digest -}}
{{- else -}}
{{- printf "%s:%s" .Values.dashboard.image.repository (.Values.dashboard.image.tag | default .Chart.AppVersion) -}}
{{- end -}}
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

{{- define "apex.hookWorkloadIdentityClientId" -}}
{{- .Values.hookWorkloadIdentity.clientId | default .Values.secretBackend.csi.hookClientId | default (include "apex.workloadIdentityClientId" .) -}}
{{- end }}

{{- define "apex.hookKeyvaultName" -}}
{{- .Values.secretBackend.csi.hookKeyvaultName | default .Values.secretBackend.csi.keyvaultName -}}
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
{{- default (include "apex.suffixedName" (dict "base" (include "apex.fullname" .) "suffix" "akv")) .Values.secretBackend.csi.secretProviderClassName -}}
{{- end }}

{{/*
Pre-install CSI hook resource names. These are deliberately distinct from the
ordinary workload SA/SPC, which Helm creates only after pre-install hooks finish.
*/}}
{{- define "apex.hookServiceAccountName" -}}
{{- if eq .Values.secretBackend.mode "secretsStoreCSI" -}}
{{- include "apex.defaultHookServiceAccountName" . -}}
{{- else -}}
default
{{- end -}}
{{- end }}

{{- define "apex.defaultHookServiceAccountName" -}}
{{- default (include "apex.suffixedName" (dict "base" (include "apex.fullname" .) "suffix" "hooks")) .Values.hookServiceAccountName -}}
{{- end }}

{{- define "apex.hookAkVSecretProviderClassName" -}}
{{- include "apex.suffixedName" (dict "base" (include "apex.akvSecretProviderClassName" .) "suffix" "hooks") -}}
{{- end }}

{{- define "apex.hookRuntimeAkVSecretProviderClassName" -}}
{{- include "apex.suffixedName" (dict "base" (include "apex.akvSecretProviderClassName" .) "suffix" "runtime-hooks") -}}
{{- end }}

{{- define "apex.csiSyncRbacName" -}}
{{- include "apex.suffixedName" (dict "base" (include "apex.fullname" .) "suffix" "csi-secret-sync") -}}
{{- end }}

{{- define "apex.databaseRoleCleanupServiceAccountName" -}}
{{- include "apex.suffixedName" (dict "base" (include "apex.fullname" .) "suffix" "db-role-cleanup") -}}
{{- end }}

{{- define "apex.backupServiceAccountName" -}}
{{- .Values.backupWorkloadIdentity.serviceAccountName | default "apex-minio-backup" -}}
{{- end }}

{{- define "apex.inventoryRbacName" -}}
{{- if .Values.rbac.clusterScope -}}
{{- $namespaceHash := sha256sum .Release.Namespace | trunc 8 -}}
{{- include "apex.suffixedName" (dict "base" (include "apex.fullname" .) "suffix" (printf "inventory-%s" $namespaceHash)) -}}
{{- else -}}
{{- include "apex.suffixedName" (dict "base" (include "apex.fullname" .) "suffix" "inventory") -}}
{{- end -}}
{{- end }}

{{/*
Dashboard names/labels. A distinct app.kubernetes.io/name (-dashboard suffix)
keeps the server Service selector from matching dashboard pods.
*/}}
{{- define "apex.dashboard.fullname" -}}
{{- include "apex.suffixedName" (dict "base" (include "apex.fullname" .) "suffix" "dashboard") -}}
{{- end }}

{{- define "apex.dashboard.selectorLabels" -}}
app.kubernetes.io/name: {{ include "apex.suffixedName" (dict "base" (include "apex.name" .) "suffix" "dashboard") }}
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
Helm smoke-test labels. Keep the name distinct from the server selector so the
server's egress policy does not accidentally isolate the test pod itself.
*/}}
{{- define "apex.test.selectorLabels" -}}
app.kubernetes.io/name: {{ include "apex.suffixedName" (dict "base" (include "apex.name" .) "suffix" "test") }}
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/component: helm-test
{{- end }}

{{- define "apex.test.labels" -}}
helm.sh/chart: {{ include "apex.chart" . }}
{{ include "apex.test.selectorLabels" . }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end }}

{{/* Hook pods must never satisfy the long-lived server Service selector. */}}
{{- define "apex.hookSelectorLabels" -}}
app.kubernetes.io/name: {{ include "apex.suffixedName" (dict "base" (include "apex.name" .) "suffix" "hooks") }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end }}

{{/* APEX DB URI used by Alembic/bootstrap; may use a dedicated migration role. */}}
{{- define "apex.hookDbEnv" -}}
{{- $defaultSecret := .Values.database.existingSecret -}}
{{- if eq .Values.secretBackend.mode "secretsStoreCSI" -}}
{{- $defaultSecret = "apex-database-bootstrap" -}}
{{- end -}}
{{- if .Values.databaseRoleProvisioning.enabled -}}
{{- $defaultSecret = .Values.databaseRoleProvisioning.migrationSecret -}}
{{- end -}}
- name: APEX_DATABASE__URI
  valueFrom:
    secretKeyRef:
      name: {{ .Values.migrations.databaseSecret | default $defaultSecret | quote }}
      key: {{ required "migrations.databaseKey is required" .Values.migrations.databaseKey | quote }}
{{- end }}

{{/* Bootstrap uses runtime DML credentials, never the migration/schema owner. */}}
{{- define "apex.bootstrapDbEnv" -}}
{{- $defaultSecret := .Values.database.existingSecret -}}
{{- if eq .Values.secretBackend.mode "secretsStoreCSI" -}}
{{- $defaultSecret = "apex-database-bootstrap" -}}
{{- end -}}
{{- $databaseSecret := .Values.bootstrap.databaseSecret | default $defaultSecret -}}
- name: DATABASE_URI
  valueFrom:
    secretKeyRef:
      name: {{ .Values.bootstrap.langgraphDatabaseSecret | default $databaseSecret | quote }}
      key: {{ .Values.bootstrap.langgraphDatabaseKey | default .Values.database.uriKey | quote }}
- name: APEX_DATABASE__URI
  valueFrom:
    secretKeyRef:
      name: {{ $databaseSecret | quote }}
      key: {{ .Values.bootstrap.databaseKey | default .Values.database.apexUriKey | quote }}
{{- end }}

{{/*
Non-secret settings for hook processes. Pre-install hooks cannot consume the
ordinary settings ConfigMap, so reproduce its derived LangGraph CORS contract
here instead of silently validating a different configuration.
*/}}
{{- define "apex.hookSettingsEnv" -}}
{{- if and (hasKey .Values.apexSettings "APEX_CORS_ORIGINS") (not (hasKey .Values.apexSettings "CORS_CONFIG")) }}
- name: CORS_CONFIG
  value: {{ dict
    "allow_origins" (get .Values.apexSettings "APEX_CORS_ORIGINS" | fromJsonArray)
    "allow_methods" (list "GET" "POST" "PUT" "PATCH" "DELETE" "OPTIONS")
    "allow_headers" (list "authorization" "content-type" "idempotency-key" "last-event-id" "x-api-key" "x-request-id")
    "allow_credentials" true
    "expose_headers" (list "content-location" "retry-after" "x-pagination-next" "x-pagination-total")
    "max_age" 600
    | toJson | quote }}
{{- end }}
{{- range $key, $value := .Values.apexSettings }}
- name: {{ $key }}
  value: {{ $value | quote }}
{{- end }}
{{- end }}

{{/* Bootstrap validates the same distributed-limit settings as the server. */}}
{{- define "apex.bootstrapRedisEnv" -}}
- name: REDIS_URI
  valueFrom:
    secretKeyRef:
      name: {{ required "redis.existingSecret is required" .Values.redis.existingSecret | quote }}
      key: {{ required "redis.uriKey is required" .Values.redis.uriKey | quote }}
{{- end }}

{{/* Runtime API-key pepper. Empty is allowed for auth-disabled/unlocked installs. */}}
{{- define "apex.authEnv" -}}
{{- if .Values.auth.existingSecret }}
- name: APEX_AUTH__API_KEY_HASH_PEPPER
  valueFrom:
    secretKeyRef:
      name: {{ required "auth.existingSecret is required" .Values.auth.existingSecret | quote }}
      key: {{ required "auth.apiKeyHashPepperKey is required" .Values.auth.apiKeyHashPepperKey | quote }}
{{- with .Values.auth.previousApiKeyHashPeppersKey }}
- name: APEX_AUTH__PREVIOUS_API_KEY_HASH_PEPPERS
  valueFrom:
    secretKeyRef:
      name: {{ required "auth.existingSecret is required" $.Values.auth.existingSecret | quote }}
      key: {{ . | quote }}
{{- end }}
{{- end }}
{{- end }}

{{/* Hook-only copy of the pepper used while bootstrap hashes the first key. */}}
{{- define "apex.bootstrapAuthEnv" -}}
{{- $defaultSecret := .Values.auth.existingSecret -}}
{{- if eq .Values.secretBackend.mode "secretsStoreCSI" -}}
{{- $defaultSecret = "apex-hook-auth" -}}
{{- end -}}
{{- $secret := .Values.bootstrap.authSecret | default $defaultSecret -}}
{{- if $secret }}
- name: APEX_AUTH__API_KEY_HASH_PEPPER
  valueFrom:
    secretKeyRef:
      name: {{ $secret | quote }}
      key: {{ required "auth.apiKeyHashPepperKey is required" .Values.auth.apiKeyHashPepperKey | quote }}
{{- with .Values.auth.previousApiKeyHashPeppersKey }}
- name: APEX_AUTH__PREVIOUS_API_KEY_HASH_PEPPERS
  valueFrom:
    secretKeyRef:
      name: {{ $secret | quote }}
      key: {{ . | quote }}
{{- end }}

{{- end }}
{{- end }}

{{/*
Reject secret-looking keys recursively before the bootstrap document is
serialized into a ConfigMap or retained in Helm release history. Runtime
bootstrap repeats this validation, but that is too late to protect
Kubernetes/Helm storage. `secret_ref` is the sole credential-bearing field and
may carry only the reference grammar supported by the runtime secrets adapter.
*/}}
{{- define "apex.assertSecretFreeOptions" -}}
{{- $value := .value -}}
{{- $label := .label -}}
{{- if kindIs "map" $value -}}
{{- range $key, $nested := $value -}}
{{- $normalizedKey := regexReplaceAll "[^A-Za-z0-9]" (lower (toString $key)) "" -}}
{{- if eq $normalizedKey "secretref" -}}
{{- if not (kindIs "string" $nested) -}}
{{- fail (printf "%s.%s must be a nonempty string using the supported env:NAME reference format" $label $key) -}}
{{- end -}}
{{- $secretRef := toString $nested -}}
{{- if not (regexMatch "^env:[A-Za-z_][A-Za-z0-9_]{0,254}$" $secretRef) -}}
{{- fail (printf "%s.%s must use the supported env:NAME reference format" $label $key) -}}
{{- end -}}
{{- else -}}
{{- $nonCredentialNames := list "authmode" "authenticationmode" "authtype" "authenticationtype" -}}
{{- $nonCredential := or (eq $normalizedKey "accesskey") (has $normalizedKey $nonCredentialNames) (regexMatch "accesskey(id|identifier)$" $normalizedKey) -}}
{{- $separatedCredential := regexMatch "(?i)(^|[^A-Za-z0-9])(password|passwd|pwd|passphrase|secret|client[_-]?secret|personal[_-]?access[_-]?token|pat|bearer|jwt|psk|(access|refresh|identity|id|session|security|api)?[_-]?token|api[_-]?key|access[_-]?key|(private|ssh|signing|encryption|shared|account|storage|subscription|session)[_-]?key|session[_-]?id|client[_-]?(certificate|cert)|private[_-]?pem|pfx|pkcs12|keystore|(set[_-]?)?cookie|(connection|database|db|postgres(ql)?|redis|broker|amqp|mongo(db)?)[_-]?(string|uri|url)|dsn|authorization|auth|credential|signature|sig|sas|x-amz-(credential|signature|security-token)|x-goog-signature)([^A-Za-z0-9]|$)" (toString $key) -}}
{{- $credentialSuffix := "(password|passwd|pwd|passphrase|secret|secretkey|clientsecret|personalaccesstoken|pat|bearer|jwt|psk|apikey|accesskey|privatekey|sshkey|signingkey|encryptionkey|sharedkey|accountkey|storagekey|subscriptionkey|sessionkey|accesstoken|refreshtoken|identitytoken|idtoken|sessiontoken|securitytoken|authtoken|sastoken|token|authheader|authorizationheader|basicauth|httpauth|sessionid|clientcertificate|clientcert|privatepem|pfx|pkcs12|keystore|authorization|authentication|credential|credentials|signature|connectionstring|databaseuri|databaseurl|postgresuri|postgresurl|postgresqluri|postgresqlurl|redisuri|redisurl|brokeruri|brokerurl|amqpuri|amqpurl|mongouri|mongourl|mongodburi|mongodburl|dsn|cookie|cookies|cookiejar)$" -}}
{{- $terminalCredential := regexMatch $credentialSuffix $normalizedKey -}}
{{- $wrappedCredential := false -}}
{{- $wrappedNonCredential := false -}}
{{- $candidate := $normalizedKey -}}
{{- if and (not $nonCredential) (not $separatedCredential) (not $terminalCredential) -}}
{{- range until 3 -}}
{{- $wrapper := regexFind "(value|string|binary|text|data|hash)$" $candidate -}}
{{- if $wrapper -}}
{{- $candidate = trimSuffix $wrapper $candidate -}}
{{- if or (has $candidate $nonCredentialNames) (regexMatch "accesskey(id|identifier)$" $candidate) -}}
{{- $wrappedNonCredential = true -}}
{{- else if regexMatch $credentialSuffix $candidate -}}
{{- $wrappedCredential = true -}}
{{- end -}}
{{- end -}}
{{- end -}}
{{- end -}}
{{- if and (not $nonCredential) (or $separatedCredential $terminalCredential (and (not $wrappedNonCredential) $wrappedCredential)) -}}
{{- fail (printf "%s contains a secret-bearing key %q; store credentials outside bootstrap.document and use secret_ref" $label $key) -}}
{{- end -}}
{{- end -}}
{{- include "apex.assertSecretFreeOptions" (dict "value" $nested "label" $label) -}}
{{- end -}}
{{- else if kindIs "slice" $value -}}
{{- range $nested := $value -}}
{{- include "apex.assertSecretFreeOptions" (dict "value" $nested "label" $label) -}}
{{- end -}}
{{- else if kindIs "string" $value -}}
{{- $authScheme := regexMatch "(?i)(^|[^A-Za-z0-9])(bearer|basic|digest)[[:space:]]+[^[:space:],;]+" $value -}}
{{- $uriUserinfo := regexMatch "(?i)[a-z][a-z0-9+.-]*://[^/@[:space:]?#]+@" $value -}}
{{- $credentialAssignment := regexMatch "(?i)(password|passwd|pwd|passphrase|token|secret|api[_-]?key|dsn|connection[_-]?(string|uri|url)|database[_-]?(string|uri|url)|db[_-]?(string|uri|url)|postgres(ql)?[_-]?(string|uri|url)|redis[_-]?(string|uri|url)|broker[_-]?(string|uri|url)|amqp[_-]?(string|uri|url)|mongo(db)?[_-]?(string|uri|url))[[:space:]]*[:=][[:space:]]*[^[:space:],;}&]+" $value -}}
{{- $compactJwt := regexMatch "(^|[^A-Za-z0-9_-])eyJ[A-Za-z0-9_-]{8,}[.][A-Za-z0-9_-]{8,}[.][A-Za-z0-9_-]{8,}($|[^A-Za-z0-9_-])" $value -}}
{{- $privateKeyBlock := regexMatch "-----BEGIN ((RSA|EC|DSA|OPENSSH|ENCRYPTED) )?PRIVATE KEY-----" $value -}}
{{- $providerToken := regexMatch "(^|[^A-Za-z0-9_-])(gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|glpat-[A-Za-z0-9_-]{20,}|xox[baprs]-[A-Za-z0-9_-]{20,}|xapp-[A-Za-z0-9_-]{20,}|[sr]k_(live|test)_[A-Za-z0-9]{16,}|sk-(proj-)?[A-Za-z0-9_-]{20,}|npm_[A-Za-z0-9]{20,}|pypi-[A-Za-z0-9_-]{20,}|hf_[A-Za-z0-9]{20,}|AIza[A-Za-z0-9_-]{35}|ya29[.][A-Za-z0-9._-]{20,}|SG[.][A-Za-z0-9_-]{22}[.][A-Za-z0-9_-]{43}|dckr_(pat|oat)_[A-Za-z0-9_-]{20,}|(?i:[a-z0-9]{14}[.]atlasv1[.][a-z0-9_=-]{60,70})|[A-Za-z0-9]{76}AZDO[A-Za-z0-9]{4})($|[^A-Za-z0-9_-])" $value -}}
{{- if or $authScheme $uriUserinfo $credentialAssignment $compactJwt $privateKeyBlock $providerToken -}}
{{- fail (printf "%s contains credential-shaped text; store credentials outside bootstrap.document and use secret_ref" $label) -}}
{{- end -}}
{{- end -}}
{{- end }}
