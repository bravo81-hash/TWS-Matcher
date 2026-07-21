#!/usr/bin/env python3
"""
ONE <-> OptionStrat reconciliation.

Matches combos, legs and prices between ONE's Summary Report and OptionStrat's
"all active" export — the two systems you keep in sync by hand. Distinct from the
IBKR<->ONE engine in reconcile.py.

MATCHING (robust to the mixed old/new naming):
  - Each combo -> account code (MGN/SMSF) + a set of leg identities
    (tradingClass, expiry, right, strike).
  - Pair ONE<->OptionStrat combos by leg-set OVERLAP (Jaccard), with a bonus when
    account codes agree and a hard block when they conflict (so a strategy mirrored
    in MGN and SMSF isn't cross-matched). Greedy, one-to-one, min 50% overlap.
  - Names are advisory: matched combos whose names differ are flagged to align.

WITHIN a matched pair, per leg:
  - present in both? qty equal? entry/open price within tolerance (a few cents —
    OptionStrat is manual, ONE may be Flex-imported).

Note ONE and OptionStrat both use the real (Friday) expiration, so no Thu/Fri
shift is needed (that only affects IBKR).

  python recon_one_os.py [one.csv] [optionstrat.xlsx]
"""

from __future__ import annotations

import json
import os
import sys
from collections import defaultdict

import one_reader
import optionstrat_reader

HERE = os.path.dirname(os.path.abspath(__file__))
PRICE_TOL = 0.10          # default per-share tolerance (cents); overridable via config
MIN_OVERLAP = 0.5


# ------------------------------------------------------------------ combos
def _leg_key(l):
    return (l["tradingClass"], l["expiry"], l["right"], float(l["strike"]))


def _os_account_code(name: str) -> str | None:
    u = name.strip().upper()
    if u.startswith("MGN"):
        return "MGN"
    if u.startswith("SMSF"):
        return "SMSF"
    if "SMSF" in u:
        return "SMSF"
    return None


def one_combos(one_path, account_map, account_codes) -> list[dict]:
    account_map = account_map or {}
    account_codes = account_codes or {}
    legs = one_reader.read_summary_report(one_path)
    trades = defaultdict(list)
    for l in legs:
        if l["is_open"]:
            trades[(l["account"], l["trade_id"])].append(l)

    out = []
    for (acct, tid), tlegs in trades.items():
        name = next((l.get("trade_name") for l in tlegs if l.get("trade_name")),
                    None) or f"trade {tid}"
        code = account_codes.get(account_map.get(acct), None)
        # net open legs by identity (use expiry_listed = real Friday expiration)
        buckets = {}
        for l in tlegs:
            key = (l["tradingClass"], l.get("expiry_listed") or l["expiry"],
                   l["right"], float(l["strike"]))
            b = buckets.setdefault(key, {"qty": 0.0, "num": 0.0, "den": 0.0})
            b["qty"] += l["qty"]
            b["num"] += abs(l["qty"]) * l["open_price"]
            b["den"] += abs(l["qty"])
        legs_out = []
        for (tc, expiry, right, strike), b in buckets.items():
            if abs(b["qty"]) < 1e-9:
                continue
            legs_out.append({"tradingClass": tc, "expiry": expiry, "right": right,
                             "strike": strike, "qty": b["qty"],
                             "price": round(b["num"] / b["den"], 4) if b["den"] else None})
        if legs_out:
            out.append({"name": name, "code": code, "one_account": acct,
                        "legs": legs_out})
    return out


def os_combos(os_path) -> list[dict]:
    out = []
    for c in optionstrat_reader.read_report(os_path):
        legs = optionstrat_reader.net_open_legs(c)
        if not legs:
            continue
        out.append({"name": c["name"], "code": _os_account_code(c["name"]),
                    "legs": [{"tradingClass": l["tradingClass"], "expiry": l["expiry"],
                              "right": l["right"], "strike": l["strike"],
                              "qty": l["qty"], "price": l["entry_price"]}
                             for l in legs]})
    return out


# ------------------------------------------------------------------ matching
def _score(a, b):
    ka = {_leg_key(l) for l in a["legs"]}
    kb = {_leg_key(l) for l in b["legs"]}
    if not ka or not kb:
        return 0.0
    inter = len(ka & kb)
    union = len(ka | kb)
    overlap = inter / union
    if a["code"] and b["code"]:
        if a["code"] != b["code"]:
            return 0.0                    # different accounts -> never match
        return overlap + 1.0              # same account -> bonus
    return overlap


def match_combos(one_list, os_list):
    cand = []
    for i, oc in enumerate(one_list):
        for j, sc in enumerate(os_list):
            s = _score(oc, sc)
            if s > 0:
                cand.append((s, i, j))
    cand.sort(reverse=True)
    used_i, used_j, pairs = set(), set(), []
    for s, i, j in cand:
        if i in used_i or j in used_j:
            continue
        # require real leg overlap (bonus alone isn't enough)
        overlap = s - 1.0 if s > 1.0 else s
        if overlap < MIN_OVERLAP:
            continue
        used_i.add(i); used_j.add(j)
        pairs.append((i, j))
    one_only = [i for i in range(len(one_list)) if i not in used_i]
    os_only = [j for j in range(len(os_list)) if j not in used_j]
    return pairs, one_only, os_only


