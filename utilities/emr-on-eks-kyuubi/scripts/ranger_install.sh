#!/bin/bash
#===============================================================================
#!# script: ranger_install.sh
#!# authors: ripani
#!# version: v0.1
#!#
#!# Install Apache Ranger Admin Server.
#!# Please note this installation is for testing purposes only. This installation
#!# does not configure HTTPS for ranger and does not setup any audit store.
#===============================================================================
DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" >/dev/null 2>&1 && pwd )"

# Force the script to run as root
if [ $(id -u) != "0" ]
then
    sudo "$0" "$@"
    exit $?
fi

# required for forked processes
source ~/.bashrc

#===============================================================================
# Input
#===============================================================================
LDAP_PROT="LDAP"
LDAP_USER="admin"
LDAP_PASSWD="Password123"
LDAP_HOST="localhost"
LDAP_DN="dc=hadoop,dc=local"
RANGER_VERSION="2.2.0"

#===============================================================================
# Requirements
#===============================================================================
wget -O epel.rpm –nv https://dl.fedoraproject.org/pub/epel/epel-release-latest-7.noarch.rpm

yum install -y jq
yum install -y sssd realmd krb5-workstation samba-common-tools adcli oddjob oddjob-mkhomedir
yum install -y openldap openldap-clients openldap-servers mariadb java-1.8.0-openjdk
yum install -y ./epel.rpm && yum -y install xmlstarlet && rm ./epel.rpm

#===============================================================================
# LDAP configurations
#===============================================================================
REGION=`curl http://169.254.169.254/latest/dynamic/instance-identity/document | grep region | awk -F\" '{print $4}'`

# ldap
LDAP_SEARCH_USER_BASE="ou=People,$LDAP_DN"
LDAP_SEARCH_GROUP_BASE="ou=Group,$LDAP_DN"
LDAP_BIND_USERNAME="CN=$LDAP_USER,$LDAP_DN"
LDAP_BIND_USER_PASSWORD="$LDAP_PASSWD"

PROTOCOL="${LDAP_PROT,,}"
PROTOCOLS=("ldap" "ldaps")
if [[ " ${PROTOCOLS[*]} " =~ " ${PROTOCOL} " ]]; then
    [[ "$PROTOCOL" = "ldaps" ]] && LDAP_URL="ldaps://${LDAP_HOST}:636" || LDAP_URL="ldap://${LDAP_HOST}:389"
else
    "error: select a valid protocol: LDAP, LDAPS"
    exit -1
fi

#===============================================================================
# Ranger configurations
#===============================================================================
# ranger database
RANGER_DB_HOST="localhost"
RANGER_DB_ADMIN="root"
RANGER_DB_ADMIN_PASSWORD="Password123"
RANGER_DB_SCHEMA="ranger"
RANGER_DB_USER="rangeradmin"
RANGER_DB_USER_PASSWORD="Password123"

# ranger
RANGER_BUILD_PATH="$DIR/ranger_build"
RANGER_FQDN=`hostname -f`
RANGER_PATH_CONF="/etc/ranger/admin/conf"
RANGER_PATH_INSTALL="/usr/lib/ranger"
RANGER_LOG_DIR="/var/log/ranger"

# mysql
MYSQL_VERSION="8.0.26"
MYSQL_JAR="mysql-connector-java-$MYSQL_VERSION.jar"
MYSQL_JAR_URL="https://repo1.maven.org/maven2/mysql/mysql-connector-java/$MYSQL_VERSION/mysql-connector-java-$MYSQL_VERSION.jar"

#===============================================================================
# Ranger build from git
#===============================================================================
# Install requirements
yum install -y java-1.8.0-openjdk-devel git python3 gcc
pip3 install requests

# clone latest or specific branch version
mkdir -p $RANGER_BUILD_PATH && cd $RANGER_BUILD_PATH
git clone --depth 1 -b "release-ranger-$RANGER_VERSION" https://github.com/apache/ranger.git
cd ranger
mvn compile package install -Dmaven.test.skip=true

#===============================================================================
# Apache Ranger Database
#===============================================================================
mysqladmin -u $RANGER_DB_ADMIN password $RANGER_DB_ADMIN_PASSWORD

