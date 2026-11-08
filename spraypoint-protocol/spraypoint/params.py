"""Spraypoint protocol parameters (p, h, l) and the shared hashing key."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Optional


def compute_levels_count(n: int, d: int, p: int) -> int:
    """Return l = max(1, ceil(log_p(n / (2 d^2)))) from the Spraypoint paper."""
    if n <= 0 or d <= 0:
        raise ValueError("n and d must be positive")
    if p < 2:
        # log base 1 (or lower) is undefined; a single level is the only option.
        return 1
    ratio = n / (2.0 * d * d)
    if ratio <= 1.0:
        return 1
    return max(1, math.ceil(math.log(ratio, p)))


@dataclass(frozen=True)
class SpraypointParams:
    """Configuration shared by every switch.

    p:        waypoints selected by each node of WP_{m-1} when building WP_m.
    h:        eligible next hops kept per pointing decision (ECMP width).
    l:        number of higher waypoint levels. ``None`` derives it from the
              fabric size with :func:`compute_levels_count`.
    hash_key: shared key for deterministic "random" choices. Every switch
              uses the same key, so all of them compute the same pointing
              graph, which is what keeps forwarding loop-free.
    """

    p: int = 2
    h: int = 1
    l: Optional[int] = None
    hash_key: str = "spraypoint"

    def __post_init__(self) -> None:
        if self.p < 1:
            raise ValueError("p must be >= 1")
        if self.h < 1:
            raise ValueError("h must be >= 1")
        if self.l is not None and self.l < 0:
            raise ValueError("l must be >= 0")

    def resolve_l(self, n: int, d: int) -> int:
        if self.l is not None:
            return self.l
        return compute_levels_count(n, d, self.p)
