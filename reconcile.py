#!/usr/bin/env python3
"""
Reconciliation core  —  IBKR truth  vs  ONE.

Consumes:
  canonical_positions.json  (from canonical_engine.py  — IBKR source of truth)
  one_positions.json        (from one_reader.py        — ONE open positions)
  config.json               (ONE-group -> IBKR-account map + tolerances)

Produces a per-IBKR-account diff and writes reconciliation.json. For each
account it classifies every instrument as:
  MATCH          qty equal AND price within tolerance
  PRICE_DRIFT    qty equal but ONE's recorded price differs > tolerance
  QTY_MISMATCH   present in both, quantities differ
  IBKR_ONLY      in IBKR, absent from ONE   (a leg ONE never modeled)
  ONE_ONLY       in ONE,  absent from IBKR  (modeled/closed in ONE, not in IBKR)

Key handling (see memory account-mapping-and-expiry-offset):
  - ONE strategy groups are commingled into one IBKR account -> net them together.
  - ONE's expiry runs 1-2 days later than IBKR's last-trade date -> match on
    (tradingClass, right, strike) + NEAREST expiry within tolerances.expiry_days.
  - IBKR avg_price folds in commission -> small price tolerance.
  - Unmapped ONE groups (e.g. TimeZone/test) are reported under ONE_ONLY,
    tagged, unless listed in ignore_one_accounts.

USAGE
  python reconcile.py            # uses the three json/config files in this dir
"""

from __future__ import annotations

import json
import os
from collections import defaultdict
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
IBKR_JSON = os.path.join(HERE, "canonical_positions.json")
ONE_JSON = os.path.join(HERE, "one_positions.json")
CONFIG = os.path.join(HERE, "config.json")
OUTPUT_JSON = os.path.join(HERE, "reconciliation.json")


# ----------------------------------------------------------------- helpers
def _expiry_to_ord(e):
    """YYYYMMDD string -> ordinal day number, or None."""
    if not e or len(str(e)) != 8:
        return None
    try:
        return datetime.strptime(str(e), "%Y%m%d").toordinal()
    except ValueError:
        return None


def _pretty_expiry(e):
    e = str(e) if e else ""
    return f"{e[:4]}-{e[4:6]}-{e[6:]}" if len(e) == 8 else (e or "-")


def label_from_bucket(b):
    """Build a readable label from a netted bucket's key + expiry.
    key is (tradingClass, right, strike) for options or (underlying,'STK',None)."""
    a, b2, c = b["key"]
    if b2 in ("C", "P"):
        return f'{a} {_pretty_expiry(b["expiry"])} {c:g}{b2}'
    return f'{a} (STK)'


def price_matches(a, b, tol):
    diff = abs(a - b)
    return diff <= tol["price_abs"] or (b and diff / abs(b) <= tol["price_pct"])


# ------------------------------------------------------------- normalization
def ibkr_key(leg):
    """Stable identity key WITHOUT expiry (expiry matched fuzzily)."""
    if leg.get("right"):
        return (leg["tradingClass"], leg["right"], float(leg["strike"]))
    return (leg.get("underlying"), "STK", None)


def one_key(pos):
    if pos.get("right"):
        return (pos["tradingClass"], pos["right"], float(pos["strike"]))
    return (pos.get("underlying"), "STK", None)


def build_fill_price_map(fills):
    """Today's option fills netted by identity -> {qty (signed), px (clean qty-wtd
    fill price)}. Used to price legs established this session with the actual fill
    price instead of the commission-loaded avgCost."""
    agg = {}
    for f in fills or []:
        if f.get("secType") not in (None, "OPT"):
            continue
        if f.get("right") not in ("C", "P") or not f.get("strike") \
                or not f.get("expiry"):
            continue
        key = (f.get("account"), f["tradingClass"], f["right"],
               float(f["strike"]), f["expiry"])
        shares = abs(float(f.get("shares") or 0))
        signed = shares if f.get("side") == "BOT" else -shares
        price = float(f.get("price") or 0)
        a = agg.setdefault(key, {"qty": 0.0, "num": 0.0, "den": 0.0})
        a["qty"] += signed
        a["num"] += shares * price
        a["den"] += shares
    return {k: {"qty": a["qty"], "px": round(a["num"] / a["den"], 4)}
            for k, a in agg.items() if a["den"] > 0}


