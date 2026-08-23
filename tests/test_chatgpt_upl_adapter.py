from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from platforms.chatgpt.upl_adapter import extract_payment_link
from core.base_platform import RegisterConfig
from platforms.chatgpt.plugin import ChatGPTPlatform


def _fake_upl_root(tmp_path: Path) -> Path:
    root = tmp_path / "upl"
    root.mkdir()
    (root / "runner.py").write_text(
        "import os\n"
        "assert os.environ.get('PP_TOKEN') == 'access-demo'\n"
        "assert os.environ.get('PP_SESSION_TOKEN') == 'session-demo'\n"
        "print('UPI 最终支付 URL:')\n"
        "print('https://payments.stripe.com/upi/instructions/demo')\n",
        encoding="utf-8",
    )
    (root / "ideal_ui.py").write_text(
        "import os\n"
        "from pathlib import Path\n"
        "PAYMENT_METHODS = {'upi': {'label': 'UPI', 'flow': 'IN/VN/IN', "
        "'available': True, 'script_path': Path(__file__).parent / 'runner.py'}}\n"
        "def build_environment(payload, payment_method, definition):\n"
        "    env = os.environ.copy()\n"
        "    env['PP_TOKEN'] = payload['token']\n"
        "    env['PP_SESSION_TOKEN'] = payload['session_token']\n"
        "    return env, {'payment_flow': definition['flow']}\n",
        encoding="utf-8",
    )
    return root


def test_extract_payment_link_runs_upstream_in_isolated_process(tmp_path):
    logs: list[str] = []
    account = SimpleNamespace(
        token="",
        extra={"access_token": "access-demo", "session_token": "session-demo"},
    )

    result = extract_payment_link(
        account,
        payment_method="upi",
        params={"proxy_seeds": "http://proxy.example:8080", "timeout_seconds": 60},
        log_fn=logs.append,
        upl_root=_fake_upl_root(tmp_path),
    )

    assert result["url"] == "https://payments.stripe.com/upi/instructions/demo"
    assert result["payment_method"] == "upi"
    assert any("UPL 提链成功" in line for line in logs)
    assert all("access-demo" not in line and "session-demo" not in line for line in logs)


def test_extract_payment_link_requires_complete_independent_upi_pools(tmp_path):
    account = SimpleNamespace(extra={"access_token": "access-demo"})

    with pytest.raises(ValueError, match="三组代理"):
        extract_payment_link(
            account,
            payment_method="upi",
            params={"checkout_proxies": "http://checkout.example:8080"},
            upl_root=_fake_upl_root(tmp_path),
        )


def test_extract_payment_link_requires_access_token(tmp_path):
    account = SimpleNamespace(extra={})

    with pytest.raises(ValueError, match="access_token"):
        extract_payment_link(
            account,
            payment_method="upi",
            params={"proxy_seeds": "http://proxy.example:8080"},
            upl_root=_fake_upl_root(tmp_path),
        )


def test_chatgpt_platform_exposes_and_persists_upl_action(monkeypatch):
    platform = ChatGPTPlatform(config=RegisterConfig(proxy="http://proxy.example:8080"))
    action_ids = {item["id"] for item in platform.get_platform_actions()}
    assert "extract_payment_link" in action_ids

    def fake_extract(account, **kwargs):
        assert kwargs["payment_method"] == "upi"
        assert kwargs["fallback_proxy"] == "http://proxy.example:8080"
        return {
            "url": "https://payments.stripe.com/upi/instructions/demo",
            "payment_url": "https://payments.stripe.com/upi/instructions/demo",
            "payment_method": "upi",
            "payment_label": "UPI",
            "payment_flow": "IN/VN/IN",
            "upstream_commit": "demo-commit",
            "message": "UPI 提链成功",
        }

    monkeypatch.setattr("platforms.chatgpt.upl_adapter.extract_payment_link", fake_extract)
    account = SimpleNamespace(extra={"account_overview": {}}, region="", email="user@example.com")

    result = platform.execute_action(
        "extract_payment_link",
        account,
        {"payment_method": "upi"},
    )

    assert result["ok"] is True
    assert result["data"]["url"].endswith("/demo")
    persisted = result["_persist"]["summary_updates"]
    assert persisted["last_payment_method"] == "upi"
    assert persisted["payment_links"]["upi"]["url"].endswith("/demo")
