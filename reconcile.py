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
  - ONE is netted on its listed expiry (one_reader.match_expiry), which leaves
    only the AM-settled monthlies offset -- IBKR reports the Thursday last-trade
    date, ONE the Friday expiration. So match on (tradingClass, right, strike) +
    NEAREST expiry within tolerances.expiry_days, which needs to be only 1.
    Keep it there: weekly SPXW expiries are >=2 days apart, so a 1-day window
    cannot pair two genuinely different contracts at the same strike.
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

import one_reader
from one_reader import normalize_account_name

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


def resolve_one_account(account, account_map):
    """Resolve a ONE report account, tolerating its numeric display prefix."""
    raw = str(account or "").strip()
    return account_map.get(raw) or account_map.get(normalize_account_name(raw))


def summarise_pnl_divergence(by_account: dict) -> dict:
    """Total dollars by which ONE's cost basis will misreport P&L vs IBKR.

    Rolled up per account and per underlying. ``gross`` sums the absolute
    divergences: offsetting legs can hide a large per-leg disagreement inside a
    small net, and the gross figure is what says how much is actually adrift.
    """
    total = gross = 0.0
    per_account: dict = defaultdict(float)
    per_ticker: dict = defaultdict(float)
    worst = []
    for acct, findings in (by_account or {}).items():
        for f in findings:
            d = f.get("pnl_divergence")
            if not d:
                continue
            total += d
            gross += abs(d)
            per_account[acct] += d
            per_ticker[f.get("underlying") or f.get("label", "?").split()[0]] += d
            worst.append({"account": acct, "label": f.get("label"),
                          "status": f.get("status"), "px_delta": f.get("px_delta"),
                          "pnl_divergence": d})
    worst.sort(key=lambda r: -abs(r["pnl_divergence"]))
    return {"net": round(total, 2), "gross": round(gross, 2),
            "by_account": {k: round(v, 2) for k, v in per_account.items()},
            "by_ticker": {k: round(v, 2) for k, v in per_ticker.items()},
            "worst": worst[:15]}


def is_expected_finding(finding):
    return bool(finding.get("expected"))


def is_actionable_finding(finding):
    """True only for a divergence that requires user attention."""
    return (
        finding.get("status") not in ("MATCH", "MATCH_FIFO_AVG")
        and not finding.get("acknowledged")
        and not is_expected_finding(finding)
    )


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


def _parse_timestamp_to_epoch(tstr: str | None) -> float:
    """Parse fill execution timestamp -> epoch float (seconds)."""
    if not tstr:
        return 0.0
    s = str(tstr).strip()
    try:
        dt = datetime.fromisoformat(s)
        return dt.timestamp()
    except Exception:
        pass
    for fmt in ("%Y-%m-%d %H:%M:%S%z", "%Y-%m-%d %H:%M:%S", "%Y%m%d  %H:%M:%S", "%Y%m%d %H:%M:%S"):
        try:
            dt = datetime.strptime(s.split(".")[0], fmt)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.timestamp()
        except Exception:
            pass
    return 0.0


def cluster_fills_by_timestamp(fills: list[dict], max_delta_secs: float = 5.0) -> list[dict]:
    """Group option fills by broker order identity, falling back to timestamp."""
    valid = []
    for f in fills or []:
        if f.get("secType") not in (None, "OPT"):
            continue
        if f.get("right") not in ("C", "P") or not f.get("strike") or not f.get("expiry"):
            continue
        t_epoch = _parse_timestamp_to_epoch(f.get("time"))
        valid.append({**f, "_epoch": t_epoch})

    by_order = defaultdict(list)
    by_acct = defaultdict(list)
    for f in valid:
        order_key = f.get("permId") or f.get("orderId")
        if order_key not in (None, "", 0, "0"):
            by_order[(f.get("account"), str(order_key))].append(f)
        else:
            by_acct[f.get("account")].append(f)

    clusters = [
        {"account": account,
         "time": min(rows, key=lambda row: row["_epoch"]).get("time"),
         "fills": sorted(rows, key=lambda row: row["_epoch"])}
        for (account, _), rows in by_order.items()
    ]
    for acct, acct_fills in by_acct.items():
        acct_fills.sort(key=lambda x: x["_epoch"])
        curr_cluster = []
        last_epoch = None
        for f in acct_fills:
            if last_epoch is None or (f["_epoch"] - last_epoch <= max_delta_secs and f["_epoch"] > 0):
                curr_cluster.append(f)
            else:
                clusters.append({"account": acct, "time": curr_cluster[0].get("time"), "fills": curr_cluster})
                curr_cluster = [f]
            last_epoch = f["_epoch"]
        if curr_cluster:
            clusters.append({"account": acct, "time": curr_cluster[0].get("time"), "fills": curr_cluster})

    clusters.sort(
        key=lambda cluster: _parse_timestamp_to_epoch(cluster.get("time")),
        reverse=True,
    )
    return clusters


