#!/bin/bash
set -xe

cp /usersync/install.properties /opt/ranger_usersync/install.properties
chmod +x /opt/ranger_usersync/install.properties
chown -R ranger /opt/ranger_usersync/

./setup.sh
# wait for ranger-admin start up first
sleep 60

RANGER_USERSYNC_CONF=$USERSYNC_HOME/conf

cp $RANGER_USERSYNC_CONF/ranger-ugsync-site.xml /tmp/ranger-ugsync-site.xml
xmlstarlet ed  -u "//property[name='ranger.usersync.enabled']/value"  -v true /tmp/ranger-ugsync-site.xml > $RANGER_USERSYNC_CONF/ranger-ugsync-site.xml

cp $RANGER_USERSYNC_CONF/ranger-ugsync-site.xml /tmp/ranger-ugsync-site.xml
xmlstarlet ed  -u "//property[name='ranger.usersync.group.searchenabled']/value"  -v true /tmp/ranger-ugsync-site.xml > $RANGER_USERSYNC_CONF/ranger-ugsync-site.xml

./ranger-usersync-services.sh start
tail -f $USERSYNC_HOME/logs/usersync-ranger-*.log