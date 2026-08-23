from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

import pytest

from core.outlook_email_mailbox import OutlookEmailMailbox


def _mailbox(**kwargs) -> OutlookEmailMailbox:
    return OutlookEmailMailbox(
        api_url="https://mailbox.test",
        api_key="test-key",
        **kwargs,
    )


def test_concurrent_workers_reserve_distinct_outlook_accounts(monkeypatch):
    mailbox = _mailbox()
    rows = [
        {"id": index, "email": f"worker-{index}@outlook.com", "status": "active"}
        for index in range(1, 6)
    ]
    monkeypatch.setattr(mailbox, "_list_accounts", lambda: list(rows))

    with ThreadPoolExecutor(max_workers=5) as pool:
        allocated = list(pool.map(lambda _index: mailbox.get_email().email, range(5)))

    assert len(set(allocated)) == 5


def test_fixed_outlook_account_cannot_be_leased_twice_in_one_task():
    mailbox = _mailbox(fixed_email="fixed@outlook.com")

    assert mailbox.get_email().email == "fixed@outlook.com"
    with pytest.raises(RuntimeError, match="另一个 worker 占用"):
        mailbox.get_email()
