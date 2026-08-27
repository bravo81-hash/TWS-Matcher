#!/usr/bin/env python3
"""
Account, P&L, Greeks and volatility metrics from IBKR.

Everything the reconciliation core does not need, but the trader does:
  - per-account NAV / cash / margin / liquidity headroom   (no market data)
  - per-position daily and unrealised P&L                  (reqPnLSingle)
  - per-position model Greeks                              (market data, tick 106)
  - per-underlying IV with IV Rank and IV Percentile       (1Y daily IV history)

Rollups per ticker and per expiry live in aggregate_metrics().

Pacing notes, learned by probing a live TWS:
  - reqPnLSingle and reqMktData each consume a per-connection subscription
    slot, and a book of ~160 options overruns the default 100 market-data
    lines, so both are issued in chunks and cancelled before the next chunk.
  - Historical IV is one pacing-limited request per underlying, and the series
    moves once a day, so a day's answer is cached on disk.
  - IBKR reports "no value" as DBL_MAX rather than null; _num() scrubs it.
"""

from __future__ import annotations

import json
import math
import os
from collections import defaultdict
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
IV_CACHE = os.path.join(HERE, "iv_cache.json")

DBL_MAX = 1.7976931348623157e308
MKT_DATA_CHUNK = 45      # stay well under the default 100-line allowance
PNL_CHUNK = 45
IV_LOOKBACK = "1 Y"

# Account fields worth showing, in display order.
ACCOUNT_TAGS = [
    "NetLiquidation", "TotalCashValue", "GrossPositionValue",
    "FullInitMarginReq", "FullMaintMarginReq", "ExcessLiquidity",
    "AvailableFunds", "BuyingPower", "UnrealizedPnL", "RealizedPnL",
]

# Cash indices quote on their own exchange; everything else is SMART equity.
INDEX_EXCHANGE = {"SPX": "CBOE", "RUT": "RUSSELL", "NDX": "NASDAQ",
                  "VIX": "CBOE", "DJX": "CBOE", "XSP": "CBOE"}


def _num(x):
    """IBKR uses DBL_MAX (and NaN) for 'not set'. Return a real float or None."""
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    if math.isnan(v) or math.isinf(v) or abs(v) >= DBL_MAX / 10:
        return None
    return v


def _chunks(seq, n):
    for i in range(0, len(seq), n):
        yield seq[i:i + n]


# --------------------------------------------------------------- accounts
def collect_account_values(ib, accounts=None) -> dict:
    """Per-account balances and margin. Needs no market-data subscription.

    ``headroom_pct`` is excess liquidity over net liquidation: the share of the
    account that can evaporate before a margin call. When short premium that is
    the number that actually decides whether you survive a gap.
    """
    raw: dict[str, dict] = defaultdict(dict)
    for v in ib.accountValues():
        if v.tag not in ACCOUNT_TAGS:
            continue
        if accounts and v.account not in accounts:
            continue
        val = _num(v.value)
        if val is None:
            continue
        cur = (v.currency or "").upper()
        # An FA reports each tag once per currency plus a BASE roll-up; the
        # concrete currency row is the one worth keeping.
        if v.tag in raw[v.account] and cur in ("", "BASE"):
            continue
        raw[v.account][v.tag] = val
        if cur not in ("", "BASE"):
            raw[v.account]["currency"] = cur

    for d in raw.values():
        nav = d.get("NetLiquidation")
        excess = d.get("ExcessLiquidity")
        init = d.get("FullInitMarginReq")
        d["headroom_pct"] = (excess / nav * 100) if nav and excess is not None else None
        d["init_margin_util_pct"] = (init / nav * 100) if nav and init is not None else None
    return dict(raw)


# ------------------------------------------------------------- position P&L
def collect_position_pnl(ib, positions) -> dict:
    """{(account, conId): daily / unrealised / value} via reqPnLSingle."""
    wanted = [(p.account, p.contract.conId) for p in positions
              if p.contract.conId and p.position]
    out: dict[tuple, dict] = {}
    for chunk in _chunks(wanted, PNL_CHUNK):
        subs = [(a, c, ib.reqPnLSingle(a, "", c)) for a, c in chunk]
        ib.sleep(4)
        for a, c, s in subs:
            out[(a, c)] = {
                "account": a, "conId": c,
                "daily_pnl": _num(s.dailyPnL),
                "unrealized_pnl": _num(s.unrealizedPnL),
                "realized_pnl": _num(s.realizedPnL),
                "market_value": _num(s.value),
                "position": _num(s.position),
            }
            ib.cancelPnLSingle(a, "", c)
    return out


