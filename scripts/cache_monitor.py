"""Host-side monitor: alerts on macOS when a Claude Code turn misses the prompt cache."""
from dataclasses import dataclass


@dataclass(frozen=True)
class Event:
    cache_read: int
    cache_creation: int
    input_tokens: int


def hit_rate(event):
    return None
