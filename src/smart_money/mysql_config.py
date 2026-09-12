"""MySQL-backed paper relationship configuration; never stores signing secrets."""
from __future__ import annotations

import json
import os
import tempfile
import hashlib
from pathlib import Path

import pymysql

from .models import address
from .paper_config import PaperConfig, ZERO_ADDRESS, load_paper_config
from .registry import load_watchlist


MYSQL_ENV_KEYS = (
    "SMART_MONEY_MYSQL_HOST", "SMART_MONEY_MYSQL_PORT",
    "SMART_MONEY_MYSQL_USER", "SMART_MONEY_MYSQL_PASSWORD",
    "SMART_MONEY_MYSQL_ADMIN_USER", "SMART_MONEY_MYSQL_ADMIN_PASSWORD",
    "SMART_MONEY_MYSQL_DATABASE", "SMART_MONEY_MYSQL_SSL_CA",
)
def mysql_connection(write: bool = False, *, dict_rows: bool = True,
                     autocommit: bool = False):
    user_key = "SMART_MONEY_MYSQL_ADMIN_USER" if write else "SMART_MONEY_MYSQL_USER"
    password_key = (
        "SMART_MONEY_MYSQL_ADMIN_PASSWORD" if write else "SMART_MONEY_MYSQL_PASSWORD")
    host = os.environ.get("SMART_MONEY_MYSQL_HOST", "127.0.0.1")
    ssl_ca = os.environ.get("SMART_MONEY_MYSQL_SSL_CA")
    if host not in {"127.0.0.1", "localhost"} and not ssl_ca:
        raise ValueError("remote MySQL requires SMART_MONEY_MYSQL_SSL_CA")
    if host not in {"127.0.0.1", "localhost"}:
        required = (
            "SMART_MONEY_MYSQL_PORT", user_key, password_key,
            "SMART_MONEY_MYSQL_DATABASE",
        )
        missing = [name for name in required if not os.environ.get(name)]
        if missing:
            raise ValueError(
                "remote MySQL requires explicit connection settings: "
                + ",".join(missing))
    ssl = ({"ca": ssl_ca, "check_hostname": True} if ssl_ca
           else {"check_hostname": False})
    try:
        return pymysql.connect(
            host=host,
            port=int(os.environ.get("SMART_MONEY_MYSQL_PORT", "3308")),
            user=os.environ.get(user_key, "smart_money" if write else "smart_money_runtime"),
            password=os.environ.get(
                password_key, "local-paper-only" if write else "local-runtime-only"),
            database=os.environ.get("SMART_MONEY_MYSQL_DATABASE", "smart_money"),
            charset="utf8mb4", autocommit=autocommit,
            cursorclass=(pymysql.cursors.DictCursor if dict_rows
                         else pymysql.cursors.Cursor),
            connect_timeout=5, read_timeout=10, write_timeout=10,
            ssl=ssl,
        )
    except (pymysql.MySQLError, RuntimeError, ValueError) as exc:
        raise ValueError(f"MySQL configuration connection failed: {type(exc).__name__}") from None


def _json(value, field):
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(value)
    except (TypeError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid MySQL JSON field: {field}") from exc


def rows_to_document(rows: list[dict]) -> dict:
    if not rows:
        raise ValueError("no enabled paper copy relationships")
    common_fields = (
        "strategy_version", "trigger_mode", "shadow_trigger_modes", "quote_policy",
        "allowed_protocols", "allowed_assets", "allowed_routes",
    )
    first = rows[0]
    common = {key: _json(first[key], key) if key in {
        "shadow_trigger_modes", "quote_policy", "allowed_protocols",
        "allowed_assets", "allowed_routes",
    } else first[key] for key in common_fields}
    wallets = []
    seen_relationships = set()
    for row in rows:
        if row.get("run_mode") != "paper":
            raise ValueError("only paper relationships are supported")
        follower = address(row["follower_wallet"])
        smart = address(row["smart_wallet"])
        relationship = (follower, smart)
        if relationship in seen_relationships:
            raise ValueError("duplicate enabled follower and smart wallet relationship")
        seen_relationships.add(relationship)
        for key in common_fields:
            value = _json(row[key], key) if key in {
                "shadow_trigger_modes", "quote_policy", "allowed_protocols",
                "allowed_assets", "allowed_routes",
            } else row[key]
            if value != common[key]:
                raise ValueError(f"enabled relationships disagree on {key}")
        def rule(prefix):
            result = {"mode": row[f"{prefix}_rule_mode"]}
            fixed = row.get(f"{prefix}_fixed_amount_raw")
            ratio = row.get(f"{prefix}_ratio_ppm")
            if fixed is not None:
                result["fixed_amount_raw"] = str(fixed)
            if ratio is not None:
                result["ratio_ppm"] = int(ratio)
            return result
        wallets.append({
            "wallet": smart,
            "label": row["smart_wallet_label"],
            "follower_wallet": follower,
            "relationship_id": str(row["id"]),
            "budget_limits": {
                "USDG": str(row["usdg_budget_limit_raw"]),
                "ETH_WETH": str(row["eth_budget_limit_raw"]),
            },
            "buy_rules": {"USDG": rule("usdg"), "ETH_WETH": rule("eth")},
            "sell_rule": rule("sell"),
        })
    return {"version": 1, **common, "wallets": wallets}


def load_mysql_paper_config() -> PaperConfig:
    connection = mysql_connection()
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT * FROM copy_relationships WHERE enabled = TRUE ORDER BY id")
            rows = cursor.fetchall()
    finally:
        connection.close()
    if not rows:
        raise ValueError("no enabled paper copy relationships")
    relationships = []
    seen = set()
    for row in rows:
        pair = (address(row["follower_wallet"]), address(row["smart_wallet"]))
        if pair in seen:
            raise ValueError("duplicate enabled follower and smart wallet relationship")
        seen.add(pair)
        document = rows_to_document([row])
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", suffix=".json") as stream:
            json.dump(document, stream)
            stream.flush()
            relationships.extend(load_paper_config(stream.name).relationships)
    snapshots = sorted(policy.snapshot_hash for policy in relationships)
    aggregate_hash = hashlib.sha256(":".join(snapshots).encode()).hexdigest()
    first = relationships[0]
    wallets = {}
    for policy in relationships:
        wallets.setdefault(policy.wallet, policy)
    return PaperConfig(
        first.strategy_version, first.trigger_mode, first.shadow_trigger_modes,
        first.quote_policy,
        frozenset().union(*(policy.allowed_protocols for policy in relationships)),
        frozenset().union(*(policy.allowed_assets for policy in relationships)),
        frozenset().union(*(policy.allowed_routes for policy in relationships)),
        wallets, tuple(relationships), aggregate_hash,
    )


def load_enabled_relationship_policy(relationship_id: str):
    """Reload one enabled relationship through the runtime read-only account."""
    if (not isinstance(relationship_id, str) or not relationship_id.isdecimal()
            or int(relationship_id) <= 0):
        raise ValueError("invalid relationship id")
    connection = mysql_connection()
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT * FROM copy_relationships WHERE id=%s AND enabled=TRUE",
                (int(relationship_id),),
            )
            rows = cursor.fetchall()
    except pymysql.MySQLError as exc:
        raise ValueError(f"MySQL relationship query failed: {type(exc).__name__}") from None
    finally:
        connection.close()
    if len(rows) != 1:
        raise ValueError("relationship is disabled or unavailable")
    document = rows_to_document(rows)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", suffix=".json") as stream:
        json.dump(document, stream)
        stream.flush()
        return load_paper_config(stream.name).relationships[0]


