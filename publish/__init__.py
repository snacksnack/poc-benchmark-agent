"""Render this repository's committed documents as a static site (ADR 0027).

Separate from `pocbench` on purpose: it is the only code here that needs a
markdown renderer (the `site` dependency group), and it computes nothing.
Every page it publishes is a committed document that CI already regenerates
and diffs, or the hand-written writeup whose figures CI re-derives, so a
published page cannot disagree with the repository it came from.
"""
