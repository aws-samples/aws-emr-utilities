#!/bin/bash
#set -x

HERE=$(cd "$(dirname "$BASH_SOURCE")"; cd -P "$(dirname "$(readlink "$BASH_SOURCE" || echo .)")"; pwd)

cd $HERE/..

if [ ! -d "dist" ]; then
  mkdir dist
else
  rm dist/*
fi

zip dist/emr-custom-autoscaler.zip emr-custom-autoscaler.py

cd -
