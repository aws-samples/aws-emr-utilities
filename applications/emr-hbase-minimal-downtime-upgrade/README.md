# EMR HBase Upgrade Using Read Replica Pre-Warm

Upgrade Amazon EMR HBase on S3 to the latest EMR release using a Read Replica pre-warm strategy — enabling zero-downtime preparation and minimal cutover windows.

## Overview

This toolkit provides a Read Replica-based upgrade path for EMR HBase clusters running on S3. A Read Replica is launched on the target EMR version pointing to the same S3 root directory, pre-warmed and validated, then promoted to become the new active cluster.

### Upgrade Paths

| Source | Target | StoreFileTracker Migration Required? |
|--------|--------|--------------------------------------|
| EMR 6.x (`hbase:storefile`) | EMR 7.12+ | **Yes** — must migrate from `hbase:storefile` to FILE tracker |
| EMR 7.x (already on FILE tracker) | EMR 7.12+ | **No** — straightforward Read Replica promotion |

**With StoreFileTracker migration:** EMR 6.x uses a proprietary `hbase:storefile` table to track HFiles in S3. EMR 7.x uses the Apache HBase FILE-based tracker (`.filelist` manifests). This toolkit includes the `generateStoreFileList` tool (integrated into HBCK2) to create those manifests on the Read Replica, enabling a seamless transition.

**Without StoreFileTracker migration:** If your cluster is already on a FILE-compatible tracker, skip Steps 7–8 in Phase 1. The rest of the process (pre-warm, validate, cutover, promote) applies identically.

## Key Benefits

- **Zero Downtime Preparation**: The primary cluster remains fully operational during all preparation steps
- **Minimal Cutover Window**: Only a brief downtime for flush → terminate → promote
- **Safe Rollback**: Snapshots taken before cutover allow rollback if needed
- **Version Jump**: Upgrade directly from EMR 6.x to 7.12+ in one operation
- **Pre-Warm Validation**: Read Replica lets you verify data visibility and region health before committing to cutover

## Prerequisites

- Primary cluster running EMR HBase on S3 (`hbase.emr.storageMode=s3`)
- Target: EMR 7.12 or later
- S3 bucket accessible by both primary and Read Replica clusters
- Bootstrap action artifacts uploaded to S3 (see `bootstrap-actions/`) — required only for StoreFileTracker migration path

## Directory Structure

```
├── bootstrap-actions/       # Unified bootstrap action (patches + hbase-operator-tools)
├── rpms/                    # RPM packages (hbase-operator-tools and required patches)
├── configs/
│   ├── primary-6.x.json    # Reference config for the primary EMR 6.x cluster
│   └── read-replica-7.12.json  # Config for the EMR 7.12 Read Replica
├── scripts/
│   ├── generate-filelists.sh   # Generate .filelist manifests for all tables
│   ├── switch-sft.sh           # Switch store file tracker to FILE + validate
│   ├── pre-cutover.sh          # Disable balancer/compactions, take snapshots
│   └── validate-migration.sh   # Post-cutover validation
└── docs/
    └── known-issues.md         # Known benign hbck warnings and rollback notes
```

## Migration Steps

### Phase 1: Preparation (Primary Cluster Running — No Downtime)

**Step 1.** Run major compactions on the primary cluster to consolidate data files and verify no regions are in SPLIT state:

```bash
echo "major_compact '<table>'" | hbase shell
```

> **Note:** For large tables (100+ TB), major compaction can take significant time. Monitor via the HBase Master UI → Region Servers → Compaction Queues.

**Step 2.** Run catalog_janitor to clean up stale region references:

```bash
echo "catalogjanitor_run" | hbase shell
```

**Step 3.** Confirm no inconsistencies on the primary cluster:

```bash
sudo -u hbase hbase hbck > hbck_report.txt
```

Verify the report shows `0 inconsistencies detected. Status: OK`.

**Step 4.** Launch a Read Replica cluster on EMR 7.12 pointing to the same S3 root directory, with the bootstrap action and `DefaultStoreFileTracker`:

```bash
aws emr create-cluster \
  --name "hbase-read-replica-migration" \
  --release-label emr-7.12.0 \
  --applications Name=HBase \
  --configurations file://configs/read-replica-7.12.json \
  --bootstrap-actions '[{
    "Name": "Install hbase-operator-tools",
    "Path": "s3://<your-bucket>/bootstrap-actions/install-hbase-operator-tools.sh"
  }]' \
  --instance-groups '[
    {"InstanceGroupType":"MASTER","InstanceCount":1,"InstanceType":"m5.2xlarge"},
    {"InstanceGroupType":"CORE","InstanceCount":3,"InstanceType":"m5.2xlarge"}
  ]' \
  --ec2-attributes SubnetId=<subnet-id>,KeyName=<key-name> \
  --service-role EMR_DefaultRole \
  --log-uri s3://<your-bucket>/emr-logs/
```

> **Important:** If migrating from EMR 6.x (StoreFileTracker path), the Read Replica must use `DefaultStoreFileTracker` initially — this allows it to read data tracked by the `hbase:storefile` table from the primary. If upgrading between 7.x versions (already on FILE tracker), omit the `hbase.store.file-tracker.impl` property.

**Step 5.** Refresh meta on the Read Replica:

```bash
echo "refresh_meta" | hbase shell
```

**Step 6.** Validate the Read Replica — verify regions show OPEN status in the HBase Master UI and run sample reads to confirm data visibility.