def classify_activity(fills, ibkr_legs):
    """Classify a set of option fills (vs the resulting current positions) into
    rolled / opened / closed / adjusted, to guide ONE + OptionStrat updates.

    For each leg: after = current net position; before = after - (net of these
    fills). Then OPENED (0->x), CLOSED (x->0), ADDED / REDUCED (same sign), or
    REVERSED. A CLOSED leg paired with an OPENED leg (same account+underlying+
    right, equal qty) is reported as a ROLL (from -> to)."""
    today, meta = {}, {}
    for f in fills or []:
        if f.get("secType") not in (None, "OPT"):
            continue
        if f.get("right") not in ("C", "P") or not f.get("strike") \
                or not f.get("expiry"):
            continue
        key = (f.get("account"), f["tradingClass"], f["expiry"],
               f["right"], float(f["strike"]))
        shares = abs(float(f.get("shares") or 0))
        signed = shares if f.get("side") == "BOT" else -shares
        today[key] = today.get(key, 0.0) + signed
        meta[key] = {"underlying": f.get("underlying"),
                     "px": float(f.get("price") or 0)}

    cur = {}
    for lg in ibkr_legs or []:
        if lg.get("right") not in ("C", "P"):
            continue
        key = (lg["account"], lg["tradingClass"], lg["expiry"],
               lg["right"], float(lg["strike"]))
        cur[key] = cur.get(key, 0.0) + lg["qty"]

    opened, closed, changed = [], [], []
    for key, net in today.items():
        if abs(net) < 1e-9:
            continue                              # round-trip flat; skip
        acct, tc, exp, right, strike = key
        after = cur.get(key, 0.0)
        before = after - net
        m = meta[key]
        base = {"account": acct, "underlying": m["underlying"], "right": right,
                "label": f"{tc} {_pretty_expiry(exp)} {strike:g}{right}",
                "px": m["px"]}
        if abs(before) < 1e-9 and abs(after) > 1e-9:
            opened.append({**base, "type": "OPENED", "qty": after})
        elif abs(after) < 1e-9 and abs(before) > 1e-9:
            closed.append({**base, "type": "CLOSED", "qty": before})
        elif before * after > 0 and abs(after) > abs(before):
            changed.append({**base, "type": "ADDED", "qty": after - before})
        elif before * after > 0 and abs(after) < abs(before):
            changed.append({**base, "type": "REDUCED", "qty": after - before})
        else:
            changed.append({**base, "type": "REVERSED", "qty": after})

    # pair closed+opened of equal size (same acct/underlying/right) as a roll
    rolled, used = [], set()
    for c in closed:
        for i, o in enumerate(opened):
            if i in used:
                continue
            if (c["account"] == o["account"] and c["right"] == o["right"]
                    and c.get("underlying") == o.get("underlying")
                    and abs(abs(c["qty"]) - abs(o["qty"])) < 1e-9):
                used.add(i)
                c["_paired"] = True
                rolled.append({"account": c["account"], "qty": abs(o["qty"]),
                               "from": c["label"], "to": o["label"]})
                break
    opened = [o for i, o in enumerate(opened) if i not in used]
    closed = [c for c in closed if not c.get("_paired")]
    return {"rolled": rolled, "opened": opened, "closed": closed,
            "changed": changed}


