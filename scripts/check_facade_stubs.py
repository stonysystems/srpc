#!/usr/bin/env python3
"""Reject missing runtime behavior, including unconditional facade panics."""
from facade_audit import main

if __name__ == "__main__":
    raise SystemExit(main())
