{{/* vim: set filetype=mustache: */}}
{{/*
Expand the name of the chart.
*/}}
{{- define "hdfs.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{/*
Create a default fully qualified app name.
We truncate at 63 chars because some Kubernetes name fields are limited to this (by the DNS naming spec).
If release name contains chart name it will be used as a full name.
*/}}
{{- define "hdfs.fullname" -}}
{{- if .Values.fullnameOverride -}}
{{- .Values.fullnameOverride | trunc 63 | trimSuffix "-" -}}
{{- else -}}
{{- $name := default .Chart.Name .Values.nameOverride -}}
{{- if contains $name .Release.Name -}}
{{- .Release.Name | trunc 63 | trimSuffix "-" -}}
{{- else -}}
{{- printf "%s-%s" .Release.Name $name | trunc 63 | trimSuffix "-" -}}
{{- end -}}
{{- end -}}
{{- end -}}

{{/*
Create chart name and version as used by the chart label.
*/}}
{{- define "hdfs.chart" -}}
{{- printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{/*
Common labels
*/}}
{{- define "hdfs.labels" -}}
helm.sh/chart: {{ include "hdfs.chart" . }}
{{ include "hdfs.selectorLabels" . }}
{{- if .Chart.AppVersion }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
{{- end }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- if .Values.labels }}
{{ toYaml .Values.labels }}
{{- end -}}
{{- end -}}

{{/*
Selector labels
*/}}
{{- define "hdfs.selectorLabels" -}}
app.kubernetes.io/name: {{ include "hdfs.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end -}}

{{- define "hdfs.namenode.volumes" -}}
{{- $count := int . -}}
{{- range $vol := until $count -}}
	{{- if (gt $vol 0) -}},{{- end -}}
/fsx/nn{{- $vol -}}/dfs/name
{{- end -}}
{{- end -}}


{{- define "hdfs.datanode.volumes" -}}
{{- $count := int .volcnt -}}
{{- range $vol := until $count -}} 
	{{- if (gt $vol 0) -}},{{- end -}}
/fsx/data{{- $vol -}}/dfs/data
	{{- end -}}
	{{- end -}}


{{- define "hdfs.namenodeServiceAccountName" -}}
{{- default (printf "%s-namenode" (include "hdfs.fullname" .)) .Values.config.rackAwareness.serviceAccountName -}}
{{- end -}}
/