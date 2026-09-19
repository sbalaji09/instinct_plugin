#!/usr/bin/env python3
"""Permission + setup checker. Run with: uv run scripts/doctor.py"""

import sys

from instinct.doctor import main

if __name__ == "__main__":
    sys.exit(main())
