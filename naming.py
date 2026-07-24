#!/usr/bin/env python3
"""
naming.py  —  canonical position-naming for TWS Matcher.

Scheme:
    {ACCOUNT}.{TICKER}.{STRATEGY}[-{STRUCTURE}].{EXPIRY}[.{SEQ}] - N LOTS

    MGN.SPX.A14-PBWB.0731 - 2 LOTS
    SMSF.SPY.ALL130-EOM.OCT-NOV.2 - 20 LOTS
    MGN.RUT.A14.0724.2 - 2 LOTS
    MGN.SPX.IF.AUG - 2 LOTS

Two-way codec (parse <-> format), expiry formatting, an advisory structure
classifier, and a skeleton generator.

WHAT IBKR CAN AND CANNOT TELL US
    ACCOUNT, TICKER, EXPIRY, N LOTS ...... derivable from leg data
    STRUCTURE geometry ................... inferable (advisory hint only)
    STRATEGY (A14/TE/ALL130/IF/...) ...... NOT derivable. Proprietary. Read it
                                           from ONE's position name (parse it
                                           with this codec) or assign by hand.
    SEQ (.1/.2 tranche) .................. NOT derivable. When IBKR nets two
                                           tranches of the same contract the
                                           split is lost; it lives in ONE.
                                           Skeletons leave SEQ blank.

NOTE (integration): feed skeleton_from_legs the REAL expiration date (Friday for
AM-settled monthlies), not IBKR's last-trade Thursday, or is_third_friday()
misclassifies monthlies. ONE's expiry_listed already is the Friday.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime
from math import gcd

# --- vocabulary (from the finalized convention) --------------------------
ACCOUNTS = {"SMSF", "MGN"}            # BORG, FA are stock-only -> not on ONE/OptionStrat
STRATEGIES = {"A14", "TE", "TZ", "ALL130", "ALL160",
              "IF", "M200", "BTMN", "STT", "488", "STK"}
STRUCTURES = {"PBWB", "EOM", "CSP", "PCS", "M"}
MONTHS = ["JAN", "FEB", "MAR", "APR", "MAY", "JUN",
          "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"]

_LOTS_RE = re.compile(r"\s*-\s*(\d+)\s+LOTS?\s*$", re.IGNORECASE)


# --- expiry formatting ---------------------------------------------------
def _to_date(d) -> date:
    if isinstance(d, datetime):
        return d.date()
    if isinstance(d, date):
        return d
    s = str(d).strip().replace("-", "")
    return datetime.strptime(s[:8], "%Y%m%d").date()


def is_third_friday(d: date) -> bool:
    return d.weekday() == 4 and 15 <= d.day <= 21


def format_one_expiry(d, *, monthly: bool | None = None,
                      ref: date | None = None) -> str:
    """>1yr out -> YYMM ; monthly/EOM/3rd-Fri -> MON ; else -> MMDD."""
    d = _to_date(d)
    ref = ref or date.today()
    if d.year > ref.year:                                    # beyond this year
        return f"{d.year % 100:02d}{d.month:02d}"           # YYMM
    if monthly is None:
        monthly = is_third_friday(d)                        # EOM must be forced by caller
    if monthly:
        return MONTHS[d.month - 1]                           # MON
    return f"{d.month:02d}{d.day:02d}"                       # MMDD


def format_expiry(expiries, *, monthly: bool | None = None,
                  ref: date | None = None) -> str:
    """One or two expiries -> convention string; a pair is joined near-far."""
    seq = expiries if isinstance(expiries, (list, tuple, set)) else [expiries]
    ds = sorted({_to_date(e) for e in seq})
    if not ds:
        return "?"
    if len(ds) == 1:
        return format_one_expiry(ds[0], monthly=monthly, ref=ref)
    if len(ds) == 2:
        return (f"{format_one_expiry(ds[0], monthly=monthly, ref=ref)}"
                f"-{format_one_expiry(ds[1], monthly=monthly, ref=ref)}")
    return "MULTI"                                           # 3+ distinct expiries


# --- the name codec ------------------------------------------------------
@dataclass
class PositionName:
    account: str
    ticker: str
    strategy: str
    expiry: str
    structure: str | None = None
    seq: str | None = None
    lots: int | None = None

    def format(self) -> str:
        strat = f"{self.strategy}-{self.structure}" if self.structure else self.strategy
        core = ".".join([self.expiry, strat, self.account, self.ticker])
        if self.seq:
            core = f"{core}.{self.seq}"
        if self.lots is not None:
            core = f"{core} - {self.lots} {'LOT' if self.lots == 1 else 'LOTS'}"
        return core

    @classmethod
    def parse(cls, s: str) -> "PositionName":
        raw = s.strip()
        lots = None
        m = _LOTS_RE.search(raw)
        if m:
            lots = int(m.group(1))
            raw = raw[:m.start()].strip()
        parts = raw.split(".")
        if len(parts) not in (4, 5):
            raise ValueError(f"expected 4-5 dot fields, got {len(parts)}: {s!r}")
        
        f0, f1, f2, f3 = parts[:4]
        seq = parts[4] if len(parts) == 5 else None
        
        # Flexibly handle both EXPIRY.STRATEGY.ACCOUNT.TICKER and ACCOUNT.TICKER.STRATEGY.EXPIRY
        if f0 in ACCOUNTS:
            account, ticker, strat_field, expiry = f0, f1, f2, f3
        else:
            expiry, strat_field, account, ticker = f0, f1, f2, f3
            
        if "-" in strat_field:
            strategy, structure = strat_field.split("-", 1)
        else:
            strategy, structure = strat_field, None
        return cls(account=account, ticker=ticker, strategy=strategy,
                   expiry=expiry, structure=structure, seq=seq, lots=lots)

    def validate(self) -> list[str]:
        w = []
        if self.account not in ACCOUNTS:
            w.append(f"unknown account {self.account!r}")
        if self.strategy not in STRATEGIES and self.strategy != "?":
            w.append(f"unknown strategy {self.strategy!r}")
        if self.structure and self.structure not in STRUCTURES:
            w.append(f"unknown structure {self.structure!r}")
        return w


# --- advisory structure classifier --------------------------------------
def _fly_kind(strikes: list[float]) -> str:
    lower, upper = strikes[1] - strikes[0], strikes[2] - strikes[1]
    return "BROKEN_WING" if abs(lower - upper) > 1e-6 else "BUTTERFLY"


def suggest_structure(legs: list[dict]) -> str:
    """Geometry hint from legs (advisory). Legs need: strike, right, qty, expiry."""
    n = len(legs)
    if n == 0:
        return "EMPTY"
    exps = {str(l.get("expiry")) for l in legs}
    rights = {l.get("right") for l in legs}
    strikes = sorted({float(l["strike"]) for l in legs if l.get("strike") is not None})
    multi_exp = len(exps) > 1

    if n == 1:
        r, q = legs[0].get("right"), legs[0].get("qty", 0)
        if r == "P":
            return "SHORT_PUT" if q < 0 else "LONG_PUT"
        return "SHORT_CALL" if q < 0 else "LONG_CALL"

    if n == 2:
        if multi_exp and len(strikes) == 1:
            return "CALENDAR"
        if multi_exp:
            return "DIAGONAL"
        if len(rights) == 2:
            return "STRADDLE" if len(strikes) == 1 else "STRANGLE"
        return "VERTICAL"

    if n == 3 and not multi_exp and len(rights) == 1 and len(strikes) == 3:
        return _fly_kind(strikes)

    if n == 4:
        if multi_exp:
            return "DOUBLE_CALENDAR" if len(strikes) <= 2 else "COMPLEX"
        if len(rights) == 2:
            return "IRON_CONDOR" if len(strikes) == 4 else "IRON_FLY"
        if len(rights) == 1 and len(strikes) == 4:
            return "CONDOR"

    return "COMPLEX"


# --- skeleton generation from IBKR/ONE legs ------------------------------
def _derive_ticker(legs: list[dict]) -> str:
    for key in ("underlying", "symbol", "tradingClass"):
        for l in legs:
            if l.get(key):
                return l[key]
    return "?"


def lot_count(legs: list[dict]) -> int:
    """Heuristic lot size = gcd of the absolute leg quantities."""
    g = 0
    for l in legs:
        q = abs(int(round(l.get("qty", 0) or 0)))
        if q:
            g = gcd(g, q)
    return g or 1


def skeleton_from_legs(account: str, legs: list[dict], *,
                       strategy: str | None = None,
                       structure: str | None = None,
                       seq: str | None = None,
                       monthly: bool | None = None,
                       ref: date | None = None) -> tuple[PositionName, str]:
    """Build the auto-derivable name. Returns (PositionName, geometry_hint).
       STRATEGY defaults to '?' — supply it from ONE or by hand."""
    expiries = [l["expiry"] for l in legs if l.get("expiry")]
    name = PositionName(
        account=account,
        ticker=_derive_ticker(legs),
        strategy=strategy or "?",
        structure=structure,
        expiry=format_expiry(expiries, monthly=monthly, ref=ref),
        seq=seq,
        lots=lot_count(legs),
    )
    return name, suggest_structure(legs)


if __name__ == "__main__":
    # tiny smoke test
    n = PositionName.parse("SMSF.SPY.ALL130-EOM.OCT-NOV.2 - 20 LOTS")
    assert n.account == "SMSF" and n.strategy == "ALL130" and n.structure == "EOM"
    assert n.seq == "2" and n.lots == 20
    assert n.format() == "SMSF.SPY.ALL130-EOM.OCT-NOV.2 - 20 LOTS"
    print("naming.py self-test OK:", n.format())
