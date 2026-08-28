{{/*
Common labels
*/}}
{{- define "online-boutique.labels" -}}
app.kubernetes.io/part-of: online-boutique
app.kubernetes.io/managed-by: helm
{{- end -}}

{{/*
Build the full image name for a service.
Uses staticImage if set, otherwise constructs from registry/prefix/tag.
*/}}
{{- define "online-boutique.image" -}}
{{- if .svc.staticImage -}}
{{ .svc.staticImage }}
{{- else -}}
{{ .global.image.registry }}/{{ .global.image.prefix }}-{{ .name }}:{{ .global.image.tag }}
{{- end -}}
{{- end -}}
