#!/usr/bin/env python3
"""Reject SRPC implementation shadows throughout the Rust facade AST."""
from facade_audit import main

if __name__ == "__main__":
    raise SystemExit(main())
