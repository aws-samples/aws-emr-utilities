# EMR Serverless Pricing Calculator

A simple command-line tool to estimate [Amazon EMR Serverless](https://aws.amazon.com/emr/serverless/) costs based on your vCPU, memory, and storage usage.

## Features

- Supports multiple AWS regions
- Graviton and x86 architecture pricing
- Optional service discount calculation
- Detailed cost breakdown output

## Usage

```bash
python3 emr-serverless-pricing-calculator.py
```

The script will prompt you for:

1. **vCPU-hours** — total vCPU hours consumed
2. **memoryGB-hours** — total memory GB hours consumed
3. **storageGB-hours** — total storage GB hours consumed
4. **Region** — AWS region (e.g., `us-east-1`)
5. **Architecture** — `graviton` or `x86`
6. **Discount %** — service discount percentage (enter `0` for none)

## Example

```
==================================================
  EMR Serverless Pricing Calculator
==================================================

Enter vCPU-hours: 2399.311
Enter memoryGB-hours: 17994.833
Enter storageGB-hours: 138542.617

Available regions: us-east-1, us-east-2, us-west-1, us-west-2, eu-west-1, eu-central-1, ap-southeast-1, ap-northeast-1
Enter region: us-east-1
Architecture (graviton/x86): graviton

              — Cost Breakdown —
  Region: us-east-1 | Architecture: graviton
  vCPU:     2399.311 hrs × $0.042094 = $    100.99
  Memory:  17994.833 hrs × $0.004628 = $     83.28
  Storage: 138542.617 hrs × $0.000111 = $     15.38
  ------------------------------------------------
  Total Estimated Cost: $199.65

Service discount %? (0 for none): 28

  Discount (28.0%): -$55.90
  Final Cost After Discount: $143.75
```

## Supported Regions

| Region | Graviton | x86 |
|--------|----------|-----|
| us-east-1 | ✅ | ✅ |
| us-east-2 | ✅ | ✅ |
| us-west-1 | ✅ | ✅ |
| us-west-2 | ✅ | ✅ |
| eu-west-1 | ✅ | ✅ |
| eu-central-1 | ✅ | ✅ |
| ap-southeast-1 | ✅ | ✅ |
| ap-northeast-1 | ✅ | ✅ |

> **Note:** Pricing is based on the [AWS EMR Pricing page](https://aws.amazon.com/emr/pricing/). Update the `PRICING` dictionary in the script to add more regions or reflect pricing changes.

## Requirements

- Python 3.6+
- No external dependencies
