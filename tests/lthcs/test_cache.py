"""Tests for lthcs.sources._cache."""

from __future__ import annotations

from pathlib import Path

import pytest

from lthcs.sources._cache import FileCache


@pytest.fixture()
def cache(tmp_path: Path) -> FileCache:
    return FileCache("test_source", root=tmp_path)


def test_miss_when_empty(cache: FileCache) -> None:
    assert cache.get("nope") is None


def test_roundtrip(cache: FileCache) -> None:
    payload = {"price": 123.45, "ticker": "AAPL"}
    cache.set("AAPL/quote", payload, ttl_seconds=60, now=1000.0)
    hit = cache.get("AAPL/quote", now=1010.0)
    assert hit is not None
    assert hit.value == payload
    assert hit.age_seconds == pytest.approx(10.0)
    assert hit.fetched_at == 1000.0


def test_expiry(cache: FileCache) -> None:
    cache.set("AAPL/quote", {"x": 1}, ttl_seconds=60, now=1000.0)
    # Still inside TTL.
    assert cache.get("AAPL/quote", now=1059.0) is not None
    # Past TTL.
    assert cache.get("AAPL/quote", now=1061.0) is None


def test_ttl_zero_never_expires(cache: FileCache) -> None:
    cache.set("forever", {"x": 1}, ttl_seconds=0, now=0.0)
    assert cache.get("forever", now=10**9) is not None


def test_negative_ttl_rejected(cache: FileCache) -> None:
    with pytest.raises(ValueError):
        cache.set("x", 1, ttl_seconds=-1)


def test_keys_with_path_chars_safe(cache: FileCache, tmp_path: Path) -> None:
    nasty = "AAPL/../../../etc/passwd?foo=bar"
    cache.set(nasty, "ok", ttl_seconds=60, now=0.0)
    hit = cache.get(nasty, now=0.0)
    assert hit is not None and hit.value == "ok"
    # Files only ever land under the cache root.
    for f in tmp_path.rglob("*"):
        assert f.is_dir() or tmp_path in f.parents


def test_distinct_keys_distinct_entries(cache: FileCache) -> None:
    cache.set("AAPL", "apple", ttl_seconds=60, now=0.0)
    cache.set("AAPL/quote", "quote", ttl_seconds=60, now=0.0)
    assert cache.get("AAPL", now=0.0).value == "apple"
    assert cache.get("AAPL/quote", now=0.0).value == "quote"


def test_delete(cache: FileCache) -> None:
    cache.set("k", 1, ttl_seconds=60, now=0.0)
    assert cache.delete("k") is True
    assert cache.delete("k") is False
    assert cache.get("k") is None


def test_clear(cache: FileCache) -> None:
    for i in range(5):
        cache.set(f"k{i}", i, ttl_seconds=60, now=0.0)
    assert cache.clear() == 5
    assert cache.get("k0") is None


def test_corrupt_file_treated_as_miss(cache: FileCache) -> None:
    cache.set("k", 1, ttl_seconds=60, now=0.0)
    # Corrupt the on-disk file.
    path = cache._path("k")
    path.write_text("{ not json")
    assert cache.get("k", now=0.0) is None


def test_two_sources_isolated(tmp_path: Path) -> None:
    a = FileCache("source_a", root=tmp_path)
    b = FileCache("source_b", root=tmp_path)
    a.set("shared_key", "a_value", ttl_seconds=60, now=0.0)
    assert b.get("shared_key", now=0.0) is None
