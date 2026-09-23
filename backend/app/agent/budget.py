"""Hard limits for one conversational turn.

No model, no network, no database - numbers and a clock, so the thing that
bounds spend can be tested without any of them.

Every limit names itself when it trips. The reason reaches the audit row, and a
bare 'budget exhausted' with no cause is a debugging session nobody can shorten.

Exhaustion is sticky and total: once any one limit trips, the budget is dead
for the rest of the turn. A later call against a *different* counter that
still has room on its own must not be allowed to paper over an already-dead
turn, so every spend method checks the sticky state first and re-raises the
original reason rather than re-evaluating its own counter.
"""

from __future__ import annotations

import time


class BudgetExhausted(Exception):
    def __init__(self, reason: str) -> None:
        super().__init__(f"turn budget exhausted: {reason}")
        self.reason = reason


class TurnBudget:
    def __init__(
        self,
        max_iterations: int,
        max_tool_calls: int,
        max_tokens: int,
        timeout_s: float,
    ) -> None:
        self._max_iterations = max_iterations
        self._max_tool_calls = max_tool_calls
        self._max_tokens = max_tokens
        self._timeout_s = timeout_s
        self._iterations = 0
        self._tool_calls = 0
        self._tokens = 0
        self._started_at: float | None = None
        # Set to the reason on first exhaustion; once set, every later call
        # refuses with this same reason regardless of which counter it is.
        self._exhausted_reason: str | None = None

    def start(self) -> None:
        self._started_at = time.monotonic()

    @property
    def spent_tokens(self) -> int:
        return self._tokens

    def _check_alive(self) -> None:
        if self._exhausted_reason is not None:
            raise BudgetExhausted(self._exhausted_reason)

    def _trip(self, reason: str) -> None:
        self._exhausted_reason = reason
        raise BudgetExhausted(reason)

    def spend_iteration(self) -> None:
        self._check_alive()
        if self._iterations >= self._max_iterations:
            self._trip("iterations")
        self._iterations += 1

    def spend_tool_call(self) -> None:
        self._check_alive()
        if self._tool_calls >= self._max_tool_calls:
            self._trip("tool_calls")
        self._tool_calls += 1

    def spend_tokens(self, prompt: int, completion: int) -> None:
        self._check_alive()
        if self._tokens + prompt + completion > self._max_tokens:
            self._trip("tokens")
        self._tokens += prompt + completion

    def check_deadline(self) -> None:
        self._check_alive()
        # Before start() there is no turn to time out; tripping here would
        # refuse every request.
        if self._started_at is None:
            return
        if time.monotonic() - self._started_at > self._timeout_s:
            self._trip("timeout")
