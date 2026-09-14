#!/usr/bin/env python3
"""Read-only business ledger + timestamped logs; never instantiate a runtime Store."""
import argparse
import json
from pathlib import Path
from smart_money.mysql_config import mysql_connection
from smart_money.early_metrics import summarize_early_trial


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--trial-id", required=True)
    parser.add_argument("--log", type=Path, required=True)
    args = parser.parse_args()
    connection = mysql_connection()
    try:
        with connection.cursor() as q:
            q.execute("START TRANSACTION READ ONLY")
            q.execute("SELECT proposal_id,source_tx_hash,input_asset,output_asset,attribution_payload FROM paper_proposals")
            proposals = []
            for row in q.fetchall():
                row["attribution"] = json.loads(row.pop("attribution_payload"))
                proposals.append(row)
            q.execute("SELECT payload FROM signals")
            signals = [json.loads(row["payload"]) for row in q.fetchall()]
    finally:
        connection.rollback()
        connection.close()
    events = []
    with args.log.open() as stream:
        for line in stream:
            try:
                event = json.loads(line)
                if isinstance(event, dict):
                    events.append(event)
            except json.JSONDecodeError:
                continue
    print(json.dumps(summarize_early_trial(proposals, signals, events, args.trial_id), ensure_ascii=False))


if __name__ == "__main__":
    main()
