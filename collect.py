"""Collect events from every source into data/events.json.

    python collect.py                  # all sources
    python collect.py --only edge      # just some (others keep their saved data)
    python collect.py --dry-run        # report only, don't write
"""
import sys

from collector.run import main

if __name__ == "__main__":
    sys.exit(main())