mysql -h $RANGER_DB_HOST -u $RANGER_DB_ADMIN -p$RANGER_DB_ADMIN_PASSWORD -e "CREATE DATABASE IF NOT EXISTS $RANGER_DB_SCHEMA;"
mysql -h $RANGER_DB_HOST -u $RANGER_DB_ADMIN -p$RANGER_DB_ADMIN_PASSWORD -e "CREATE USER '$RANGER_DB_USER'@'localhost' IDENTIFIED BY '$RANGER_DB_USER_PASSWORD';"
mysql -h $RANGER_DB_HOST -u $RANGER_DB_ADMIN -p$RANGER_DB_ADMIN_PASSWORD -e "GRANT ALL PRIVILEGES ON \`%\`.* TO '$RANGER_DB_USER'@'localhost';"
mysql -h $RANGER_DB_HOST -u $RANGER_DB_ADMIN -p$RANGER_DB_ADMIN_PASSWORD -e "GRANT ALL PRIVILEGES ON \`%\`.* TO '$RANGER_DB_USER'@'localhost' WITH GRANT OPTION;"
mysql -h $RANGER_DB_HOST -u $RANGER_DB_ADMIN -p$RANGER_DB_ADMIN_PASSWORD -e "CREATE USER '$RANGER_DB_USER'@'%' IDENTIFIED BY '$RANGER_DB_USER_PASSWORD';"
mysql -h $RANGER_DB_HOST -u $RANGER_DB_ADMIN -p$RANGER_DB_ADMIN_PASSWORD -e "GRANT ALL PRIVILEGES ON \`%\`.* TO '$RANGER_DB_USER'@'%';"
mysql -h $RANGER_DB_HOST -u $RANGER_DB_ADMIN -p$RANGER_DB_ADMIN_PASSWORD -e "GRANT ALL PRIVILEGES ON \`%\`.* TO '$RANGER_DB_USER'@'%' WITH GRANT OPTION;"
mysql -h $RANGER_DB_HOST -u $RANGER_DB_ADMIN -p$RANGER_DB_ADMIN_PASSWORD -e "FLUSH PRIVILEGES;"

#===============================================================================
# Apache Ranger Admin
#===============================================================================
rm -rf $RANGER_PATH_INSTALL && mkdir -p $RANGER_PATH_INSTALL && cd $RANGER_PATH_INSTALL && mkdir -p $RANGER_LOG_DIR
cp $RANGER_BUILD_PATH/ranger/target/ranger-${RANGER_VERSION}-admin.tar.gz ./
tar -xvf $RANGER_PATH_INSTALL/ranger-${RANGER_VERSION}-admin.tar.gz && rm -rf $RANGER_PATH_INSTALL/ranger-${RANGER_VERSION}-admin.tar.gz
wget $MYSQL_JAR_URL

cd ranger-${RANGER_VERSION}-admin

# configure db
sed -i "s|SQL_CONNECTOR_JAR=.*|SQL_CONNECTOR_JAR=$RANGER_PATH_INSTALL/$MYSQL_JAR|g" install.properties
sed -i "s|db_host=.*|db_host=$RANGER_DB_HOST:3306|g" install.properties
sed -i "s|db_root_user=.*|db_root_user=$RANGER_DB_ADMIN|g" install.properties
sed -i "s|db_root_password=.*|db_root_password=$RANGER_DB_ADMIN_PASSWORD|g" install.properties
sed -i "s|db_name=.*|db_name=$RANGER_DB_SCHEMA|g" install.properties
sed -i "s|db_user=.*|db_user=$RANGER_DB_USER|g" install.properties
sed -i "s|db_password=.*|db_password=$RANGER_DB_USER_PASSWORD|g" install.properties
sed -i "s|policymgr_external_url=.*|policymgr_external_url=http://$RANGER_FQDN:6080|g" install.properties

