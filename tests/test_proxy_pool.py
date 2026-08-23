from __future__ import annotations

from sqlmodel import Session

from core.db import ProxyModel, engine
from core.proxy_pool import ProxyPool


def _seed_proxies(count: int, *, proven_index: int | None = None, region: str = "US") -> list[str]:
    urls = [
        f"http://user-{index}:pass@gateway-{index}.example:10000"
        for index in range(1, count + 1)
    ]
    with Session(engine) as session:
        for index, url in enumerate(urls):
            session.add(
                ProxyModel(
                    url=url,
                    region=region,
                    success_count=8 if index == proven_index else 0,
                )
            )
        session.commit()
    return urls


def _static_pool(monkeypatch) -> ProxyPool:
    pool = ProxyPool()
    monkeypatch.setattr(pool, "_dynamic_proxy", lambda: "")
    return pool


def test_round_robin_does_not_exclude_unproven_imports(monkeypatch):
    expected = set(_seed_proxies(10, proven_index=5))
    pool = _static_pool(monkeypatch)

    assigned = [pool.get_next("US") for _ in range(10)]

    assert set(assigned) == expected
    assert len(set(assigned)) == 10


def test_concurrent_leases_spread_before_reusing_a_proxy(monkeypatch):
    _seed_proxies(4)
    pool = _static_pool(monkeypatch)

    leases = [pool.acquire_next("US") for _ in range(4)]
    reused = pool.acquire_next("US")

    assert all(leases)
    assert len(set(leases)) == 4
    assert reused == leases[0]

    for url in [*leases, reused]:
        pool.release(url)
    assert pool._lease_counts == {}


def test_typed_proxy_failure_temporarily_rotates_away(monkeypatch):
    _seed_proxies(2)
    pool = _static_pool(monkeypatch)

    failed = pool.acquire_next("US")
    pool.release(failed)
    pool.report_fail(failed)
    replacement = pool.acquire_next("US")

    assert failed
    assert replacement
    assert replacement != failed
    pool.release(replacement)


def test_region_match_is_preferred_but_other_regions_are_a_fallback(monkeypatch):
    us_url = "http://user-us:pass@gateway-us.example:10000"
    jp_url = "http://user-jp:pass@gateway-jp.example:10000"
    with Session(engine) as session:
        session.add(ProxyModel(url=us_url, region="US"))
        session.add(ProxyModel(url=jp_url, region="JP"))
        session.commit()

    pool = _static_pool(monkeypatch)

    assert pool.get_next("JP") == jp_url
    assert pool.get_next("BR") in {us_url, jp_url}
