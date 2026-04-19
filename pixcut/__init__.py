"""
Minimal PixCut host-side toolkit.

Modules are intentionally small so we can tweak transport, framing, and
job orchestration independently while we learn more about the device.
"""

__all__ = [
    "transport",
    "framing",
    "logging_utils",
    "orchestrator",
    "svg_to_plt",
]
