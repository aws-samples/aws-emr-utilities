# EMR Serverless Config Advisor - Quick Reference

## Common Commands

### 1. Daily Production Run (Most Common)
```bash
# Process last 24 hours, generate individual cost-optimized job configs
python3 pipeline_wrapper.py \
  --input-path s3://YOUR_BUCKET/event-logs/ \
  --output-path s3://YOUR_BUCKET/staging/ \
  --output daily_recommendations.json \
  --last-hours 24 \
  --cost-optimized \
  --format-job-config \
  --individual-files \
  --limit 50
```

### 2. Weekly Performance Review
```bash
# Analyze last 7 days, both optimization modes
python3 pipeline_wrapper.py \
  --input-path s3://YOUR_BUCKET/event-logs/ \
  --output-path s3://YOUR_BUCKET/staging/ \
  --output weekly_report.json \
  --last-hours 168 \
  --limit 100
```

### 3. Quick Local Test
```bash
# Test with local event logs
python3 spark_processor.py --last-hours 24

python3 emr_recommender.py \
  --input-path ~/test_output/ \
  --output-cost /tmp/cost.json \
  --limit 10
```

---

## Feature Flags Quick Reference

| Flag | Purpose | Example |
|------|---------|---------|
| `--last-hours N` | Process only recent logs | `--last-hours 24` |
| `--cost-optimized` | Generate only cost recommendations | `--cost-optimized` |
| `--performance-optimized` | Generate only perf recommendations | `--performance-optimized` |
| `--individual-files` | Separate JSON per job | `--individual-files` |
| `--format-job-config` | Deployment-ready format | `--format-job-config` |
| `--target-partition-size N` | Shuffle partition size (MiB) | `--target-partition-size 512` |
| `--limit N` | Max applications to process | `--limit 50` |

---

## Time Filters

| Value | Duration | Use Case |
|-------|----------|----------|
| `--last-hours 1` | Last hour | Real-time monitoring |
| `--last-hours 2` | Last 2 hours | Recent job analysis |
| `--last-hours 24` | Last day | Daily optimization |
| `--last-hours 168` | Last week | Weekly review |
| `--last-hours 720` | Last month | Monthly analysis |

---

## Output Formats

### Standard Format (Default)
```bash
python3 emr_recommender.py \
  --input-path s3://bucket/metrics/ \
  --output-cost cost.json \
  --output-perf perf.json
```
**Output:** Single JSON file with all recommendations

### Individual Files
```bash
python3 emr_recommender.py \
  --input-path s3://bucket/metrics/ \
  --output-cost /output/cost.json \
  --individual-files
```
**Output:** `1-jobname.json`, `2-jobname.json`, etc.

### Job Config Format
```bash
python3 emr_recommender.py \
  --input-path s3://bucket/metrics/ \
  --output-cost cost.json \
  --format-job-config
```
**Output:** Deployment-ready EMR Serverless job configs

---

## Partition Sizing

| Size (MiB) | Effect | Command |
|------------|--------|---------|
| 256 | 4x parallelism | `--target-partition-size 256` |
| 512 | 2x parallelism | `--target-partition-size 512` |
| 1024 | Default (recommended) | `--target-partition-size 1024` |
| 2048 | Fewer executors | `--target-partition-size 2048` |

---

## Common Combinations

### Cost-Optimized Individual Configs
```bash
--cost-optimized --format-job-config --individual-files
```

### Performance-Optimized with Time Filter
```bash
--performance-optimized --last-hours 24 --limit 50
```

### Daily Production Pipeline
```bash
--last-hours 24 --cost-optimized --format-job-config --individual-files
```

---

## Troubleshooting

### No files found
- Check S3 path/permissions
- Verify time filter isn't too restrictive
- Ensure event logs exist in specified location

### Out of memory
- Reduce `--limit` value
- Process in smaller batches
- Increase EMR cluster memory

### Slow processing
- Use `--last-hours` to filter recent logs
- Reduce `--limit` value
- Check S3 region matches `--region` flag

---

## Performance Tips

1. **Use time filters** for incremental processing: `--last-hours 24`
2. **Limit applications** for faster runs: `--limit 50`
3. **Skip extraction** if metrics exist: `--skip-extraction`
4. **Use selective output** to skip unwanted mode: `--cost-optimized`
5. **Run on EMR cluster** for large-scale processing (20 parallel workers)

---

## File Locations

### Input
- Event logs: `s3://bucket/event-logs/` or `/local/path/`

### Output
- Metrics: `s3://bucket/staging/task_stage_summary/`
- Recommendations: `./recommendations_cost.json`, `./recommendations_perf.json`
- Job configs: `./recommendations_cost_job_config.json` (if `--format-job-config`)
- Individual files: `./1-jobname.json`, `./2-jobname.json`, etc. (if `--individual-files`)

---

## Quick Start

1. **Process event logs:**
   ```bash
   python3 spark_processor.py --last-hours 24
   ```

2. **Generate recommendations:**
   ```bash
   python3 emr_recommender.py \
     --input-path ~/test_output/ \
     --output-cost cost.json \
     --limit 10
   ```

3. **Or use pipeline (one command):**
   ```bash
   python3 pipeline_wrapper.py \
     --input-path s3://bucket/logs/ \
     --output-path s3://bucket/staging/ \
     --output recommendations.json \
     --last-hours 24 \
     --limit 50
   ```

---

## Need Help?

- Full documentation: See README.md
- Feature guide: README.md → Feature Guide section
- Examples: README.md → Complete Examples section
- Performance benchmarks: README.md → Performance Benchmarks section
