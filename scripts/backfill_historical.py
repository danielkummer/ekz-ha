#!/usr/bin/env python3
"""
Historical data backfill helper script.

Due to the complexity of automating period navigation in the EKZ portal,
this provides a simple framework for manual historical data collection.

Usage:
    docker compose run --rm ekz-scraper python scripts/backfill_historical.py

Process:
1. Run this script to scrape current period data
2. Manually navigate to previous period in the EKZ portal
3. Re-run this script to scrape that period
4. Repeat for desired history depth

For automated backfill, significant portal navigation complexity would be
required (~270 lines), adding state management, duplicate detection, and
increasing maintenance burden. The manual approach keeps the codebase simple.
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

print("\n" + "=" * 60)
print("EKZ Historical Data Backfill Helper")
print("=" * 60)
print("\nManual Backfill Process:")
print("  1. Run the main scraper to capture current period")
print("  2. Manually navigate to previous period in EKZ portal")
print("  3. Re-run the scraper for that period")
print("  4. Repeat for desired history depth")
print("\nTo run a scrape:")
print("  docker compose run --rm ekz-scraper python -m scraper.main")
print("\nFor automated backfill, see scripts/README.md for alternatives.")
print("=" * 60 + "\n")

sys.exit(0)
