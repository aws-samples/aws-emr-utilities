#!/bin/bash
#===============================================================================
#!# ldap-install.sh - Install a ldap server on an Amazon Linux 2 instance.
#!# The script also craetes two users named (kyuubi and analyst)
#!#
#!#  version         1.0
#!#  author          ripani
#!#
#===============================================================================
#?#
#?# usage: ./ldap_install.sh <ADMIN_PASSWD> <USER_LIST> <USER_PASSWD> <BASE_DIRECTORY>
#?#        ./ldap_install.sh "Password123" "kyuubi,analyst" "Password123" "dc=hadoop,dc=local"
#?#
#?#   ADMIN_PASSWD            Admin Password for the admin user (admin)
#?#   USER_LIST               List of users(comma separated) to be created in the ldap
#?#   USER_PASSWD             Default password for LDAP uysers
#?#   BASE_DIRECTORY          Base directory of the LDAP. Example: dc=hadoop,dc=local
#?#
#===============================================================================

DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" >/dev/null 2>&1 && pwd )"

# Force the script to run as root
if [ $(id -u) != "0" ]
then
    sudo "$0" "$@"
    exit $?
fi

function usage() {
  [ "$*" ] && echo "$0: $*"
  sed -n '/^#?#/,/^$/s/^#?# \{0,1\}//p' "$0"
  exit -1
}

[[ $# -ne 4 ]] && echo "error: wrong parameters" && usage

#===============================================================================
# Configurations
#===============================================================================
ADMIN_PASSWD="$1"
USER_LIST="$2"
USER_PASSWD="$3"
BASE_DIRECTORY="$4"

#===============================================================================
# Requirements
#===============================================================================
yum -y install openldap compat-openldap openldap-clients openldap-servers openldap-servers-sql openldap-devel

systemctl enable slapd
systemctl start slapd

#===============================================================================
# LDAP Setup
#===============================================================================
mkdir -p $DIR/ldap_temp && cd $DIR/ldap_temp

ADMIN_HASH=`slappasswd -h {SSHA} -s $ADMIN_PASSWD`

cat <<EOF > db.ldif
dn: olcDatabase={2}hdb,cn=config
changetype: modify
replace: olcSuffix
olcSuffix: $BASE_DIRECTORY

dn: olcDatabase={2}hdb,cn=config
changetype: modify
replace: olcRootDN
olcRootDN: cn=admin,$BASE_DIRECTORY

dn: olcDatabase={2}hdb,cn=config
changetype: modify
replace: olcRootPW
olcRootPW: $ADMIN_HASH
EOF

cat <<EOF > monitor.ldif
dn: olcDatabase={1}monitor,cn=config
changetype: modify
replace: olcAccess
olcAccess: {0}to * by dn.base="gidNumber=0+uidNumber=0,cn=peercred,cn=external, cn=auth" read by dn.base="cn=admin,$BASE_DIRECTORY" read by * none
EOF

cp /usr/share/openldap-servers/DB_CONFIG.example /var/lib/ldap/DB_CONFIG
chown ldap:ldap /var/lib/ldap/*

ldapmodify -Y EXTERNAL  -H ldapi:/// -f db.ldif
ldapmodify -Y EXTERNAL  -H ldapi:/// -f monitor.ldif
ldapadd -Y EXTERNAL -H ldapi:/// -f /etc/openldap/schema/cosine.ldif
ldapadd -Y EXTERNAL -H ldapi:/// -f /etc/openldap/schema/nis.ldif
ldapadd -Y EXTERNAL -H ldapi:/// -f /etc/openldap/schema/inetorgperson.ldif

cat <<EOF > base.ldif
dn: $BASE_DIRECTORY
dc: hadoop
objectClass: top
objectClass: domain

dn: cn=admin,$BASE_DIRECTORY
objectClass: organizationalRole
cn: admin
description: LDAP Manager

dn: ou=People,$BASE_DIRECTORY
objectClass: organizationalUnit
ou: People

dn: ou=Group,$BASE_DIRECTORY
objectClass: organizationalUnit
ou: Group
EOF

ldapadd -x -w $ADMIN_PASSWD -D "cn=admin,$BASE_DIRECTORY" -f base.ldif

# Create groups
cat <<EOF > groups.ldif
dn: cn=analysts,ou=Group,$BASE_DIRECTORY
cn: analysts
gidNumber: 1999
objectclass: top
objectclass: posixGroup
EOF

ldapadd -x -w $ADMIN_PASSWD -D "cn=admin,$BASE_DIRECTORY" -f groups.ldif

IFS=,
for user in $USER_LIST; do

cat <<EOF > ./$user.ldif
# User $user
dn: uid=$user,ou=People,$BASE_DIRECTORY
cn: $user
sn: Test
uid: $user
uidNumber: 5000
gidNumber: 1999
homeDirectory: /home/$user
loginShell: /bin/bash
objectClass: top
objectClass: inetOrgPerson
objectClass: posixAccount
objectClass: shadowAccount
EOF

  ldapadd -x -w $ADMIN_PASSWD -D "cn=admin,$BASE_DIRECTORY" -f "./$user.ldif"
  ldappasswd -x -D "cn=admin,$BASE_DIRECTORY" -w $ADMIN_PASSWD -s $USER_PASSWD "uid=$user,ou=People,$BASE_DIRECTORY"
  ldapsearch -x uid=$user -w $ADMIN_PASSWD -D "cn=admin,$BASE_DIRECTORY" -b "ou=People,$BASE_DIRECTORY"

done

cd -
rm $DIR/ldap_temp -rf
