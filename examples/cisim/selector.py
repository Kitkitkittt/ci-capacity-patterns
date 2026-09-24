"""Test impact selection with an explicit fail-closed staleness policy.

The selector answers one question: given a set of changed files, which test
suites must run? It reads a dependency graph (source package -> suites that
cover it) and a history of recorded outcomes, and it produces a decision that
is always safe to act on.

Three rules make the decision safe. They are not optimisations; each one closes
a specific way the selector can silently skip tests.

1. **Map, do not guess.** Every changed file must resolve to at least one suite
   through the dependency graph. A changed file that resolves to nothing
   selects the full suite. Selecting nothing because a file is unmapped is the
   silent-skip bug: the pipeline reports green because no test ran.

2. **Stale means full.** A suite with no history entry, or an entry older than
   the freshness window, is selected as a full run. An old green result is not
   evidence about today's tree.

3. **Unknown is not empty.** A malformed history file is an infrastructure
   error. It must not be read as "no recorded failures, therefore nothing to
   run".

The selector never returns an empty selection for a non-empty change set. That
property is asserted directly in ``tests/test_selection.py``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

from . import InfraError

__all__ = [
    "Decision",
    "Reason",
    "Selection",
    "Selector",
    "parse_history_rows",
]

#: A history row is keyed by suite name; freshness is measured in the same
#: integer units as ``Selector.freshness_window`` (the fixtures use "hours
#: since the result was recorded").
FULL_SUITE = "__full__"


@dataclass(frozen=True)
class Reason:
    """Why a suite ended up in the selection.

    Carried through to the report so an operator can tell an intentional
    full run apart from a conservative fallback caused by missing data.
    """

    suite: str
    code: str
    detail: str

    def as_dict(self) -> dict[str, str]:
        return {"suite": self.suite, "code": self.code, "detail": self.detail}


@dataclass(frozen=True)
class Selection:
    """The selector's output for one change set."""

    changed: tuple[str, ...]
    suites: tuple[str, ...]
    reasons: tuple[Reason, ...]
    full_suite_forced: bool
    unmapped_files: tuple[str, ...]
    stale_suites: tuple[str, ...]
    missing_history_suites: tuple[str, ...]

    @property
    def is_empty(self) -> bool:
        return not self.suites

    def suite_set(self) -> set[str]:
        return set(self.suites)

    def as_dict(self) -> dict[str, Any]:
        return {
            "changed": list(self.changed),
            "suites": list(self.suites),
            "full_suite_forced": self.full_suite_forced,
            "unmapped_files": list(self.unmapped_files),
            "stale_suites": list(self.stale_suites),
            "missing_history_suites": list(self.missing_history_suites),
            "reasons": [reason.as_dict() for reason in self.reasons],
        }


@dataclass(frozen=True)
class Decision:
    """The whole outcome, including the degraded path.

    ``error`` non-empty means the run could not be trusted at all;
    ``degraded`` non-empty means it completed but rested on incomplete data.
    A consumer that ignores both fields is the bug this type exists to make
    visible.
    """

    selection: Selection | None
    error: str = ""
    degraded: tuple[str, ...] = field(default_factory=tuple)

    @property
    def ok(self) -> bool:
        return not self.error

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "error": self.error,
            "degraded": list(self.degraded),
            "selection": self.selection.as_dict() if self.selection else None,
        }


def parse_history_rows(raw: Any) -> dict[str, dict[str, Any]]:
    """Normalise a history payload into ``{suite: {"age": int, "status": str}}``.

    Accepts either a mapping keyed by suite or a list of records with a
    ``suite`` field. Anything else raises :class:`InfraError`: a history file
    we cannot parse is an infrastructure fault, and treating it as empty would
    hand the caller a silent skip.
    """
    if raw is None:
        raise InfraError("history payload is missing")
    rows: dict[str, dict[str, Any]] = {}
    if isinstance(raw, Mapping):
        items = list(raw.items())
        for suite, record in items:
            if not isinstance(suite, str) or not suite:
                raise InfraError(f"history has a non-string suite key: {suite!r}")
            if not isinstance(record, Mapping):
                raise InfraError(f"history entry for {suite!r} is not an object")
            age = record.get("age")
            if not isinstance(age, int) or isinstance(age, bool) or age < 0:
                raise InfraError(
                    f"history entry for {suite!r} needs a non-negative int 'age'"
                )
            status = record.get("status", "unknown")
            if not isinstance(status, str):
                raise InfraError(f"history entry for {suite!r} has a non-string status")
            rows[suite] = {"age": age, "status": status}
        return rows
    if isinstance(raw, Sequence) and not isinstance(raw, (str, bytes, bytearray)):
        for index, record in enumerate(raw):
            if not isinstance(record, Mapping):
                raise InfraError(f"history row {index} is not an object")
            suite = record.get("suite")
            if not isinstance(suite, str) or not suite:
                raise InfraError(f"history row {index} has no usable 'suite'")
            age = record.get("age")
            if not isinstance(age, int) or isinstance(age, bool) or age < 0:
                raise InfraError(f"history row {index} needs a non-negative int 'age'")
            status = record.get("status", "unknown")
            if not isinstance(status, str):
                raise InfraError(f"history row {index} has a non-string status")
            rows[suite] = {"age": age, "status": status}
        return rows
    raise InfraError(f"unsupported history payload type: {type(raw).__name__}")


