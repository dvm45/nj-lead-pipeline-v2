"""Tests for the token usage tracker."""

from njlead.token_tracker import TokenTracker


def test_basic_tracking():
    t = TokenTracker()
    t.record("opus", input_tokens=5000, output_tokens=1200)
    t.record("opus", input_tokens=4800, output_tokens=800)
    s = t.summary()
    assert s["opus"]["input"] == 9800
    assert s["opus"]["output"] == 2000
    assert s["opus"]["calls"] == 2


def test_empty_calls():
    t = TokenTracker()
    t.record("opus", input_tokens=4500, output_tokens=100, empty=True)
    t.record("opus", input_tokens=5000, output_tokens=1200, empty=False)
    s = t.summary()
    assert s["opus"]["empty_calls"] == 1
    assert s["opus"]["calls"] == 2


def test_multiple_models():
    t = TokenTracker()
    t.record("haiku", input_tokens=100, output_tokens=10)
    t.record("opus", input_tokens=5000, output_tokens=1200)
    t.record("sonnet", input_tokens=3000, output_tokens=500)
    s = t.summary()
    assert "haiku" in s
    assert "opus" in s
    assert "sonnet" in s


def test_reset():
    t = TokenTracker()
    t.record("opus", input_tokens=5000, output_tokens=1200)
    t.reset()
    assert t.summary() == {}
