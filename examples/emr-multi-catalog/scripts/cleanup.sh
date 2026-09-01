#!/usr/bin/env bash
#
# Clean up the demo: drop the demo tables/database and wipe the S3 warehouse.
# Delegates to the validated `run_demo.sh --phase cleanup` (reads .env for
# APP_ID / ROLE_ARN / BUCKET / REGION; extra flags are forwarded).
#
# Usage:
#   ./scripts/cleanup.sh
#
# Note: this does not delete the EMR Serverless application or the execution
# role. Remove those separately if you created them only for this demo.
#
set -euo pipefail
SELF_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec "${SELF_DIR}/run_demo.sh" --phase cleanup "$@"
