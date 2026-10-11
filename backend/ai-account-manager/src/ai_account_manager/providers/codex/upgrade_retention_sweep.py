from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime

from ai_account_manager.providers.codex.upgrade_retention import CodexUpgradeRetention


def main() -> None:
    parser = argparse.ArgumentParser(description="Run one bounded Codex upgrade retention sweep")
    parser.add_argument(
        "--operation-id",
        default=f"scheduled-{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}",
    )
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--include-historical", action="store_true")
    args = parser.parse_args()
    retention = CodexUpgradeRetention()
    result = retention.sweep(
        operation_id=args.operation_id,
        dry_run=args.dry_run,
        include_historical=args.include_historical,
    )
    # Public-safe summary only. Exact paths remain in private manifests.
    print(json.dumps(result.public_status(), sort_keys=True))


if __name__ == "__main__":
    main()
