#!/usr/bin/env python3
"""
ONE Flex-import generator  —  component 4.

Emits an IBKR "Flex Query"-format CSV from today's canonical IBKR fills, so the
user can one-click import real fills into ONE (cuts manual re-entry). ONE's
import still needs its manual "link trades into positions" wizard step — that's
unavoidable — but the typing is gone.

Output matches the exact format ONE imports (decoded from the user's
ONEImportTest.csv):
    DateTime,Buy/Sell,AssetClass,Symbol,Quantity,TradePrice,IBCommission,
    UnderlyingSymbol,NetCash
  - DateTime  = "YYYYMMDD,HHMMSS"
  - Symbol    = OSI: root(6, space-padded)+yymmdd+C/P+strike*1000(8)
  - Quantity  = signed (BUY +, SELL -)
  - NetCash   = -(signedQty * price * multiplier) + commission

One file is written per IBKR account (ONE imports per account).

  python flex_export.py            # reads canonical_positions.json
"""

from __future__ import annotations

import csv
import json
import os
import tempfile
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

HERE = os.path.dirname(os.path.abspath(__file__))
IBKR_JSON = os.path.join(HERE, "canonical_positions.json")
OUT_DIR = os.path.join(HERE, "flex_export")
JOURNAL_FILE = os.path.join(OUT_DIR, "execution_journal.json")
DEFAULT_TZ = "Australia/Sydney"          # ONE runs in local time; match it
TRADING_TZ = ZoneInfo("America/New_York")

HEADER = ["DateTime", "Buy/Sell", "AssetClass", "Symbol", "Quantity",
          "TradePrice", "IBCommission", "UnderlyingSymbol", "NetCash"]

COMPLETE = "COMPLETE"
POSSIBLY_INCOMPLETE = "POSSIBLY_INCOMPLETE"
JOURNAL_VERSION = 2


# Index roots that are AM-settled 3rd-Friday MONTHLIES: IBKR reports the
# last-trade day (Thursday); the OSI/expiration date ONE wants is the Friday.
# (The W-suffixed classes SPXW/RUTW and QQQ/SPY are PM-settled: last-trade ==
# expiration, so no shift.)
AM_MONTHLY_ROOTS = {"SPX", "RUT", "NDX", "DJX", "OEX", "XSP"}


def actual_expiry(tradingClass: str, expiry: str) -> str:
    """IBKR last-trade date -> real expiration for AM-settled monthlies
    (Thursday -> Friday). Everything else passes through unchanged."""
    if tradingClass in AM_MONTHLY_ROOTS and len(str(expiry)) == 8:
        try:
            d = datetime.strptime(expiry, "%Y%m%d")
        except ValueError:
            return expiry
        if d.weekday() == 3:                  # Thursday -> expiration Friday
            return (d + timedelta(days=1)).strftime("%Y%m%d")
    return expiry


def osi_symbol(tradingClass: str, expiry: str, right: str, strike: float) -> str:
    """SPXW, 20260702, P, 7350 -> 'SPXW  260702P07350000' (uses real expiration)."""
    root = tradingClass.ljust(6)              # 6-char, space-padded
    yymmdd = actual_expiry(tradingClass, expiry)[2:8]
    strike8 = f"{int(round(strike * 1000)):08d}"
    return f"{root}{yymmdd}{right}{strike8}"


def fmt_datetime(t: str, tz=None) -> str:
    """'2026-06-23 20:08:11+00:00' (UTC) -> local-tz '20260624,060811'.
    ONE/IBKR-Flex use LOCAL time, so convert from the fill's UTC before writing."""
    s = str(t)
    dt = None
    for fmt in ("%Y-%m-%d %H:%M:%S%z", "%Y-%m-%d %H:%M:%S"):
        try:
            dt = datetime.strptime(s.split(".")[0], fmt)
            break
        except ValueError:
            continue
    if dt is None:
        return s
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    if tz is not None:
        dt = dt.astimezone(tz)
    return dt.strftime("%Y%m%d,%H%M%S")


def is_option_leg(f: dict) -> bool:
    if f.get("secType") not in (None, "OPT"):     # exclude STK, BAG/COMB
        return False
    return f.get("right") in ("C", "P") and f.get("strike") and f.get("expiry")


def _option_key(item: dict) -> tuple | None:
    """Economic option identity used to validate fills against position moves."""
    if not is_option_leg(item):
        return None
    account = str(item.get("account") or "").strip()
    if not account:
        return None
    return (
        account,
        str(item.get("tradingClass") or item.get("underlying") or ""),
        str(item.get("expiry") or ""),
        float(item.get("strike") or 0),
        str(item.get("right") or ""),
    )


