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


@dataclass
class EnergyConsumer:
    """Represents an energy consumer with control functions."""
    group: str
    name: str
    phase: str
    current: float

    turn_on: callable
    turn_off: callable
    can_be_delayed: callable
    consume_more: callable

    def update_current(self, current):
        """Update the current value if the new value is higher."""
        if current > self.current:
            self.current = current