def _find_best_one_trade(cluster_fills: list[dict], one_trades: list[dict] | None,
                         account_map: dict | None, expiry_tol_days: int = 2) -> dict | None:
    """Match an IBKR execution cluster to an active ONE trade ID using leg identity & expiry proximity."""
    if not cluster_fills or not one_trades:
        return None

    acct = cluster_fills[0].get("account")
    candidates = []
    for t in one_trades:
        one_acct = t.get("account")
        mapped = (resolve_one_account(one_acct, account_map)
                  if account_map else None) or normalize_account_name(one_acct)
        if mapped == acct:
            candidates.append(t)

    if not candidates:
        return None

    best_trade = None
    best_score = 0

    for t in candidates:
        score = 0
        t_legs = t.get("legs", [])
        for f in cluster_fills:
            f_tc = f.get("tradingClass") or f.get("underlying")
            f_und = f.get("underlying")
            f_exp_ord = _expiry_to_ord(f.get("expiry"))
            f_right = f.get("right")
            f_strike = float(f.get("strike") or 0)

            for l in t_legs:
                l_tc = l.get("tradingClass") or l.get("underlying")
                l_und = l.get("underlying")
                l_exp_ord = _expiry_to_ord(l.get("expiry_listed") or l.get("expiry"))
                l_right = l.get("right")
                l_strike = float(l.get("strike") or 0)

                und_match = (f_und == l_und) or (f_tc == l_tc)
                right_match = (f_right == l_right)
                strike_match = abs(f_strike - l_strike) < 1e-4
                exp_dist = abs(f_exp_ord - l_exp_ord) if (f_exp_ord and l_exp_ord) else 999
                exp_match = exp_dist <= expiry_tol_days

                if und_match and right_match and strike_match and exp_match:
                    score += 10
                elif und_match and right_match and strike_match:
                    score += 6
                elif und_match and exp_match:
                    score += 3
                elif und_match:
                    score += 1

        if score > best_score:
            best_score = score
            best_trade = t

    if best_score >= 3 and best_trade:
        return {"trade_id": str(best_trade.get("trade_id")),
                "trade_name": best_trade.get("trade_name"),
                "trade_obj": best_trade}
    return None


