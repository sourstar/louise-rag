"""Signed vector bundles — the derived plane (architecture-v2.md §5).

A bundle lets one node import vectors another node computed, instead of
spending its own GPU-hours. Import is an alternative implementation of the
`embed` stage, so it inherits resumability and the per-document commit
boundary for free.

Read format.py's module docstring before changing anything here: the wire
format's security properties depend on rules that are easy to undo by
accident (chiefly "never re-serialize the header to verify it").
"""
from .format import (BundleError, BundleFormatError, BundleHeader,
                      BundleSignatureError, parse_header, parse_possession)

__all__ = [
    "BundleError", "BundleFormatError", "BundleHeader", "BundleSignatureError",
    "parse_header", "parse_possession",
]
