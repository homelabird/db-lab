{{- define "db-lab.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 40 | trimSuffix "-" -}}
{{- end -}}
{{- define "db-lab.fullname" -}}
{{- $name := printf "%s-%s" .Release.Name (include "db-lab.name" .) -}}
{{- if gt (len $name) 40 -}}
{{- printf "%s-%s" ($name | trunc 31 | trimSuffix "-") ($name | sha256sum | trunc 8) -}}
{{- else -}}{{ $name }}{{- end -}}
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

{{- define "db-lab.dns" -}}
{{- printf "%s.%s.svc.%s" .service .root.Release.Namespace .root.Values.clusterDomain -}}
{{- end -}}
{{- define "db-lab.storageClass" -}}
{{- if ne .Values.storageClassName nil }}
storageClassName: {{ .Values.storageClassName | quote }}
{{- end }}
{{- end -}}
{{- define "db-lab.placement" -}}
affinity:
  podAntiAffinity:
    {{- if .root.Values.scheduling.requireSeparateNodes }}
    requiredDuringSchedulingIgnoredDuringExecution:
      - topologyKey: kubernetes.io/hostname
        labelSelector:
          matchLabels:
            app.kubernetes.io/instance: {{ .root.Release.Name }}
            component: {{ .component }}
    {{- else }}
    preferredDuringSchedulingIgnoredDuringExecution:
      - weight: 100
        podAffinityTerm:
          topologyKey: kubernetes.io/hostname
          labelSelector:
            matchLabels:
              app.kubernetes.io/instance: {{ .root.Release.Name }}
              component: {{ .component }}
    {{- end }}
{{- end -}}