def classify_activity(fills, ibkr_legs, one_trades=None, one_legs=None, account_map=None):
    """Classify today's TWS fills by execution timestamp cluster & ONE Trade ID.

    Returns:
      by_trade: list of trade adjustment dicts, each detailing what happened
                under that ONE Trade ID (or [New Trade]).
      rolled, opened, closed, changed: flat lists for backward compatibility.
    """
    if one_trades is None and one_legs:
        import one_reader
        one_trades = one_reader.extract_one_trades(one_legs)
    elif one_trades is None:
        one_trades = []

    clusters = cluster_fills_by_timestamp(fills)

    # Calculate current IBKR positions
    cur_pos = {}
    for lg in ibkr_legs or []:
        if lg.get("right") not in ("C", "P"):
            continue
        key = (lg["account"], lg["tradingClass"], lg["expiry"], lg["right"], float(lg["strike"]))
        cur_pos[key] = cur_pos.get(key, 0.0) + float(lg["qty"])

    trade_groups = defaultdict(lambda: {"account": None, "trade_id": None, "trade_name": None,
                                        "time": None, "fills": [], "trade_obj": None})

    for cluster_index, c in enumerate(clusters):
        acct = c["account"]
        trade_match = _find_best_one_trade(c["fills"], one_trades, account_map)
        tid = trade_match.get("trade_id") if trade_match else None
        tname = trade_match.get("trade_name") if trade_match else None
        tobj = trade_match.get("trade_obj") if trade_match else None

        # Keep every broker order/fill cluster as its own adjustment event.
        # Multiple executions can legitimately map to the same ONE trade ID;
        # merging them hides the earlier execution time and its legs.
        group_key = (acct, tid or f"NEW_{c['time']}", cluster_index)
        g = trade_groups[group_key]
        g["account"] = acct
        g["trade_id"] = tid
        g["trade_name"] = tname
        g["trade_obj"] = tobj
        if (
            g["time"] is None
            or _parse_timestamp_to_epoch(c.get("time"))
            > _parse_timestamp_to_epoch(g["time"])
        ):
            g["time"] = c["time"]
        g["fills"].extend(c["fills"])

    by_trade = []
    flat_rolled = []
    flat_opened = []
    flat_closed = []
    flat_changed = []
    replay_pos = dict(cur_pos)

    # Clusters are newest-first. Walk backward from current positions so each
    # execution is classified against the position state immediately after it.
    for (acct, gkey, cluster_index), grp in trade_groups.items():
        tid = grp["trade_id"]
        tname = grp["trade_name"]
        tobj = grp["trade_obj"]
        group_fills = grp["fills"]

        group_net = {}
        meta = {}
        for f in group_fills:
            key = (acct, f["tradingClass"], f["expiry"], f["right"], float(f["strike"]))
            shares = abs(float(f.get("shares") or 0))
            signed = shares if f.get("side") == "BOT" else -shares
            group_net[key] = group_net.get(key, 0.0) + signed
            meta[key] = {"underlying": f.get("underlying"), "px": float(f.get("price") or 0)}

        opened_legs, closed_legs, changed_legs = [], [], []
        for key, net in group_net.items():
            if abs(net) < 1e-9:
                continue
            tc, exp, right, strike = key[1], key[2], key[3], key[4]
            after = replay_pos.get(key, 0.0)
            before = after - net
            replay_pos[key] = before
            m = meta[key]

            base = {
                "account": acct, "underlying": m["underlying"], "right": right,
                "tradingClass": tc, "expiry": exp, "strike": strike,
                "label": f"{tc} {_pretty_expiry(exp)} {strike:g}{right}",
                "px": m["px"],
                "timestamp": grp["time"],
                "one_trade_id": tid, "one_trade_name": tname
            }

            if abs(before) < 1e-9 and abs(after) > 1e-9:
                opened_legs.append({**base, "type": "OPENED", "qty": after})
            elif abs(after) < 1e-9 and abs(before) > 1e-9:
                closed_legs.append({**base, "type": "CLOSED", "qty": before})
            elif before * after > 0 and abs(after) > abs(before):
                changed_legs.append({**base, "type": "ADDED", "qty": after - before})
            elif before * after > 0 and abs(after) < abs(before):
                changed_legs.append({**base, "type": "REDUCED", "qty": after - before})
            else:
                changed_legs.append({**base, "type": "REVERSED", "qty": after})

        # Pair closed + opened of equal size as rolls within this trade group
        rolled_pairs = []
        used_open = set()
        for c in closed_legs:
            for i, o in enumerate(opened_legs):
                if i in used_open:
                    continue
                if (c["account"] == o["account"] and c["right"] == o["right"]
                        and c.get("underlying") == o.get("underlying")
                        and abs(abs(c["qty"]) - abs(o["qty"])) < 1e-9):
                    used_open.add(i)
                    c["_paired"] = True
                    r_item = {
                        "account": acct, "qty": abs(o["qty"]),
                        "from": c["label"], "to": o["label"],
                        "from_strike": c["strike"], "to_strike": o["strike"],
                        "from_px": c["px"], "to_px": o["px"],
                        "underlying": c.get("underlying") or o.get("underlying"),
                        "timestamp": grp["time"],
                        "one_trade_id": tid, "one_trade_name": tname,
                        "wizard_hint": f"Update Trade #{tid}" if tid else "Select [New Trade]"
                    }
                    rolled_pairs.append(r_item)
                    flat_rolled.append(r_item)
                    break

        unpaired_opened = [o for i, o in enumerate(opened_legs) if i not in used_open]
        unpaired_closed = [c for c in closed_legs if not c.get("_paired")]

        flat_opened.extend(unpaired_opened)
        flat_closed.extend(unpaired_closed)
        flat_changed.extend(changed_legs)

        # Determine trade closure status if tid is an existing ONE trade
        is_trade_closed = False
        if tid and tobj:
            all_flat = True
            for l in tobj.get("legs", []):
                one_exp = _expiry_to_ord(
                    l.get("expiry_listed") or l.get("expiry"))
                matching_qty = 0.0
                for (cur_acct, cur_tc, cur_exp, cur_right,
                     cur_strike), cur_qty in cur_pos.items():
                    cur_exp_ord = _expiry_to_ord(cur_exp)
                    expiry_close = (
                        one_exp is not None and cur_exp_ord is not None
                        and abs(one_exp - cur_exp_ord) <= 4
                    )
                    if (
                        cur_acct == acct
                        and cur_tc == l["tradingClass"]
                        and cur_right == l["right"]
                        and abs(cur_strike - float(l["strike"])) < 1e-9
                        and expiry_close
                    ):
                        matching_qty += cur_qty
                if abs(matching_qty) > 1e-9:
                    all_flat = False
                    break
            is_trade_closed = all_flat

        if not tid:
            status = "NEW_TRADE"
            status_label = "NEW TRADE"
            hint = "Select [New Trade] in ONE wizard"
        elif is_trade_closed:
            status = "TRADE_CLOSED"
            status_label = "TRADE CLOSED"
            hint = f"Mark Trade #{tid} ({tname}) as CLOSED in ONE"
        elif rolled_pairs:
            status = "ROLLED"
            status_label = "ROLLED"
            hint = f"Update Trade #{tid} ({tname}) in ONE wizard"
        elif unpaired_opened or changed_legs:
            status = "ADJUSTED"
            status_label = "ADJUSTED"
            hint = f"Update Trade #{tid} ({tname}) in ONE wizard"
        else:
            status = "LEG_CLOSED"
            status_label = "LEG CLOSED"
            hint = f"Update Trade #{tid} ({tname}) in ONE wizard"

        trade_item = {
            "account": acct,
            "trade_id": tid,
            "trade_name": tname or (f"Trade #{tid}" if tid else "[New Trade]"),
            "status": status,
            "status_label": status_label,
            "timestamp": grp["time"],
            "underlying": group_fills[0].get("underlying") if group_fills else "OPT",
            "rolled": rolled_pairs,
            "opened": unpaired_opened,
            "closed": unpaired_closed,
            "changed": changed_legs,
            "is_trade_closed": is_trade_closed,
            "wizard_hint": hint,
        }
        by_trade.append(trade_item)

    time_key = lambda item: _parse_timestamp_to_epoch(item.get("timestamp"))
    by_trade.sort(key=time_key, reverse=True)
    flat_rolled.sort(key=time_key, reverse=True)
    flat_opened.sort(key=time_key, reverse=True)
    flat_closed.sort(key=time_key, reverse=True)
    flat_changed.sort(key=time_key, reverse=True)

    return {
        "by_trade": by_trade,
        "rolled": flat_rolled,
        "opened": flat_opened,
        "closed": flat_closed,
        "changed": flat_changed
    }


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
                                    "secType": lg.get("secType"),
                                    "multiplier": float(lg.get("multiplier") or 100)})
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
        one_account = normalize_account_name(p["account"])
        ibkr_acct = resolve_one_account(one_account, account_map)
        if ibkr_acct is None:
            unmapped[one_account].append(p)
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
            expected = ir.get("secType") == "STK" or ir["key"][1] == "STK"
            finding = {"status": "IBKR_ONLY", "label": label_from_bucket(ir),
                       "ibkr_qty": ir["qty"], "ibkr_px": ir["avg_price"],
                       "one_qty": None, "one_px": None,
                       "expiry_ibkr": ir["expiry"], "expiry_one": None}
            if expected:
                finding.update({
                    "expected": True,
                    "reason": "Stock/ETF holding; ONE models option strategies only",
                })
            out.append(finding)
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
        # A position established entirely by today's fills has an exact broker
        # execution price and a genuine disagreement must remain actionable.
        # Older IBKR positions use FIFO avgCost while ONE reports a weighted
        # entry average, so a price-only difference is informational.
        status = "PRICE_DRIFT" if ir.get("price_source") == "fill" \
            else "MATCH_FIFO_AVG"
    else:
        status = "MATCH"
    # ONE's report carries no P&L for an OPEN leg, so unrealised P&L cannot be
    # diffed directly. What can be priced exactly is the consequence of the two
    # cost bases disagreeing: when the position is closed, ONE will report this
    # many dollars more profit than IBKR. Positive = ONE flatters the result.
    mult = float(ir.get("multiplier") or orow.get("multiplier") or 100)
    divergence = ((ir["avg_price"] - orow["avg_price"]) * orow["qty"] * mult
                  if qty_ok else None)
    return {"status": status, "label": label_from_bucket(ir),
            "ibkr_qty": ir["qty"], "ibkr_px": ir["avg_price"],
            "one_qty": orow["qty"], "one_px": orow["avg_price"],
            "px_delta": round(ir["avg_price"] - orow["avg_price"], 4),
            "pnl_divergence": round(divergence, 2) if divergence is not None else None,
            "underlying": ir.get("underlying") or orow.get("underlying"),
            "expiry_ibkr": ir["expiry"], "expiry_one": orow["expiry"],
            "expiry_offset_days": expiry_dist}