# ----------------------------------------------------------------- Greeks
def collect_greeks(ib, positions) -> dict:
    """{conId: greeks} from IB's option model (generic tick 106).

    Keyed by contract rather than by position, so an option held in several
    accounts costs one market-data line instead of one per account.
    """
    uniq = {}
    for p in positions:
        c = p.contract
        if c.secType == "OPT" and c.conId not in uniq:
            uniq[c.conId] = c

    out: dict[int, dict] = {}
    for chunk in _chunks(list(uniq.values()), MKT_DATA_CHUNK):
        live = []
        for c in chunk:
            if not c.exchange:
                c.exchange = "SMART"
            live.append((c, ib.reqMktData(c, genericTickList="106",
                                          snapshot=False)))
        ib.sleep(8)
        for c, t in live:
            g = t.modelGreeks
            if g:
                out[c.conId] = {
                    "iv": _num(g.impliedVol), "delta": _num(g.delta),
                    "gamma": _num(g.gamma), "vega": _num(g.vega),
                    "theta": _num(g.theta), "opt_price": _num(g.optPrice),
                    "und_price": _num(g.undPrice),
                }
            ib.cancelMktData(c)
    return out


# ------------------------------------------------------------- IV / IVR / IVP
def _load_iv_cache(path: str) -> dict:
    try:
        with open(path) as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


def collect_iv_stats(ib, underlyings, trading_day: str | None = None,
                     cache_path: str = IV_CACHE) -> dict:
    """{symbol: iv, iv_rank, iv_percentile, chg, chg_pct} from 1Y daily IV.

    IV Rank places today's IV in the year's high-low range; IV Percentile is the
    share of days that closed below it. They answer different questions -- a
    single spike distorts rank but barely moves percentile -- so both are kept.
    """
    from ib_async import Index, Stock

    day = trading_day or datetime.now(timezone.utc).strftime("%Y-%m-%d")
    cache = _load_iv_cache(cache_path)
    out, dirty = {}, False

    for sym in sorted({s for s in underlyings if s}):
        hit = cache.get(sym)
        if hit and hit.get("day") == day:
            out[sym] = {k: v for k, v in hit.items() if k != "day"}
            continue
        try:
            contract = (Index(sym, INDEX_EXCHANGE[sym], "USD")
                        if sym in INDEX_EXCHANGE else Stock(sym, "SMART", "USD"))
            details = ib.reqContractDetails(contract)
            if not details:
                out[sym] = {"error": "no contract details"}
                continue
            bars = ib.reqHistoricalData(details[0].contract, "", IV_LOOKBACK,
                                        "1 day", "OPTION_IMPLIED_VOLATILITY",
                                        True, 1)
            vals = [b.close for b in bars if _num(b.close)]
            if len(vals) < 2:
                out[sym] = {"error": "no IV history"}
                continue
            cur, lo, hi, prev = vals[-1], min(vals), max(vals), vals[-2]
            rec = {
                "iv": cur,
                "iv_rank": ((cur - lo) / (hi - lo) * 100) if hi > lo else None,
                "iv_percentile": sum(1 for v in vals if v < cur) / len(vals) * 100,
                "chg": cur - prev,
                "chg_pct": ((cur - prev) / prev * 100) if prev else None,
                "low_1y": lo, "high_1y": hi, "samples": len(vals),
            }
            out[sym] = rec
            cache[sym] = {**rec, "day": day}
            dirty = True
        except Exception as exc:            # one bad symbol must not kill the run
            out[sym] = {"error": f"{type(exc).__name__}: {exc}"}

    if dirty:
        try:
            with open(cache_path, "w") as fh:
                json.dump(cache, fh, indent=2)
        except OSError:
            pass
    return out


# ---------------------------------------------------------------- rollups
def _expiry_label(e):
    e = str(e or "")
    return f"{e[:4]}-{e[4:6]}-{e[6:]}" if len(e) == 8 else (e or "-")


