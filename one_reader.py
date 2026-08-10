#!/usr/bin/env python3
"""
ONE reader  —  Step 2 of the TWS / ONE / OptionStrat reconciliation system.

Reads ONE's "Summary Report" or "Detail Report" CSV export (filter
Status='Open', grouped by Account) and normalizes it to the SAME
canonical leg schema the IBKR engine emits, so the two can be diffed.

Why the CSV (not the .mdb): ONE's local user database is a
password-encrypted ACE database; the password is ONE-internal (not the user
login) so it can't be opened. The Summary Report export is the integration
point and additionally carries ONE's recorded per-share price (AvgOpenPrice),
which is the drift signal we hunt.

WHAT IT DOES
  - Walks the hierarchical report (group lines, TRADE rows, LEG rows).
  - Keeps only OPEN legs (a leg row with a CloseDate/AvgClosePrice is closed).
  - Signs qty: Buy = +, Sell = -.
  - Parses the OSI symbol -> tradingClass (SPX vs SPXW...), expiry, strike, right.
  - Nets legs by economic identity (account, tradingClass, expiry, strike, right)
    into signed qty + qty-weighted avg price (ONE's commit price).
  - Writes one_positions.json in canonical shape.

USAGE
  python one_reader.py [path-to-ONESummaryReport.csv]
  (defaults to the newest *ONESummaryReport*.csv in ~/Downloads, else the
   bundled sample under ./samples/)
"""

from __future__ import annotations

import csv
import glob
import json
import os
import re
import sys
from collections import defaultdict
from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

# OSI: root letters, optional pad spaces, 6-digit yymmdd, C/P, 8-digit strike*1000
OSI_RE = re.compile(r"^([A-Z]+)\s*(\d{6})([CP])(\d{8})$")
ACCOUNT_ORDER_PREFIX_RE = re.compile(r"^\s*\d+\.\s*")

# Equity-index options are all x100; keep a map in case ONE ever lists others.
DEFAULT_MULTIPLIER = 100.0

DEFAULT_EXPORT_DIRS = [
    os.path.join(os.path.expanduser("~"), "Downloads"),
    os.path.join(os.path.expanduser("~"), "Documents"),   # ONE exports here
]
EXPORT_GLOBS = ("*ONESummaryReport*.csv", "*ONEDetailReport*.csv")
SAMPLE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                      "samples", "2026-06-24-ONESummaryReport.csv")
OUTPUT_JSON = "one_positions.json"
US_EASTERN = ZoneInfo("America/New_York")


def _f(x, default=0.0):
    try:
        return float(str(x).replace(",", "").strip())
    except (TypeError, ValueError):
        return default


def _parse_dmy(s: str):
    """ONE's D/M/YYYY date (e.g. '17/07/2026') -> 'YYYYMMDD', or None."""
    s = (s or "").strip()
    if not s:
        return None
    try:
        return datetime.strptime(s, "%d/%m/%Y").strftime("%Y%m%d")
    except ValueError:
        return None


def is_expired(expiry: str | None, as_of: date | None = None) -> bool:
    """Return whether an option expiry is before the current US trading date.

    Expiry-day positions remain visible until that date has ended.  Using the
    New York calendar also avoids hiding them early when the service runs from
    Australia.
    """
    if not expiry:
        return False
    try:
        expiry_date = datetime.strptime(str(expiry), "%Y%m%d").date()
    except ValueError:
        return False
    trading_date = as_of or datetime.now(US_EASTERN).date()
    return expiry_date < trading_date


def normalize_account_name(name: str | None) -> str:
    """Remove ONE's optional report-order prefix (for example ``1.SMSF``).

    ONE adds these numeric prefixes when a report is grouped/sorted by Account.
    They are presentation metadata, not part of the configured account name.
    """
    return ACCOUNT_ORDER_PREFIX_RE.sub("", str(name or "").strip())


def parse_osi(symbol: str):
    """'SPXW  260702P07400000' -> (tradingClass, expiry YYYYMMDD, right, strike)."""
    m = OSI_RE.match(symbol.strip())
    if not m:
        return None
    root, yymmdd, right, strike8 = m.groups()
    expiry = "20" + yymmdd                      # 260702 -> 20260702
    strike = int(strike8) / 1000.0
    return root, expiry, right, strike


