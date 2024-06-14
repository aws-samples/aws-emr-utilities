#!/bin/bash
set -xe
# wait for database start up first
sleep 15

./setup.sh
ranger-admin start
tail -f logfile