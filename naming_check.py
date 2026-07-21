#!/usr/bin/env python3
"""
naming_check.py  —  migration aid for the position-naming convention.

For each ONE combo (grouped by TradeId), report:
  - the current ONE trade name,
  - whether it conforms to the naming.py convention (parses + validates),
  - a suggested skeleton name auto-derived from the combo's ONE legs
    (account code + ticker + expiry + lots; STRATEGY left '?' for you to fill).

Feeds naming.py from ONE data on purpose: ONE's `expiry_listed` is the REAL
expiration (the Friday), so is_third_friday() classifies monthlies correctly —
IBKR's last-trade Thursday would misfire.

  python naming_check.py [path-to-ONESummaryReport.csv]
"""

from __future__ import annotations

import json
import os
import sys
from collections import defaultdict

import naming
import one_reader

HERE = os.path.dirname(os.path.abspath(__file__))


def _account_code(one_account, account_map, account_codes):
    """ONE group name -> IBKR account id -> short account code (MGN/SMSF)."""
    ibkr = account_map.get(one_account)
    return account_codes.get(ibkr, ibkr or one_account)


def report(one_path, account_map=None, account_codes=None):
    account_map = account_map or {}
    account_codes = account_codes or {}
    legs = one_reader.read_summary_report(one_path)

    trades = defaultdict(list)
    for l in legs:
        if l["is_open"]:
            trades[(l["account"], l["trade_id"])].append(l)

    out = []
    for (acct, tid), tlegs in sorted(trades.items()):
        name = next((l.get("trade_name") for l in tlegs if l.get("trade_name")),
                    None) or f"trade {tid}"
        code = _account_code(acct, account_map, account_codes)

        # suggested skeleton: use the REAL expiration (expiry_listed) so monthlies
        # render as MON, not MMDD.
        norm = [{**l, "expiry": l.get("expiry_listed") or l.get("expiry")}
                for l in tlegs]
        try:
            skel, geom = naming.skeleton_from_legs(code, norm)
            suggested = skel.format()
        except Exception as exc:                       # never break the panel
            suggested, geom = f"(err: {exc})", ""

        # does the CURRENT ONE name already conform?
        try:
            pn = naming.PositionName.parse(name)
            warnings = pn.validate()
            conforms = not warnings
        except Exception:
            conforms, warnings = False, ["doesn't match template"]

        out.append({
            "account": acct, "code": code, "trade_id": tid,
            "current": name, "conforms": conforms, "warnings": warnings,
            "suggested": suggested, "geometry": geom,
        })
    return out


def main():
    path = sys.argv[1] if len(sys.argv) > 1 else one_reader.find_default_csv()
    cfg = {}
    cfg_path = os.path.join(HERE, "config.json")
    if os.path.exists(cfg_path):
        cfg = json.load(open(cfg_path))
    rows = report(path, cfg.get("account_map"), cfg.get("account_codes"))
    ok = sum(1 for r in rows if r["conforms"])
    print(f"Naming convention: {ok}/{len(rows)} combos conform\n")
    for r in sorted(rows, key=lambda r: (r["code"], r["current"])):
        mark = "OK " if r["conforms"] else "-> "
        print(f"  {mark} {r['current']}")
        if not r["conforms"]:
            print(f"        suggest: {r['suggested']}   [{r['geometry']}]")


if __name__ == "__main__":
    main()
