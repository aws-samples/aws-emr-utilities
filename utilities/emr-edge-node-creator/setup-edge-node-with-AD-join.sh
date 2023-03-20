#!/bin/bash

# AWS EMR edge node setup script with ec2 joining Active Directory (tested for Amazon Linux and Ubuntu)
# Pre-reqs:
#   1. the emr client dependencies generated with the step s3://tomzeng/BAs/copy-emr-client-deps.sh
#   2. the edge node has Python, JDK 8, AWS CLI, and Ruby installed
#   3. python on the edge node should be the same version as pyspark on EMR (2.7 or 3.4), conda can be used
#   4. this script is copied to edge node by running "aws s3 cp s3://aws-emr-bda-public-us-west-2/BAs/setup-emr-edge-node-s3.sh ."
#   5. make sure to replace AD related variables within the script for successful AD join for edge nodes

# Usage: bash setup-emr-edge-node-s3.sh --emr-client-deps <EMR client dependencies file in S3>
#
# Example: bash setup-emr-edge-node-s3.sh --emr-client-deps s3://aws-emr-bda-public-us-west-2/emr-client-deps/emr-client-deps-j-2WRQJIKRMUIMN.tar.gz

EMR_CLIENT_DEPS=""

# error message
error_msg ()
{
  echo 1>&2 "Error: $1"
}

# get input parameters
while [ $# -gt 0 ]; do
    case "$1" in
    --emr-client-deps)
      shift
      EMR_CLIENT_DEPS=$1
      ;;
    -*)
      # do not exit out, just note failure
      error_msg "unrecognized option: $1"
      ;;
    *)
      break;
      ;;
    esac
    shift
done

aws s3 cp $EMR_CLIENT_DEPS .
EMR_CLIENT_DEPS_LOCAL=$(python -c "print('$EMR_CLIENT_DEPS'.split('/')[-1])")


if grep -q Ubuntu /etc/issue; then
  apt-get install sudo -y || true # this might be needed inside Docker container
  sudo apt-get install sudo openjdk-8-jdk -y
  sudo update-java-alternatives -s java-1.8.0-openjdk-amd64
  export JAVA_HOME=/usr/lib/jvm/java-8-openjdk-amd64
  sudo sh -c "echo 'export JAVA_HOME=/usr/lib/jvm/java-8-openjdk-amd64' >> /etc/bashrc"
  echo "Note Hive Server2 service setup not yet support for Ubuntu edge node"
