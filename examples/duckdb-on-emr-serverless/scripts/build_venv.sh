#!/usr/bin/env bash
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
# Build the two archives the jobs expect and upload them to S3.
# Run it in AWS CloudShell or on any Amazon Linux 2023 x86_64 host: the native libraries must match the
# EMR Serverless runtime (Python 3.11, x86_64).
#
# Usage: S3_BUCKET=s3://<your-bucket> bash build_venv.sh
set -euo pipefail
: "${S3_BUCKET:?Set S3_BUCKET, for example s3://amzn-s3-demo-bucket}"
DUCKDB_VER="${DUCKDB_VER:-2.0.0}"

export TMPDIR="$HOME/tmp"; mkdir -p "$TMPDIR"
command -v python3.11 >/dev/null || sudo dnf install -y python3.11

cd "$HOME"
rm -rf duckdb_env duckdb_env.tar.gz tpcds_ext tpcds_ext_tmp tpcds_ext.tar.gz

# 1. DuckDB venv with the extensions bundled (no internet access at run time)
python3.11 -m venv --copies duckdb_env
source duckdb_env/bin/activate
# Pinned versions for a reproducible build. Extensions come from the official DuckDB extension repository;
# DuckDB checks their signatures and refuses unsigned extensions by default.
pip install --quiet "pip==26.3"
pip install --quiet "duckdb==$DUCKDB_VER" "venv-pack==0.2.0"
mkdir -p duckdb_env/duckdb_extensions
python - <<'PY'
import os, duckdb
con = duckdb.connect(config={"extension_directory": os.path.abspath("duckdb_env/duckdb_extensions")})
for e in ("httpfs", "aws", "avro", "iceberg"):
    con.install_extension(e)
    con.load_extension(e)
print("DuckDB", duckdb.__version__, "extensions OK")
PY
venv-pack -f -o duckdb_env.tar.gz
aws s3 cp duckdb_env.tar.gz "$S3_BUCKET/envs/duckdb20_env.tar.gz"

# 2. The tpcds extension (dsdgen) is only needed to generate the data, so it ships as a second archive
mkdir -p tpcds_ext
python - <<'PY'
import os, duckdb
con = duckdb.connect(config={"extension_directory": os.path.abspath("tpcds_ext_tmp")})
con.install_extension("tpcds")
PY
cp "$(find tpcds_ext_tmp -name tpcds.duckdb_extension | head -1)" tpcds_ext/
tar czf tpcds_ext.tar.gz -C tpcds_ext tpcds.duckdb_extension
aws s3 cp tpcds_ext.tar.gz "$S3_BUCKET/envs/tpcds_ext.tar.gz"

echo "DONE: $S3_BUCKET/envs/duckdb20_env.tar.gz and $S3_BUCKET/envs/tpcds_ext.tar.gz"