def net_ibkr(legs, fill_map=None):
    """Net IBKR legs by (account, key, expiry) -> qty + qty-wtd avg price.

    If a leg's whole net position was established by today's fills (net fill qty ==
    net position qty), use the clean fill price as the comparison price instead of
    avgCost — accurate drift detection for new/adjusted trades, free of commission
    cost-basis noise. Aged positions keep avgCost."""
    buckets = {}
    for lg in legs:
        k = (lg["account"], ibkr_key(lg), lg.get("expiry"))
        b = buckets.setdefault(k, {"account": lg["account"], "key": ibkr_key(lg),
                                    "expiry": lg.get("expiry"), "qty": 0.0,
                                    "num": 0.0, "den": 0.0,
                                    "underlying": lg.get("underlying"),
                                    "secType": lg.get("secType")})
        b["qty"] += lg["qty"]
        b["num"] += abs(lg["qty"]) * lg["avg_price"]
        b["den"] += abs(lg["qty"])
    out = _finish(buckets)
    if fill_map:
        for b in out:
            tc, right, strike = b["key"]
            fm = fill_map.get((b["account"], tc, right, strike, b["expiry"]))
            if fm and abs(fm["qty"] - b["qty"]) < 1e-9:   # whole position from today
                b["avg_price"] = fm["px"]
                b["price_source"] = "fill"
    return out


def net_one(positions, account_map):
    """Map ONE groups -> IBKR account, then net by (IBKR account, key, expiry)."""
    buckets = {}
    unmapped = defaultdict(list)
    for p in positions:
        ibkr_acct = account_map.get(p["account"])
        if ibkr_acct is None:
            unmapped[p["account"]].append(p)
            continue
        k = (ibkr_acct, one_key(p), p.get("expiry"))
        b = buckets.setdefault(k, {"account": ibkr_acct, "key": one_key(p),
                                   "expiry": p.get("expiry"), "qty": 0.0,
                                   "num": 0.0, "den": 0.0,
                                   "underlying": p.get("underlying"),
                                   "secType": "OPT" if p.get("right") else "STK"})
        b["qty"] += p["qty"]
        b["num"] += abs(p["qty"]) * p["avg_price"]
        b["den"] += abs(p["qty"])
    return _finish(buckets), unmapped


def _finish(buckets):
    out = []
    for b in buckets.values():
        if abs(b["qty"]) < 1e-9:
            continue
        b["avg_price"] = round(b["num"] / b["den"], 4) if b["den"] else 0.0
        del b["num"], b["den"]
        out.append(b)
    return out


# ------------------------------------------------------------- the matching
def reconcile_account(ibkr_rows, one_rows, tol):
    """Match within one IBKR account. Returns list of finding dicts."""
    # index by key (expiry-less) -> list of rows
    ibkr_by_key = defaultdict(list)
    for r in ibkr_rows:
        ibkr_by_key[r["key"]].append(dict(r))
    one_by_key = defaultdict(list)
    for r in one_rows:
        one_by_key[r["key"]].append(dict(r))

    findings = []
    all_keys = set(ibkr_by_key) | set(one_by_key)
    for key in all_keys:
        i_rows = ibkr_by_key.get(key, [])
        o_rows = one_by_key.get(key, [])
        findings.extend(_match_within_key(i_rows, o_rows, tol))
    return findings


