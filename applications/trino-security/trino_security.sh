#!/bin/bash
#===============================================================================
#!# script: trino_security.sh
#!# authors: ripani, sandonas
#!# version: v0.2
#!#
#!# Configure an EMR cluster with Kerberos authentication, and (if enabled)
#!# Trino File Base Access Control using Secret Manager as rule repository.
#===============================================================================
#?#
#?# usage: ./trino_security.sh
#?#
#===============================================================================

#set -x

# Print the usage helper using the header as source
function usage() {
    [ "$*" ] && echo "$0: $*"
    sed -n '/^#?#/,/^$/s/^#?# \{0,1\}//p' "$0"
    exit -1
}

[[ "$1" = "-h" ]] && usage && exit 0

# Force the script to run as root
if [ $(id -u) != "0" ]; then
    sudo "$0" "$@"
    exit $?
fi

#===============================================================================
# Input
#===============================================================================
DEBUG_ENABLED="true"
FBAC_ENABLED="false"
KDC_ADMIN_PASSWORD="YOUR_KDC_PASSWORD"
TRINO_FBAC_RULES_SECRET="emr/trino_fbac_rules.json"

#===============================================================================
# Configurations
#===============================================================================
INSTANCE_INFO="/emr/instance-controller/lib/info/instance.json"
INSTANCE_EXTRA_INFO="/emr/instance-controller/lib/info/extraInstanceData.json"

CURRENT_HOSTNAME=$(hostname -f)
IS_MASTER=$(cat $INSTANCE_INFO | jq '.isMaster')
REGION=$(cat $INSTANCE_EXTRA_INFO | jq -r .region)

# Retrieve EMR Security Configuration
EMR_SEC_CONF=`cat $INSTANCE_EXTRA_INFO | jq -r .securityConfiguration`
KERBEROS_ENABLED=`echo $EMR_SEC_CONF | jq -r '.AuthenticationConfiguration | has("KerberosConfiguration")'`
ENCRYPTION_ENABLED=`echo $EMR_SEC_CONF | jq -r '.EncryptionConfiguration.InTransitEncryptionConfiguration.TLSCertificateConfiguration.CertificateProviderType | contains("PEM")'`

## Kerberos requirements
KDC_REALM=$(cat /etc/krb5.conf | grep "default_realm" | awk -F " " '{print $3}')

## Trino configurations
TRINO_CONF="/etc/trino/conf/config.properties"
TRINO_CONF_JVM="/etc/trino/conf/jvm.config"
TRINO_CONF_ENV="/etc/trino/conf/trino-env.sh"
TRINO_CONF_FBAC="/usr/lib/trino/etc/access-control.properties"
TRINO_CONF_FBAC_RULES="/usr/lib/trino/etc/rules.json"
TRINO_CONF_USER_MAPPING="/usr/lib/trino/etc/user-mapping.json"
TRINO_CONF_HIVE_CATALOG="/etc/trino/conf/catalog/hive.properties"

TRINO_KEYTAB="/etc/trino.keytab"

## TLS requirements
TRUST_STORE="/usr/share/aws/emr/security/conf/truststore.jks"
TRUST_PASSWORD=`grep "truststore.key" $TRINO_CONF | cut -d"=" -f2 |  sed 's/^ *//g'`
TRINO_COORDINATOR=`grep "discovery.uri" $TRINO_CONF | cut -d"=" -f2 |  sed 's/^ *//g'`

if [ "$KERBEROS_ENABLED" != true ] ; then
    echo "error: To enable Trino with Kerberos authentication is required to launch the EMR cluster with a Kerberos authentication enabled."
    exit -1
fi

if [ "$ENCRYPTION_ENABLED" != true ] ; then
    echo "error: To enable Trino with Kerberos authentication you must launch the cluster with In Transit encryption enabled."
    exit -1
fi

#===============================================================================
# Kerberos
#===============================================================================
## Create kerberos principal and keytab for the host
kadmin -p kadmin/admin -w ${KDC_ADMIN_PASSWORD} -q "addprinc -randkey trino/${CURRENT_HOSTNAME}@${KDC_REALM}"
kadmin -p kadmin/admin -w ${KDC_ADMIN_PASSWORD} -q "ktadd -k $TRINO_KEYTAB trino/${CURRENT_HOSTNAME}@${KDC_REALM}"

chown trino:trino $TRINO_KEYTAB
chmod 400 $TRINO_KEYTAB

