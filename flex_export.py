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
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

HERE = os.path.dirname(os.path.abspath(__file__))
IBKR_JSON = os.path.join(HERE, "canonical_positions.json")
OUT_DIR = os.path.join(HERE, "flex_export")
DEFAULT_TZ = "Australia/Sydney"          # ONE runs in local time; match it

HEADER = ["DateTime", "Buy/Sell", "AssetClass", "Symbol", "Quantity",
          "TradePrice", "IBCommission", "UnderlyingSymbol", "NetCash"]


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
        if not is_option_leg(f):
            skipped += 1
            continue
        by_acct[f["account"]].append(fill_to_row(f, tz))
    return by_acct, skipped


def main():
    snap = json.load(open(IBKR_JSON))
    tz_name = DEFAULT_TZ
    cfg_path = os.path.join(HERE, "config.json")
    if os.path.exists(cfg_path):
        tz_name = json.load(open(cfg_path)).get("flex_timezone", DEFAULT_TZ)
    by_acct, skipped = generate(snap, tz_name)
    os.makedirs(OUT_DIR, exist_ok=True)

    total = 0
    for acct, rows in sorted(by_acct.items()):
        path = os.path.join(OUT_DIR, f"ONEImport_{acct}.csv")
        with open(path, "w", newline="") as fh:
            w = csv.writer(fh, quoting=csv.QUOTE_ALL)
            w.writerow(HEADER)
            w.writerows(rows)
        total += len(rows)
        print(f"  {acct}: {len(rows):>3} fills -> {path}")

    print(f"\nWrote {len(by_acct)} account file(s), {total} fill rows "
          f"({skipped} non-option/combo fills skipped) into {OUT_DIR}\\")
    if any(r[6] == "" for rows in by_acct.values() for r in rows):
        print("NOTE: some rows have blank IBCommission (commission not yet "
              "captured in this snapshot; re-run canonical_engine.py for exact "
              "cost basis). NetCash for those excludes commission.")


if __name__ == "__main__":
    main()