def _match_within_key(i_rows, o_rows, tol):
    """Greedy nearest-expiry pairing of IBKR vs ONE rows sharing a key."""
    out = []
    i_left = list(i_rows)
    o_left = list(o_rows)

    # build candidate pairs by expiry proximity
    pairs = []
    for ii, ir in enumerate(i_left):
        for oi, orow in enumerate(o_left):
            io, oo = _expiry_to_ord(ir["expiry"]), _expiry_to_ord(orow["expiry"])
            if io is None and oo is None:          # both stocks
                dist = 0
            elif io is None or oo is None:
                continue
            else:
                dist = abs(io - oo)
            if dist <= tol["expiry_days"]:
                pairs.append((dist, ii, oi))
    pairs.sort()

    used_i, used_o = set(), set()
    for dist, ii, oi in pairs:
        if ii in used_i or oi in used_o:
            continue
        used_i.add(ii); used_o.add(oi)
        ir, orow = i_left[ii], o_left[oi]
        out.append(_compare(ir, orow, dist, tol))

    for ii, ir in enumerate(i_left):
        if ii not in used_i:
            out.append({"status": "IBKR_ONLY", "label": label_from_bucket(ir),
                        "ibkr_qty": ir["qty"], "ibkr_px": ir["avg_price"],
                        "one_qty": None, "one_px": None,
                        "expiry_ibkr": ir["expiry"], "expiry_one": None})
    for oi, orow in enumerate(o_left):
        if oi not in used_o:
            out.append({"status": "ONE_ONLY", "label": label_from_bucket(orow),
                        "ibkr_qty": None, "ibkr_px": None,
                        "one_qty": orow["qty"], "one_px": orow["avg_price"],
                        "expiry_ibkr": None, "expiry_one": orow["expiry"]})
    return out


def _compare(ir, orow, expiry_dist, tol):
    qty_ok = abs(ir["qty"] - orow["qty"]) < 1e-9
    px_ok = price_matches(ir["avg_price"], orow["avg_price"], tol)
    if not qty_ok:
        status = "QTY_MISMATCH"
    elif not px_ok:
        status = "PRICE_DRIFT"
    else:
        status = "MATCH"
    return {"status": status, "label": label_from_bucket(ir),
            "ibkr_qty": ir["qty"], "ibkr_px": ir["avg_price"],
            "one_qty": orow["qty"], "one_px": orow["avg_price"],
            "px_delta": round(ir["avg_price"] - orow["avg_price"], 4),
            "expiry_ibkr": ir["expiry"], "expiry_one": orow["expiry"],
            "expiry_offset_days": expiry_dist}


# ----------------------------------------------------------------- reporting
ORDER = ["QTY_MISMATCH", "ONE_ONLY", "IBKR_ONLY", "PRICE_DRIFT", "MATCH"]
SYM = {"MATCH": "OK ", "PRICE_DRIFT": "~px", "QTY_MISMATCH": "!QTY",
       "IBKR_ONLY": ">IB", "ONE_ONLY": ">ONE"}


def print_report(by_account, unmapped, ignore, accounts):
    print("\n" + "#" * 78)
    print(f"RECONCILIATION  IBKR vs ONE     {datetime.now().isoformat(timespec='seconds')}")
    print("#" * 78)

    grand = defaultdict(int)
    for acct in accounts:
        findings = by_account.get(acct, [])
        counts = defaultdict(int)
        n_ack = 0
        for f in findings:
            if f.get("acknowledged"):
                n_ack += 1
                continue
            counts[f["status"]] += 1
            grand[f["status"]] += 1
        clean = all(counts[s] == 0 for s in ("QTY_MISMATCH", "ONE_ONLY",
                                             "IBKR_ONLY", "PRICE_DRIFT"))
        tag = "MATCH" if clean else "DIFF"
        summary = " ".join(f"{s}:{counts[s]}" for s in ORDER if counts[s])
        if n_ack:
            summary += f" ACK:{n_ack}"
        print(f"\n=== [{acct}]  {tag}   {summary or '(no positions)'} ===")

        flags = [f for f in findings
                 if f["status"] != "MATCH" and not f.get("acknowledged")]
        flags.sort(key=lambda f: ORDER.index(f["status"]))
        if not flags:
            print(f"    all {counts['MATCH']} instruments reconcile.")
        for f in flags:
            iq = "" if f["ibkr_qty"] is None else f'{f["ibkr_qty"]:+.0f}'
            oq = "" if f["one_qty"] is None else f'{f["one_qty"]:+.0f}'
            ip = "" if f["ibkr_px"] is None else f'{f["ibkr_px"]:.4f}'
            op = "" if f["one_px"] is None else f'{f["one_px"]:.4f}'
            extra = ""
            if f["status"] == "PRICE_DRIFT":
                extra = f'  d_px={f["px_delta"]:+.4f}'
            print(f'    {SYM[f["status"]]:<4} {f["label"]:<26} '
                  f'IBKR[{iq:>5} @{ip:>9}]  ONE[{oq:>5} @{op:>9}]{extra}')

    if unmapped:
        print("\n=== UNMAPPED ONE GROUPS (no IBKR account in config) ===")
        for grp, rows in unmapped.items():
            note = "  (ignored)" if grp in ignore else "  <-- REVIEW"
            print(f"    {grp}: {len(rows)} net legs{note}")

    print("\n" + "-" * 78)
    print("GRAND TOTAL  " + "  ".join(f"{s}:{grand[s]}" for s in ORDER if grand[s]))
    print("-" * 78 + "\n")


