# Hive Failures (2.3 → 3.1)

### HIVE3_ACID_DEFAULT

- **Cause**: Hive 3.x defaults all managed tables to ACID/transactional. `INSERT OVERWRITE` on transactional tables fails.
- **Detection**: `SemanticException [Error 10265]`, `INSERT OVERWRITE` failures on managed tables, `FAILED: SemanticException ... is not an INSERT-only table`.
- **Fix**:
  - Convert affected tables: `ALTER TABLE t SET TBLPROPERTIES('EXTERNAL'='TRUE', 'external.table.purge'='true');`
  - For new tables: explicitly use `CREATE EXTERNAL TABLE`
  - **WARNING**: Do NOT use `SET hive.create.as.acid=false` or `SET hive.create.as.insert.only=false` in scripts — these properties do NOT exist on EMR 7.x and will cause the script to fail with `hive configuration ... does not exists`. Handle ACID at the table level (EXTERNAL) or via cluster-level `hive-site` configuration classification at launch time.

### HIVE3_MANAGED_TABLE

- **Cause**: Hive 3 enforces that external tools cannot access managed table data files directly. S3/HDFS reads of managed table paths fail with permission errors.
- **Detection**: Permission errors accessing managed table paths, `HiveAccessControlException`, tools reading warehouse paths get empty results.
- **Fix**: `ALTER TABLE <t> SET TBLPROPERTIES('EXTERNAL'='TRUE', 'external.table.purge'='true');`
- For bulk conversion: query `information_schema.tables` or Glue Catalog to list all managed tables and batch-convert.

### HIVE3_SYNTAX_CHANGES

- **Cause**: New reserved keywords in Hive 3.x that were unreserved in 2.x.
- **Detection**: `ParseException`, `SemanticException` on DDL/DML that was valid in Hive 2.3.
- **Fix**: Backtick-quote reserved words: `` `date` ``, `` `time` ``, `` `timestamp` ``, `` `interval` ``, `` `user` ``, `` `role` ``, `` `groups` ``, `` `index` ``, `` `exchange` ``
- Scan ALL .hql files for unquoted usage of these keywords as column/table names.

### HIVE3_TYPE_CONVERSION

- **Cause**: Stricter type checking; implicit conversions (string↔numeric, timestamp↔string) removed.
- **Detection**: `SemanticException: Cannot convert column`, `TypeError`, unexpected NULL results from comparisons.
- **Fix**: Add explicit `CAST(col AS <type>)`. Workaround: `SET hive.strict.checks.type.safety=false;`

### HIVE3_EXECUTION_ENGINE

- **Cause**: MapReduce execution engine deprecated in Hive 3.x; `hive.execution.engine=mr` may produce errors or degraded performance.
- **Detection**: Slow execution, warnings about deprecated MR engine, `FAILED: Execution Error` from MR tasks.
- **Fix**: `SET hive.execution.engine=tez;` — this is the default on EMR 7.x. Remove any `hive.execution.engine=mr` from configurations.

### HIVE3_MERGE_STATEMENT

- **Cause**: Hive 3 MERGE syntax differs from Hive 2 workarounds.
- **Detection**: `ParseException` on MERGE statements, unexpected behavior in upsert patterns.
- **Fix**: Use standard Hive 3 MERGE: `MERGE INTO target USING source ON condition WHEN MATCHED THEN UPDATE ... WHEN NOT MATCHED THEN INSERT ...`

### HIVE_METASTORE_SCHEMA

- **Cause**: Hive 3.x metastore schema incompatible with 2.x.
- **Detection**: `MetaException` on schema validation, connection errors to HMS.
- **Fix**: Glue Catalog: no action (fully managed). External HMS: `schematool -upgradeSchema -dbType <type>`.

### HIVE2_ACID_DELTA_FORMAT_INCOMPATIBLE

- **Cause**: Hive 2.x ACID tables store data in ORC delta files (`delta_NNNNNN_NNNNNN/bucket_NNNNN`) that use a file format incompatible with Hive 3.x. When migrating to EMR 7.x, recreating the table at the same S3 LOCATION and running `MSCK REPAIR TABLE` discovers the partitions but **data is invisible** — `SELECT *` returns zero rows. This is because Hive 3.x cannot read the Hive 2.x delta file layout; it expects either base files produced by major compaction or the Hive 3.x delta format.
- **Detection**:
  - `SELECT * FROM table` returns 0 rows on EMR 7.x despite partitions being present
  - `MSCK REPAIR TABLE` successfully adds partitions but data is empty
  - S3 listing shows `delta_NNNNNN_NNNNNN/` directories (not `base_NNNNNN/`) under partition paths
  - Table has `transactional=true` in TBLPROPERTIES
  - Source cluster was EMR 5.x with `hive.txn.manager=org.apache.hadoop.hive.ql.lockmgr.DbTxnManager`
