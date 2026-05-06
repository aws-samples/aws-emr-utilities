# EMR HBase Reconfiguration Guard

Prevent HBase services from restarting when reconfiguring an EMR cluster.

## Problem

When you reconfigure an EMR HBase cluster (e.g., updating `hbase-site.xml` properties via `modify-instance-groups`), all HBase services — RegionServer, Master, Thrift, and REST — are automatically restarted. This causes unexpected downtime and disrupts in-flight operations.

## Solution

A bootstrap action that modifies the HBase Puppet manifest so that configuration changes are applied to disk without triggering automatic service restarts. This gives you full control over when HBase services are restarted.

> **Important:** After reconfiguration, the config files on disk are updated but the running HBase services still use the previous settings. You must restart the services yourself for the new configuration to take effect.

### Restarting HBase Services

After reconfiguration, restart services on each node when you are ready:

```bash
# On core/task nodes
sudo systemctl restart hbase-regionserver

# On master node
sudo systemctl restart hbase-master

# If applicable
sudo systemctl restart hbase-thrift
sudo systemctl restart hbase-rest
```

You can perform a rolling restart — one RegionServer at a time — to minimize impact on your workload.

## Setup

1. Upload the modified `init.pp` to your S3 bucket:
   ```bash
   aws s3 cp init.pp s3://YOUR-BUCKET/stop_hbase_restart/init.pp
   ```

2. Update the S3 path in `replace_hbase_init_pp.sh` to point to your bucket.

3. Upload the bootstrap script:
   ```bash
   aws s3 cp replace_hbase_init_pp.sh s3://YOUR-BUCKET/scripts/replace_hbase_init_pp.sh
   ```

4. Add the bootstrap action when creating your EMR cluster:
   ```bash
   aws emr create-cluster \
     --bootstrap-actions '[{
       "Name": "Stop HBase restart on Reconfiguration",
       "Path": "s3://YOUR-BUCKET/scripts/replace_hbase_init_pp.sh"
     }]' \
     ...
   ```

## Artifacts

| File | Description |
|------|-------------|
| `replace_hbase_init_pp.sh` | Bootstrap action script — backs up the original manifest and replaces it with the modified version |
| `init.pp` | Modified Puppet manifest for HBase (EMR 7.12) |

> **Note:** The `init.pp` file may differ between EMR versions. Always verify the original at `/var/aws/emr/bigtop-deploy/puppet/modules/hadoop_hbase/manifests/init.pp` on your target version before deploying.

## Test Results — EMR 7.12

Two EMR 7.12.0 clusters (m5.xlarge, 1 master + 1 core, HBase on S3). Reconfiguration added `hbase.rpc.timeout=120000` via `modify-instance-groups`.

### Without Bootstrap Action

| | Before | After |
|---|---|---|
| RegionServer PID | `12489` | `15499` **(changed)** |
| hbase-site.xml updated | — | ✅ |

HBase RegionServer was restarted automatically during reconfiguration.

### With Bootstrap Action

| | Before | After |
|---|---|---|
| RegionServer PID | `9457` | `9457` **(unchanged)** |
| hbase-site.xml updated | — | ✅ |

HBase RegionServer was **not** restarted. Config was applied to disk. Service continued running uninterrupted.
