#!/bin/bash
#set -x

VER=1.1

HERE=$(cd "$(dirname "$BASH_SOURCE")"; cd -P "$(dirname "$(readlink "$BASH_SOURCE" || echo .)")"; pwd)

cd $HERE/..

if [ ! -d "dist" ]; then
  mkdir dist
else
  rm dist/*
fi

zip dist/emr-custom-autoscaler-$VER.zip emr-custom-autoscaler.py

cd -