#elif grep -q Amazon /etc/issue; then
else # default to Amazon Linux
  yum install sudo -y || true # this might be needed inside Docker container
  sudo yum install krb5-workstation krb5-libs -y
  sudo yum install krb5-server -y
  sudo yum install jq -y
  sudo mkdir -p /etc/init
  sudo cp -pr etc/init/* /etc/init/
  sudo yum install sudo java-1.8.0-openjdk java-1.8.0-openjdk-devel -y
  sudo alternatives --set java java-1.8.0-openjdk.x86_64
  export JAVA_HOME=/usr/lib/jvm/java-1.8.0-openjdk
  sudo sh -c "echo 'export JAVA_HOME=/usr/lib/jvm/java-1.8.0-openjdk' >> /etc/bashrc"
  #sudo start hive-server2 || true
fi

tar xvfz $EMR_CLIENT_DEPS_LOCAL
cd emr-client-deps
sudo cp -pr usr/bin/* /usr/bin || true
sudo cp -pr usr/lib/* /usr/lib || true
sudo mkdir -p /usr/share/aws
sudo mkdir -p /var/log/kerberos
sudo cp -pr usr/share/aws/* /usr/share/aws/ || true
sudo mkdir -p /opt/aws
sudo cp -pr opt/aws/* /opt/aws/
sudo cp -pr emr /
sudo ln -s /emr/instance-controller/lib/info /var/lib/info
sudo ln -s /opt/aws/apitools/cfn-init/bin/cfn-signal /opt/aws/apitools/cfn-signal

sudo mkdir -p /etc/hadoop
sudo mkdir -p /etc/hive
sudo mkdir -p /etc/spark
sudo ln -s /usr/lib/hadoop/etc/hadoop /etc/hadoop/conf
sudo ln -s /usr/lib/hive/conf /etc/hive/
sudo ln -s /usr/lib/spark/conf /etc/spark/

sudo cp -pr etc/presto /etc || true
sudo cp -pr etc/hudi /etc || true
sudo cp -pr etc/tez /etc || true
sudo cp -pr etc/*.keytab /etc || true
sudo cp -pr etc/krb5.conf /etc || true

cd ..
rm -rf emr-client-deps
rm $EMR_CLIENT_DEPS_LOCAL

sudo ln -s /tmp /mnt/ || true

# set up user hadoop, additional users can be set up similarly
sudo groupadd sudo || true # may need this in docker container
sudo useradd -m hadoop -p hadoop
sudo mkdir -p /var/log/hive/user/hadoop
sudo chown hadoop:hadoop /var/log/hive/user/hadoop
sudo usermod -a -G sudo hadoop

sudo useradd -m hdfs -p hdfs
sudo mkdir -p /var/log/hadoop-hdfs
sudo chown hdfs:hadoop /var/log/hadoop-hdfs

sudo useradd -m hive -p hive
sudo mkdir -p /var/log/hive/user/hive
sudo chown hive:hive /var/log/hive/user/hive
sudo usermod -a -G sudo hive

sudo useradd -m hbase -p hbase
sudo mkdir -p /var/log/hbase/
sudo ln -s /usr/lib/hbase/logs /var/log/hbase || true
sudo chmod a+rwx -R /var/log/hbase/logs/ || true

sudo mkdir -p /mnt/s3
sudo chmod a+rwx /mnt/s3

hostip=$(cat /etc/spark/conf/spark-env.sh | grep SPARK_PUBLIC_DNS | sed -e 's/ip-//' -e 's/-/./g' -e 's/.ec2.internal//g' -e 's/export SPARK_PUBLIC_DNS=//g')

hostnm=$(cat /etc/spark/conf/spark-env.sh | grep SPARK_PUBLIC_DNS | cut -f2 -d'=' -)

echo $hostip $hostnm >> /etc/hosts

###################### CHANGE THE FOLLOWING VARIABLES ###########################

####AD####
# Active Directory domain name. Set this to your AD domain name.
AD_DOMAIN='emr.net'
AD_KRB5_REALM='EMR.NET'
# FQDN of the AD domain controller. //hostname.ADdomainname
AD_FQDN='EC2AMAZ-K9MA6F7.emr.net'

####AD BIND USER####
# Set this to your ADMIN user
BIND_USER='username'
#Set your ADMIN password
BIND_PASSWORD='password'
#echo $BIND_PASSWORD

# Set this to your ad/ldap search base
LDAP_SEARCH_BASE='dc=emr,dc=net'

####EMR####
# EMR VPC Domain Name
EMR_DOMAIN='ec2.internal'
# REALM of KDC
EMR_KRB5_REALM='EC2.INTERNAL'
# FQDN of the Cluster Dedicated KDC. For External KDC please comment the next two lines and uncooment the third line to set the external KDC FQDN
CLUSTER_ID=`cat /emr/instance-controller/lib/info/job-flow.json | jq -r ".jobFlowId"`
ADMIN_SERVER=`aws emr list-instances --cluster-id $CLUSTER_ID --region us-east-1 --instance-group-types MASTER --query "Instances[0].PrivateDnsName" --output text`
#ADMIN_SERVER=


#CrossRealmTrustPassword-Set this to the trust password that you have created
## (Ref: https://docs.aws.amazon.com/emr/latest/ManagementGuide/emr-kerberos-cross-realm.html#emr-kerberos-ad-configure-trust)
TRUST_PASS='YourStrongPassword'

#################################################################################

LOWER_CASE_AD_FQDN=$(echo ${AD_FQDN} | tr '[A-Z]' '[a-z]')

set -x
IDP="ad"

# Wait for EMR provisioning to complete
while [[ $(sed '/localInstance {/{:1; /}/!{N; b1}; /nodeProvision/p}; d' /emr/instance-controller/lib/info/job-flow-state.txt | sed '/nodeProvisionCheckinRecord {/{:1; /}/!{N; b1}; /status/p}; d' | awk '/SUCCESSFUL/' | xargs) != "status: SUCCESSFUL" ]];
do
  sleep 5
done

## create keytab for sssd 
printf "%b" "addent -password -p $BIND_USER@$AD_KRB5_REALM -k 1 -e aes256-cts-hmac-sha1-96\n$BIND_PASSWORD\nwrite_kt /etc/krb5.keytab" | ktutil

##Add cross realm trust principal
printf "%b" "addprinc -e aes256-cts-hmac-sha1-96 krbtgt/$EMR_KRB5_REALM@$AD_KRB5_REALM\n$TRUST_PASS\n$TRUST_PASS" | kadmin.local

yum list installed | grep sssd
sudo yum -y install sssd
sudo authconfig --enablemkhomedir --enablesssd --enablesssdauth  --update

echo "Set sssd.conf"

sudo touch /etc/sssd/sssd.conf
sudo chown root:root /etc/sssd/sssd.conf
sudo chmod 700 /etc/sssd/sssd.conf

sudo cat > /etc/sssd/sssd.conf << ENDOFFILE
[sssd]
config_file_version = 2
services = nss, pam, sudo, ssh
domains = $AD_DOMAIN
debug_level = 9
[nss]
filter_groups = root
filter_users = root
reconnection_retries = 3
[pam]
[domain/$AD_DOMAIN]
debug_level = 9
override_homedir = /home/%u@%d
override_shell = /bin/bash
enumerate = False
case_sensitive = false
cache_credentials = True
min_id = 1000
ldap_id_mapping = True
use_fully_qualified_names = False
id_provider = ldap
auth_provider = krb5
chpass_provider = none
access_provider = permit
ldap_tls_reqcert = allow
ldap_schema = ad
ldap_sasl_mech = GSSAPI
ldap_sasl_authid = $BIND_USER@$AD_KRB5_REALM
krb5_fast_principal = $BIND_USER@$AD_KRB5_REALM
krb5_use_fast = try
krb5_canonicalize = false
ldap_access_order = expire
ldap_account_expire_policy = ad
ldap_force_upper_case_realm = true
ldap_pwd_policy = none
krb5_realm = $AD_KRB5_REALM
ldap_uri = ldap://$AD_FQDN
ldap_search_base = $LDAP_SEARCH_BASE
ldap_referrals = false
ENDOFFILE

sudo chmod 600 /etc/sssd/sssd.conf

echo "Set krb5.conf"

sudo chmod 744 /etc/krb5.conf

sudo cat /dev/null > /etc/krb5.conf

sudo cat > /etc/krb5.conf << ENDOFFILE
[libdefaults]
    default_realm = $EMR_KRB5_REALM
    dns_lookup_realm = false
    dns_lookup_kdc = false
    rdns = false
    ticket_lifetime = 24h
    forwardable = true
    udp_preference_limit = 1
    default_tkt_enctypes = aes256-cts-hmac-sha1-96 aes128-cts-hmac-sha1-96 des3-cbc-sha1
    default_tgs_enctypes = aes256-cts-hmac-sha1-96 aes128-cts-hmac-sha1-96 des3-cbc-sha1
    permitted_enctypes = aes256-cts-hmac-sha1-96 aes128-cts-hmac-sha1-96 des3-cbc-sha1
[realms]
    $EMR_KRB5_REALM = {
        kdc = $ADMIN_SERVER
        admin_server = $ADMIN_SERVER
        default_domain = $EMR_DOMAIN
    }
    $AD_KRB5_REALM = {
        kdc = $AD_FQDN
        admin_server = $AD_FQDN
        default_domain = $AD_DOMAIN
    }
[domain_realm]
     $LOWER_CASE_AD_FQDN = $AD_KRB5_REALM
     forestdnszones.$AD_DOMAIN = $AD_KRB5_REALM
     domaindnszones.$AD_DOMAIN = $AD_KRB5_REALM
    .$AD_DOMAIN = $EMR_KRB5_REALM
     $AD_DOMAIN = $AD_KRB5_REALM
[logging]
    kdc = FILE:/var/log/kerberos/krb5kdc.log
    admin_server = FILE:/var/log/kerberos/kadmin.log
    default = FILE:/var/log/kerberos/krb5lib.log
ENDOFFILE

sudo chmod 644 /etc/krb5.conf

sudo systemctl restart sssd

sudo systemctl status sssd

echo "Enable password authentication"

sudo sed -i -e 's/PasswordAuthentication no/PasswordAuthentication yes/g' /etc/ssh/sshd_config
sudo sed -i -e 's/GSSAPIAuthentication yes/GSSAPIAuthentication no/g' /etc/ssh/sshd_config

sudo systemctl restart sshd

echo "Finished copy EMR edge node client dependencies and joined Active Directory Successfully"
exec "$@"
