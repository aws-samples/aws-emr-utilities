css.cluster.name = {{ .Env.CLUSTER_NAME | default "csscluster" }}
css.worker.registry.type = {{ .Env.REGISTRY_TYPE | default "zookeeper" }}
css.zookeeper.address = {{ .Env.ZK_ADDRESS }}

# local disk storage
css.diskFlusher.base.dirs = {{ .Env.SHUFFLE_DATA_DIR }}
css.disk.dir.num.min = 1

# css worker common conf
{{- if (index .Env "CSS_CONF_PARAMS")  }}
  {{- $conf_list := .Env.CSS_CONF_PARAMS | strings.Split ";" }}
  {{- range $parameter := $conf_list}}
{{ $parameter }}
  {{- end }}
{{- end }}