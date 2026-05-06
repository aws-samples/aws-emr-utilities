# HBase Benchmark

Run HBase benchmarks on Amazon EMR as a single EMR step.

## Supported Benchmarks

| Benchmark | Description |
|-----------|-------------|
| **YCSB** | Yahoo! Cloud Serving Benchmark — workloads A–F against HBase |

## Setup (one-time)

Copy the benchmark script to your S3 bucket:

```bash
aws s3 cp benchmarks/ycsb/run.sh s3://YOUR-BUCKET/scripts/hbase-benchmark/ycsb/run.sh
```

## Run Benchmark

```bash
aws emr add-steps \
  --cluster-id j-XXXXXXXXXXXXX \
  --steps '[{
    "Type": "CUSTOM_JAR",
    "Name": "ycsb-benchmark",
    "Jar": "command-runner.jar",
    "Args": [
      "bash", "-c",
      "aws s3 cp s3://YOUR-BUCKET/scripts/hbase-benchmark/ycsb/run.sh /tmp/ycsb_run.sh && chmod +x /tmp/ycsb_run.sh && bash /tmp/ycsb_run.sh --record-count 1000000 --operation-count 200000 --threads 64 --s3-results-path s3://YOUR-BUCKET/ycsb-results/$(date +%Y%m%d-%H%M%S)"
    ],
    "ActionOnFailure": "CONTINUE"
  }]'
```

Replace:
- `j-XXXXXXXXXXXXX` with your EMR cluster ID
- `YOUR-BUCKET` with your S3 bucket

### Parameters

| Parameter | Default | Description |
|-----------|---------|-------------|
| `--record-count` | 1,000,000 | Total records to load |
| `--operation-count` | 200,000 | Operations per workload |
| `--threads` | 64 | Concurrent client threads |
| `--n-splits` | 50 | HBase region pre-splits (10 × worker nodes) |
| `--s3-results-path` | _(none)_ | S3 path for results upload |

### Results

```
s3://YOUR-BUCKET/ycsb-results/20260311-190500/
├── load_batch_*.txt
├── workloada.txt      # 50% read, 50% update
├── workloadb.txt      # 95% read, 5% update
├── workloadc.txt      # 100% read
├── workloadd.txt      # 95% read, 5% insert (read latest)
├── workloade.txt      # 95% scan, 5% insert
└── workloadf.txt      # 50% read, 50% read-modify-write
```

## Prerequisites

- EMR cluster with HBase installed
- Cluster EC2 instance profile needs `s3:GetObject` (script) and `s3:PutObject` (results) on the bucket
- Cluster must have internet access (to clone YCSB from GitHub on first run)
