#!/usr/bin/env python3
"""
DEPRECATED: Use emr_recommender.py instead

This file is kept for backward compatibility only.
The new emr_recommender.py supports:
- Both local and S3 paths
- Dual-mode (cost + performance) recommendations
- All features from this script

Migration:
  OLD: python3 emr_recommender_dual_mode.py --s3-path s3://bucket/prefix/
  NEW: python3 emr_recommender.py --input-path s3://bucket/prefix/
"""

import sys

print("=" * 80)
print("DEPRECATED: emr_recommender_dual_mode.py")
print("=" * 80)
print("\nPlease use emr_recommender.py instead:")
print("  python3 emr_recommender.py --input-path <s3://bucket/prefix or /local/path>")
print("\nThe new script supports both local and S3 paths with dual-mode optimization.")
print("=" * 80)
sys.exit(1)
