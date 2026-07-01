# HBase RPM Replacement Bootstrap Script

## Overview

The `replace_hbase_rpms_bootstrap.sh` script is designed to replace HBase RPMs on Amazon EMR clusters during bootstrap. This script enables minimal downtime upgrades by installing custom or patched HBase RPMs from a specified S3 location.

## Prerequisites

- Amazon EMR cluster with appropriate IAM permissions to access S3
- Custom HBase RPM packages stored in S3
- EMR cluster configured to run bootstrap actions

## Required IAM Permissions

Ensure your EMR cluster's EC2 instance profile has the following permissions:

```json
{
    "Version": "2012-10-17",
    "Statement": [
        {
            "Effect": "Allow",
            "Action": [
                "s3:GetObject",
                "s3:ListBucket"
            ],
            "Resource": [
                "arn:aws:s3:::your-bucket/hbase_patch/rpm/*",
                "arn:aws:s3:::your-bucket/hbase_patch/rpm"
            ]
        }
    ]
}
```

## Configuration

### **IMPORTANT: Update S3 Bucket Location**

Before using this script, you **MUST** update the S3 bucket location in the script:

1. Open `replace_hbase_rpms_bootstrap.sh`
2. Locate line 5: `bucket=s3://your-bucket/hbase_patch/rpm/`
3. Replace `your-bucket` with your actual S3 bucket name
4. Update the path if your RPMs are stored in a different location

**Example:**
```bash
# Before (default placeholder)
bucket=s3://your-bucket/hbase_patch/rpm/

# After (your actual configuration)
bucket=s3://my-emr-patches/hbase/2.4.17/rpm/
```

## Script Functionality

The bootstrap script performs the following actions:

1. **Creates Local Repository Directory**: Creates `/var/aws/emr/packages/bigtop` for storing RPMs locally
2. **Downloads RPMs**: Syncs all RPM files from the specified S3 location to the local directory
3. **Sets Up Local Yum Repository**: 
   - Installs `createrepo` utility
   - Creates a local yum repository with high priority (priority=1)
   - Configures repository metadata
4. **Replaces Existing Packages**: Identifies installed packages and reinstalls them from the local repository

## Usage

### Method 1: EMR Console

1. When creating an EMR cluster, navigate to "Bootstrap Actions"
2. Add bootstrap action with:
   - **Script location**: `s3://your-script-bucket/replace_hbase_rpms_bootstrap.sh`
   - **Optional arguments**: None required

### Method 2: AWS CLI

```bash
aws emr create-cluster \
    --name "HBase-Upgrade-Cluster" \
    --release-label emr-6.15.0 \
    --instance-type m5.xlarge \
    --instance-count 3 \
    --bootstrap-actions Path=s3://your-script-bucket/replace_hbase_rpms_bootstrap.sh \
    --ec2-attributes InstanceProfile=EMR_EC2_DefaultRole \
    --service-role EMR_DefaultRole
```

### Method 3: Terraform

```hcl
resource "aws_emr_cluster" "hbase_cluster" {
  name          = "hbase-upgrade-cluster"
  release_label = "emr-6.15.0"
  
  bootstrap_action {
    name = "Replace HBase RPMs"
    path = "s3://your-script-bucket/replace_hbase_rpms_bootstrap.sh"
  }
  
  # ... other configuration
}
```

## S3 Directory Structure

Ensure your S3 bucket follows this structure:

```
s3://your-bucket/hbase_patch/rpm/
├── hbase-2.4.17-1.el7.x86_64.rpm
├── hbase-client-2.4.17-1.el7.x86_64.rpm
├── hbase-master-2.4.17-1.el7.x86_64.rpm
├── hbase-regionserver-2.4.17-1.el7.x86_64.rpm
└── hbase-thrift-2.4.17-1.el7.x86_64.rpm
```

## Troubleshooting

### Common Issues

1. **Permission Denied**: Verify EMR instance profile has S3 access permissions
2. **Script Fails to Download**: Check S3 bucket name and path in the script
3. **Package Installation Fails**: Ensure RPM packages are compatible with the EMR version

### Logs

Bootstrap action logs can be found at:
- `/var/log/bootstrap-actions/` on cluster nodes
- CloudWatch Logs (if configured)
- EMR Console under "Steps" tab

### Debugging Commands

If the bootstrap action fails, SSH into the cluster and run:

```bash
# Check if local repository was created
ls -la /var/aws/emr/packages/bigtop/

# Verify yum repository configuration
cat /etc/yum.repos.d/emr_replace_rpms.repo

# List available packages in local repo
yum --disablerepo "*" --enablerepo "emr_replace_rpms" list available
```

## Security Considerations

- Use dedicated S3 bucket with restricted access
- Enable S3 bucket versioning for RPM management
- Consider using S3 bucket encryption
- Validate RPM package integrity before deployment

## Version Compatibility

This script is compatible with:
- Amazon EMR 6.x series
- HBase 2.x versions
- RHEL/CentOS 7-based EMR AMIs

## Support

For issues related to this bootstrap script, please refer to:
- [EMR Bootstrap Actions Documentation](https://docs.aws.amazon.com/emr/latest/ManagementGuide/emr-plan-bootstrap.html)
- [HBase on EMR Best Practices](https://docs.aws.amazon.com/emr/latest/ReleaseGuide/emr-hbase.html)

## Contributing

When modifying this script:
1. Test on a development EMR cluster first
2. Validate with different EMR versions
3. Update this README with any configuration changes
4. Consider backward compatibility

## Disclaimer

The examples provided in this repository are not supported by AWS EMR. The use of this code is your responsibility and at your own risk.