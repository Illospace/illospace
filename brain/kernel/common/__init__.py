"""Small shared primitives used across Brain modules.

Keep this package dependency-light.  Helpers here should be deterministic,
stdlib-only, and narrow enough to prevent feature modules from growing local
copies of the same coercion, serialization, environment, and time utilities.
"""
