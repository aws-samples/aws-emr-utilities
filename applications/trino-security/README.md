# Trino Security
The following script configures Kerberos authentication while launching an Amazon EMR cluster with Trino installed.

The script also allows you to setup the [Trino File Based Access Control](https://trino.io/docs/current/security/file-system-access-control.html) feature, storing ACLs rules in a [Secret Manager](https://aws.amazon.com/secrets-manager/) secret. When using this feature, the script also creates a crontab to periodically refresh the rules, so that you can modify the ACLs directly in Secret Manager.

## Requirements
In order to work, the cluster should be launched with an [EMR Security Configuration](https://docs.aws.amazon.com/emr/latest/ManagementGuide/emr-security-configurations.html), with Kerberos and [In Transit Encryption](https://docs.aws.amazon.com/emr/latest/ManagementGuide/emr-data-encryption-options.html) (PEM certificates) enabled.

## Usage
To set up the Trino Kerberos authentication, please modify the **KDC_ADMIN_PASSWORD** in the `trino_security.sh` script to match the KDC password that will be used to launch the EMR cluster.

Once done, copy the bash scripts in an S3 bucket that can be accessed from the EMR cluster. Finally, configure an EMR Bootstrap Action while launching the cluster, specifying the following parameters:

- **Script location** s3://BUCKET/PREFIX/emr_postinstall.sh
- **Optional Arguments** s3://BUCKET/PREFIX/trino_security.sh

## (Experimental) File Based Access Control
By default the script doesn't enable the File Based Access Control feature. To enable it, set the **FBAC_ENABLED** variable in the `trino_security.sh` script to **true**.

To create the Trino rules in Secret Manager you can use the following command:

```bash
aws secretsmanager create-secret --name "emr/trino_fbac_rules.json" --description "Trino FBAC rules (JSON)" --secret-string '{
   "catalogs": [{
           "user": ".*",
           "catalog": "hive",
           "allow": true
       },
       {
           "user": ".*",
           "catalog": "system",
           "allow": true
       },
       {
           "user": ".*",
           "catalog": "jmx",
           "allow": true
       }
   ]
}'
```

Adjust the rules accordingly to your needs. For more details, see [Trino File Based Access Control](https://trino.io/docs/current/security/file-system-access-control.html)

Finally, you must provide read access to the EMR cluster to retrieve the policies stored in Secret Manager. To do this, attach the following policy in the EMR Instance Profile role:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": "secretsmanager:GetSecretValue",
      "Resource": "arn:aws:secretsmanager:*:*:secret:emr/trino_fbac_rules.json*"
    }
  ]
}
```

If you stored the rules with a different name in Secret Manager (default **emr/trino_fbac_rules.json**), modify the secret's name in the IAM policy and in the **TRINO_FBAC_RULES_SECRET** variable within the `trino_security.sh` script.

## Additional Notes
- Support the Glue Data Catalog or external Hive metastore
- Support Trino starting EMR 6.4.0


## Troubleshooting
In order to troubleshoot issues while executing the script, you can review the following logs file generated during the EMR provisioning.

```
cat /var/log/provision-node/apps-phase/0/*/stdout
cat /var/log/provision-node/apps-phase/0/*/stderr
```
