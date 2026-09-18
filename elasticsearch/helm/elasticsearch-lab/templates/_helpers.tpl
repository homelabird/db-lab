{{- define "elasticsearch-lab.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" }}
{{- end }}

{{- define "elasticsearch-lab.fullname" -}}
{{- if .Values.fullnameOverride }}
{{- .Values.fullnameOverride | trunc 63 | trimSuffix "-" }}
{{- else }}
{{- printf "%s-%s" .Release.Name (include "elasticsearch-lab.name" .) | trunc 63 | trimSuffix "-" }}
{{- end }}
{{- end }}

{{- define "elasticsearch-lab.labels" -}}
helm.sh/chart: {{ printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | quote }}
app.kubernetes.io/name: {{ include "elasticsearch-lab.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end }}

{{- define "elasticsearch-lab.selectorLabels" -}}
app.kubernetes.io/name: {{ include "elasticsearch-lab.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end }}

{{- define "elasticsearch-lab.esHeadless" -}}
{{- printf "%s-elasticsearch-headless" (include "elasticsearch-lab.fullname" .) }}
{{- end }}

{{- define "elasticsearch-lab.esHttp" -}}
{{- printf "%s-elasticsearch-http" (include "elasticsearch-lab.fullname" .) }}
{{- end }}

{{- define "elasticsearch-lab.discoveryHosts" -}}
{{- $root := . -}}
{{- range $i, $_ := until (int .Values.elasticsearch.replicas) -}}
{{- if $i }},{{ end }}{{ include "elasticsearch-lab.fullname" $root }}-elasticsearch-{{ $i }}.{{ include "elasticsearch-lab.esHeadless" $root }}
{{- end -}}
{{- end }}

{{- define "elasticsearch-lab.initialMasterNodes" -}}
{{- $root := . -}}
{{ include "elasticsearch-lab.fullname" $root }}-elasticsearch-0,{{ include "elasticsearch-lab.fullname" $root }}-elasticsearch-1,{{ include "elasticsearch-lab.fullname" $root }}-elasticsearch-2
{{- end }}