- **Root Cause Detail**: Hive 2.x ACID writes produce delta files only (no base file). Hive 3.x changed the internal transaction file format — it can read Hive 2.x **base files** (produced by major compaction) but NOT raw Hive 2.x delta files. The metastore schema upgrade (`schematool -upgradeSchema`) fixes the metadata schema but does **NOT** fix the on-disk ORC delta file format — this is a common misconception.
- **Fix**:

  **Option A — Export to non-ACID EXTERNAL table (Required — primary approach)**:

  > **WARNING**: Major compaction alone is NOT sufficient. Testing confirmed that even compacted `base_NNNNNN/` files retain Hive 2.x ACID ORC column encoding, which Hive 3.x cannot read (`ClassCastException: BytesColumnVector cannot be cast to LongColumnVector`). The ONLY reliable fix is exporting data to a new non-ACID table.

  On the EMR 5.x cluster (or a temporary one if the original is terminated):

  ```sql
  -- On EMR 5.x cluster (source)
  -- 1. Identify all ACID tables
  SELECT TBL_NAME, DB.NAME as DB_NAME
  FROM TBLS t
  JOIN DBS DB ON t.DB_ID = DB.DB_ID
  JOIN TABLE_PARAMS tp ON t.TBL_ID = tp.TBL_ID
  WHERE tp.PARAM_KEY = 'transactional' AND tp.PARAM_VALUE = 'true';

  -- 2. Export each ACID table to clean non-ACID format:
  CREATE EXTERNAL TABLE db.table_name_export
  STORED AS ORC
  LOCATION 's3://bucket/migration-export/table_name/'
  AS SELECT * FROM db.table_name;

  -- 3. Verify export contains all rows:
  SELECT COUNT(*) FROM db.table_name;
  SELECT COUNT(*) FROM db.table_name_export;

  -- 4. On EMR 7.x, create table pointing to exported data:
  CREATE EXTERNAL TABLE db.table_name
  (... original schema ...)
  STORED AS ORC
  LOCATION 's3://bucket/migration-export/table_name/'
  TBLPROPERTIES ('external.table.purge'='true');
  ```

  **Option B — Re-ingestion via temporary EMR 5.x cluster (when source is terminated)**:

  If the original EMR 5.x cluster has already been terminated and data exists only as delta files in S3:

  ```sql
  -- 1. Launch a TEMPORARY EMR 5.x cluster with same Hive config
  --    (hive.txn.manager=DbTxnManager, same external metastore)

  -- 2. On the temporary EMR 5.x cluster, export data to a new non-ACID location:
  CREATE EXTERNAL TABLE db.table_name_export
  STORED AS ORC
  LOCATION 's3://bucket/migration-export/table_name/'
  AS SELECT * FROM db.table_name;

  -- 3. On EMR 7.x cluster, create table pointing to exported data:
  CREATE EXTERNAL TABLE db.table_name
  (... schema ...)
  STORED AS ORC
  LOCATION 's3://bucket/migration-export/table_name/'
  TBLPROPERTIES ('external.table.purge'='true');

  -- 4. Verify data is accessible:
  SELECT COUNT(*) FROM db.table_name;
  ```

  **Option C — Full re-ingestion from upstream source**:

  If data can be regenerated from upstream (source of truth), re-run the ingestion pipeline on the EMR 7.x cluster. This produces data natively in Hive 3.x format.

- **Important Notes**:
  - `schematool -upgradeSchema` upgrades the **metastore schema** (MySQL/PostgreSQL tables) but does NOT convert the **ORC delta file format** — both steps are needed for ACID table migration
  - AWS Glue Data Catalog does NOT support Hive ACID transactions — if customer uses Glue Catalog, ACID tables must be converted to EXTERNAL tables regardless
  - Non-ACID tables (no `transactional=true`) work fine without export — their ORC files are standard and readable by any Hive version
  - Major compaction is NOT sufficient — compacted base files still retain Hive 2.x ACID column encoding that Hive 3.x cannot read (validated in E2E testing on EMR 7.5)

### HIVE3_CTAS_EXTERNAL

- **Cause**: Hive 3.x rejects `CREATE EXTERNAL TABLE ... AS SELECT` (CTAS with EXTERNAL keyword). In Hive 2.x, this was silently accepted (the EXTERNAL keyword was ignored and the table was created as managed). In Hive 3.x, this is an explicit error because EXTERNAL tables and SELECT-based creation have conflicting semantics around data ownership.
- **Detection**: `SemanticException: CREATE-TABLE-AS-SELECT cannot create an external table`, `FAILED: SemanticException ... CTAS ... EXTERNAL`
- **Fix** (choose one):
  - **Option A — Two-step creation (recommended)**:
    ```sql
    -- Step 1: Create external table with explicit schema
    CREATE EXTERNAL TABLE db.table_name (col1 STRING, col2 INT, ...)
    STORED AS ORC
    LOCATION 's3://bucket/path/';
    
    -- Step 2: Insert data
    INSERT INTO db.table_name SELECT col1, col2, ... FROM source_table;
    ```
  - **Option B — Create as managed, then convert**:
    ```sql
    CREATE TABLE db.table_name AS SELECT * FROM source_table;
    ALTER TABLE db.table_name SET TBLPROPERTIES('EXTERNAL'='TRUE', 'external.table.purge'='true');
    ```
  - **Option C — Use INSERT OVERWRITE with pre-created table**:
    ```sql
    CREATE EXTERNAL TABLE db.table_name LIKE source_table STORED AS ORC LOCATION 's3://...';
    INSERT OVERWRITE TABLE db.table_name SELECT * FROM source_table;
    ```
- **Important**: When scanning Hive scripts, flag ALL occurrences of `CREATE EXTERNAL TABLE ... AS SELECT` for rewrite. The pattern `CREATE EXTERNAL TABLE ... LIKE` (without AS SELECT) is fine and unchanged.
