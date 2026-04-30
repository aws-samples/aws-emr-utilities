#!/bin/bash
# Renders Splunk config templates from environment variables and starts the forwarder.
#
# Required env vars:
#   SPLUNK_FORWARD_SERVER  - host:port of Splunk receiver (e.g. splunk.example.com:9997)
#
# Optional env vars:
#   SPLUNK_APP_NAME        - application name (default: emr-serverless-app)
#   SPLUNK_DEPLOYMENT_URI  - deployment server host (omit to skip deployment client)
#   SPLUNK_INDEX           - target index (default: main)

set +e

APP_NAME="${SPLUNK_APP_NAME:-emr-serverless-app}"
FORWARD_SERVER="${SPLUNK_FORWARD_SERVER:-}"
DEPLOYMENT_URI="${SPLUNK_DEPLOYMENT_URI:-}"
INDEX="${SPLUNK_INDEX:-main}"
CURRENT_HOST="${APP_NAME}-$(hostname)"

if [ -z "$FORWARD_SERVER" ]; then
    echo "SPLUNK_FORWARD_SERVER not set, skipping Splunk UF startup" >&2
    exit 0
fi

# Render inputs.conf
sed -e "s|{{APP_NAME}}|${APP_NAME}|g" \
    -e "s|{{HOST}}|${CURRENT_HOST}|g" \
    -e "s|{{INDEX}}|${INDEX}|g" \
    /opt/splunkforwarder/etc/system/local/inputs.conf.template \
    > /opt/splunkforwarder/etc/system/local/inputs.conf

# Render outputs.conf
sed -e "s|{{FORWARD_SERVER}}|${FORWARD_SERVER}|g" \
    /opt/splunkforwarder/etc/system/local/outputs.conf.template \
    > /opt/splunkforwarder/etc/system/local/outputs.conf

# Render deploymentclient.conf (only if deployment URI is set)
if [ -n "$DEPLOYMENT_URI" ]; then
    sed -e "s|{{APP_NAME}}|${APP_NAME}|g" \
        -e "s|{{HOST}}|${CURRENT_HOST}|g" \
        -e "s|{{DEPLOYMENT_URI}}|${DEPLOYMENT_URI}|g" \
        /opt/splunkforwarder/etc/system/local/deploymentclient.conf.template \
        > /opt/splunkforwarder/etc/system/local/deploymentclient.conf
fi

# Start Splunk UF in background
/opt/splunkforwarder/bin/splunk start --accept-license --no-prompt --answer-yes \
    > /tmp/splunk-start.log 2>&1

set -e