cat <<- EOF >> $TRINO_CONF
http-server.authentication.type = KERBEROS
http-server.authentication.krb5.service-name = trino
http-server.authentication.krb5.keytab = $TRINO_KEYTAB
http.authentication.krb5.config = /etc/krb5.conf
http-server.authentication.krb5.user-mapping.file=etc/user-mapping.json
EOF

# If Hive is installed and the trino-hive-connector is not configured to
# use the Glue Data Catalog, add kerberos configurations for hive:
if [ -f "$TRINO_CONF_HIVE_CATALOG" ]; then
    if ! grep "hive.metastore = glue" $TRINO_CONF_HIVE_CATALOG; then
        cat <<- EOF >> $TRINO_CONF_HIVE_CATALOG
		hive.metastore.authentication.type = KERBEROS
		hive.metastore.client.principal = trino/_HOST@${KDC_REALM}
		hive.metastore.client.keytab  =  $TRINO_KEYTAB
		hive.metastore.service.principal = hive/_HOST@${KDC_REALM}
		hive.hdfs.authentication.type = KERBEROS
		hive.hdfs.trino.principal = trino/_HOST@${KDC_REALM}
		hive.hdfs.trino.keytab = $TRINO_KEYTAB
		hive.security = allow-all
		hive.hdfs.authentication.type = KERBEROS
		hive.hdfs.trino.principal = trino/_HOST@${KDC_REALM}
		hive.hdfs.trino.keytab = $TRINO_KEYTAB
		EOF
    fi
fi

cat <<- 'EOF' >> $TRINO_CONF_USER_MAPPING
{
  "rules": [
	{
	  "pattern": "(.*)/(.*)(@.*)"
	},
	{
	  "pattern": "(.*)(@.*)"
	}
  ]
}
EOF

## Configure env variables for the trino-cli
cat <<- EOF > $TRINO_CONF_ENV
JAVA11_HOME=\$(ls -d /usr/lib/jvm/java-11-amazon-corretto.*)
export JAVA_HOME=\$JAVA11_HOME
export PATH=\$JAVA_HOME/bin:\$PATH

TRUST_STORE=$TRUST_STORE
TRUST_PASSWORD=$TRUST_PASSWORD
TRINO_COORDINATOR=$TRINO_COORDINATOR

USER_KRB_PRINCIPAL=\$(klist | grep Default | cut -d':' -f2 | tr -d ' ')
USER_TICKET_CACHE=\$(klist | grep cache | cut -d ':' -f3)

export EXTRA_ARGS="--server \${TRINO_COORDINATOR} \
--truststore-path \${TRUST_STORE} \
--truststore-password \${TRUST_PASSWORD} \
--krb5-config-path /etc/krb5.conf \
--krb5-remote-service-name trino
--krb5-principal \${USER_KRB_PRINCIPAL} \
--krb5-credential-cache-path \${USER_TICKET_CACHE}"
EOF

chmod 744 $TRUST_STORE

## Enable debug
if [ "$DEBUG_ENABLED" = true ] ; then
    cat <<- 'EOF' >> $TRINO_CONF_JVM
	-Djavax.net.debug=ssl
	-Dlog.enable-console=true
	-Dsun.security.krb5.debug=true
	EOF
fi

#===============================================================================
# Trino FBAC
#===============================================================================
if [[ "$FBAC_ENABLED" == "true" && "$IS_MASTER" == "true" ]] ; then

    # Retrive rules from Secret Manager
    aws secretsmanager get-secret-value --secret-id $TRINO_FBAC_RULES_SECRET --region $REGION --query 'SecretString' --output text > $TRINO_CONF_FBAC_RULES

    cat <<- EOF > $TRINO_CONF_FBAC
	access-control.name=file
	security.config-file=etc/rules.json
	security.refresh-period=1s
	EOF

    # Rule refresher crontab - Default frequncy 5 minutes
    # Use mv command to prevent issues while refreshing policies
    crontab -l | { cat; echo "*/5 * * * * aws secretsmanager get-secret-value --secret-id $TRINO_FBAC_RULES_SECRET --region $REGION --query 'SecretString' --output text > /tmp/trino_rules.json && mv /tmp/trino_rules.json $TRINO_CONF_FBAC_RULES"; } | crontab -

fi

# Restart Trino server
systemctl stop trino-server
systemctl start trino-server
