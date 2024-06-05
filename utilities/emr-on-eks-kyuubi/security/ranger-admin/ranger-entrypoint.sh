#!/bin/sh

# Wait for MySQL becomes available
sleep 30

cd $RANGER_HOME
./setup.sh
ranger-admin start

# Keep the container running
tail -f /dev/null
