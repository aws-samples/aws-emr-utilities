#!/bin/sh

# Copyright 2020 Crown Copyright
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.


if [ -z "${HADOOP_CONF_DIR}" ]; then
	HADOOP_CONF_DIR=${HADOOP_HOME}/etc/hadoop
fi

if [ "$1" = "hdfs" ] && [ "$2" = "namenode" ]; then
	NAME_DIRS=$(xmlstarlet sel -t -v "/configuration/property[name='dfs.namenode.name.dir']/value" ${HADOOP_CONF_DIR}/hdfs-site.xml)
	FIRST_NAME_DIR=${NAME_DIRS%%,*}

	if [ "${FIRST_NAME_DIR}" != "" ] && [ ! -d "${FIRST_NAME_DIR}" ]; then
		echo "Formatting HDFS NameNode volumes: ${NAME_DIRS}"
		hdfs namenode -format -force
		# sleep 3000
	fi
fi

exec /usr/bin/dumb-init -- "$@"
