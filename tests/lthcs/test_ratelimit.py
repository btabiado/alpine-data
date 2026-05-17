"""Tests for lthcs.sources._ratelimit."""

from __future__ import annotations

import pytest

from lthcs.sources._ratelimit import TokenBucket


class FakeClock:
    def __init__(self) -> None:
        self.t = 0.0
        self.sleeps: list[float] = []

    def now(self) -> float:
        return self.t

    def sleep(self, secs: float) -> None:
        self.sleeps.append(secs)
        self.t += secs


def make_bucket(capacity: float, refill_rate: float) -> tuple[TokenBucket, FakeClock]:
    clock = FakeClock()
    bucket = TokenBucket(
        capacity=capacity,
        refill_rate=refill_rate,
        _clock=clock.now,
        _sleep=clock.sleep,
    )
    return bucket, clock


def test_starts_full() -> None:
    bucket, _ = make_bucket(capacity=5, refill_rate=1.0)
    assert bucket.tokens == pytest.approx(5.0)


def test_try_acquire_consumes() -> None:
    bucket, _ = make_bucket(capacity=3, refill_rate=0)
    assert bucket.try_acquire() is True
    assert bucket.try_acquire() is True
    assert bucket.try_acquire() is True
    assert bucket.try_acquire() is False


def test_refill_over_time() -> None:
    bucket, clock = make_bucket(capacity=10, refill_rate=2.0)
    # Drain.
    for _ in range(10):
        assert bucket.try_acquire() is True
    assert bucket.try_acquire() is False
    # Advance 3s -> 6 tokens (capped at 10).
    clock.t += 3
    assert bucket.tokens == pytest.approx(6.0)


def test_refill_capped_at_capacity() -> None:
    bucket, clock = make_bucket(capacity=5, refill_rate=10.0)
    # Drain a couple.
    bucket.try_acquire()
    bucket.try_acquire()
    # Wait long enough to refill way past capacity.
    clock.t += 100
    assert bucket.tokens == pytest.approx(5.0)


def test_acquire_blocks_then_succeeds() -> None:
    bucket, clock = make_bucket(capacity=1, refill_rate=1.0)
    bucket.try_acquire()  # drain
    assert bucket.acquire() is True
    # Should have slept ~1 second.
    assert sum(clock.sleeps) == pytest.approx(1.0, abs=1e-6)


def test_acquire_timeout_returns_false() -> None:
    bucket, clock = make_bucket(capacity=1, refill_rate=0.1)  # 1 token / 10s
    bucket.try_acquire()
    assert bucket.acquire(timeout=0.5) is False
    # Should not have consumed a token.
    clock.t += 100  # let it refill
    assert bucket.tokens == pytest.approx(1.0)


def test_wait_time_zero_when_available() -> None:
    bucket, _ = make_bucket(capacity=2, refill_rate=1.0)
    assert bucket.wait_time() == 0.0


def test_wait_time_when_empty() -> None:
    bucket, _ = make_bucket(capacity=2, refill_rate=2.0)
    bucket.try_acquire()
    bucket.try_acquire()
    assert bucket.wait_time() == pytest.approx(0.5)


def test_zero_refill_rate_blocks_forever() -> None:
    bucket, _ = make_bucket(capacity=1, refill_rate=0)
    bucket.try_acquire()
    assert bucket.wait_time() == float("inf")
    assert bucket.acquire(timeout=0.1) is False


def test_alpha_vantage_shape() -> None:
    """25 requests / day == 25 / 86400 tokens per second."""
    bucket = TokenBucket(capacity=25, refill_rate=25 / 86400)
    # Burst of 25 succeeds immediately.
    for _ in range(25):
        assert bucket.try_acquire() is True
    # 26th fails.
    assert bucket.try_acquire() is False


def test_invalid_n_rejected() -> None:
    bucket, _ = make_bucket(capacity=1, refill_rate=1.0)
    with pytest.raises(ValueError):
        bucket.try_acquire(0)
    with pytest.raises(ValueError):
        bucket.acquire(-1)
    with pytest.raises(ValueError):
        bucket.wait_time(0)


def test_invalid_capacity_rejected() -> None:
    with pytest.raises(ValueError):
        TokenBucket(capacity=0, refill_rate=1.0)
    with pytest.raises(ValueError):
        TokenBucket(capacity=1, refill_rate=-1.0)
