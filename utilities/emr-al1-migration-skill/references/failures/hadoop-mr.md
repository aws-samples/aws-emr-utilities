# Hadoop / YARN / MapReduce Failures

### HADOOP3_API_BREAK

- **Cause**: Hadoop 3.x removed/relocated APIs from Hadoop 2.x.
- **Detection**: `NoSuchMethodError`/`ClassNotFoundException` for `org.apache.hadoop.*` classes.
- **Fix**:
  - `NativeS3FileSystem` (s3n scheme) → On EMR 7.1–7.5: remove `fs.s3n.impl` (EMRFS handles s3://). On EMR 7.10+: use `org.apache.hadoop.fs.s3a.S3AFileSystem`.
  - Recompile custom JARs against Hadoop 3.3.x.

### MAPREDUCE_OLD_API

- **Cause**: Code using the deprecated `org.apache.hadoop.mapred.*` (old MR API) may encounter removed methods in Hadoop 3.
- **Detection**: `NoSuchMethodError` in `org.apache.hadoop.mapred.JobConf`, `org.apache.hadoop.mapred.FileInputFormat`, etc.
- **Fix**:

| Old API (`org.apache.hadoop.mapred.*`) | New API (`org.apache.hadoop.mapreduce.*`) |
|-----|--------|
| `JobConf conf = new JobConf()` | `Job job = Job.getInstance(new Configuration())` |
| `conf.setMapperClass(MyMapper.class)` | `job.setMapperClass(MyMapper.class)` |
| `conf.setReducerClass(MyReducer.class)` | `job.setReducerClass(MyReducer.class)` |
| `FileInputFormat.setInputPaths(conf, ...)` | `FileInputFormat.setInputPaths(job, ...)` |
| `JobClient.runJob(conf)` | `job.waitForCompletion(true)` |
| `Reporter reporter` parameter | `Context context` parameter |

### MAPREDUCE_CLASSPATH

- **Cause**: Hadoop JAR paths changed between EMR 5.x and 7.x.
- **Detection**: `ClassNotFoundException` when launching MR jobs, missing JARs at expected paths.
- **Fix**:
  - `/usr/lib/hadoop/lib/` → `/usr/lib/hadoop/share/hadoop/common/lib/`
  - `/usr/lib/hadoop-mapreduce/hadoop-streaming.jar` path unchanged
  - Update any hardcoded paths in scripts or step definitions

### MAPREDUCE_STREAMING_PYTHON

- **Cause**: Hadoop Streaming jobs using Python scripts fail because Python 2 is removed on AL2023.
- **Detection**: `/usr/bin/python: No such file or directory` in streaming mapper/reducer, `SyntaxError` from Python 2 code.
- **Fix**: Update `-mapper`/`-reducer` to use `python3`. Convert scripts to Python 3 syntax.

### YARN_DEPRECATED_CONFIG

- **Cause**: YARN properties removed or renamed in Hadoop 3.x.
- **Detection**: Warnings in ResourceManager logs, unexpected defaults, `InvalidConfigurationException`.
- **Fix**: Verify `yarn.nodemanager.aux-services` is set to `mapreduce_shuffle` (the canonical name in both Hadoop 2 and 3). Remove `yarn.resourcemanager.resource-tracker.address` if duplicating hostname (auto-derived in Hadoop 3).

### S3_SCHEME_DEPRECATED

- **Cause**: `s3n://` scheme is deprecated (still works on EMR 7.5 but will be removed in a future release). `NativeS3FileSystem` class may be removed in future EMR versions.
- **Detection**: `No FileSystem for scheme: s3n` (if/when removed), `ClassNotFoundException: NativeS3FileSystem`
- **Fix**: Replace `s3n://` → `s3://` in all input/output paths, configs, and scripts. Remove `fs.s3n.impl` and all `fs.s3n.*` config properties.