# configure ldap
sed -i "s|authentication_method=.*|authentication_method=LDAP|g" install.properties
sed -i "s|xa_ldap_url=.*|xa_ldap_url=$LDAP_URL|g" install.properties
sed -i "s|xa_ldap_userDNpattern=.*|xa_ldap_userDNpattern=uid={0},cn=users,$LDAP_DN|g" install.properties
sed -i "s|xa_ldap_groupSearchBase=.*|xa_ldap_groupSearchBase=$LDAP_DN|g" install.properties
sed -i "s|xa_ldap_groupSearchFilter=.*|xa_ldap_groupSearchFilter=objectclass=group|g" install.properties
sed -i "s|xa_ldap_groupRoleAttribute=.*|xa_ldap_groupRoleAttribute=cn|g" install.properties
sed -i "s|xa_ldap_base_dn=.*|xa_ldap_base_dn=$LDAP_DN|g" install.properties
sed -i "s|xa_ldap_bind_dn=.*|xa_ldap_bind_dn=$LDAP_BIND_USERNAME|g" install.properties
sed -i "s|xa_ldap_bind_password=.*|xa_ldap_bind_password=$LDAP_BIND_USER_PASSWORD|g" install.properties
sed -i "s|xa_ldap_referral=.*|xa_ldap_referral=ignore|g" install.properties
sed -i "s|xa_ldap_userSearchFilter=.*|xa_ldap_userSearchFilter=(sAMAccountName={0})|g" install.properties
sed -i "s|RANGER_ADMIN_LOG_DIR=.*|RANGER_ADMIN_LOG_DIR=$RANGER_LOG_DIR|g" install.properties
sudo sed -i "s|audit_solr_urls=.*|audit_solr_urls=http://localhost:8983/solr/ranger_audits|g" install.properties

chmod +x setup.sh && ./setup.sh

#===============================================================================
# Apache Ranger Usersync
#===============================================================================
cd $RANGER_PATH_INSTALL
cp $RANGER_BUILD_PATH/ranger/target/ranger-${RANGER_VERSION}-usersync.tar.gz ./
tar -xvf $RANGER_PATH_INSTALL/ranger-${RANGER_VERSION}-usersync.tar.gz && rm -rf $RANGER_PATH_INSTALL/ranger-${RANGER_VERSION}-usersync.tar.gz
cd ranger-${RANGER_VERSION}-usersync

# configuration
sed -i "s|POLICY_MGR_URL =.*|POLICY_MGR_URL=http://$RANGER_FQDN:6080|g" install.properties
# ldap
sed -i "s|SYNC_SOURCE =.*|SYNC_SOURCE =ldap|g" install.properties
sed -i "s|SYNC_LDAP_URL =.*|SYNC_LDAP_URL =$LDAP_URL|g" install.properties
sed -i "s|SYNC_LDAP_BIND_DN =.*|SYNC_LDAP_BIND_DN =$LDAP_BIND_USERNAME|g" install.properties
sed -i "s|SYNC_LDAP_BIND_PASSWORD =.*|SYNC_LDAP_BIND_PASSWORD =$LDAP_BIND_USER_PASSWORD|g" install.properties
sed -i "s|SYNC_LDAP_SEARCH_BASE =.*|SYNC_LDAP_SEARCH_BASE =$LDAP_DN|g" install.properties
sed -i "s|SYNC_LDAP_USER_SEARCH_BASE =.*|SYNC_LDAP_USER_SEARCH_BASE =$LDAP_SEARCH_USER_BASE|g" install.properties
sed -i "s|SYNC_LDAP_USER_SEARCH_FILTER =.*|SYNC_LDAP_USER_SEARCH_FILTER =uid=*|g" install.properties
sed -i "s|SYNC_LDAP_USER_NAME_ATTRIBUTE =.*|SYNC_LDAP_USER_NAME_ATTRIBUTE =uid|g" install.properties
sed -i "s|SYNC_GROUP_SEARCH_BASE=.*|SYNC_GROUP_SEARCH_BASE=$LDAP_SEARCH_GROUP_BASE|g" install.properties
sed -i "s|SYNC_GROUP_OBJECT_CLASS=.*|SYNC_GROUP_OBJECT_CLASS=group|g" install.properties
sed -i "s|SYNC_INTERVAL =.*|SYNC_INTERVAL =360|g" install.properties

./setup.sh

xmlstarlet ed -L -u "/configuration/property[name='ranger.usersync.enabled']/value" -v true conf/ranger-ugsync-site.xml

#===============================================================================
# Fix Permissions
#===============================================================================
chown ranger:ranger $RANGER_LOG_DIR
chown ranger:ranger $RANGER_PATH_INSTALL -R

#===============================================================================
# Launch
#===============================================================================
# Ranger Admin
/etc/init.d/ranger-admin start

# wait ranger admin to start
#while ! echo exit | nc localhost 6080; do echo "waiting for ranger to come up..."; sleep 10; done
sleep 60

# Ranger UserSync
/etc/init.d/ranger-usersync start

cd -
rm -rf $RANGER_BUILD_PATH
