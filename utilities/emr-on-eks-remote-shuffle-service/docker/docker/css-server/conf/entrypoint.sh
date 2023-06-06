#!/bin/bash

function log () {
    level=$1
    message=$2
    echo $(date  '+%d-%m-%Y %H:%M:%S') [${level}]  ${message}
}

CONF_DIR="$CSS_HOME/conf"

if [ -d ${CONF_DIR}/templates ]; then
  log "INFO" "🤖- Te(Go)mplating files!"
  log "INFO" "🗃️- Files to templating:"
  log "INFO" $(find ${CONF_DIR}/templates/* -maxdepth 1)
  for file in $(ls -1 ${CONF_DIR}/templates | grep -E '\.tpl$'); do
    log "INFO" "🚀- Templating $file"
    out_file=${file%%.tpl}
    if [[ "$file" =~ ^css-defaults ]]; then
      log "INFO" "🚀- Moving css-defaults.conf to $CONF_DIR/css-defaults.conf"
      gomplate -f ${CONF_DIR}/templates/${file} -o $CONF_DIR/${out_file}
    fi
    if [ $? -ne 0 ]; then
      log "ERROR" "Error rendering config template file ${CONF_DIR}/${out_file}. Aborting."
      exit 1
    fi
    log "INFO" "Generated config file from template in ${CONF_DIR}/${out_file}"
  done
fi