class MySqlRelationshipGate:
    """Fresh database gate used immediately before any signing stage."""

    def validate(self, relationship_id: str, follower_wallet: str,
                 smart_wallet: str, snapshot_hash: str):
        policy = load_enabled_relationship_policy(relationship_id)
        if (policy.relationship_id != relationship_id
                or policy.follower_wallet != address(follower_wallet)
                or policy.wallet != address(smart_wallet)
                or policy.snapshot_hash != snapshot_hash):
            raise ValueError("enabled relationship no longer matches execution snapshot")
        return policy


def import_watchlist_relationships(follower_wallet: str, follower_label: str,
                                   watchlist_path: str | Path,
                                   template_path: str | Path) -> tuple[int, int]:
    follower = address(follower_wallet)
    if follower == ZERO_ADDRESS:
        raise ValueError("zero follower wallet cannot be imported")
    if not isinstance(follower_label, str) or not follower_label.strip() or len(follower_label) > 100:
        raise ValueError("invalid follower label")
    follower_label = follower_label.strip()
    template = json.loads(Path(template_path).read_text(encoding="utf-8"))
    validated = load_paper_config(template_path)
    watchlist = load_watchlist(watchlist_path)
    if len(validated.relationships) != 1:
        raise ValueError("import template must contain exactly one wallet policy")
    policy = next(iter(template["wallets"]))
    common = (
        template["strategy_version"], template.get("trigger_mode", "swap_evidenced"),
        json.dumps(template.get("shadow_trigger_modes", ["feed_intent", "receipt_success"])),
        json.dumps(template["quote_policy"]), json.dumps(template["allowed_protocols"]),
        json.dumps(template["allowed_assets"]), json.dumps(template["allowed_routes"]),
    )
    values = []
    for smart, metadata in watchlist.items():
        if address(smart) == ZERO_ADDRESS:
            raise ValueError("zero smart wallet cannot be imported")
        label = (metadata.get("handle") or smart).strip()[:100]
        usdg, eth = policy["buy_rules"]["USDG"], policy["buy_rules"]["ETH_WETH"]
        sell = policy["sell_rule"]
        values.append((
            follower, follower_label, smart, label, False, "paper", *common,
            usdg["mode"], usdg.get("fixed_amount_raw"), usdg.get("ratio_ppm"),
            policy["budget_limits"]["USDG"],
            eth["mode"], eth.get("fixed_amount_raw"), eth.get("ratio_ppm"),
            policy["budget_limits"]["ETH_WETH"],
            sell["mode"], sell.get("fixed_amount_raw"), sell.get("ratio_ppm"),
        ))
    sql = """INSERT IGNORE INTO copy_relationships (
      follower_wallet,follower_label,smart_wallet,smart_wallet_label,enabled,run_mode,
      strategy_version,trigger_mode,shadow_trigger_modes,quote_policy,allowed_protocols,
      allowed_assets,allowed_routes,usdg_rule_mode,usdg_fixed_amount_raw,usdg_ratio_ppm,
      usdg_budget_limit_raw,eth_rule_mode,eth_fixed_amount_raw,eth_ratio_ppm,
      eth_budget_limit_raw,sell_rule_mode,sell_fixed_amount_raw,sell_ratio_ppm)
      VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)"""
    connection = mysql_connection(write=True)
    try:
        with connection.cursor() as cursor:
            inserted = cursor.executemany(sql, values)
        connection.commit()
        return inserted, len(values) - inserted
    except pymysql.MySQLError as exc:
        connection.rollback()
        raise ValueError(f"MySQL relationship import failed: {type(exc).__name__}") from None
    finally:
        connection.close()
