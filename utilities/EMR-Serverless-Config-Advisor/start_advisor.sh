#!/bin/bash
# Starts the EMR Serverless Advisor UI with a self-healing SSM tunnel to
# Prometheus on the monitoring host. Use this instead of `python3 app.py`
# when the monitoring host's 9090 isn't directly reachable (corp egress
# filtering) — the tunnel is watched and restarted if the SSM session expires.
#
#   ./start_advisor.sh            # foreground app, background tunnel-keeper
#
MONITORING_INSTANCE="${MONITORING_INSTANCE:?set MONITORING_INSTANCE to your monitoring EC2 instance id}"
LOCAL_PORT="${LOCAL_PORT:-19090}"
REGION="${AWS_REGION:-us-east-1}"
DIR="$(cd "$(dirname "$0")" && pwd)"

tunnel_alive() {
    curl -s -m 3 -o /dev/null "http://localhost:${LOCAL_PORT}/api/v1/query?query=up"
}

keep_tunnel() {
    while true; do
        if ! tunnel_alive; then
            echo "[tunnel-keeper] (re)starting SSM port-forward to ${MONITORING_INSTANCE}:9090"
            pkill -f "session-manager-plugin.*${LOCAL_PORT}" 2>/dev/null
            nohup aws ssm start-session \
                --target "$MONITORING_INSTANCE" \
                --document-name AWS-StartPortForwardingSession \
                --parameters "{\"portNumber\":[\"9090\"],\"localPortNumber\":[\"${LOCAL_PORT}\"]}" \
                --region "$REGION" >> /tmp/advisor-ssm-tunnel.log 2>&1 &
            sleep 8
        fi
        sleep 30
    done
}

keep_tunnel &
KEEPER_PID=$!
trap 'kill $KEEPER_PID 2>/dev/null' EXIT

export PROMETHEUS_URL="http://localhost:${LOCAL_PORT}"
export MODEL_ID="${MODEL_ID:-global.anthropic.claude-fable-5}"
cd "$DIR"
exec python3 app.py