def _key_text(key: tuple) -> str:
    return json.dumps(list(key), separators=(",", ":"))


def _key_tuple(value: str) -> tuple:
    account, trading_class, expiry, strike, right = json.loads(value)
    return account, trading_class, expiry, float(strike), right


def _signed_fill_qty(fill: dict) -> float:
    shares = abs(float(fill.get("shares") or 0))
    return shares if fill.get("side") == "BOT" else -shares


def fill_identity(fill: dict) -> str:
    """Stable execution identity; execId is authoritative when supplied."""
    exec_id = str(fill.get("execId") or "").strip()
    if exec_id:
        return f"exec:{str(fill.get('account') or '').strip()}:{exec_id}"
    fallback = [
        fill.get("account"), fill.get("time"), fill.get("permId"),
        fill.get("orderId"), fill.get("tradingClass"), fill.get("expiry"),
        fill.get("strike"), fill.get("right"), fill.get("side"),
        fill.get("shares"), fill.get("price"),
    ]
    return "fallback:" + json.dumps(
        fallback, separators=(",", ":"), default=str)


def _parse_fill_time(value) -> datetime | None:
    s = str(value or "").strip()
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        try:
            dt = datetime.strptime(s.split(".")[0], "%Y-%m-%d %H:%M:%S%z")
        except ValueError:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _trading_date(snapshot: dict, now: datetime | None = None) -> str:
    if now is None:
        try:
            now = datetime.fromisoformat(str(snapshot.get("captured_at") or ""))
        except ValueError:
            now = datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    return now.astimezone(TRADING_TZ).date().isoformat()


def _position_map(legs: list[dict]) -> dict[str, float]:
    positions: dict[str, float] = defaultdict(float)
    for leg in legs or []:
        key = _option_key(leg)
        if key:
            positions[_key_text(key)] += float(leg.get("qty") or 0)
    return {key: qty for key, qty in positions.items() if abs(qty) > 1e-9}


def _fill_map(fills: list[dict]) -> dict[str, float]:
    quantities: dict[str, float] = defaultdict(float)
    for fill in fills or []:
        key = _option_key(fill)
        if key:
            quantities[_key_text(key)] += _signed_fill_qty(fill)
    return dict(quantities)


def _load_journal(path: str) -> dict:
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}


def _atomic_json(path: str, payload: dict) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(
        prefix=".execution_journal_", suffix=".tmp", dir=os.path.dirname(path))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2, default=str)
        os.replace(tmp_path, path)
    finally:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)


