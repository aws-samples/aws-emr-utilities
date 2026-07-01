# Known Issues and Troubleshooting

## Benign hbck Warnings

### `hbase:storefile` Inconsistency on Read Replica / Promoted Cluster

**Symptom:**
```
ERROR: (region hbase:storefile,@\x00\x00\x00\x00\x00\x00\x00,...) First region should start with an empty key.
ERROR: Last region should end with an empty key.
ERROR: Found inconsistency in table hbase:storefile
```

**Cause:** The `hbase:storefile` table is EMR's proprietary persistent HFile tracking mechanism used on EMR 6.x clusters. On the 7.12 Read Replica configured with `DefaultStoreFileTracker`, this table still exists in S3 from the original cluster but has non-standard region key boundaries that hbck flags.

**Impact:** None. After switching to the FILE-based tracker (`change_sft`), this table is no longer used. The inconsistency is cosmetic.

**Resolution:** Safe to ignore. If you want a clean hbck report after cutover, you can drop the table on the promoted cluster:
```bash
echo "disable 'hbase:storefile'" | hbase shell
echo "drop 'hbase:storefile'" | hbase shell
```

> ⚠️ Only drop `hbase:storefile` if you are **certain** you will not roll back to EMR 6.x. See the rollback section below.

---

### `hbase:meta_j-*` Error on Old or New Cluster

**Symptom:**
```
ERROR: Region { meta => null, hdfs => s3://<bucket>/<root>/data/hbase/meta_j-<CLUSTER-ID>/..., deployed => , replicaId => 0 }
  on HDFS, but not listed in hbase:meta or deployed on any region server
ERROR: There is a hole in the region chain between  and .
ERROR: Found inconsistency in table hbase:meta_j-<CLUSTER-ID>
```

**Cause:** The `meta_j-*` directory is an EMR-internal meta journal created per cluster. When a Read Replica cluster is terminated, its meta journal directory remains in S3. A subsequent cluster sees the orphaned directory but doesn't recognize it in its own `hbase:meta`.

**Impact:** None. This is a leftover artifact from a terminated cluster.

**Resolution:** Safe to ignore. To clean up, delete the orphaned directory from S3:
```bash
aws s3 rm s3://<bucket>/<root>/data/hbase/meta_j-<CLUSTER-ID> --recursive
```

Replace `<CLUSTER-ID>` with the EMR cluster ID shown in the error (e.g., `meta_j-1M5U1HHKAJ1FK`).

---

## Rollback Pitfall: Do NOT Drop `hbase:storefile`

### Problem

If you drop `hbase:storefile` on the promoted 7.12 cluster and then need to roll back to EMR 6.x, the new 6.x cluster will re-create `hbase:storefile` with fresh regions. This results in **duplicate region errors** because residual metadata or region directories from the old table conflict with the newly created ones:

```
ERROR: (region hbase:storefile,,1782833757338...) Multiple regions have the same startkey:
ERROR: (region hbase:storefile,,1782856289147...) Multiple regions have the same startkey:
```

### Prevention

- **Do NOT** drop `hbase:storefile` until you are fully committed to the 7.12 cluster and have confirmed no rollback is needed.
- If you must suppress the hbck warning during validation, simply **ignore** it rather than dropping the table.

### Recovery (if already dropped and rolled back)

If you've already hit this state:

1. Identify the stale storefile region directories in S3. Compare timestamps — the older regions (from before the drop) are the orphans:
   ```bash
   aws s3 ls s3://<bucket>/<root>/data/hbase/storefile/ 
   ```

2. Remove the orphaned region directories (the ones with older timestamps that don't match the freshly-created regions).

3. Run `fixMeta` to resolve metadata inconsistencies:
   ```bash
   sudo -u hbase hbase hbck -j /usr/lib/hbase-operator-tools/hbase-hbck2.jar fixMeta
   ```

4. Re-run hbck to confirm resolution.

---

## Common Mistakes

### Wrong Namespace in `generateStoreFileList`

**Symptom:**
```
ERROR [main] hbase.StoreFileListGenerator: Table directory does not exist: s3://<bucket>/<root>/data/dafault/<table>
```

**Cause:** Specifying `default:<table>` as the argument. The tool interprets `default` as a literal namespace directory name, but in S3 the default namespace tables are stored under `data/default/` (note: this path is automatically resolved when you just pass the table name).

**Fix:** Use the table name without the namespace prefix for default namespace tables:
```bash
# Wrong:
sudo -u hbase hbase hbck -j /usr/lib/hbase-operator-tools/hbase-hbck2-1.2.0.jar generateStoreFileList default:mytable

# Correct:
sudo -u hbase hbase hbck -j /usr/lib/hbase-operator-tools/hbase-hbck2-1.2.0.jar generateStoreFileList mytable
```

For non-default namespaces, use the full `namespace:table` format as normal.

---

## Session Timeouts on Large Tables

For clusters with large data volumes (200+ TB), long-running operations like `major_compact` or `generateStoreFileList` across many tables may outlive SSH or HBase shell sessions.

**Recommendations:**

- Use `screen` or `tmux` to persist terminal sessions:
  ```bash
  screen -S migration
  # ... run commands ...
  # Ctrl+A, D to detach; screen -r migration to reattach
  ```

- Use the provided helper scripts which log progress to `/tmp/sft-migration-logs/` — if your session disconnects, you can check the log file for completion status.

- Monitor HBase Master UI for compaction/region status independently of your terminal session.
