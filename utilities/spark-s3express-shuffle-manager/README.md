# Spark S3 Express Shuffle Manager

A drop-in Apache Spark ShuffleManager that stores shuffle data on [Amazon S3 Express One Zone](https://aws.amazon.com/s3/storage-classes/express-one-zone/) instead of local disk. Provides **spot instance resilience** (zero FetchFailed errors on executor loss) at **2.1x local EBS** overall on TPC-DS 3TB.

## Performance Summary

Benchmarked on EMR 7.13.0, TPC-DS 3TB, all 104 queries, 3 iterations each (median reported):

| Metric | Value |
|--------|-------|
| Overall (total time vs local EBS) | **2.12x** |
| Mean per-query ratio | 2.02x |
| Queries faster than local disk | 20 (19%) |
| Queries within 1.5x | 56 (54%) |
| Queries within 2x | 70 (67%) |
| Queries within 3x | 87 (84%) |
| Best query | q90 at 0.67x |

**Cluster:** 1x m7g.4xlarge (driver) + 5x m7g.12xlarge (core), 47 executors (4 cores, 6 GB each), us-east-1.

## Features

- **Spot resilience** — Shuffle data persists on S3 Express when executors are terminated. Zero FetchFailed errors, zero stage retries across all 312 query executions.
- **Performance** — 67% of TPC-DS queries complete within 2x of local EBS. 19% are *faster* than local disk.
- **Zero tuning required** — Plugin auto-configures S3A settings (`fast.upload.buffer`, `multipart.size`, `create.performance`). Only 6 required configs to enable.
- **Memory efficient** — Consumer-driven backpressure limits in-flight data to 48MB per task. Shared fetch pool across tasks prevents thread explosion.
- **Auto cleanup** — Shuffle data is automatically deleted from S3 on application completion.
- **Compatible** — Works with EMR 7.x (Spark 3.5.x). No application code changes required.

## Prerequisites

1. **EMR cluster** running EMR 7.13.0+ (tested on m7g.12xlarge Graviton3 instances)
2. **S3 Express One Zone directory bucket** in the **same Availability Zone** as your cluster
3. **IAM permissions** — EMR instance role needs `s3express:CreateSession` on the directory bucket

## Quick Start

### 1. Create an S3 Express One Zone directory bucket

```bash
# Replace use1-az4 with the AZ where your EMR cluster runs
aws s3api create-bucket \
  --bucket my-shuffle-bucket--use1-az4--x-s3 \
  --region us-east-1 \
  --create-bucket-configuration '{
    "Location": {"Type": "AvailabilityZone", "Name": "use1-az4"},
    "Bucket": {"Type": "Directory", "DataRedundancy": "SingleAvailabilityZone"}
  }'
```

> **Finding your cluster's AZ:** In the EMR console, check the cluster's "Availability Zone" field, or run:
> ```bash
> aws emr describe-cluster --cluster-id j-XXXXX \
>   --query 'Cluster.Ec2InstanceAttributes.Ec2AvailabilityZone'
> ```

### 2. Deploy the plugin JAR to all cluster nodes

```bash
# Upload the JAR to S3
aws s3 cp jars/spark-s3express-shuffle-manager.jar \
  s3://my-bucket/jars/spark-s3express-shuffle-manager.jar

# Deploy to all nodes via SSM (replace with your cluster's instance IDs)
aws ssm send-command \
  --document-name "AWS-RunShellScript" \
  --targets "Key=tag:aws:elasticmapreduce:instance-group-role,Values=CORE,MASTER" \
  --parameters 'commands=["aws s3 cp s3://my-bucket/jars/spark-s3express-shuffle-manager.jar /usr/lib/spark/jars/spark-s3express-shuffle-manager.jar"]'
```

### 3. Submit your Spark application

```bash
spark-submit --deploy-mode cluster \
  --class com.example.MyApp \
  \
  # Required plugin configs (6 settings)
  --conf spark.shuffle.manager=org.apache.spark.shuffle.cloud.CloudShuffleManager \
  --conf spark.shuffle.sort.io.plugin.class=com.amazonaws.spark.shuffle.io.cloud.ChopperPlugin \
  --conf spark.shuffle.storage.path=s3a://my-shuffle-bucket--use1-az4--x-s3/shuffle/ \
  --conf spark.shuffle.storage.s3express.enabled=true \
  --conf spark.shuffle.storage.s3express.endpoint.region=us-east-1 \
  --conf spark.shuffle.service.enabled=false \
  \
  # Performance configs (recommended)
  --conf spark.shuffle.cloud.fetchParallelism=200 \
  --conf spark.sql.files.maxPartitionBytes=536870912 \
  --conf spark.sql.adaptive.enabled=true \
  --conf spark.sql.adaptive.coalescePartitions.enabled=true \
  --conf spark.sql.adaptive.advisoryPartitionSizeInBytes=256m \
  --conf spark.io.compression.codec=zstd \
  --conf spark.io.compression.zstd.level=3 \
  --conf spark.shuffle.compress=true \
  --conf spark.shuffle.file.buffer=1m \
  --conf spark.shuffle.spill.initialMemoryThreshold=524288 \
  --conf spark.hadoop.fs.s3a.connection.maximum=1000 \
  --conf spark.hadoop.fs.s3a.threads.max=64 \
  --conf spark.executor.heartbeatInterval=300s \
  --conf spark.network.timeout=2000 \
  \
  # Your executor/driver settings
  --conf spark.executor.cores=4 \
  --conf spark.executor.memory=6g \
  --conf spark.executor.memoryOverhead=4G \
  --conf spark.driver.memory=64g \
  \
  s3://my-bucket/app.jar
```

### 4. Validate it's working

1. **Plugin loaded** — Look for `CloudShuffleManager` in driver logs
2. **Shuffle data on S3** — During the job: `aws s3 ls s3://my-shuffle-bucket--use1-az4--x-s3/shuffle/<app-id>/`
3. **Auto cleanup** — After completion, the shuffle prefix should be empty
4. **No FetchFailed** — Grep driver/executor logs for `FetchFailed` (should be zero)

## Configuration Reference

### Required (6 settings to enable)

| Config | Value | Description |
|--------|-------|-------------|
| `spark.shuffle.manager` | `org.apache.spark.shuffle.cloud.CloudShuffleManager` | Enables the S3 Express shuffle manager |
| `spark.shuffle.sort.io.plugin.class` | `com.amazonaws.spark.shuffle.io.cloud.ChopperPlugin` | I/O plugin for write sessions |
| `spark.shuffle.storage.path` | `s3a://<bucket>--<az>--x-s3/shuffle/` | S3 Express directory bucket path |
| `spark.shuffle.storage.s3express.enabled` | `true` | Enables S3 Express-specific optimizations |
| `spark.shuffle.storage.s3express.endpoint.region` | e.g. `us-east-1` | Region of the directory bucket |
| `spark.shuffle.service.enabled` | `false` | Must be disabled (shuffle data is on S3) |

### Performance Configs (Recommended)

These settings were validated on TPC-DS 3TB (3-iteration median) and collectively improve performance from 4.2x to 2.1x vs local disk:

| Config | Value | Impact | Why |
|--------|-------|--------|-----|
| `spark.sql.files.maxPartitionBytes` | `536870912` (512MB) | **Largest single improvement** | 4x fewer map tasks = 4x fewer shuffle files = 4x fewer S3 GETs per reducer |
| `spark.shuffle.cloud.fetchParallelism` | `200` | -15% latency | More parallel GETs saturate S3 Express bandwidth |
| `spark.io.compression.codec` | `zstd` | -10% | 30-50% better ratio than lz4, less data to/from S3 |
| `spark.io.compression.zstd.level` | `3` | -5% | Better compression with moderate CPU cost |
| `spark.sql.adaptive.enabled` | `true` | -10% | AQE optimizes shuffle partitions at runtime |
| `spark.sql.adaptive.coalescePartitions.enabled` | `true` | — | Merges small shuffle partitions, fewer S3 operations |
| `spark.sql.adaptive.advisoryPartitionSizeInBytes` | `256m` | — | Target size for coalesced partitions |
| `spark.hadoop.fs.s3a.connection.maximum` | `1000` | — | Supports 200 fetch threads without pool exhaustion |
| `spark.hadoop.fs.s3a.threads.max` | `64` | — | S3A internal thread pool |
| `spark.shuffle.file.buffer` | `1m` | -2% | Larger write buffer reduces syscalls |
| `spark.shuffle.compress` | `true` | — | Required for zstd to take effect |

### Stability Configs (Recommended)

| Config | Value | Why |
|--------|-------|-----|
| `spark.shuffle.spill.initialMemoryThreshold` | `524288` | Prevents OOM from sort buffer growth (512KB forces early spill check; ~3s overhead on q4) |
| `spark.executor.heartbeatInterval` | `300s` | Prevents timeout on large shuffles |
| `spark.network.timeout` | `2000` | Prevents RPC timeout on shuffle-heavy stages |
| `spark.executor.memoryOverhead` | `4G` | Off-heap memory for S3 connection buffers |
| `spark.driver.memory` | `64g` | Driver heap for shuffle metadata tracking |

### Auto-Configured by the Plugin

These are set automatically when `s3express.enabled=true`. Your explicit `spark.hadoop.*` settings always take precedence.

| Property | Auto Value | Why |
|----------|-----------|-----|
| `fs.s3a.fast.upload.buffer` | `disk` | A/B validated: gp3 absorbs spooling; bytebuffer costs more multipart requests |
| `fs.s3a.multipart.size` | `128M` | Most shuffle files fit in a single PUT |
| `fs.s3a.create.performance` | `true` | Skips HEAD+parent-dir checks on create |
| `fs.s3a.connection.maximum` | `500` (floor) | Raised if user set lower; supports fetch parallelism |
| `fs.s3a.change.detection.mode` | `none` | Directory buckets don't support ETag change detection |
| `fs.s3a.select.enabled` | `false` | S3 Select not supported on directory buckets |
| `fs.s3a.bucket.probe` | `0` | Skip bucket existence probe at FS init |

## Performance Details

### TPC-DS 3TB — Full Benchmark (3-iteration median)

EMR 7.13.0, 5x m7g.12xlarge, 47 executors, us-east-1:

| Config | Total Runtime | vs Local Disk | Spot Resilient |
|--------|--------------|---------------|----------------|
| Local EBS gp3 (baseline) | 941s (15.7 min) | 1.0x | No |
| S3 Express (default partition size) | 3,924s (65.4 min) | 4.17x | Yes |
| **S3 Express (optimized, maxPart=512MB)** | **1,999s (33.3 min)** | **2.12x** | **Yes** |

### Distribution

| Ratio Band | Count | % |
|-----------|-------|---|
| < 1.0x (faster than local) | 20 | 19% |
| 1.0x - 1.5x | 36 | 35% |
| 1.5x - 2.0x | 14 | 13% |
| 2.0x - 3.0x | 17 | 16% |
| 3.0x - 5.0x | 11 | 11% |
| > 5.0x | 6 | 6% |

### Key Queries

| Query | Local Disk | S3 Express | Ratio | Notes |
|-------|-----------|-----------|-------|-------|
| q90 | 4.6s | 3.1s | 0.67x | Faster than local (compute-bound) |
| q76 | 31.5s | 21.4s | 0.68x | Faster than local |
| q14b | 26.7s | 21.3s | 0.80x | Faster than local |
| q4 | 30.4s | 39.5s | 1.30x | Heavy shuffle (51 GB), low overhead |
| q23a | 32.5s | 58.3s | 1.79x | 12K mappers, stress test |
| q67 | 25.2s | 285.3s | 11.33x | Worst case: RANK() over 6 grouping sets |

### Why Some Queries Are Faster Than Local

Queries that are faster on S3 Express (ratio < 1.0x) are compute-bound workloads where:
- Shuffle data is small relative to compute time
- The plugin's 200-thread parallel fetch saturates the network while the CPU works
- S3 Express's high concurrency (~1ms first-byte latency) beats the serial local disk read path

### Why Some Queries Are Slower (>3x)

The 17 queries above 3x share a common pattern: high mapper-to-reducer fan-out with small per-partition payloads. Each reducer issues one ranged GET per mapper file. With 12,000 mappers, that's 12,000 HTTP round trips at ~1-3ms each. The `maxPartitionBytes=512MB` setting halves this by reducing mapper count, but the per-mapper file layout is the fundamental constraint. A planned per-partition layout (Celeborn-style) will address this.

### Spot Resilience

Across all 312 query executions (104 queries x 3 iterations):
- **Zero** FetchFailed errors
- **Zero** stage retries
- **Zero** data loss on executor termination

## How It Works

```
┌─────────────────────────────────────────────────────────────────────────┐
│ Map Task                         Reduce Task                            │
│ ┌──────────────────┐            ┌─────────────────────────────────────┐ │
│ │ ExternalSorter   │            │ Consumer-driven submission window   │ │
│ │      │           │            │ (48 MB max in-flight)               │ │
│ │      ▼           │            │      │                              │ │
│ │ S3 PUT (128MB    │            │      ▼                              │ │
│ │ multipart)       │            │ 200 parallel ranged GETs            │ │
│ └────────┬─────────┘            │ (1 GET per mapper, covers all       │ │
│          │                      │  needed partitions for that mapper)  │ │
│          ▼                      └───────────────┬─────────────────────┘ │
│ S3 Express Directory Bucket                     │                       │
│ shuffle/<appId>/shuffle_<shuffleId>_<mapId>.data │                       │
│ shuffle/<appId>/shuffle_<shuffleId>_<mapId>.index◄───────────────────────┘ │
└─────────────────────────────────────────────────────────────────────────┘

Driver: CloudMapOutputTracker
  - Tracks mapIndex → (mapId, file length, partition offsets)
  - Never clears metadata on executor loss (spot resilience)
  - Driver-side range filtering: only sends relevant offsets to each reducer
  - ~288 KB per RPC (not the full mapper×partition matrix)
```

## Troubleshooting

| Symptom | Cause | Fix |
|---------|-------|-----|
| `NoSuchBucket` on PutObject | EMRFS V1 SDK path doesn't support directory buckets | Ensure plugin is loaded (it sets `preferEmrfs=false` by default) |
| Slow first query, faster subsequent | S3A connection pool + JIT warmup | Expected. Steady-state is iterations 2+. |
| OOM on executors | Sort buffer growth before spill | Set `spark.shuffle.spill.initialMemoryThreshold=524288` |
| Timeout errors on large shuffles | Default heartbeat too short | Set `spark.executor.heartbeatInterval=300s` |
| `AccessDenied` on directory bucket | Missing S3 Express permissions | Add `s3express:CreateSession` to EMR instance role |
| `FetchFailed` / stage retries | Should not happen with this plugin | Check that `spark.shuffle.service.enabled=false` |

## Compatibility

| Component | Tested Version |
|-----------|---------------|
| EMR | 7.12.0, 7.13.0 |
| Spark | 3.5.4, 3.5.6 |
| Hadoop | 3.4.1 |
| Instance types | m7g.12xlarge, m7g.4xlarge (ARM/Graviton3) |
| Java | 17 (EMR 7.x default) |

## Limitations

- Performance is ~2.1x local EBS overall; 6 shuffle-extreme queries (6%) exceed 5x
- S3 Express directory bucket must be in the same Availability Zone as the cluster
- Requires `spark.shuffle.service.enabled=false` (no external shuffle service)
- Dynamic allocation not currently supported (planned)

## License

This library is licensed under the MIT-0 License. See the LICENSE file.
