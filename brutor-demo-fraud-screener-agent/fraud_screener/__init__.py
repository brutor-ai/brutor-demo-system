"""Brutor Demo System: mock Fraud and Sanctions Screener (A2A remote agent).

See ../DESIGN.md section 5.4. It is a mock: a synthetic sanctions list, two
velocity heuristics and one model call through the Brutor gateway that
carries the caller's signed delegation chain.

The release version lives only in pyproject.toml; the running code reads it
from the installed distribution (identity.py, RFC 0023).
"""