# ----------------------------------------------------------------- reporting
ORDER = ["QTY_MISMATCH", "ONE_ONLY", "IBKR_ONLY", "PRICE_DRIFT", "MATCH_FIFO_AVG", "MATCH"]
SYM = {"MATCH": "OK ", "MATCH_FIFO_AVG": "~px(FIFO)", "PRICE_DRIFT": "~px", "QTY_MISMATCH": "!QTY",
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
        n_expected = 0
        for f in findings:
            if f.get("acknowledged"):
                n_ack += 1
                continue
            if is_expected_finding(f):
                n_expected += 1
                continue
            counts[f["status"]] += 1
            grand[f["status"]] += 1
        clean = not any(is_actionable_finding(f) for f in findings)
        tag = "MATCH" if clean else "DIFF"
        summary = " ".join(f"{s}:{counts[s]}" for s in ORDER if counts[s])
        if n_ack:
            summary += f" ACK:{n_ack}"
        if n_expected:
            summary += f" EXPECTED_STOCK_ETF:{n_expected}"
        print(f"\n=== [{acct}]  {tag}   {summary or '(no positions)'} ===")

        flags = [f for f in findings if is_actionable_finding(f)]
        flags.sort(key=lambda f: ORDER.index(f["status"]))
        if not flags:
            verified = counts["MATCH"] + counts["MATCH_FIFO_AVG"]
            print(f"    all {verified} modeled option instruments reconcile.")
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
    ignore = sorted({normalize_account_name(a)
                     for a in cfg.get("ignore_one_accounts", [])})
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

    # calculate trade activity / adjustments
    activity_fills = ibkr_snapshot.get("fills_today", [])
    one_source_mtime = one_snapshot.get("source_mtime")
    if one_source_mtime:
        activity_fills = [
            fill for fill in activity_fills
            if _parse_timestamp_to_epoch(fill.get("time")) > one_source_mtime
        ]
    activity = classify_activity(
        activity_fills,
        ibkr_snapshot.get("legs", []),
        one_trades=one_snapshot.get("trades"),
        one_legs=one_snapshot.get("raw_legs"),
        account_map=account_map
    )

    # Ghost trades reconcile fine leg-by-leg (their legs ARE in the report), so
    # they never surface as a finding -- carry them through explicitly.
    ghosts = []
    for g in one_snapshot.get("ghost_trades", []) or []:
        ghosts.append({**g,
                       "ibkr_account": resolve_one_account(g["account"],
                                                           account_map)})

    # Expired-but-unsettled legs leave BOTH sides at once -- IBKR drops the
    # contract, this reader stops counting it -- so they can never show up as a
    # finding either.  Carry them through or the day after expiry looks clean.
    unsettled = []
    for t in one_snapshot.get("unsettled_trades", []) or []:
        unsettled.append({**t,
                          "ibkr_account": resolve_one_account(t["account"],
                                                              account_map)})

    return {
        "pnl_divergence": summarise_pnl_divergence(by_account),
        "reconciled_at": datetime.now(timezone.utc).isoformat(),
        "ibkr_source": ibkr_snapshot.get("captured_at"),
        "one_source": one_snapshot.get("source_file"),
        "ghost_trades": ghosts,
        "unsettled_trades": unsettled,
        "one_source_mtime": one_source_mtime,
        "tolerances": tol,
        "accounts": by_account,
        "unmapped_one_groups": {g: len(r) for g, r in unmapped.items()},
        "ignore_one_accounts": ignore,
        "fills_today": ibkr_snapshot.get("fills_today", []),
        "activity": activity,
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
    one_reader.print_ghost_trades(result.get("ghost_trades", []))
    one_reader.print_unsettled_trades(result.get("unsettled_trades", []))

    with open(OUTPUT_JSON, "w") as fh:
        json.dump(result, fh, indent=2, default=str)
    print(f"Wrote {OUTPUT_JSON}")


if __name__ == "__main__":
    main()
