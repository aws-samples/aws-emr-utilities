#!/usr/bin/env bash

######################
#Script Name       :emr_ad_auth.sh
#Description       :Bootstrap Action which installs and configures SSSD in EMR establishing cross-realm trust with AD 
#Date              :Jun 13, 2022
#Author            :Suthan Phillips
#Email             :suthan@amazon.com
#Version           :1
######################

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
CLUSTER_ID=`cat /mnt/var/lib/info/job-flow.json | jq -r ".jobFlowId"`
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
