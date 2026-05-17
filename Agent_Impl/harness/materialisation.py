from __future__ import annotations
from dataclasses import dataclass, field
from typing import Any, Optional


@dataclass
class MaterialisationLog:
    """
    Records what happened during fault materialisation for this trial.
    Populated by environment.py and passed into build_and_write().

    Fields are intentionally Optional — not every fault uses every field.
    e.g. fixed-wait faults (F1, F2, F4, F5) have no poll_count or threshold.

    strategy:           "fixed_wait" | "threshold_poll" | "event_poll"
    elapsed_seconds:    Wall clock time from inject → materialisation confirmed.
    poll_count:         How many polls were made (poll strategies only).
    satisfied_at_poll:  Which poll number first satisfied the condition.
    final_signal_value: The metric value that triggered completion.
    threshold:          The threshold that had to be crossed (if applicable).
    notes:              Free-text explanation of what was observed.
    """
    strategy: str
    elapsed_seconds: float
    poll_count: Optional[int] = None
    satisfied_at_poll: Optional[int] = None
    final_signal_value: Optional[Any] = None
    threshold: Optional[Any] = None
    notes: Optional[str] = None
