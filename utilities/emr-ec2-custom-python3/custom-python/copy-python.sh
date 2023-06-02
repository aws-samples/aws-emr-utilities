#!/bin/bash
set -e

PYTHON_ARCHIVE=$1

# Copy and extract the Python archive into `/usr/local`
sudo aws s3 cp "${PYTHON_ARCHIVE}" /usr/local/
cd /usr/local/
sudo tar -xf "$(basename "${PYTHON_ARCHIVE}")" && rm "$(basename "${PYTHON_ARCHIVE}")"
