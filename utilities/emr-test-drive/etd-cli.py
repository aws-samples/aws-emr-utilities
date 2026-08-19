#!/usr/bin/env python3
"""Convenience wrapper so you can run ./etd.py without installing anything."""
import sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).parent))
from etd.cli import main
raise SystemExit(main())