**Step 7.** *(StoreFileTracker migration only — skip if already on FILE tracker)* Run the `generateStoreFileList` tool on the Read Replica to create `.filelist` manifests for all tables:

```bash
# For a single table:
sudo -u hbase hbase hbck -j /usr/lib/hbase-operator-tools/hbase-hbck2-1.2.0.jar \
  generateStoreFileList <table>

# For all tables (using the helper script):
./scripts/generate-filelists.sh
```

> **Important:** Use the table name without namespace for default namespace tables (e.g., `usertable` not `default:usertable`). Including the namespace prefix causes the tool to look for a non-existent path.

The tool is idempotent — re-running skips stores that already have manifests.

**Validation:** Confirm manifests were created by checking S3:
```bash
aws s3 ls s3://<your-bucket>/<hbase-root>/data/default/<table>/<region-hash>/<column-family>/ | grep ".filelist"
```

**Step 8.** *(StoreFileTracker migration only — skip if already on FILE tracker)* Switch the store file tracker on the Read Replica:

```bash
# For a single table:
echo "change_sft '<table>', 'FILE'" | hbase shell

# For all tables (using the helper script):
./scripts/switch-sft.sh
```

Verify data is accessible after each switch:

```bash
echo "scan '<table>', {LIMIT => 1}" | hbase shell
```

Confirm the tracker is set by describing the table:
```bash
echo "describe '<table>'" | hbase shell
# Should show: METADATA => {'hbase.store.file-tracker.impl' => 'FILE'}
```

**Step 9.** Prepare for cutover on the primary cluster — disable balancing and compactions, take snapshots:

```bash
# Using the helper script:
./scripts/pre-cutover.sh

# Or manually:
echo "balance_switch false" | hbase shell
echo "compaction_switch false" | hbase shell
echo "snapshot '<table>', '<table>_pre_migration_$(date +%Y%m%d)'" | hbase shell
```

**Step 10.** Final refresh on the Read Replica:

```bash
echo "refresh_meta" | hbase shell
hbase org.apache.hadoop.hbase.client.example.RefreshHFilesClient '<table>'
```

**Step 11.** Check for inconsistencies on the Read Replica:

```bash
sudo -u hbase hbase hbck > hbck_report.txt
```

> **Expected:** You may see inconsistencies on `hbase:storefile` — this is benign. See [known-issues.md](docs/known-issues.md) for details. What matters is that your **user tables** show no inconsistencies.

---

### Phase 2: Cutover (Brief Downtime)

1. **Stop application traffic** to the primary cluster — shut down or pause HBase client applications writing to the primary.

2. **Flush in-memory data** on the primary:

```bash
echo "flush '<table>'" | hbase shell
echo "flush 'hbase:meta'" | hbase shell
echo "flush 'hbase:namespace'" | hbase shell
```

3. **Terminate the primary cluster.**

4. **Promote the Read Replica** to active (read-write) mode:

```bash
echo "readonly_switch false" | hbase shell
echo "readonly_state" | hbase shell   # Verify: should return "ACTIVE"
```

5. **Update application connection strings** to point to the new cluster's master node endpoint, then restart applications.

> **Tip:** If you use a DNS layer (e.g., Route 53 private hosted zone) in front of EMR, steps 1 and 5 become DNS record updates instead of application changes.

---

### Phase 3: Validation

Run the validation script or perform manually:

```bash
./scripts/validate-migration.sh
```

Manual checks:
- Execute test write operations to confirm the cluster accepts writes
- Check the HBase Master UI to verify regions are serving both reads and writes
- Confirm data integrity with scans on key tables
- Run hbck and verify no user-table inconsistencies

---

## Rollback

If issues are discovered after cutover:

1. Stop traffic to the promoted cluster
2. Terminate the promoted 7.12 cluster
3. Launch a new EMR 6.x cluster pointing to the same S3 root directory
4. Restore from the pre-migration snapshot if needed:
   ```bash
   echo "restore_snapshot '<table>_pre_migration_<date>'" | hbase shell
   ```

> **⚠️ Important:** Do NOT drop `hbase:storefile` on the promoted cluster before rolling back. The 6.x cluster needs this table intact. See [known-issues.md](docs/known-issues.md#rollback-pitfall-do-not-drop-hbasestorefile) for details.

---

## Tool Reference

### generateStoreFileList Options

| Option | Description |
|--------|-------------|
| *(no flags)* | Generates `.filelist` manifests for all stores in the table. Skips stores with existing manifests (idempotent). |
| `-r -cf` | Target a specific region and column family for manifest generation. |
| `-force -r -cf` | Force-regenerate an existing manifest for a specific region and column family. Requires the region to be CLOSED. |

---

## Notes

- If using HBase clients outside EMR, ensure you have copied the HBase JAR file from your EMR cluster (matching your primary's version) to your client machine. This avoids potential `hbase:meta_j-xxxx not found` errors.
- You can directly migrate from EMR 6.x to 7.12+ using this approach.
- **Always test in a lower environment before production.**
- For large datasets (200+ TB), each step may take significant time. The helper scripts log progress to `/tmp/sft-migration-logs/` for tracking.

## Security

See [CONTRIBUTING](../../CONTRIBUTING.md#security-issue-notifications) for more information.

## License

This library is licensed under the MIT-0 License. See the [LICENSE](../../LICENSE) file.

## Disclaimer

The examples provided in this repository are not supported by AWS EMR. The use of this code is your responsibility and at your own risk.
