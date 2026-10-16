import rnsh.helpers as helpers


class _FakeClock:
    def __init__(self, now: float):
        self.now = now

    def __call__(self) -> float:
        return self.now


def _sleeper(monkeypatch, period: float, start: float = 1000.0):
    clock = _FakeClock(start)
    monkeypatch.setattr(helpers.time, "time", clock)
    return helpers.SleepRate(period), clock


def test_sleep_rate_honors_target_period(monkeypatch):
    # Regression for the hard-coded 0.01 that ignored target_period (fixed in #42).
    sleeper, clock = _sleeper(monkeypatch, 0.25)
    clock.now += 0.01
    assert sleeper.next_sleep_time() == pytest_approx(0.24)


def test_sleep_rate_late_caller_does_not_sleep(monkeypatch):
    sleeper, clock = _sleeper(monkeypatch, 0.25)
    clock.now += 0.5
    assert sleeper.next_sleep_time() == 0


def test_sleep_rate_measures_from_previous_call(monkeypatch):
    # The period is counted from the previous call, not from the previous wake:
    # a call exactly one period after the last one owes no sleep, and the call
    # right after that owes a full period again.
    sleeper, clock = _sleeper(monkeypatch, 0.1)
    assert sleeper.next_sleep_time() == pytest_approx(0.1)
    clock.now += 0.1
    assert sleeper.next_sleep_time() == 0
    assert sleeper.next_sleep_time() == pytest_approx(0.1)


def test_sleep_rate_short_period(monkeypatch):
    sleeper, clock = _sleeper(monkeypatch, 0.01)
    clock.now += 0.004
    assert sleeper.next_sleep_time() == pytest_approx(0.006)


def pytest_approx(value: float):
    import pytest
    return pytest.approx(value, abs=1e-9)