def _row_kind(row: list[str]) -> str:
    """Classify a raw CSV row: 'leg', 'trade', 'group', or 'other'.

    LEG rows have 2 leading empty cells; TRADE rows have 1 leading empty cell
    then a non-empty account; group lines are a lone account name."""
    if len(row) >= 3 and row[0] == "" and row[1] == "" and row[2].strip():
        return "leg"
    if len(row) >= 2 and row[0] == "" and row[1].strip():
        return "trade"
    if len(row) >= 1 and row[0].strip() and all(c.strip() == "" for c in row[1:]):
        return "group"
    return "other"


# Default LEG column indices (the standard ONE export order). Overridden per file
# by the leg header row when present, so column reordering in ONE can't break us.
DEFAULT_LEG_COLS = {
    "Account": 2, "TradeId": 3, "Transaction": 5, "Qty": 6, "Symbol": 7,
    "Expiry": 8, "Underlying": 11, "AvgOpenPrice": 12, "CloseDate": 14,
}


def parse_leg_row(row: list[str], cols: dict | None = None) -> dict | None:
    cols = cols or DEFAULT_LEG_COLS

    def g(name):
        i = cols.get(name)
        return row[i] if (i is not None and i < len(row)) else ""

    account = normalize_account_name(g("Account"))
    trade_id = g("TradeId").strip()
    txn = g("Transaction").strip().lower()
    qty = _f(g("Qty"))
    symbol = g("Symbol").strip()
    expiry_listed = _parse_dmy(g("Expiry").strip())   # ONE's actual expiration (Fri)
    underlying = g("Underlying").strip()
    # Summary reports expose AvgOpenPrice. Detail reports expose each fill as
    # Price; those transactions are collapsed to the remaining open position
    # by _collapse_detail_transactions().
    avg_open = _f(g("AvgOpenPrice") or g("Price"))
    close_date = g("CloseDate").strip()

    if not symbol or qty == 0:
        return None
    parsed = parse_osi(symbol)
    if not parsed:
        print(f"  [warn] unparseable symbol: {symbol!r}")
        return None
    trading_class, expiry, right, strike = parsed

    signed = qty if txn == "buy" else -qty
    # Prefer ONE's listed expiry because an AM-settled monthly OSI symbol can
    # carry the following Saturday.  ONE may leave an expired leg marked open;
    # it is no longer an economic position and must not become ONE_ONLY.
    expired = is_expired(expiry_listed or expiry)
    is_open = close_date == "" and not expired

    return {
        "account": account,
        "trade_id": trade_id,
        "underlying": underlying or trading_class,
        "tradingClass": trading_class,
        "expiry": expiry,                    # from OSI symbol (Sat for AM monthlies)
        "expiry_listed": expiry_listed,      # ONE's actual expiration date (Fri)
        "strike": strike,
        "right": right,
        "multiplier": DEFAULT_MULTIPLIER,
        "qty": signed,
        "open_price": avg_open,              # ONE's recorded per-share commit price
        "is_open": is_open,
        "is_expired": expired,
        "symbol": symbol,
    }


def _collapse_detail_transactions(legs: list[dict]) -> list[dict]:
    """Turn Detail Report transaction history into current open legs.

    ONE's Detail Report contains every buy/sell belonging to each still-open
    trade, including transactions that closed earlier legs. Maintain a moving
    average for additions and preserve it for reductions; fully closed legs
    disappear. This produces the same shape downstream code expects from a
    Summary Report.
    """
    buckets = {}
    for lg in legs:
        key = (lg["account"], lg["trade_id"], lg["tradingClass"],
               lg["expiry"], lg["strike"], lg["right"])
        b = buckets.get(key)
        delta = lg["qty"]
        if b is None:
            b = {"qty": 0.0, "avg": 0.0, "leg": lg.copy()}
            buckets[key] = b

        old_qty = b["qty"]
        new_qty = old_qty + delta
        if abs(old_qty) < 1e-9 or old_qty * delta > 0:
            total = abs(old_qty) + abs(delta)
            b["avg"] = ((abs(old_qty) * b["avg"] +
                         abs(delta) * lg["open_price"]) / total
                        if total else 0.0)
        elif abs(delta) > abs(old_qty):
            # The transaction crossed through flat and opened the other side.
            b["avg"] = lg["open_price"]
        # A partial reduction leaves the remaining position's entry unchanged.
        b["qty"] = new_qty
        b["leg"] = {**b["leg"], "trade_name": lg.get("trade_name")}

    out = []
    for b in buckets.values():
        if abs(b["qty"]) < 1e-9:
            continue
        lg = b["leg"]
        lg["qty"] = b["qty"]
        lg["open_price"] = round(b["avg"], 4)
        lg["is_open"] = True
        out.append(lg)
    return out


