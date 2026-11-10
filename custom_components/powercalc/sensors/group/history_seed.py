"""Initial values passed from history import to the existing sensor lifecycle."""

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal

from homeassistant.core import State


@dataclass
class GroupHistorySeed:
    total: Decimal
    timestamp: datetime
    members: dict[str, State] = field(default_factory=dict)
