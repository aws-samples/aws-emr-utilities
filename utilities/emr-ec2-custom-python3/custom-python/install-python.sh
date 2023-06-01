#!/bin/bash
set -e

PYTHON_VERSION=3.11.3

# Replace old OpenSSL and add build utilities
sudo yum -y remove openssl-devel-1.0.2k-24.amzn2.0.6.x86_64 && \
sudo yum -y install gcc openssl11-devel bzip2-devel libffi-devel tar gzip wget make expat-devel

# Install Python
wget https://www.python.org/ftp/python/${PYTHON_VERSION}/Python-${PYTHON_VERSION}.tgz
tar xzvf Python-${PYTHON_VERSION}.tgz
cd Python-${PYTHON_VERSION}

# We aim for similar `CONFIG_ARGS` that AL2 Python is built with
./configure --enable-loadable-sqlite-extensions --with-dtrace --with-lto --enable-optimizations --with-system-expat \
    --prefix=/usr/local/python${PYTHON_VERSION}

# Install into /usr/local/python3.11.x
# Note that "make install" links /usr/local/python3.11.3/bin/python3 while "altinstall" does not
sudo make altinstall

# Good practice to upgrade pip
sudo /usr/local/python${PYTHON_VERSION}/bin/python3.11 -m pip install --upgrade pip

# You could also install additional job-specific libraries in the bootstrap
# /usr/local/python${PYTHON_VERSION}/bin/python3.11 -m pip install pyarrow==12.0.0