def update_execution_journal(ibkr_snapshot: dict,
                             journal_path: str = JOURNAL_FILE,
                             now: datetime | None = None):
    """Persist and validate the current US trading day's option executions.

    Returns ``(snapshot_with_merged_fills, completeness_by_account)``. The
    baseline is the prior session's ending positions when available; on first
    use it is inferred from current positions less the full-day executions TWS
    supplied. Later position changes without matching executions are therefore
    visible and block a normal ONE import file.
    """
    trade_date = _trading_date(ibkr_snapshot, now)
    state = _load_journal(journal_path)
    if state.get("version") != JOURNAL_VERSION:
        state = {}
    current_positions = _position_map(ibkr_snapshot.get("legs", []))
    raw_fills = ibkr_snapshot.get("fills_today", []) or []

    captured = []
    unassigned = []
    passthrough_fills = []
    for fill in raw_fills:
        if not is_option_leg(fill):
            passthrough_fills.append(fill)
            continue
        fill_dt = _parse_fill_time(fill.get("time"))
        if (fill_dt and
                fill_dt.astimezone(TRADING_TZ).date().isoformat() != trade_date):
            continue
        if not str(fill.get("account") or "").strip():
            unassigned.append(fill)
            continue
        captured.append(fill)

    if state.get("trade_date") == trade_date:
        stored_fills = state.get("fills") or {}
        stored_unassigned = state.get("unassigned_fills") or {}
        baseline = state.get("baseline_positions") or {}
        baseline_source = state.get("baseline_source") or "persisted"
    else:
        stored_fills = {}
        stored_unassigned = {}
        previous_positions = state.get("positions") or {}
        previous_updated = state.get("updated_at")
        use_previous = bool(previous_positions and previous_updated)
        if use_previous:
            try:
                updated = datetime.fromisoformat(previous_updated)
                age = datetime.now(timezone.utc) - updated
                use_previous = age <= timedelta(days=7)
            except (TypeError, ValueError):
                use_previous = False
        baseline = dict(previous_positions) if use_previous else {}
        baseline_source = (
            "previous_session" if use_previous else "inferred_first_capture")

    for fill in captured:
        identity = fill_identity(fill)
        old = stored_fills.get(identity)
        # A later callback often adds commission. Keep the richer version
        # without creating a duplicate execution.
        if (old and old.get("commission") is not None and
                fill.get("commission") is None):
            continue
        stored_fills[identity] = fill
    for fill in unassigned:
        stored_unassigned[fill_identity(fill)] = fill

    merged_fills = list(stored_fills.values())
    merged_fills.sort(key=lambda row: str(row.get("time") or ""))
    fill_quantities = _fill_map(merged_fills)

    if baseline_source == "inferred_first_capture":
        keys = set(current_positions) | set(fill_quantities)
        baseline = {
            key: current_positions.get(key, 0.0) - fill_quantities.get(key, 0.0)
            for key in keys
            if abs(current_positions.get(key, 0.0) -
                   fill_quantities.get(key, 0.0)) > 1e-9
        }
        baseline_source = "inferred_baseline"

    discrepancies: dict[str, list[dict]] = defaultdict(list)
    keys = set(baseline) | set(fill_quantities) | set(current_positions)
    for key_text in keys:
        expected = (float(baseline.get(key_text, 0)) +
                    float(fill_quantities.get(key_text, 0)))
        actual = float(current_positions.get(key_text, 0))
        if abs(expected - actual) <= 1e-9:
            continue
        account, trading_class, expiry, strike, right = _key_tuple(key_text)
        discrepancies[account].append({
            "instrument": f"{trading_class} {expiry} {strike:g}{right}",
            "expected_qty": expected,
            "position_qty": actual,
            "unexplained_qty": actual - expected,
        })

    accounts = {
        str(account) for account in (ibkr_snapshot.get("managed_accounts") or [])
        if str(account).strip()
    }
    accounts.update(_key_tuple(key)[0] for key in current_positions)
    accounts.update(str(fill.get("account")) for fill in merged_fills
                    if str(fill.get("account") or "").strip())
    completeness = {}
    unassigned_ids = sorted(stored_unassigned)
    unassigned_issue = ({
        "instrument": "option execution without destination account",
        "execution_ids": unassigned_ids,
    } if unassigned_ids else None)
    for account in sorted(accounts):
        issues = list(discrepancies.get(account, []))
        if unassigned_issue:
            issues.append(unassigned_issue)
        completeness[account] = {
            "status": POSSIBLY_INCOMPLETE if issues else COMPLETE,
            "trade_date": trade_date,
            "fill_count": sum(1 for fill in merged_fills
                              if str(fill.get("account")) == account),
            "baseline_source": baseline_source,
            "discrepancies": issues,
        }
    if unassigned_ids:
        completeness["UNASSIGNED"] = {
            "status": POSSIBLY_INCOMPLETE,
            "trade_date": trade_date,
            "fill_count": len(unassigned_ids),
            "baseline_source": baseline_source,
            "discrepancies": [unassigned_issue],
        }

    state = {
        "version": JOURNAL_VERSION,
        "trade_date": trade_date,
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "baseline_source": baseline_source,
        "baseline_positions": baseline,
        "positions": current_positions,
        "fills": stored_fills,
        "unassigned_fills": stored_unassigned,
        "completeness": completeness,
    }
    _atomic_json(journal_path, state)
    combined = {}
    for fill in merged_fills + list(stored_unassigned.values()) + passthrough_fills:
        combined[fill_identity(fill)] = fill
    all_fills = sorted(combined.values(), key=lambda row: str(row.get("time") or ""))
    return {**ibkr_snapshot, "fills_today": all_fills}, completeness


def fill_to_row(f: dict, tz=None) -> list:
    shares = abs(float(f["shares"]))
    signed = shares if f["side"] == "BOT" else -shares
    price = float(f["price"])
    mult = float(f.get("multiplier") or 100.0)
    commission = f.get("commission")
    comm = float(commission) if commission is not None else 0.0
    net_cash = -(signed * price * mult) + comm
    return [
        fmt_datetime(f["time"], tz),
        "BUY" if signed > 0 else "SELL",
        "OPT",
        osi_symbol(f["tradingClass"], f["expiry"], f["right"], float(f["strike"])),
        f"{signed:g}",
        f"{price:g}",
        ("" if commission is None else f"{comm:g}"),
        f["underlying"],
        f"{net_cash:g}",
    ]


