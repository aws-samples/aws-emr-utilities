#!/usr/bin/env bash

less << EOF

NAME

  emr - Amazon EMR Command Line Interface

DESCRIPTION

  The  EMR  Command  Line  Interface is a unified tool written in bash that
  provides utility commands to perform the following operations within an EMR
  cluster:

  - Collect logs from the cluster
  - Evaluate cluster configurations to reduce costs and improve performance
  - Manage cluster and application frameworks
  - Monitor cluster nodes and applications

SYNOPSIS

  usage: emr <command> <subcommand> [parameters]

  To see help text, you can run:

    emr help
    emr <command> help
    emr <command> <subcommand> help

  The synopsis for each command shows its parameters and usage.
  Optional parameters are shown within square brackets.

AVAILABLE COMMANDS

  - apps
  - benchmark
  - cluster
  - hbase
  - hdfs
  - logs
  - news
  - node
  - update

EOF
exit 0
