"""Data models for the energy manager system."""

from dataclasses import dataclass


@dataclass
class AdditionalConsumer:
    """Represents an additional energy consumer with stage information."""
    stage: int
    usage: float
    real_usage: float


@dataclass
class VirtualEntity:
    """Represents a virtual entity with switch state and event handlers."""
    switched: bool
    events: dict

