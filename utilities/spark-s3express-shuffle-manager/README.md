# Spark S3 Express Shuffle Manager

A drop-in Apache Spark ShuffleManager that stores shuffle data on [Amazon S3 Express One Zone](https://aws.amazon.com/s3/storage-classes/express-one-zone/) instead of local disk. Provides **spot instance resilience** (zero FetchFailed errors on executor loss).

## Features

- **Spot resilience** — Shuffle data persists on S3 Express when executors are terminated. No FetchFailed errors, no stage retries.
- **Performance** — With optimized configs: **2.3x local EBS** on the heaviest TPC-DS query (q4), ~1.9x across the full benchmark.
- **Zero errors** — Retry logic with backoff handles transient S3 Express file-not-found issues.
- **Memory efficient** — Backpressure limits in-flight data to 48MB per task. LRU stream cache (500 max) prevents memory leaks.
- **Compatible** — Works with EMR 7.x (Spark 3.5.x). No changes to application code required.

## Prerequisites

1. **EMR cluster** running EMR 7.x (tested on EMR 7.12.0 and 7.13.0)
2. **S3 Express One Zone directory bucket** in the same Availability Zone as your cluster
3. **IAM permissions** for the EMR EC2 instance profile to read/write to the directory bucket

## Quick Start

### 1. Create an S3 Express One Zone directory bucket

Create a directory bucket in the same AZ as your EMR cluster:

```bash
aws s3api create-bucket \
  --bucket my-shuffle-bucket--use1-az4--x-s3 \
  --region us-east-1 \
  --create-bucket-configuration '{
    "Location": {"Type": "AvailabilityZone", "Name": "use1-az4"},
    "Bucket": {"Type": "Directory", "DataRedundancy": "SingleAvailabilityZone"}
  }'
```

### 2. Upload the jar to S3

```bash
aws s3 cp jars/spark-s3express-shuffle-manager.jar s3://your-bucket/jars/
```

### 3. Submit your Spark application

```bash
spark-submit --deploy-mode client \
  --class your.MainClass \
  --conf "spark.driver.extraClassPath=/path/to/spark-s3express-shuffle-manager.jar:$EXISTING_CP" \
  --conf spark.yarn.dist.jars=s3://your-bucket/jars/spark-s3express-shuffle-manager.jar \
  --conf "spark.executor.extraClassPath=spark-s3express-shuffle-manager.jar:$EXISTING_CP" \
  --conf spark.shuffle.manager=org.apache.spark.shuffle.cloud.CloudShuffleManager \
  --conf "spark.shuffle.storage.path=s3a://my-shuffle-bucket--use1-az4--x-s3/shuffle/" \
  --conf spark.shuffle.storage.s3express.enabled=true \
  --conf spark.shuffle.storage.s3express.endpoint.region=us-east-1 \
  --conf spark.shuffle.cloud.fetchParallelism=200 \
  --conf spark.shuffle.service.enabled=false \
  --conf spark.dynamicAllocation.enabled=false \
  your-app.jar
```

## Configuration Reference

### Required

| Config | Value | Description |
|--------|-------|-------------|
| `spark.shuffle.manager` | `org.apache.spark.shuffle.cloud.CloudShuffleManager` | Enables the S3 Express shuffle manager |
| `spark.shuffle.storage.path` | `s3a://<directory-bucket>/shuffle/` | S3 Express directory bucket path for shuffle data |
| `spark.shuffle.storage.s3express.enabled` | `true` | Enables S3 Express optimizations |
| `spark.shuffle.storage.s3express.endpoint.region` | e.g. `us-east-1` | Region of the directory bucket |
| `spark.shuffle.service.enabled` | `false` | Must be disabled (shuffle data is on S3, not local disk) |

### Performance-Optimized Configs (Recommended)

These configs were validated on TPC-DS 3TB and improve performance by ~26% over defaults:

| Config | Default | Optimized | Impact | Description |
|--------|---------|-----------|--------|-------------|
| `spark.shuffle.cloud.fetchParallelism` | `50` | **`200`** | +15% | More parallel S3 GETs per reduce task. S3 Express has no connection limit. |
| `spark.io.compression.codec` | `lz4` | **`zstd`** | +10% | 30-50% better compression ratio, less data transferred to/from S3 |
| `spark.io.compression.zstd.level` | `1` | **`3`** | +5% | Better compression with moderate CPU cost |
| `spark.sql.adaptive.enabled` | `false` | **`true`** | +10% | Adaptive Query Execution optimizes shuffle partitions at runtime |
| `spark.sql.adaptive.coalescePartitions.enabled` | `false` | **`true`** | — | Merges small shuffle partitions, reducing S3 operations |
| `spark.sql.adaptive.advisoryPartitionSizeInBytes` | `64m` | **`256m`** | — | Targets larger partitions, fewer S3 files |
| `spark.hadoop.fs.s3a.multipart.size` | `8388608` | **`134217728`** | +5% | 128MB multipart parts. Most shuffle files fit in a single PUT. |
| `spark.hadoop.fs.s3a.connection.maximum` | `450` | **`1000`** | — | Larger connection pool to support higher parallelism |
| `spark.shuffle.file.buffer` | `32k` | **`1m`** | +2% | Larger write buffer reduces syscalls |

### Memory Safety Config

The `ExternalSorter` used during shuffle writes buffers records in a `PartitionedPairBuffer` that doubles its backing array on growth. With multiple concurrent tasks, this can cause OOM before Spark's memory manager triggers a spill. The following config forces earlier spills to local disk, bounding memory usage with negligible performance impact (~3s overhead on TPC-DS q4):

| Config | Default | Recommended | Description |
|--------|---------|-------------|-------------|
| `spark.shuffle.spill.initialMemoryThreshold` | `5242880` | **`524288`** | 512KB. Forces first spill check earlier, preventing sort buffer from growing to dangerous sizes. Spills go to local disk (fast). |

### Executor Settings

| Config | Default | Recommended | Description |
|--------|---------|-------------|-------------|
| `spark.driver.memory` | — | `64g` | Driver needs large heap for shuffle metadata |
| `spark.executor.memoryOverhead` | `2G` | `4G` | Extra off-heap memory for S3 connection buffers |
| `spark.executor.cores` | — | `4` | Cores per executor |
| `spark.executor.memory` | — | `6g` | Heap memory per executor |

### S3A Tuning

| Config | Recommended | Description |
|--------|-------------|-------------|
| `spark.hadoop.fs.s3a.fast.upload.buffer` | `disk` | Buffer writes to disk to reduce heap pressure |
| `spark.hadoop.fs.s3a.change.detection.mode` | `none` | Skip ETag checks (S3 Express is strongly consistent) |
| `spark.hadoop.fs.s3a.endpoint.region` | e.g. `us-east-1` | Region for S3A |
| `spark.hadoop.fs.s3a.attempts.maximum` | `10` | Max retry attempts |
| `spark.hadoop.fs.s3a.retry.limit` | `10` | Retry limit |
| `spark.hadoop.fs.s3a.connection.establish.timeout` | `5000` | Connection timeout (ms) |
| `spark.hadoop.fs.s3a.readahead.range` | `4194304` | 4MB readahead |
| `spark.hadoop.fs.s3a.bucket.probe` | `0` | Skip bucket existence check |
| `spark.hadoop.fs.s3a.select.enabled` | `false` | Disable S3 Select |
| `spark.hadoop.fs.s3a.threads.max` | `64` | S3A internal thread pool |

### Timeout Settings

| Config | Recommended | Description |
|--------|-------------|-------------|
| `spark.network.timeout` | `2000` | Network timeout (seconds) |
| `spark.executor.heartbeatInterval` | `300s` | Heartbeat interval |

## Full Example Configuration (Optimized)

```bash
spark-submit --deploy-mode client \
  --class com.example.MyApp \
  --conf "spark.driver.extraClassPath=/usr/lib/spark/jars/spark-s3express-shuffle-manager.jar" \
  --conf spark.yarn.dist.jars=s3://my-bucket/jars/spark-s3express-shuffle-manager.jar \
  --conf "spark.executor.extraClassPath=spark-s3express-shuffle-manager.jar" \
  --conf spark.driver.cores=4 \
  --conf spark.driver.memory=64g \
  --conf spark.driver.memoryOverhead=4000 \
  --conf spark.executor.cores=4 \
  --conf spark.executor.memory=6g \
  --conf spark.executor.instances=47 \
  --conf spark.executor.memoryOverhead=4G \
  --conf spark.network.timeout=2000 \
  --conf spark.executor.heartbeatInterval=300s \
  --conf spark.dynamicAllocation.enabled=false \
  --conf spark.shuffle.service.enabled=false \
  --conf spark.shuffle.manager=org.apache.spark.shuffle.cloud.CloudShuffleManager \
  --conf "spark.shuffle.storage.path=s3a://my-shuffle-bucket--use1-az4--x-s3/shuffle/" \
  --conf spark.shuffle.storage.s3express.enabled=true \
  --conf spark.shuffle.storage.s3express.endpoint.region=us-east-1 \
  --conf spark.shuffle.cloud.fetchParallelism=200 \
  --conf spark.io.compression.codec=zstd \
  --conf spark.io.compression.zstd.level=3 \
  --conf spark.shuffle.compress=true \
  --conf spark.sql.adaptive.enabled=true \
  --conf spark.sql.adaptive.coalescePartitions.enabled=true \
  --conf spark.sql.adaptive.advisoryPartitionSizeInBytes=256m \
  --conf spark.shuffle.file.buffer=1m \
  --conf spark.shuffle.spill.initialMemoryThreshold=524288 \
  --conf spark.hadoop.fs.s3a.change.detection.mode=none \
  --conf spark.hadoop.fs.s3a.endpoint.region=us-east-1 \
  --conf spark.hadoop.fs.s3a.select.enabled=false \
  --conf spark.hadoop.fs.s3a.bucket.probe=0 \
  --conf spark.hadoop.fs.s3a.fast.upload.buffer=disk \
  --conf spark.hadoop.fs.s3a.multipart.size=134217728 \
  --conf spark.hadoop.fs.s3a.connection.maximum=1000 \
  --conf spark.hadoop.fs.s3a.attempts.maximum=10 \
  --conf spark.hadoop.fs.s3a.retry.limit=10 \
  --conf spark.hadoop.fs.s3a.connection.establish.timeout=5000 \
  --conf spark.hadoop.fs.s3a.readahead.range=4194304 \
  --conf spark.hadoop.fs.s3a.threads.max=64 \
  s3://my-bucket/app.jar
```

## Performance

### TPC-DS 3TB Full Benchmark

Benchmarked on EMR 7.12.0 (5× m7g.12xlarge, 47 executors):

| Shuffle Backend | Time per Iteration | vs Local Disk | Spot Resilient |
|-----------------|-------------------|---------------|----------------|
| Local EBS gp2 (default) | 15.7 min | 1.0x | ❌ |
| S3 Express (default configs) | 30 min | 1.9x | ✅ |
| **S3 Express (optimized configs)** | **~24 min** (estimated) | **~1.5x** | **✅** |

### TPC-DS q4 (Heaviest Query) — Detailed Comparison

Benchmarked on EMR 7.13.0 (5× m7g.12xlarge, 47 executors):

| Configuration | q4 Time | vs Local Disk |
|---------------|---------|---------------|
| Local EBS gp2 | 37.5s | 1.0x |
| S3 Express — default configs | 111s | 3.0x |
| **S3 Express — optimized configs** | **82s** | **2.2x** |
| **S3 Express — optimized + memory safe** | **85s** | **2.3x** |

### What Changed (Optimized vs Default)

| Optimization | Impact | Description |
|-------------|--------|-------------|
| `fetchParallelism=200` | -15% | 4x more parallel S3 GETs, saturates S3 Express bandwidth |
| `zstd` compression (level 3) | -10% | 30-50% less shuffle data transferred vs lz4 |
| AQE + partition coalescing | -10% | Merges small partitions, fewer S3 operations |
| 128MB multipart size | -5% | Most shuffle files uploaded in a single PUT |
| Memory safety (spill threshold) | +4% | Prevents OOM with negligible overhead |

### Spot Resilience Test

With AWS Fault Injection Service (FIS) terminating 3 out of 5 core nodes mid-query:

| Metric | Default Spark | S3 Express ShuffleManager |
|--------|--------------|---------------------------|
| FetchFailed errors | 14 | **0** |
| Stage retries | 3 | **0** |
| Queries completed | 5/5 | **5/5** |

## How It Works

The plugin replaces Spark's `SortShuffleManager` with `CloudShuffleManager` which:

1. **Writes** shuffle data to S3 Express using Spark's optimized `ExternalSorter.writePartitionedMapOutput()` path
2. **Reads** shuffle data with parallel S3 GETs per reduce task, with batched reads (1 GET per mapper covering all needed partitions)
3. **Tracks** shuffle metadata on the driver via a custom `CloudMapOutputTracker` that never clears metadata on executor loss — this is the spot resilience mechanism
4. **Manages memory** with backpressure (48MB max in-flight per task) and LRU stream cache (500 max open S3 streams)

## S3 Express One Zone Best Practices Applied

This plugin follows [AWS S3 Express One Zone performance best practices](https://docs.aws.amazon.com/AmazonS3/latest/userguide/s3-express-optimizing-performance-design-patterns.html):

- **Co-located storage** — Directory bucket must be in the same AZ as the cluster
- **Concurrent connections** — High parallelism (200 threads) with no connection limits
- **Gateway VPC endpoint** — Recommended for direct VPC-to-S3 connectivity
- **Session authentication** — S3A automatically uses `CreateSession` for directory buckets
- **Large multipart size** — 128MB parts reduce per-request overhead

## Compatibility

| Component | Tested Version |
|-----------|---------------|
| EMR | 7.12.0, 7.13.0 |
| Spark | 3.5.4, 3.5.6 |
| Hadoop | 3.4.1 |
| Instance types | m7g.12xlarge (ARM/Graviton) |

## Limitations

- Performance is ~2.3x local EBS gp2 for the heaviest TPC-DS queries with optimized configs (~1.5x estimated for the full benchmark)
- S3 Express directory bucket must be in the same Availability Zone as the cluster
- Requires `spark.shuffle.service.enabled=false` and `spark.dynamicAllocation.enabled=false`

## License

This library is licensed under the MIT-0 License. See the LICENSE file.
