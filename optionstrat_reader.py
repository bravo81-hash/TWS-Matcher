#!/usr/bin/env python3
"""
OptionStrat reader — parses OptionStrat's "all active" .xlsx export into combos
with per-leg symbol / signed qty / entry price, normalized to the same leg
identity the ONE reader uses, for ONE <-> OptionStrat reconciliation.

Report shape (single "Trades" sheet, hierarchical):
  row: combo header  -> col A = name ("MGN.SPX.ALLANTIS.130/160.NOV/DEC.1 - 2 LOTS")
  row: leg           -> col A = ".SPX261120P7450", B=Quantity(signed),
                        C=Entry Price, D=Current Price, E=Close Price
A leg with a Close Price is CLOSED (excluded from the current position), same as
ONE's CloseDate legs. Leg symbol uses the real (Friday) expiration — no Thu/Fri
shift needed (unlike IBKR).

  python optionstrat_reader.py [path-to-all-active-*.xlsx]
"""

from __future__ import annotations

import glob
import os
import re
import sys
from collections import defaultdict

import openpyxl

DEFAULT_EXPORT_DIRS = [
    os.path.join(os.path.expanduser("~"), "Downloads"),
    os.path.join(os.path.expanduser("~"), "Documents"),
]
EXPORT_GLOB = "all-active*.xlsx"

# .SPXW261030P7300 / .MSFT260821P340 / .BR260821P135
LEG_RE = re.compile(r"^\.([A-Za-z]+)(\d{6})([CP])(\d+(?:\.\d+)?)$")


def find_default_xlsx(search_dirs=None) -> str | None:
    dirs = search_dirs or DEFAULT_EXPORT_DIRS
    hits = []
    for d in dirs:
        hits += glob.glob(os.path.join(os.path.expanduser(d), EXPORT_GLOB))
    return max(hits, key=os.path.getmtime) if hits else None


def parse_leg_symbol(sym: str):
    """'.SPXW261030P7300' -> (tradingClass, expiry YYYYMMDD, right, strike)."""
    m = LEG_RE.match(sym.strip())
    if not m:
        return None
    root, yymmdd, right, strike = m.groups()
    return root, "20" + yymmdd, right, float(strike)


def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def read_report(path: str) -> list[dict]:
    """-> [{name, legs:[{tradingClass,expiry,right,strike,qty,entry_price,is_open}]}]."""
    wb = openpyxl.load_workbook(path, data_only=True, read_only=True)
    ws = wb.worksheets[0]
    combos: list[dict] = []
    cur = None
    for row in ws.iter_rows(values_only=True):
        a = row[0] if row else None
        if a is None or str(a).strip() == "":
            continue
        a = str(a).strip()
        if a in ("Name", "Symbol"):          # the two header-label rows
            continue
        if a.startswith("."):                # leg row
            if cur is None:
                continue
            parsed = parse_leg_symbol(a)
            if not parsed:
                continue
            tc, expiry, right, strike = parsed
            qty = _num(row[1]) if len(row) > 1 else None
            entry = _num(row[2]) if len(row) > 2 else None
            close_price = _num(row[4]) if len(row) > 4 else None
            if qty is None or qty == 0:
                continue
            cur["legs"].append({
                "tradingClass": tc, "expiry": expiry, "right": right,
                "strike": strike, "qty": qty, "entry_price": entry,
                "is_open": close_price is None,
            })
        else:                                # combo header row
            cur = {"name": a, "legs": []}
            combos.append(cur)
    wb.close()
    return [c for c in combos if c["legs"]]


def net_open_legs(combo: dict) -> list[dict]:
    """Net a combo's OPEN legs by identity -> signed qty + qty-wtd entry price."""
    buckets = {}
    for lg in combo["legs"]:
        if not lg["is_open"]:
            continue
        key = (lg["tradingClass"], lg["expiry"], lg["right"], lg["strike"])
        b = buckets.setdefault(key, {"qty": 0.0, "num": 0.0, "den": 0.0})
        b["qty"] += lg["qty"]
        if lg["entry_price"] is not None:
            b["num"] += abs(lg["qty"]) * lg["entry_price"]
            b["den"] += abs(lg["qty"])
    out = []
    for (tc, expiry, right, strike), b in buckets.items():
        if abs(b["qty"]) < 1e-9:
            continue
        out.append({"tradingClass": tc, "expiry": expiry, "right": right,
                    "strike": strike, "qty": b["qty"],
                    "entry_price": round(b["num"] / b["den"], 4) if b["den"] else None})
    out.sort(key=lambda r: (r["tradingClass"], r["expiry"], r["strike"], r["right"]))
    return out


def _pretty(e):
    return f"{e[:4]}-{e[4:6]}-{e[6:]}" if e and len(e) == 8 else e


def main():
    path = sys.argv[1] if len(sys.argv) > 1 else find_default_xlsx()
    if not path:
        print("No OptionStrat all-active*.xlsx found in Downloads/Documents.")
        return
    print(f"Reading OptionStrat report: {path}\n")
    combos = read_report(path)
    print(f"{len(combos)} combos\n")
    for c in combos:
        legs = net_open_legs(c)
        n_closed = sum(1 for l in c["legs"] if not l["is_open"])
        extra = f"  (+{n_closed} closed legs)" if n_closed else ""
        print(f"{c['name']}  [{len(legs)} open legs]{extra}")
        for lg in legs:
            side = "+" if lg["qty"] > 0 else ""
            px = "" if lg["entry_price"] is None else f"@ {lg['entry_price']}"
            print(f"    {lg['tradingClass']:5} {_pretty(lg['expiry'])} "
                  f"{lg['strike']:g}{lg['right']}  {side}{lg['qty']:g}  {px}")


if __name__ == "__main__":
    main()