def _is_leg_header(row):
    return "Symbol" in row and "Transaction" in row and "Qty" in row


def _is_trade_header(row):
    return "TradeName" in row and "TradeId" in row and "Symbol" not in row


def read_summary_report(path: str):
    legs = []
    current_name = None          # TradeName from the most recent TRADE data row
    leg_cols = None              # name->index map from the leg header row
    trade_name_idx = 5           # fallback (old layout); set from trade header
    detail_mode = False
    with open(path, newline="", encoding="utf-8-sig") as fh:
        reader = csv.reader(fh)
        for row in reader:
            if not row:
                continue
            # header rows define column positions (ONE lets you reorder columns)
            if _is_leg_header(row):
                leg_cols = {name: i for i, name in enumerate(row) if name}
                detail_mode = "Price" in leg_cols and "AvgOpenPrice" not in leg_cols
                continue
            if _is_trade_header(row):
                trade_name_idx = {name: i for i, name in enumerate(row)
                                  if name}.get("TradeName", trade_name_idx)
                continue
            kind = _row_kind(row)
            if kind == "trade":
                current_name = (row[trade_name_idx].strip()
                                if len(row) > trade_name_idx else "") or None
            elif kind == "leg":
                leg = parse_leg_row(row, leg_cols)
                if leg:
                    leg["trade_name"] = current_name   # the combo name
                    legs.append(leg)
    return _collapse_detail_transactions(legs) if detail_mode else legs


def net_positions(legs: list[dict]) -> list[dict]:
    """Net OPEN legs by economic identity into canonical position rows."""
    buckets: dict[tuple, dict] = {}
    for lg in legs:
        if not lg["is_open"]:
            continue
        key = (lg["account"], lg["tradingClass"], lg["expiry"],
               lg["strike"], lg["right"])
        b = buckets.setdefault(key, {
            "account": lg["account"],
            "underlying": lg["underlying"],
            "tradingClass": lg["tradingClass"],
            "expiry": lg["expiry"],
            "strike": lg["strike"],
            "right": lg["right"],
            "multiplier": lg["multiplier"],
            "qty": 0.0,
            "_px_num": 0.0,      # sum |qty| * price  -> qty-weighted avg
            "_px_den": 0.0,
            "trade_ids": set(),
        })
        b["qty"] += lg["qty"]
        b["_px_num"] += abs(lg["qty"]) * lg["open_price"]
        b["_px_den"] += abs(lg["qty"])
        b["trade_ids"].add(lg["trade_id"])

    out = []
    for b in buckets.values():
        if abs(b["qty"]) < 1e-9:             # fully flattened -> not a position
            continue
        avg = b["_px_num"] / b["_px_den"] if b["_px_den"] else 0.0
        out.append({
            "account": b["account"],
            "underlying": b["underlying"],
            "tradingClass": b["tradingClass"],
            "expiry": b["expiry"],
            "strike": b["strike"],
            "right": b["right"],
            "multiplier": b["multiplier"],
            "qty": b["qty"],
            "avg_price": round(avg, 4),
            "source": "ONE",
            "trade_ids": sorted(b["trade_ids"]),
        })
    out.sort(key=lambda r: (r["account"], r["underlying"], r["expiry"],
                            r["strike"], r["right"]))
    return out


def _pretty_expiry(e):
    return f"{e[:4]}-{e[4:6]}-{e[6:]}" if e and len(e) == 8 else e