def load_config(path=CONFIG):
    return json.load(open(path))


def reconcile_snapshots(ibkr_snapshot, one_snapshot, cfg):
    """In-memory reconciliation. Returns a result dict (also the json shape).

    ibkr_snapshot: {legs, captured_at, ...}   (canonical_engine.build_snapshot)
    one_snapshot:  {positions, source_file, ...} (one_reader.build_one_snapshot)
    Reused by both the CLI main() and the dashboard daemon.
    """
    account_map = cfg["account_map"]
    ignore = sorted(set(cfg.get("ignore_one_accounts", [])))
    tol = cfg["tolerances"]

    fill_map = build_fill_price_map(ibkr_snapshot.get("fills_today", []))
    ibkr_net = net_ibkr(ibkr_snapshot["legs"], fill_map)
    one_net, unmapped = net_one(one_snapshot["positions"], account_map)

    ibkr_by_acct = defaultdict(list)
    for r in ibkr_net:
        ibkr_by_acct[r["account"]].append(r)
    one_by_acct = defaultdict(list)
    for r in one_net:
        one_by_acct[r["account"]].append(r)

    accounts = sorted(set(ibkr_by_acct) | set(one_by_acct))
    by_account = {a: reconcile_account(ibkr_by_acct.get(a, []),
                                       one_by_acct.get(a, []), tol)
                  for a in accounts}

    # mark user-acknowledged (reviewed-OK) flags so they show as ACK, not DIFF
    ack = set(cfg.get("acknowledged", []))
    for acct, finds in by_account.items():
        for f in finds:
            if f["status"] != "MATCH" and f"{acct}|{f['label']}" in ack:
                f["acknowledged"] = True

    return {
        "reconciled_at": datetime.now(timezone.utc).isoformat(),
        "ibkr_source": ibkr_snapshot.get("captured_at"),
        "one_source": one_snapshot.get("source_file"),
        "tolerances": tol,
        "accounts": by_account,
        "unmapped_one_groups": {g: len(r) for g, r in unmapped.items()},
        "ignore_one_accounts": ignore,
        "fills_today": ibkr_snapshot.get("fills_today", []),
    }


# ----------------------------------------------------------------- main
def main():
    cfg = load_config()
    ibkr = json.load(open(IBKR_JSON))
    one = json.load(open(ONE_JSON))

    result = reconcile_snapshots(ibkr, one, cfg)
    accounts = sorted(result["accounts"])
    # rebuild unmapped detail for printing (counts already in result)
    _, unmapped = net_one(one["positions"], cfg["account_map"])
    print_report(result["accounts"], unmapped, set(result["ignore_one_accounts"]),
                 accounts)

    with open(OUTPUT_JSON, "w") as fh:
        json.dump(result, fh, indent=2, default=str)
    print(f"Wrote {OUTPUT_JSON}")


if __name__ == "__main__":
    main()
