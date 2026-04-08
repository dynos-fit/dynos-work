"""Standalone autofix scanner for dynos-work.

Detects technical debt across six categories and routes findings through
a risk-based pipeline: low/medium actionable findings get auto-fixed via
the dynos-work foundry pipeline, while high/critical findings open GitHub
issues for human review.
"""
