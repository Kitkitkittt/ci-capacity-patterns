"""Shared, deterministic primitives for the simulations in this repository.

Everything here is standard library only and free of wall-clock or
environment dependence: given the same inputs, the same output is produced on
any machine. That property is what lets the fixtures in ``tests/`` assert
behaviour rather than merely observe it.

The module is deliberately small. It provides:

* :class:`Rng` -- a seeded xorshift64* generator, so results do not depend on
  the Python version's ``random`` implementation.
* :func:`percentile` and :class:`Series` -- tiny statistics helpers.
* :class:`InfraError` / :class:`Degraded` -- the error vocabulary that keeps
  infrastructure failures from ever being reported as success.
* JSON and report-writing helpers shared by the example scripts.
"""

from __future__ import annotations

import json
import math
import os
from dataclasses import dataclass, field
from typing import Any, Iterable, Sequence

__all__ = [
    "Degraded",
    "InfraError",
    "Rng",
    "Series",
    "percentile",
    "load_json",
    "render_report",
    "write_report",
]


class InfraError(RuntimeError):
    """An infrastructure failure that must never be reported as a pass.

    Raised for conditions where the simulation cannot make a trustworthy
    statement: a fixture that does not parse, a referenced label that no pool
    provides, an impossible configuration. Callers convert this into a
    non-zero exit status with an explicit error verdict in the report. The
    important property is that it never degrades into an empty-but-green
    result; see :class:`Degraded` for the softer variant.
    """


@dataclass
class Degraded:
    """A run that completed but whose answer is not trustworthy on its own.

    ``reasons`` is non-empty by construction. Any consumer that treats a
    degraded run as a clean pass is making the mistake this class exists to
    prevent.
    """

    reasons: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        if not isinstance(self.reasons, list) or not self.reasons:
            raise InfraError("Degraded requires at least one reason")
        for reason in self.reasons:
            if not isinstance(reason, str) or not reason.strip():
                raise InfraError("Degraded reasons must be non-empty strings")

    @property
    def is_degraded(self) -> bool:
        return True

    def as_dict(self) -> dict[str, Any]:
        return {"degraded": True, "reasons": list(self.reasons)}


class Rng:
    """Deterministic xorshift64* pseudo-random generator.

    Implemented locally rather than delegating to :mod:`random` so that the
    fixtures in ``tests/`` pin behaviour that does not move when the standard
    library changes its algorithm. Not suitable for anything security
    related, and not used for anything security related.
    """

    _MASK = (1 << 64) - 1

    def __init__(self, seed: int) -> None:
        if not isinstance(seed, int):
            raise InfraError(f"seed must be an int, got {type(seed).__name__}")
        if seed == 0:
            # xorshift64 has a zero fixed point; offset to a non-zero state.
            seed = 0x9E3779B97F4A7C15
        self._state = seed & self._MASK
        self._calls = 0

    @property
    def calls(self) -> int:
        """Number of draws taken. Used by tests to confirm determinism."""
        return self._calls

    def next_u64(self) -> int:
        x = self._state
        x ^= (x >> 12) & self._MASK
        x ^= (x << 25) & self._MASK
        x ^= (x >> 27) & self._MASK
        self._state = x & self._MASK
        self._calls += 1
        return (self._state * 0x2545F4914F6CDD1D) & self._MASK

    def random(self) -> float:
        """Uniform float in [0.0, 1.0)."""
        return self.next_u64() / float(1 << 64)

    def randint(self, low: int, high: int) -> int:
        """Uniform int in [low, high] inclusive."""
        if high < low:
            raise InfraError(f"empty range: low={low} high={high}")
        return low + int(self.random() * (high - low + 1))

    def chance(self, probability: float) -> bool:
        """True with the given probability."""
        if not 0.0 <= probability <= 1.0:
            raise InfraError(f"probability out of range: {probability}")
        return self.random() < probability

    def poisson(self, mean: float) -> int:
        """Poisson draw by Knuth's method; used for arrival counts.

        A ``mean`` of exactly zero has a degenerate but valid answer: the only
        outcome is zero events. That case is handled explicitly, because the
        general loop would otherwise return ``-1`` -- ``limit`` is ``1.0``,
        the comparison fails immediately, and ``count - 1`` goes negative. A
        negative arrival count is not a small numerical wart; it is a
        nonsensical result that would corrupt any downstream accounting.

        ``mean`` is capped so a mis-specified fixture fails loudly instead of
        spinning for a long time, and the exponential is computed in log space
        to avoid losing all precision to underflow.
        """
        if mean < 0:
            raise InfraError(f"poisson mean must be non-negative: {mean}")
        if mean > 500:
            raise InfraError(f"poisson mean too large to sample: {mean}")
        if mean == 0:
            return 0


        limit = math.exp(-mean)
        product = 1.0
        count = 0
        while product > limit:
            product *= self.random()
            count += 1
        return count - 1


