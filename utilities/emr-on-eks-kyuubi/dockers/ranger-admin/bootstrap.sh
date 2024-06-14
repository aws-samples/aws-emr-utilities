#!/bin/bash
set -xe
# wait for postgresDB start up first
sleep 15

./setup.sh
ranger-admin start
tail -f logfile