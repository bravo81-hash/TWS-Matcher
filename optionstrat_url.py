#!/usr/bin/env python3
"""
OptionStrat URL generator  —  component 3.

OptionStrat has no broker sync; a strategy is fully encoded in its build URL.
So OptionStrat is DERIVED from our truth, never diffed: we emit one build URL
per strategy and the user opens/saves it.

Grouping: one URL per ONE *trade* (e.g. "A14.JULY 02 - 4 LOTS") — the
human-meaningful strategy unit (from the ONE Summary Report, open legs only).

Expiry: OptionStrat lists a contract under its ACTUAL EXPIRATION (the Friday for
AM-settled monthlies). That is ONE's "Expiry" column (expiry_listed), NOT the
OSI-symbol date (Saturday) and NOT IBKR's last-trade date (Thursday). Using the
Thursday makes OptionStrat say "options no longer available", so we use ONE's
listed expiry per leg.

URL grammar (confirmed from two real OptionStrat build URLs:
  1-lot:  /build/.../QQQ/.QQQ261016P690,-.QQQ261016P715
  4-lot:  /build/.../QQQ/.QQQ261016P690x4,.QQQ261016P715x-4 ):
    https://optionstrat.com/build/custom/{TICKER}/{leg},{leg},...
    leg base = .{ROOT}{YYMMDD}{C|P}{STRIKE}   STRIKE = plain (690, 7350)
    quantity/direction (only confirmed-valid forms used):
       +1        -> base               (no suffix)
       -1        -> -base              (leading minus)
       |q| > 1   -> base x{signed_q}   (suffix, sign = direction: x4, x-4)
This URL encodes legs only (the samples carried no cost-basis); entry prices are
not embedded.

  python optionstrat_url.py [path-to-ONESummaryReport.csv]
"""

from __future__ import annotations

import json
import sys
from collections import defaultdict
from datetime import datetime, timezone

import one_reader

BASE = "https://optionstrat.com/build/custom"
OUTPUT_JSON = "optionstrat_urls.json"


def _strike(s: float) -> str:
    return f"{s:g}"                       # 7350.0 -> "7350", 7.5 -> "7.5"


# ----------------------------------------------------------------- encoding
def encode_leg(leg: dict, expiry: str) -> str:
    qty = int(round(leg["qty"]))
    root = leg["tradingClass"]
    yymmdd = str(expiry)[2:8]
    base = f".{root}{yymmdd}{leg['right']}{_strike(leg['strike'])}"
    if qty == 1:
        return base
    if qty == -1:
        return f"-{base}"
    return f"{base}x{qty}"                        # x4, x-4 (sign = direction)


def build_url(underlying: str, legs_with_exp) -> str:
    tokens = [encode_leg(l, exp) for l, exp in legs_with_exp]
    return f"{BASE}/{underlying}/" + ",".join(tokens)


def group_by_trade(legs):
    trades = defaultdict(list)
    for l in legs:
        if l["is_open"]:
            trades[(l["account"], l["trade_id"])].append(l)
    return trades


def leg_expiry(leg: dict) -> tuple[str, bool]:
    """The date OptionStrat lists the contract under = ONE's *actual expiration*
    (its Expiry column, the Friday for AM-settled monthlies), NOT the OSI-symbol
    date (Saturday) nor IBKR's last-trade date (Thursday). Falls back to the OSI
    date if the listed expiry is missing."""
    e = leg.get("expiry_listed")
    if e:
        return e, True
    return leg["expiry"], False


def _pretty_exp(e):
    e = str(e) if e else ""
    return f"{e[:4]}-{e[4:6]}-{e[6:]}" if len(e) == 8 else (e or "-")


def combo_legs(tlegs):
    """The combo's current authoritative legs (what your saved OptionStrat combo
    of the same name should contain)."""
    out = []
    for l in tlegs:
        exp, _ = leg_expiry(l)
        qty = int(round(l["qty"]))
        out.append({
            "side": "Buy" if qty > 0 else "Sell",
            "qty": abs(qty),
            "label": f"{l['tradingClass']} {_pretty_exp(exp)} "
                     f"{_strike(l['strike'])}{l['right']}",
            "price": round(l["open_price"], 4),
        })
    out.sort(key=lambda r: r["label"])
    return out


def generate(path: str, ibkr_legs=None) -> dict:
    # ibkr_legs kept for call-compatibility; OptionStrat uses ONE's listed expiry.
    # One entry per ONE combo (by name) = the saved OptionStrat strategy to EDIT.
    legs = one_reader.read_summary_report(path)
    trades = group_by_trade(legs)
    out = []
    for (account, trade_id), tlegs in trades.items():
        name = next((l.get("trade_name") for l in tlegs if l.get("trade_name")),
                    None) or f"trade {trade_id}"
        # build-URL(s) for CREATING a brand-new combo (one per underlying)
        new_urls = []
        for u in sorted({l["underlying"] for l in tlegs}):
            u_legs = [l for l in tlegs if l["underlying"] == u]
            resolved = [(l, leg_expiry(l)[0]) for l in u_legs]
            new_urls.append({"underlying": u, "url": build_url(u, resolved)})
        out.append({
            "account": account,
            "trade_id": trade_id,
            "name": name,
            "underlyings": sorted({l["underlying"] for l in tlegs}),
            "legs": combo_legs(tlegs),
            "create_urls": new_urls,
        })
    out.sort(key=lambda r: (r["account"], r["name"]))
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "source_file": path,
        "strategies": out,
    }


def main():
    path = sys.argv[1] if len(sys.argv) > 1 else one_reader.find_default_csv()
    print(f"Reading ONE Summary Report: {path}")
    result = generate(path)

    by_acct = defaultdict(list)
    for s in result["strategies"]:
        by_acct[s["account"]].append(s)

    for acct in sorted(by_acct):
        print(f"\n[{acct}]")
        for s in by_acct[acct]:
            print(f'  {s["name"]}  ({len(s["legs"])} legs)')
            for lg in s["legs"]:
                print(f'      {lg["side"]:<4} {lg["qty"]:>3} {lg["label"]:<26} '
                      f'@ {lg["price"]}')
            for u in s["create_urls"]:
                print(f'      [create new {u["underlying"]}] {u["url"]}')

    with open(OUTPUT_JSON, "w") as fh:
        json.dump(result, fh, indent=2)
    print(f"\nWrote {OUTPUT_JSON}  ({len(result['strategies'])} combos)")


if __name__ == "__main__":
    main()
