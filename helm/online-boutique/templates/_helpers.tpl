{{/*
Common labels
*/}}
{{- define "online-boutique.labels" -}}
app.kubernetes.io/part-of: online-boutique
app.kubernetes.io/managed-by: helm
{{- end -}}

{{/*
Build the full image name for a service from registry/prefix/tag.
A service's own `imageTag` overrides the global image.tag. Only sreagent sets
one, so rolling every service back to an older tag (which may predate the
agent's first image) never takes the agent down with it.
The former `staticImage` escape hatch was removed with the shopping assistant's
nginx placeholder - every deployed service now runs its own built image.
*/}}
{{- define "online-boutique.image" -}}
{{ .global.image.registry }}/{{ .global.image.prefix }}-{{ .name }}:{{ .svc.imageTag | default .global.image.tag }}
{{- end -}}
