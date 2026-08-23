"""Create and immediately delete a synthetic Sub2API Agent Identity account."""

from __future__ import annotations

import base64
import secrets
import sys
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.sub2api_sync import Sub2ApiClient
from core.sub2api_sync import _get_config


PKCS8_PREFIX = bytes.fromhex("302e020100300506032b657004220420")


def main() -> int:
    config = _get_config()
    if not config["url"] or not config["email"] or not config["password"]:
        raise RuntimeError("Sub2API configuration is incomplete")

    suffix = secrets.token_hex(6)
    auth_json = {
        "auth_mode": "agentIdentity",
        "agent_identity": {
            "agent_runtime_id": f"agent-smoke-{suffix}",
            "agent_private_key": base64.b64encode(PKCS8_PREFIX + secrets.token_bytes(32)).decode("ascii"),
            "task_id": f"task-smoke-{suffix}",
            "account_id": f"account-smoke-{suffix}",
            "chatgpt_user_id": f"user-smoke-{suffix}",
            "email": f"agent-smoke-{suffix}@example.invalid",
            "plan_type": "free",
            "chatgpt_account_is_fedramp": False,
        },
    }

    client = Sub2ApiClient(config["url"], config["email"], config["password"])
    remote_id = 0
    try:
        remote_id = client.import_agent_identity(auth_json, name=auth_json["agent_identity"]["email"])
        print(f"imported_remote_account_id={remote_id}")
    finally:
        if remote_id:
            client.delete_account(remote_id)
            print(f"deleted_remote_account_id={remote_id}")
        client.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