class Series:
    """An append-only list of numbers with the summary statistics we report."""

    __slots__ = ("_values",)

    def __init__(self, values: Iterable[float] = ()) -> None:
        self._values: list[float] = list(values)

    def append(self, value: float) -> None:
        self._values.append(value)

    def __len__(self) -> int:
        return len(self._values)

    def __iter__(self):
        return iter(self._values)

    @property
    def values(self) -> list[float]:
        return list(self._values)

    def total(self) -> float:
        return float(sum(self._values))

    def mean(self) -> float:
        return float(sum(self._values)) / len(self._values) if self._values else 0.0

    def peak(self) -> float:
        return float(max(self._values)) if self._values else 0.0

    def percentile(self, fraction: float) -> float:
        return percentile(self._values, fraction)

    def summary(self) -> dict[str, float]:
        return {
            "count": len(self._values),
            "total": self.total(),
            "mean": self.mean(),
            "p50": self.percentile(0.50),
            "p95": self.percentile(0.95),
            "peak": self.peak(),
        }


def percentile(values: Sequence[float], fraction: float) -> float:
    """Linear-interpolation percentile over a copy of ``values``.

    Returns 0.0 for an empty input rather than raising, because every caller
    here is summarising a possibly-empty series and an empty series
    legitimately has no percentile to report.
    """
    if not 0.0 <= fraction <= 1.0:
        raise InfraError(f"fraction out of range: {fraction}")
    if not values:
        return 0.0
    ordered = sorted(values)
    if len(ordered) == 1:
        return float(ordered[0])
    position = fraction * (len(ordered) - 1)
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    weight = position - lower
    return float(ordered[lower] * (1.0 - weight) + ordered[upper] * weight)


def load_json(path: str) -> Any:
    """Read JSON from ``path``, raising :class:`InfraError` on any problem.

    A missing or malformed fixture is an infrastructure failure, not an empty
    dataset: returning ``{}`` here would let a typo silently degrade into
    "nothing to test", which is exactly the fail-open shape these examples
    exist to avoid.
    """
    if not isinstance(path, str) or not path:
        raise InfraError("load_json requires a non-empty path")
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return json.load(handle)
    except FileNotFoundError as exc:
        raise InfraError(f"fixture not found: {path}") from exc
    except json.JSONDecodeError as exc:
        raise InfraError(f"fixture is not valid JSON: {path}: {exc}") from exc
    except OSError as exc:
        raise InfraError(f"cannot read fixture {path}: {exc}") from exc


def render_report(title: str, lines: Sequence[str]) -> str:
    """Render a plain-text report with a simple underlined heading."""
    rule = "=" * len(title)
    body = "\n".join(lines)
    return f"{title}\n{rule}\n{body}\n" if body else f"{title}\n{rule}\n"


def write_report(report_dir: str, name: str, text: str) -> str:
    """Write ``text`` as ``<report_dir>/<name>`` and return the full path."""
    if not report_dir:
        raise InfraError("write_report requires a report directory")
    os.makedirs(report_dir, exist_ok=True)
    target = os.path.join(report_dir, name)
    with open(target, "w", encoding="utf-8") as handle:
        handle.write(text)
    return target