def print_table(positions: list[dict]) -> None:
    by_acct: dict[str, list[dict]] = defaultdict(list)
    for p in positions:
        by_acct[p["account"]].append(p)

    print("\n" + "=" * 74)
    print(f"ONE OPEN POSITIONS (from Summary Report)   "
          f"{datetime.now().isoformat(timespec='seconds')}")
    print(f"ONE accounts: {', '.join(sorted(by_acct)) or '(none)'}")
    print("=" * 74)
    for acct in sorted(by_acct):
        rows = by_acct[acct]
        print(f"\n[{acct}]  {len(rows)} net legs")
        print(f'    {"INSTRUMENT":<30}{"QTY":>7}{"ONE PX":>12}')
        for r in rows:
            lbl = f'{r["tradingClass"]} {_pretty_expiry(r["expiry"])} ' \
                  f'{r["strike"]:g}{r["right"]}'
            print(f'    {lbl:<30}{r["qty"]:>7.0f}{r["avg_price"]:>12.4f}')
    print("=" * 74 + "\n")


def find_default_csv(search_dirs=None) -> str:
    """Newest supported ONE report in Downloads/Documents, else sample."""
    dirs = search_dirs or DEFAULT_EXPORT_DIRS
    hits = []
    for d in dirs:
        for pattern in EXPORT_GLOBS:
            hits += glob.glob(os.path.join(os.path.expanduser(d), pattern))
    if hits:
        return max(hits, key=os.path.getmtime)
    return SAMPLE


def extract_one_trades(legs: list[dict]) -> list[dict]:
    """Group open ONE legs by (account, trade_id) to reconstruct active ONE trades."""
    grouped = defaultdict(list)
    for lg in legs:
        if lg.get("is_open"):
            grouped[(lg["account"], lg.get("trade_id"))].append(lg)

    trades = []
    for (acct, tid), tlegs in grouped.items():
        name = next((l.get("trade_name") for l in tlegs if l.get("trade_name")),
                    None) or (f"Trade #{tid}" if tid else "Open Trade")
        und = next((l.get("underlying") for l in tlegs if l.get("underlying")),
                   tlegs[0].get("tradingClass"))
        trades.append({
            "account": acct,
            "trade_id": tid,
            "trade_name": name,
            "underlying": und,
            "legs": tlegs,
        })
    return trades


def build_one_snapshot(path: str) -> dict:
    """Read a ONE Summary Report CSV -> canonical ONE snapshot dict.
    Reused by the dashboard daemon (no console output / file write)."""
    legs = read_summary_report(path)
    positions = net_positions(legs)
    trades = extract_one_trades(legs)
    return {
        "captured_at": datetime.now(timezone.utc).isoformat(),
        "source": "ONE/SummaryReport",
        "source_file": path,
        "source_mtime": os.path.getmtime(path) if os.path.exists(path) else None,
        "accounts": sorted({p["account"] for p in positions}),
        "positions": positions,
        "trades": trades,
        "raw_legs": legs,
    }


def main() -> None:
    path = sys.argv[1] if len(sys.argv) > 1 else find_default_csv()
    print(f"Reading ONE Summary Report: {path}")
    legs = read_summary_report(path)
    open_legs = [l for l in legs if l["is_open"]]
    print(f"Parsed {len(legs)} leg rows  ({len(open_legs)} open, "
          f"{len(legs) - len(open_legs)} closed)")

    positions = net_positions(legs)
    trades = extract_one_trades(legs)
    print_table(positions)

    snapshot = {
        "captured_at": datetime.now(timezone.utc).isoformat(),
        "source": "ONE/SummaryReport",
        "source_file": path,
        "source_mtime": os.path.getmtime(path) if os.path.exists(path) else None,
        "accounts": sorted({p["account"] for p in positions}),
        "positions": positions,
        "trades": trades,
        "raw_legs": legs,
    }
    with open(OUTPUT_JSON, "w") as fh:
        json.dump(snapshot, fh, indent=2, default=str)
    print(f"Wrote {OUTPUT_JSON}  ({len(positions)} net positions across "
          f"{len(snapshot['accounts'])} accounts, {len(trades)} active ONE trades)")


if __name__ == "__main__":
    main()
