#!/usr/bin/env python
"""Entry point: python run.py once | watch | dashboard | test-notify | alerts"""
import sys

from ipo_engine.cli import main

if __name__ == "__main__":
    sys.exit(main())