def _px_ok(a, b, tol):
    if a is None or b is None:
        return True                       # can't compare -> don't flag
    return abs(a - b) <= tol


def diff_pair(one_c, os_c, tol):
    om = {_leg_key(l): l for l in one_c["legs"]}
    sm = {_leg_key(l): l for l in os_c["legs"]}
    legrows, ok = [], True
    for k in sorted(set(om) | set(sm)):
        o, s = om.get(k), sm.get(k)
        tc, expiry, right, strike = k
        label = f"{tc} {expiry[:4]}-{expiry[4:6]}-{expiry[6:]} {strike:g}{right}"
        if o and not s:
            legrows.append({"label": label, "status": "ONE_ONLY",
                            "one_qty": o["qty"], "one_px": o["price"],
                            "os_qty": None, "os_px": None}); ok = False
        elif s and not o:
            legrows.append({"label": label, "status": "OS_ONLY",
                            "one_qty": None, "one_px": None,
                            "os_qty": s["qty"], "os_px": s["price"]}); ok = False
        else:
            qty_ok = abs(o["qty"] - s["qty"]) < 1e-9
            px_ok = _px_ok(o["price"], s["price"], tol)
            status = ("QTY_MISMATCH" if not qty_ok
                      else "PRICE_DIFF" if not px_ok else "MATCH")
            if status != "MATCH":
                ok = False
            legrows.append({"label": label, "status": status,
                            "one_qty": o["qty"], "one_px": o["price"],
                            "os_qty": s["qty"], "os_px": s["price"]})
    return {"one_name": one_c["name"], "os_name": os_c["name"],
            "code": one_c["code"] or os_c["code"],
            "name_aligned": _norm(one_c["name"]) == _norm(os_c["name"]),
            "clean": ok, "legs": legrows}


def _norm(name):
    import re
    n = re.sub(r"\s*-\s*\d+\s+LOTS?\s*$", "", name.strip(), flags=re.I)
    return re.sub(r"[^A-Z0-9]", "", n.upper())


def reconcile(one_path, os_path, cfg=None):
    cfg = cfg or {}
    tol = float(cfg.get("one_os_price_tol", PRICE_TOL))
    ones = one_combos(one_path, cfg.get("account_map"), cfg.get("account_codes"))
    oss = os_combos(os_path)
    pairs, one_only, os_only = match_combos(ones, oss)
    matched = [diff_pair(ones[i], oss[j], tol) for i, j in pairs]
    return {
        "one_source": one_path, "os_source": os_path, "price_tol": tol,
        "matched": matched,
        "one_only": [{"name": ones[i]["name"], "code": ones[i]["code"],
                      "legs": len(ones[i]["legs"])} for i in one_only],
        "os_only": [{"name": oss[j]["name"], "code": oss[j]["code"],
                     "legs": len(oss[j]["legs"])} for j in os_only],
    }


# ------------------------------------------------------------------ CLI
def main():
    cfg = {}
    cfg_path = os.path.join(HERE, "config.json")
    if os.path.exists(cfg_path):
        cfg = json.load(open(cfg_path))
    one_path = sys.argv[1] if len(sys.argv) > 1 else \
        one_reader.find_default_csv(cfg.get("one_export_dirs"))
    os_path = sys.argv[2] if len(sys.argv) > 2 else \
        optionstrat_reader.find_default_xlsx(cfg.get("one_export_dirs"))
    if not os_path:
        print("No OptionStrat all-active*.xlsx found."); return
    print(f"ONE:        {one_path}")
    print(f"OptionStrat:{os_path}")
    r = reconcile(one_path, os_path, cfg)

    clean = sum(1 for m in r["matched"] if m["clean"])
    print(f"\n{'='*74}\nONE <-> OptionStrat   tol=${r['price_tol']:.2f}/sh")
    print(f"matched {len(r['matched'])} combos ({clean} clean), "
          f"{len(r['one_only'])} only in ONE, {len(r['os_only'])} only in OptionStrat\n"
          + "=" * 74)

    for m in r["matched"]:
        if m["clean"] and m["name_aligned"]:
            continue
        tag = "OK" if m["clean"] else "DIFF"
        print(f"\n[{tag}] {m['one_name']}")
        if not m["name_aligned"]:
            print(f"     OptionStrat name: {m['os_name']}  <-- names differ")
        for lg in m["legs"]:
            if lg["status"] == "MATCH":
                continue
            oq = "" if lg["one_qty"] is None else f"{lg['one_qty']:+g}"
            sq = "" if lg["os_qty"] is None else f"{lg['os_qty']:+g}"
            op = "" if lg["one_px"] is None else f"{lg['one_px']:.2f}"
            sp = "" if lg["os_px"] is None else f"{lg['os_px']:.2f}"
            print(f"     {lg['status']:<12} {lg['label']:<26} "
                  f"ONE[{oq:>4} @{op:>8}]  OS[{sq:>4} @{sp:>8}]")

    if r["one_only"]:
        print("\n--- only in ONE (no OptionStrat combo) ---")
        for c in r["one_only"]:
            print(f"     {c['name']}  ({c['legs']} legs)")
    if r["os_only"]:
        print("\n--- only in OptionStrat (no ONE combo) ---")
        for c in r["os_only"]:
            print(f"     {c['name']}  ({c['legs']} legs)")


if __name__ == "__main__":
    main()
