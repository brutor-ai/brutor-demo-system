"""Brutor Demo System: loan pre-screening agent.

A LangGraph graph with eight fixed nodes. Every governed action (LLM, MCP,
skill, A2A) goes through the Brutor gateway and carries the run, turn and
step headers described in ../DESIGN.md sections 4 and 8.

The release version lives only in pyproject.toml; the running code reads it
from the installed distribution (identity.py, RFC 0023).
"""
