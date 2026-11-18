#!/usr/bin/env bash
#
# EMR bootstrap action (emr-spark-8.2.0): install the Python 3.11 libraries the
# release ships to the wrong directory, so they are not importable on
# /usr/bin/python3.11 (the PySpark interpreter): boto3, cycler, jmespath,
# pyparsing, python-dateutil, six.
#
# Any bootstrap-action arguments are passed straight through to `pip install`,
# e.g. Args=[--index-url,https://mirror/simple/,--trusted-host,mirror].
#
# set -e makes a genuine pip failure fail the bootstrap (and the launch).
set -euo pipefail

PYTHON="${PYTHON:-/usr/bin/python3.11}"
PKGS=(boto3 cycler jmespath pyparsing python-dateutil six)

# Pin installed packages so pip doesn't upgrade/replace what EMR shipped
# (notably botocore); boto3<->botocore are lockstep, so pin boto3 to match.
constraints="$(mktemp)"
trap 'rm -f "$constraints"' EXIT
"$PYTHON" -m pip list --format=freeze --disable-pip-version-check 2>/dev/null \
  | grep -E '^[A-Za-z0-9_.-]+==' > "$constraints" || true
boto_ver="$(grep -iE '^botocore==' "$constraints" | head -1 | cut -d'=' -f3 || true)"
if [[ -n "$boto_ver" ]] && ! grep -iqE '^boto3==' "$constraints"; then
  echo "boto3==$boto_ver" >> "$constraints"
fi

# "$@" = any extra bootstrap-action args, forwarded verbatim to pip install
# (user flags come after our defaults so they win, before -c / the packages).
sudo -H "$PYTHON" -m pip install --disable-pip-version-check \
  --no-warn-script-location --retries 5 --timeout 60 \
  "$@" -c "$constraints" "${PKGS[@]}"
