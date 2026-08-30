{{/*
Common labels
*/}}
{{- define "online-boutique.labels" -}}
app.kubernetes.io/part-of: online-boutique
app.kubernetes.io/managed-by: helm
{{- end -}}

{{/*
Build the full image name for a service from registry/prefix/tag.
The former `staticImage` escape hatch was removed with the shopping assistant's
nginx placeholder - every deployed service now runs its own built image.
*/}}
{{- define "online-boutique.image" -}}
{{ .global.image.registry }}/{{ .global.image.prefix }}-{{ .name }}:{{ .global.image.tag }}
{{- end -}}
