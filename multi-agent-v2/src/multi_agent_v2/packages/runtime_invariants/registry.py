from __future__ import annotations

import re
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass


class InvariantViolation(RuntimeError):
    code = "INVARIANT"

    def __init__(self, owner: str, message: str) -> None:
        self.owner = owner
        super().__init__(f'invariant violated by "{owner}": {message}')


@dataclass(frozen=True, slots=True)
class InvariantRegistration:
    owner: str
    check: Callable[[], Awaitable[None]]


class InvariantRegistry:
    def __init__(
        self,
        *,
        enabled: bool = True,
        allowlist: Iterable[str] = (),
        blocklist: Iterable[str] = (),
    ) -> None:
        self._enabled = enabled
        self._allowlist = _patterns(allowlist)
        self._blocklist = _patterns(blocklist)
        self._registrations: dict[str, InvariantRegistration] = {}

    def register(
        self,
        owner: str,
        check: Callable[[], Awaitable[None]],
    ) -> Callable[[], None]:
        normalized = owner.strip()
        if not normalized or any(character.isspace() for character in normalized):
            raise ValueError("invariant owner must be a non-blank token")
        if normalized in self._registrations:
            raise ValueError(f"invariant owner is already registered: {normalized}")
        self._registrations[normalized] = InvariantRegistration(normalized, check)

        def dispose() -> None:
            self._registrations.pop(normalized, None)

        return dispose

    async def check_all(self) -> None:
        if not self._enabled:
            return
        for registration in tuple(self._registrations.values()):
            if not self._selected(registration.owner):
                continue
            try:
                await registration.check()
            except InvariantViolation:
                raise
            except Exception as exc:
                raise InvariantViolation(registration.owner, str(exc)) from exc

    def fail(self, owner: str, message: str) -> None:
        raise InvariantViolation(owner, message)

    def _selected(self, owner: str) -> bool:
        if any(pattern.search(owner) for pattern in self._blocklist):
            return False
        return not self._allowlist or any(pattern.search(owner) for pattern in self._allowlist)


def assert_monotonic_sequence(owner: str, sequences: Iterable[int]) -> None:
    previous = 0
    for sequence in sequences:
        if sequence != previous + 1:
            raise InvariantViolation(owner, "event sequence is not contiguous")
        previous = sequence


def assert_single_terminal(owner: str, statuses: Iterable[str]) -> None:
    terminal = {"succeeded", "failed", "timed_out", "cancelled"}
    if sum(status in terminal for status in statuses) > 1:
        raise InvariantViolation(owner, "execution contains multiple terminal outcomes")


def assert_terminal_transition(owner: str, previous: str, current: str) -> None:
    terminal = {
        "succeeded",
        "failed",
        "timed_out",
        "cancelled",
        "reconciliation_required",
    }
    if previous in terminal and current != previous:
        raise InvariantViolation(owner, f"terminal state {previous} changed to {current}")


def _patterns(values: Iterable[str]) -> tuple[re.Pattern[str], ...]:
    patterns: list[re.Pattern[str]] = []
    seen: set[str] = set()
    for value in values:
        if not value or value != value.strip() or value in seen:
            raise ValueError("invariant patterns must be unique non-blank trimmed strings")
        try:
            patterns.append(re.compile(value))
        except re.error as exc:
            raise ValueError(f"invalid invariant pattern: {value}") from exc
        seen.add(value)
    return tuple(patterns)
