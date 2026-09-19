#!/usr/bin/env python3
"""Refresh bundled KRX snapshots outside the dashboard request path."""

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "api"))
import _market


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--index", choices=list(_market.KRX_INDEX_CONFIG))
    args = parser.parse_args()
    if _market.IS_SERVERLESS:
        parser.error("Run this updater offline before deployment, not in a web request.")
    if not _market.krx_auth_key():
        parser.error("KRX_AUTH_KEY is required.")

    for index_id in ([args.index] if args.index else _market.KRX_INDEX_CONFIG):
        rows = _market.build_krx_rows(index_id)
        if len(rows) < 2:
            raise RuntimeError(f"Insufficient KRX data: {index_id}")
        _market.build_breadth_summaries(
            index_id, [row["date"] for row in rows[-_market.BREADTH_WINDOW_DAYS:]]
        )
        for path, cache in (
            (_market.krx_snapshot_path(index_id), _market.load_krx_cache(index_id)),
            (_market.breadth_snapshot_path(index_id), _market.load_breadth_cache(index_id)),
        ):
            temporary = path.with_suffix(".json.tmp")
            temporary.write_text(json.dumps(cache, ensure_ascii=False), encoding="utf-8")
            temporary.replace(path)
        print(f"{index_id}: {len(rows)} days, latest {rows[-1]['date']}")


if __name__ == "__main__":
    main()
