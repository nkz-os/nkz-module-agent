import time

import pytest

from app.agent.budget import BudgetExhausted, TurnBudget


def test_iterations_are_capped():
    b = TurnBudget(max_iterations=2, max_tool_calls=9, max_tokens=9999, timeout_s=99)
    b.spend_iteration()
    b.spend_iteration()
    with pytest.raises(BudgetExhausted) as e:
        b.spend_iteration()
    assert e.value.reason == "iterations"


def test_tool_calls_are_capped():
    b = TurnBudget(max_iterations=9, max_tool_calls=1, max_tokens=9999, timeout_s=99)
    b.spend_tool_call()
    with pytest.raises(BudgetExhausted) as e:
        b.spend_tool_call()
    assert e.value.reason == "tool_calls"


def test_tokens_accumulate_and_are_capped():
    b = TurnBudget(max_iterations=9, max_tool_calls=9, max_tokens=100, timeout_s=99)
    b.spend_tokens(40, 40)
    assert b.spent_tokens == 80
    with pytest.raises(BudgetExhausted) as e:
        b.spend_tokens(30, 0)
    assert e.value.reason == "tokens"


def test_deadline_trips_after_timeout():
    b = TurnBudget(max_iterations=9, max_tool_calls=9, max_tokens=9999, timeout_s=0)
    b.start()
    time.sleep(0.01)
    with pytest.raises(BudgetExhausted) as e:
        b.check_deadline()
    assert e.value.reason == "timeout"


def test_deadline_does_not_trip_before_start():
    """A budget that trips before the turn begins would refuse every request."""
    b = TurnBudget(max_iterations=9, max_tool_calls=9, max_tokens=9999, timeout_s=0)
    b.check_deadline()  # must not raise


def test_exhaustion_names_which_limit():
    """The reason is written to the audit row, so a vague one costs a debugging
    session later: 'budget_exhausted' with no cause cannot be acted on."""
    b = TurnBudget(max_iterations=1, max_tool_calls=1, max_tokens=1, timeout_s=99)
    b.spend_iteration()
    with pytest.raises(BudgetExhausted) as e:
        b.spend_iteration()
    assert e.value.reason in {"iterations", "tool_calls", "tokens", "timeout"}


def test_exhaustion_is_sticky_across_counters():
    """Exhausting one limit must refuse spends against a DIFFERENT counter
    that still has room of its own — otherwise a loop that keeps calling a
    still-under-budget dimension (tool calls) could ride past a turn whose
    iteration cap already tripped."""
    b = TurnBudget(max_iterations=1, max_tool_calls=9999, max_tokens=9999, timeout_s=99)
    b.spend_iteration()
    with pytest.raises(BudgetExhausted) as e:
        b.spend_iteration()
    assert e.value.reason == "iterations"

    # tool_calls has ample room on its own — a non-sticky implementation
    # would let this succeed.
    with pytest.raises(BudgetExhausted) as e:
        b.spend_tool_call()
    assert e.value.reason == "iterations"

    with pytest.raises(BudgetExhausted) as e:
        b.spend_tokens(1, 1)
    assert e.value.reason == "iterations"


def test_exhaustion_is_sticky_for_deadline_check():
    """A counter exhaustion must also close off the deadline check — even
    one called before start(), which alone would return silently."""
    b = TurnBudget(max_iterations=9999, max_tool_calls=1, max_tokens=9999, timeout_s=99)
    b.spend_tool_call()
    with pytest.raises(BudgetExhausted) as e:
        b.spend_tool_call()
    assert e.value.reason == "tool_calls"

    # No start() was ever called; without stickiness this would return
    # normally (see test_deadline_does_not_trip_before_start).
    with pytest.raises(BudgetExhausted) as e:
        b.check_deadline()
    assert e.value.reason == "tool_calls"


def test_timeout_exhaustion_blocks_later_counter_spends():
    """A deadline trip must also close off counters that still have room."""
    b = TurnBudget(max_iterations=9999, max_tool_calls=9999, max_tokens=9999, timeout_s=0)
    b.start()
    time.sleep(0.01)
    with pytest.raises(BudgetExhausted) as e:
        b.check_deadline()
    assert e.value.reason == "timeout"

    with pytest.raises(BudgetExhausted) as e:
        b.spend_iteration()
    assert e.value.reason == "timeout"
