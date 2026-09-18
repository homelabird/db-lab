{{- define "db-lab.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" -}}
{{- end -}}
{{- define "db-lab.fullname" -}}
{{- printf "%s-%s" .Release.Name (include "db-lab.name" .) | trunc 63 | trimSuffix "-" -}}
{{- end -}}
{{- define "db-lab.labels" -}}
app.kubernetes.io/name: {{ include "db-lab.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
helm.sh/chart: {{ printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | quote }}
{{- end -}}
{{- define "db-lab.selector" -}}
app.kubernetes.io/name: {{ include "db-lab.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end -}}
