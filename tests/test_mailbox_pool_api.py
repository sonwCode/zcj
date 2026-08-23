import json

from api.mailbox_pool import MailboxPoolInventoryRequest, MailboxPoolSplitRequest
from api.mailbox_pool import inspect_mailbox_pool, split_unused_mailboxes


def test_split_unused_api_reports_blocked_rows(tmp_path):
    state_file = tmp_path / "state.json"
    state_file.write_text(
        json.dumps(
            {
                "blocked": {
                    "used@hotmail.com": {
                        "email": "used@hotmail.com",
                        "reason_code": "already_registered",
                    }
                }
            }
        ),
        encoding="utf-8",
    )

    result = split_unused_mailboxes(
        MailboxPoolSplitRequest(
            text=(
                "used@hotmail.com----https://example.test/used\n"
                "fresh@hotmail.com----https://example.test/fresh"
            ),
            state_file=str(state_file),
        )
    )

    assert result["unused_count"] == 1
    assert result["blocked_count"] == 1
    assert result["unused_text"].startswith("fresh@hotmail.com----")


def test_inventory_api_expands_aliases_and_hides_credentials(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "core.local_ms_mailbox.generate_microsoft_pool_aliases",
        lambda email, count, existing=None: [
            f"base+reg{index}@hotmail.com" for index in range(1, count + 1)
        ],
    )
    state_file = tmp_path / "state.json"
    state_file.write_text(
        json.dumps(
            {
                "blocked": {
                    "base+reg1@hotmail.com": {
                        "email": "base+reg1@hotmail.com",
                        "reason": "user_already_exists",
                        "reason_code": "already_registered",
                    }
                }
            }
        ),
        encoding="utf-8",
    )

    result = inspect_mailbox_pool(
        MailboxPoolInventoryRequest(
            text="base@hotmail.com----mail-pass----client-id----refresh-token",
            state_file=str(state_file),
            alias_enabled=True,
            alias_count=3,
        )
    )

    assert result["total_count"] == 3
    assert result["available_count"] == 2
    assert result["blocked_count"] == 1
    assert result["items"][0]["email"] == "base+reg1@hotmail.com"
    assert "refresh-token" not in json.dumps(result)
