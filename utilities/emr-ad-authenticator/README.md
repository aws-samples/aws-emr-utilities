# EMR AD Authenticator

EMR AD Authenticator is a bootstrap action script to integrate Amazon EMR with Active Directory for user authentication. It installs SSSD on all EMR  nodes and configures cross-realm trust with AD that doesn't require domain join. 

## Why EMR AD Authenticator
 
When cross-realm trust is enabled for an EMR cluster in the EMR Security Configuration, SSSD gets installed and configured on all the nodes of the EMR cluster. The configuration that EMR uses for SSSD requires that all the EMR cluster nodes join the AD domain. 
<https://docs.aws.amazon.com/emr/latest/ManagementGuide/emr-kerberos-cross-realm.html>

There are scenarios in which customers cannot use this method to establish cross-realm trust with AD using EMR Security Configuration which requires domain join. For example, as Active Directory has a 15-character limit for registering joined computer names, it truncates longer names and there are chances that the truncated hostname of new ec2 instance will match the truncated hostname of an existing ec2-instance. In this scenario, when AD joins the new ec2-instance, it bumps up the keytab version and deploys it on the new ec2 machine as it already has a keytab for truncated computer name. This will invalidate authentication of older ec2-instance, resulting in application failures.

 
##How to use

*1. Setup on Active Directory* 

Step 1: create a trust in Active Directory, which is a one-way, incoming, non-transitive, realm trust with the cluster-dedicated/external KDC. Here, the example we use for the cluster's realm is EC2.INTERNAL and AD Domain is ad.domain.com.

Open the Windows command prompt with administrator privileges and type the following commands to create the trust relationship on the Active Directory domain controller:

* C:\Users\Administrator> ksetup /addkdc EC2.INTERNAL KDC-FQDN
* C:\Users\Administrator> netdom trust EC2.INTERNAL /Domain:ad.domain.com /add /realm /passwordt:MyVeryStrongPassword
* C:\Users\Administrator> ksetup /SetEncTypeAttr EC2.INTERNAL AES256-CTS-HMAC-SHA1-96

*2. Setup on EMR*

Step 1: Use the Bootstrap Action Script for SSSD EMR-AD Integration 

	i.  Modify emr_ad_auth.sh per your AD, Bind User and External KDC details. Save this script in S3.
	ii. Modify emr_ad_auth_wrapper.sh to point to emr_ad_auth.sh in S3. Save this script in S3.

Step 2: Use the second bootstrap script to create HDFS home directory for AD user when logging in

Step 3: Add the mapping for Kerberos Principals to Usernames in EMR configuration

	i.  In core-site, hadoop.security.auth_to_local
	ii. In kms-site, hadoop.kms.authentication.kerberos.name.rules

For example, if the AD Realm Name is EC2.INTERNAL

--configurations '[{"Classification":"core-site","Properties":{"hadoop.security.token.service.use_ip":"true","hadoop.security.auth_to_local":"RULE:[1:$1@$0](.*@EC2\\.INTERNAL)s/@.*///L RULE:[2:$1@$0](.*@EC2\\.INTERNAL)s/@.*///L DEFAULT"}},{"Classification":"hadoop-kms-site","Properties":{"hadoop.kms.authentication.kerberos.name.rules<\/":"RULE:[1:$1@$0](.*@EC2.INTERNAL)s/@.*///L RULE:[2:$1@$0](.*@EC2.INTERNAL)s/@.*///L DEFAULT"}}]'

Note:  hadoop.security.auth_to_local configuration in core-site.xml needs to match exactly with hadoop.kms.authentication.kerberos.name.rules configuration in kms-site.xml

Step 4: Create the Kerberized EMR Cluster

Step 5: Once the cluster is up and running, check if you can ssh with an AD user, verify if the hdfs dir is created for the user and you are able to execute a sample job successfully with the user.

##Common Errors


