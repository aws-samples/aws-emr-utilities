#!/usr/bin/env bash

_emr() {
	local cur prev
	COMPREPLY=()
	cur="${COMP_WORDS[COMP_CWORD]}"
	prev="${COMP_WORDS[COMP_CWORD-1]}"
	prev_two="${COMP_WORDS[COMP_CWORD-2]}"

	# commands
	base="apps benchmark cluster hbase hdfs help logs news node update"

	# subcommands
	apps="help kill list logs monitor"
	benchmark="help hbase spark teragen terasort"
	cluster="help details evaluate execute metrics nodes reboot scale share update"
	hbase="help check disable enable ns regions restart shell status"
	hdfs="help balancer configs fs-image ls namenode report safe-mode secondary status under-replica usage"
	logs="help emr-ic emr-is emr-step hadoop-hdfs hadoop-yarn hive hbase hue livy phoenix trino zeppelin"
	node="help configs disk gc heap memory net thread"

	# HDFS path listing
	if [[ ${prev_two} == "hdfs" && ${prev} == "ls" ]] ; then

		directory="$cur"
		if [[ "$directory" == "" ]] ; then
			directory="/"
		else
			directory=$(echo "$directory" | sed 's/\(.*\)\/.*/\1\//')
		fi

		compopt +o default
		COMPREPLY=($(compgen -W "$(curl "http://localhost:14000/webhdfs/v1$directory?op=LISTSTATUS&user.name=hadoop" 2>/dev/null| jq -r --arg directory "$directory" '.FileStatuses.FileStatus[] | select(.type=="DIRECTORY") | "\($directory)\(.pathSuffix)\/"')" -- $cur))
		[[ $COMPREPLY == */ ]] && compopt -o nospace
		return 0
	fi

	if [[ ${prev} == "share" ]] ; then
		compopt -o default
		COMPREPLY=()
		return 0
	fi
	if [[ ${prev} != "emr" ]] ; then
		COMPREPLY=( $(compgen -W "${!prev}" -- ${cur}) )
		return 0
	fi

	COMPREPLY=( $(compgen -W "${base}" -- ${cur}) )
	return 0
}

complete -o bashdefault -o default -F _emr emr