def _resolve_tz(tz_name):
    try:
        return ZoneInfo(tz_name or DEFAULT_TZ)
    except Exception:
        return ZoneInfo(DEFAULT_TZ)


def generate(ibkr_snapshot: dict, tz_name: str = None):
    """Returns {account: [rows]}. Times are written in tz_name (default AEST) so
    ONE (local time) reads the right trade date."""
    tz = _resolve_tz(tz_name)
    by_acct: dict = defaultdict(list)
    skipped = 0
    for f in ibkr_snapshot.get("fills_today", []):
        if not is_option_leg(f) or not str(f.get("account") or "").strip():
            skipped += 1
            continue
        by_acct[f["account"]].append(fill_to_row(f, tz))
    return by_acct, skipped


def _write_csv(path: str, rows: list[list]) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(prefix=".ONEImport_", suffix=".tmp",
                                    dir=os.path.dirname(path))
    try:
        with os.fdopen(fd, "w", newline="", encoding="utf-8") as fh:
            writer = csv.writer(fh, quoting=csv.QUOTE_ALL)
            writer.writerow(HEADER)
            writer.writerows(rows)
        os.replace(tmp_path, path)
    finally:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)


def _supersede(path: str) -> None:
    """Delete an export that a newer generation has replaced.

    These CSVs are derived data -- any of them can be regenerated from
    execution_journal.json -- so keeping a superseded copy beside the current
    one buys nothing and actively invites importing the wrong file into ONE.
    """
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass


def sweep_superseded(out_dir: str = OUT_DIR) -> int:
    """Clear ``.stale`` leftovers from when superseded files were renamed.

    Earlier versions parked replaced exports as ONEImport_*.csv.stale instead of
    removing them, so the folder grew a copy per account per run.  Sweeping on
    every save lets an existing folder heal itself.
    """
    if not os.path.isdir(out_dir):
        return 0
    removed = 0
    for name in os.listdir(out_dir):
        if name.startswith("ONEImport_") and name.endswith(".stale"):
            _supersede(os.path.join(out_dir, name))
            removed += 1
    return removed


def save_account_files(by_account: dict, completeness: dict,
                       out_dir: str = OUT_DIR) -> dict:
    """Atomically save safe imports and quarantine files that fail validation."""
    os.makedirs(out_dir, exist_ok=True)
    sweep_superseded(out_dir)
    paths = {}
    accounts = set(by_account) | {
        account for account in completeness if account != "UNASSIGNED"
    }
    for account in accounts:
        rows = by_account.get(account) or []
        status = (completeness.get(str(account)) or {}).get(
            "status", POSSIBLY_INCOMPLETE)
        normal = os.path.join(out_dir, f"ONEImport_{account}.csv")
        blocked = os.path.join(
            out_dir, f"ONEImport_{account}_{POSSIBLY_INCOMPLETE}.csv")
        if status == COMPLETE and rows:
            _write_csv(normal, rows)
            _supersede(blocked)
            paths[account] = normal
        elif status == COMPLETE:
            _supersede(normal)
            paths[account] = ""
        else:
            _supersede(normal)
            _write_csv(blocked, rows)
            paths[account] = blocked
    return paths


def main():
    snap = json.load(open(IBKR_JSON))
    tz_name = DEFAULT_TZ
    cfg_path = os.path.join(HERE, "config.json")
    if os.path.exists(cfg_path):
        tz_name = json.load(open(cfg_path)).get("flex_timezone", DEFAULT_TZ)
    snap, completeness = update_execution_journal(snap)
    by_acct, skipped = generate(snap, tz_name)
    paths = save_account_files(by_acct, completeness)

    total = 0
    for acct, rows in sorted(by_acct.items()):
        total += len(rows)
        status = completeness.get(acct, {}).get("status", POSSIBLY_INCOMPLETE)
        print(f"  {acct}: {len(rows):>3} fills [{status}] -> {paths[acct]}")

    print(f"\nWrote {len(by_acct)} account file(s), {total} fill rows "
          f"({skipped} non-option/combo fills skipped) into {OUT_DIR}\\")
    if any(r[6] == "" for rows in by_acct.values() for r in rows):
        print("NOTE: some rows have blank IBCommission (commission not yet "
              "captured in this snapshot; re-run canonical_engine.py for exact "
              "cost basis). NetCash for those excludes commission.")


if __name__ == "__main__":
    main()