def aggregate_metrics(positions, pnl, greeks, iv_stats, nav=None) -> dict:
    """Roll positions up per underlying and per (underlying, expiry).

    Greeks are position-weighted. IB quotes them per share, so every one is
    scaled by qty * multiplier: delta and gamma read as share-equivalents,
    theta as dollars a day, vega as dollars per volatility point.
    ``delta_dollars`` (delta x underlying price) is what makes delta comparable
    across a 7,700 index and a 200 stock.

    Two daily-P&L percentages, because neither alone is honest for a spread
    book. ``daily_pnl_pct`` is measured against GROSS prior value -- the sum of
    each leg's absolute opening value -- since a vertical's net market value
    nets toward zero and would produce meaningless percentages. Alongside it,
    ``daily_pnl_pct_nav`` gives the contribution to account equity, which is
    what actually matters when sizing.
    """
    buckets: dict[tuple, dict] = {}
    per_ticker: dict[str, dict] = {}

    def blank(**kw):
        return {"delta": 0.0, "gamma": 0.0, "vega": 0.0, "theta": 0.0,
                "delta_dollars": 0.0, "daily_pnl": 0.0, "unrealized_pnl": 0.0,
                "market_value": 0.0, "prior_value": 0.0, "gross_prior_value": 0.0,
                "positions": 0, "contracts": 0.0, "greeks_missing": 0, **kw}

    for p in positions:
        c = p.contract
        if c.secType != "OPT" or not p.position:
            continue
        sym = c.symbol or c.tradingClass
        exp = c.lastTradeDateOrContractMonth
        mult = float(c.multiplier or 100)
        qty = float(p.position)

        tkey = sym
        ekey = (sym, exp)
        per_ticker.setdefault(tkey, blank(underlying=sym))
        buckets.setdefault(ekey, blank(underlying=sym, expiry=exp,
                                       expiry_label=_expiry_label(exp)))

        g = greeks.get(c.conId) or {}
        row = pnl.get((p.account, c.conId)) or {}
        daily = row.get("daily_pnl") or 0.0
        unreal = row.get("unrealized_pnl") or 0.0
        mv = row.get("market_value") or 0.0

        for target in (per_ticker[tkey], buckets[ekey]):
            if g.get("delta") is None:
                target["greeks_missing"] += 1
            else:
                shares = qty * mult
                target["delta"] += g["delta"] * shares
                target["gamma"] += (g.get("gamma") or 0.0) * shares
                target["vega"] += (g.get("vega") or 0.0) * shares
                target["theta"] += (g.get("theta") or 0.0) * shares
                if g.get("und_price"):
                    target["delta_dollars"] += g["delta"] * shares * g["und_price"]
            target["daily_pnl"] += daily
            target["unrealized_pnl"] += unreal
            target["market_value"] += mv
            target["prior_value"] += mv - daily
            target["gross_prior_value"] += abs(mv - daily)
            target["positions"] += 1
            target["contracts"] += qty

    def finish(d):
        base = d["gross_prior_value"]
        d["daily_pnl_pct"] = (d["daily_pnl"] / base * 100) if base else None
        d["daily_pnl_pct_nav"] = ((d["daily_pnl"] / nav * 100)
                                  if nav else None)
        return d

    tickers = []
    for sym, d in per_ticker.items():
        finish(d)
        d.update({k: v for k, v in (iv_stats.get(sym) or {}).items()})
        tickers.append(d)
    tickers.sort(key=lambda d: -abs(d["daily_pnl"]))

    expiries = [finish(d) for d in buckets.values()]
    expiries.sort(key=lambda d: (d["underlying"], d["expiry"] or ""))
    return {"by_ticker": tickers, "by_expiry": expiries}


def collect_all(ib, accounts=None) -> dict:
    """One pass: accounts, per-position P&L, Greeks, IV, and the rollups."""
    positions = [p for p in ib.positions()
                 if not accounts or p.account in accounts]
    pnl = collect_position_pnl(ib, positions)
    greeks = collect_greeks(ib, positions)
    unders = {p.contract.symbol for p in positions
              if p.contract.secType == "OPT" and p.contract.symbol}
    iv_stats = collect_iv_stats(ib, unders)
    acct_values = collect_account_values(ib, accounts)
    nav = sum(d.get("NetLiquidation") or 0.0 for d in acct_values.values())
    return {
        "captured_at": datetime.now(timezone.utc).isoformat(),
        "accounts": acct_values,
        "nav_total": nav,
        "iv": iv_stats,
        **aggregate_metrics(positions, pnl, greeks, iv_stats, nav=nav),
    }
