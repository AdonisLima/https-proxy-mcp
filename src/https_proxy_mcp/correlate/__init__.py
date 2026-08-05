"""Correlation layer: Steps and Exchanges into a Timeline.

Implements owner-plus-overlap attribution (see docs/adr/0004-overlap-attribution.md):
an Exchange is owned by the Step active when it started, and every later Step it
overlaps is recorded. Depends only on the Step contract, never on a test tool.
"""
