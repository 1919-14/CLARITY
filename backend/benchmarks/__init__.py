"""Reproducible synthetic benchmarks for CLARITY components.

Benchmarks live outside the test suite because they run multiple seeds and a full
discovery loop. They never participate in discovery decisions: ground-truth
scoring happens afterwards in this evaluator-side script.
"""
