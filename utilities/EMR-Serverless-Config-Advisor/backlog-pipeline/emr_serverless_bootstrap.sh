#!/bin/bash
# ============================================================================
# EMR Serverless Bootstrap Script - Python Virtual Environment Builder
# ============================================================================
# Build a portable ARM64 venv with venv-pack using Python 3.11 official image
# Uses Python 3.11 on Debian Bullseye (GLIBC 2.31) for EMR Serverless compatibility
#
# Requirements:
#   - Docker installed and running
#   - requirements.txt in the same directory as this script
#
# Output:
#   - pyspark_venv.tar.gz in the output/ subdirectory
# ============================================================================

set -e

# Get the directory where this script is located
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REQUIREMENTS_FILE="requirements.txt"
VENV_ARCHIVE="pyspark_venv.tar.gz"

echo "============================================================================"
echo "📦 Building portable ARM64 venv with Python 3.11"
echo "============================================================================"
echo "Script directory: $SCRIPT_DIR"
echo "Requirements file: $SCRIPT_DIR/$REQUIREMENTS_FILE"
echo "Output archive: $VENV_ARCHIVE"
echo ""

# Verify requirements.txt exists
if [ ! -f "$SCRIPT_DIR/$REQUIREMENTS_FILE" ]; then
    echo "❌ Error: $REQUIREMENTS_FILE not found in $SCRIPT_DIR"
    exit 1
fi

echo "✅ Found $REQUIREMENTS_FILE"
echo ""

# Create output directory
mkdir -p "$SCRIPT_DIR/output"

echo "🐳 Building inside Python 3.11 Docker container..."
echo ""

docker run --rm --platform linux/arm64 \
    -v "$SCRIPT_DIR/$REQUIREMENTS_FILE:/requirements.txt:ro" \
    -v "$SCRIPT_DIR/output:/output" \
    python:3.11-slim-bullseye \
    bash -c "
        set -e

        echo '==> Checking Python and GLIBC version...'
        python --version
        ldd --version | head -1

        echo ''
        echo '==> Installing system dependencies...'
        apt-get update -qq > /dev/null
        apt-get install -y -qq gcc g++ binutils > /dev/null

        echo ''
        echo '==> Upgrading pip...'
        pip install --upgrade pip --quiet

        echo ''
        echo '==> Installing venv-pack...'
        pip install venv-pack --quiet

        echo ''
        echo '==> Creating virtual environment with --copies flag...'
        python -m venv --copies /tmp/myenv

        echo ''
        echo '==> Activating virtual environment...'
        source /tmp/myenv/bin/activate

        echo ''
        echo '==> Verifying Python version...'
        python --version

        echo ''
        echo '==> Installing dependencies from requirements.txt...'
        pip install -r /requirements.txt

        echo ''
        echo '==> Verifying key packages...'
        python -c 'import boto3; print(f\"✓ boto3: {boto3.__version__}\")'
        python -c 'import pandas; print(f\"✓ pandas: {pandas.__version__}\")'
        python -c 'import numpy; print(f\"✓ numpy: {numpy.__version__}\")'
        python -c 'import pyarrow; print(f\"✓ pyarrow: {pyarrow.__version__}\")'

        echo ''
        echo '==> Verifying standard library is present...'
        python -c 'import encodings; print(\"✓ encodings module found\")'
        python -c 'import json; print(\"✓ json module found\")'
        python -c 'import ssl; print(\"✓ ssl module found\")'

        echo ''
        echo '==> Ensuring standard library is in venv...'
        # Copy Python stdlib if not already present
        if [ ! -d /tmp/myenv/lib/python3.11/encodings ]; then
            echo '  Copying Python standard library...'
            cp -r /usr/local/lib/python3.11/* /tmp/myenv/lib/python3.11/
        else
            echo '  ✓ Standard library already present'
        fi

        echo ''
        echo '==> Packing virtual environment with venv-pack...'
        venv-pack -o /output/$VENV_ARCHIVE

        echo ''
        echo '==> Archive contents (first 30 files)...'
        tar -tf /output/$VENV_ARCHIVE | head -30

        echo ''
        echo '==> Checking for symlinks...'
        SYMLINKS=\$(tar -tvf /output/$VENV_ARCHIVE 2>/dev/null | grep -c ' -> ' || echo 0)
        if [ \"\$SYMLINKS\" -eq 0 ]; then
            echo '✅ No symlinks found'
        else
            echo \"⚠️  Found \$SYMLINKS symlinks\"
        fi

        echo ''
        echo '==> Archive statistics...'
        echo \"Total files: \$(tar -tf /output/$VENV_ARCHIVE | wc -l)\"
        ls -lh /output/$VENV_ARCHIVE | awk '{print \"Archive size: \" \$5}'

        echo ''
        echo '==> Checking Python binary GLIBC dependencies...'
        mkdir -p /tmp/check_venv
        tar -xzf /output/$VENV_ARCHIVE -C /tmp/check_venv
        strings /tmp/check_venv/bin/python3.11 | grep 'GLIBC_' | sort -u

        echo ''
        echo '==> Verifying stdlib in archive...'
        if tar -tf /output/$VENV_ARCHIVE | grep -q 'lib/python3.11/encodings'; then
            echo '✅ Standard library included in archive'
        else
            echo '⚠️  Standard library NOT found in archive'
        fi

        echo ''
        echo '✅ Build complete!'
    "

# Fix ownership (Docker writes as root)
if [ -f "$SCRIPT_DIR/output/$VENV_ARCHIVE" ]; then
    if [ "$(id -u)" != "0" ] && [ "$(stat -f '%u' "$SCRIPT_DIR/output/$VENV_ARCHIVE" 2>/dev/null || stat -c '%u' "$SCRIPT_DIR/output/$VENV_ARCHIVE" 2>/dev/null)" = "0" ]; then
        echo ""
        echo "🔧 Fixing file ownership..."
        sudo chown "$(id -u):$(id -g)" "$SCRIPT_DIR/output/$VENV_ARCHIVE" 2>/dev/null || true
    fi
fi

echo ""
echo "============================================================================"
echo "✅ BUILD COMPLETE"
echo "============================================================================"
echo "Output file:    $SCRIPT_DIR/output/$VENV_ARCHIVE"
if [ -f "$SCRIPT_DIR/output/$VENV_ARCHIVE" ]; then
    echo "Archive size:   $(du -h "$SCRIPT_DIR/output/$VENV_ARCHIVE" | cut -f1)"
fi
echo ""
echo "Python: 3.11 (Debian Bullseye - GLIBC 2.31)"
echo ""
echo "Next steps:"
echo "1. Upload to S3:"
echo "   aws s3 cp $SCRIPT_DIR/output/$VENV_ARCHIVE \\"
echo "     s3://${S3_BUCKET}/pipeline-files-v1/backlog-scale-dw/dependencies/"
echo ""
echo "2. Upload orchestrator:"
echo "   aws s3 cp 02_orchestrator_backlog_emr_submit.py \\"
echo "     s3://${S3_BUCKET}/pipeline-files-v1/backlog-scale-dw/"
echo ""
echo "3. Test from EMR cluster:"
echo "   ./RUN_ORCHESTRATOR.sh"
echo "============================================================================"
