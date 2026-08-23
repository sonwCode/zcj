"""Backfill active ChatGPT accounts that have not reached Sub2API."""

from __future__ import annotations

import json
import argparse
import sys
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.sub2api_sync import backfill_unsynced_accounts
from core.sub2api_sync import repair_missing_auth_proxy_from_active_pool


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=500)
    parser.add_argument("--repair-missing-proxy", action="store_true")
    parser.add_argument("--allow-failures", action="store_true")
    args = parser.parse_args()
    if args.repair_missing_proxy:
        repaired = repair_missing_auth_proxy_from_active_pool(limit=max(args.limit, 1))
        print(f"repaired_auth_proxy={repaired}")
    result = backfill_unsynced_accounts(limit=max(args.limit, 1), log_fn=print)
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result["failed"] == 0 or args.allow_failures else 1


if __name__ == "__main__":
    raise SystemExit(main())