class Selector:
    """Maps changed files to test suites, conservatively.

    Parameters
    ----------
    graph:
        ``{source_glob_or_prefix: [suite, ...]}``. Keys are matched by exact
        equality first, then by prefix on path segments, so both
        ``"api/handlers.py"`` and ``"api/"`` are usable keys.
    all_suites:
        The complete set of suite names known to exist. Needed so that the
        full-suite fallback can be expressed in terms of real suite names
        rather than an opaque sentinel.
    freshness_window:
        Maximum age, in the fixtures' integer units, for a history entry to be
        considered usable. Older entries force a full run for that suite.
    """

    def __init__(
        self,
        graph: Mapping[str, Sequence[str]],
        all_suites: Sequence[str],
        freshness_window: int,
    ) -> None:
        if not isinstance(freshness_window, int) or isinstance(freshness_window, bool):
            raise InfraError("freshness_window must be an int")
        if freshness_window < 0:
            raise InfraError("freshness_window must not be negative")
        if not graph:
            raise InfraError("dependency graph is empty")
        if not all_suites:
            raise InfraError("all_suites is empty")

        normalised: dict[str, tuple[str, ...]] = {}
        for key, suites in graph.items():
            if not isinstance(key, str) or not key:
                raise InfraError(f"dependency graph key must be a non-empty string: {key!r}")
            if not isinstance(suites, Sequence) or isinstance(suites, (str, bytes)):
                raise InfraError(f"suites for {key!r} must be a sequence of names")
            cleaned = tuple(str(s) for s in suites if isinstance(s, str) and s)
            if not cleaned:
                raise InfraError(f"dependency graph key {key!r} maps to no suites")
            normalised[key] = cleaned

        known = set(all_suites)
        for key, suites in normalised.items():
            unknown = sorted(set(suites) - known)
            if unknown:
                raise InfraError(
                    f"dependency graph key {key!r} references unknown suites: {unknown}"
                )

        self._graph = normalised
        self._all_suites = tuple(sorted(known))
        self._all_suites_set = set(self._all_suites)
        self._freshness_window = freshness_window

    # -- introspection ----------------------------------------------------

    @property
    def all_suites(self) -> tuple[str, ...]:
        return self._all_suites

    @property
    def freshness_window(self) -> int:
        return self._freshness_window

    @property
    def graph(self) -> dict[str, tuple[str, ...]]:
        return dict(self._graph)

    def suites_for_file(self, path: str) -> tuple[str, ...]:
        """Resolve one changed path to suites, longest-prefix match first.

        Exact matches win. Otherwise the longest key that is a path prefix of
        ``path`` wins, so a specific rule can override a broader one. Returns
        an empty tuple when nothing matches; the caller decides what that
        means, and in :meth:`select` it always means "run everything".
        """
        if not isinstance(path, str) or not path:
            raise InfraError("suites_for_file requires a non-empty path")
        if path in self._graph:
            return self._graph[path]
        best_key = ""
        best: tuple[str, ...] = ()
        for key, suites in self._graph.items():
            if key.endswith("/"):
                if path.startswith(key) and len(key) > len(best_key):
                    best_key, best = key, suites
            else:
                prefix = key + "/"
                if path.startswith(prefix) and len(prefix) > len(best_key):
                    best_key, best = prefix, suites
        return best

    # -- selection --------------------------------------------------------

    def select(self, changed: Sequence[str], history: Any) -> Decision:
        """Select suites for ``changed``, applying the fail-closed rules.

        Returns a :class:`Decision`. A history payload that cannot be parsed
        produces ``error`` with no selection at all, rather than a degraded
        guess, because a selector running on a corrupt history is not making a
        smaller claim -- it is making an unsupported one.
        """
        if not changed:
            raise InfraError("select requires a non-empty change set")

        try:
            rows = parse_history_rows(history)
        except InfraError as exc:
            return Decision(selection=None, error=str(exc))

        changed_tuple = tuple(sorted({str(c) for c in changed if str(c)}))
        if not changed_tuple:
            raise InfraError("select requires at least one non-empty path")

        selected: dict[str, Reason] = {}
        unmapped: list[str] = []
        forced_full = False

        for path in changed_tuple:
            suites = self.suites_for_file(path)
            if not suites:
                unmapped.append(path)
                forced_full = True
                continue
            for suite in suites:
                if suite in selected:
                    continue
                selected[suite] = Reason(
                    suite=suite,
                    code="changed",
                    detail=f"mapped from {path}",
                )

        # Rule 2: freshness, evaluated before widening. A suite whose history
        # is stale or absent cannot be trusted to be *correctly narrow*: the
        # history that justified leaving its neighbours out is exactly the
        # history we do not have. So an impacted stale/missing suite escalates
        # the whole selection to every known suite.
        #
        # This is the difference that makes "stale means full" real. Merely
        # relabelling an already-selected suite would leave the selected set
        # identical to the fresh case, so the policy would change nothing that
        # a caller could observe.
        stale: list[str] = []
        missing: list[str] = []
        for suite in sorted(selected):
            row = rows.get(suite)
            if row is None:
                missing.append(suite)
                selected[suite] = Reason(
                    suite=suite,
                    code="missing-history",
                    detail=f"{selected[suite].detail}; no recorded history",
                )
                continue
            if row["age"] > self._freshness_window:
                stale.append(suite)
                selected[suite] = Reason(
                    suite=suite,
                    code="stale-history",
                    detail=(
                        f"{selected[suite].detail}; history age {row['age']} exceeds "
                        f"freshness window {self._freshness_window}"
                    ),
                )

        needs_widening = bool(stale or missing or unmapped)
        if needs_widening:
            for suite in self._all_suites:
                if suite in selected:
                    existing = selected[suite]
                    selected[suite] = Reason(
                        suite=suite,
                        code=existing.code + "+widened",
                        detail=(
                            existing.detail
                            + "; selection widened to the full suite because "
                            + _widening_cause(unmapped, stale, missing)
                        ),
                    )
                    continue
                selected[suite] = Reason(
                    suite=suite,
                    code="widened-full-suite",
                    detail=(
                        "selected because "
                        + _widening_cause(unmapped, stale, missing)
                    ),
                )

        selection = Selection(
            changed=changed_tuple,
            suites=tuple(sorted(selected)),
            reasons=tuple(selected[s] for s in sorted(selected)),
            full_suite_forced=forced_full or needs_widening,
            unmapped_files=tuple(unmapped),
            stale_suites=tuple(stale),
            missing_history_suites=tuple(missing),
        )

        degraded: list[str] = []
        if stale:
            degraded.append(
                f"{len(stale)} impacted suite(s) had stale history; selection "
                "widened to the full suite: " + ", ".join(stale)
            )
        if missing:
            degraded.append(
                f"{len(missing)} impacted suite(s) had no recorded history; "
                "selection widened to the full suite: " + ", ".join(missing)
            )
        if unmapped:
            degraded.append(
                f"{len(unmapped)} changed file(s) unmapped; full suite selected: "
                + ", ".join(unmapped)
            )

        return Decision(selection=selection, degraded=tuple(degraded))


def _widening_cause(
    unmapped: Sequence[str], stale: Sequence[str], missing: Sequence[str]
) -> str:
    """Human-readable reason the selection escalated to the full suite."""
    causes: list[str] = []
    if unmapped:
        causes.append("changed file(s) with no dependency-graph entry: " + ", ".join(unmapped))
    if stale:
        causes.append("impacted suite(s) with stale history: " + ", ".join(stale))
    if missing:
        causes.append("impacted suite(s) with no recorded history: " + ", ".join(missing))
    return "; ".join(causes) if causes else "an unknown reason"
