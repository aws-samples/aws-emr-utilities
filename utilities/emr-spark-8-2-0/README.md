# EMR Spark 8.2.0 Python 3.11 Library Bootstrap Action

Make the Python 3.11 libraries that `emr-spark-8.2.0` ships importable from the PySpark interpreter.

## Problem

On `emr-spark-8.2.0`, several Python 3.11 libraries are shipped to the wrong directory, so they cannot be imported from `/usr/bin/python3.11`, the interpreter PySpark uses. PySpark jobs that import any of them fail with `ModuleNotFoundError`:

- `boto3`
- `cycler`
- `jmespath`
- `pyparsing`
- `python-dateutil`
- `six`

## Solution

A bootstrap action, `emr-8-2-pypi-ba.sh`, that installs these libraries with `pip` for `/usr/bin/python3.11` on every node when the cluster launches.

To avoid disturbing what the release already ships, the script:

- Pins every package already installed for the interpreter as a pip constraint, so pip does not upgrade or replace them (notably `botocore`).
- Pins `boto3` to the installed `botocore` version, because the two are released in lockstep.
- Retries downloads (`--retries 5 --timeout 60`).
- Runs with `set -euo pipefail`, so a pip failure fails the bootstrap action, and with it the cluster launch, instead of leaving a cluster without the libraries.

## Setup

1. Upload the bootstrap action to your S3 bucket:
   ```bash
   aws s3 cp emr-8-2-pypi-ba.sh s3://YOUR-BUCKET/bootstrap/emr-8-2-pypi-ba.sh
   ```

2. Add the bootstrap action when creating your EMR cluster:
   ```bash
   aws emr create-cluster \
     --release-label emr-spark-8.2.0 \
     --bootstrap-actions '[{
       "Name": "Install Python 3.11 libraries",
       "Path": "s3://YOUR-BUCKET/bootstrap/emr-8-2-pypi-ba.sh"
     }]' \
     ...
   ```

### Using a private PyPI mirror

Any bootstrap action arguments are passed straight through to `pip install`, after the script's defaults so they take precedence. For example, to install from a private mirror:

```bash
--bootstrap-actions '[{
  "Name": "Install Python 3.11 libraries",
  "Path": "s3://YOUR-BUCKET/bootstrap/emr-8-2-pypi-ba.sh",
  "Args": ["--index-url", "https://mirror.example.com/simple/", "--trusted-host", "mirror.example.com"]
}]'
```

### Using a different interpreter

The script targets `/usr/bin/python3.11` by default. Set the `PYTHON` environment variable to target another interpreter when running the script by hand.

## Verification

On any node of the cluster, confirm that the libraries import from the PySpark interpreter:

```bash
/usr/bin/python3.11 -c "import boto3, cycler, jmespath, pyparsing, dateutil, six; print('ok')"
```

## Artifacts

| File | Description |
|------|-------------|
| `emr-8-2-pypi-ba.sh` | Bootstrap action script: installs the missing Python 3.11 libraries, pinned to the versions the release already ships |
