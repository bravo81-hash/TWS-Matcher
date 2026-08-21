#!/usr/bin/env python3
"""Portable TWS Matcher reconciliation and session email report."""

from __future__ import annotations

import html as html_lib
import json
import os
import smtplib
import sys
import time
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

import one_reader
import optionstrat_reader
import optionstrat_url
import recon_one_os
import reconcile

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(HERE, "config.json")
IBKR_JSON = os.path.join(HERE, "canonical_positions.json")
ONE_JSON = os.path.join(HERE, "one_positions.json")


def _escape(value) -> str:
    return html_lib.escape(str(value if value is not None else ""), quote=True)


def _num(value, digits=4) -> str:
    if value is None:
        return ""
    try:
        return f"{float(value):.{digits}f}"
    except (TypeError, ValueError):
        return _escape(value)


def _data_attrs(**fields) -> str:
    """Build escaped data attributes for the portable report controls."""
    css_class = " ".join(
        part for part in ("report-filterable", str(fields.pop("_class", "")))
        if part
    )
    attrs = [f"class='{_escape(css_class)}'"]
    for name, value in fields.items():
        if isinstance(value, (list, tuple, set)):
            value = "|".join(str(item) for item in value if item not in (None, ""))
        attrs.append(
            f"data-{name.replace('_', '-')}='{_escape(value)}'"
        )
    return " ".join(attrs)


def _trade_facets(trade: dict) -> tuple[list[str], list[str]]:
    tickers = set()
    strikes = set()
    if trade.get("underlying"):
        tickers.add(str(trade["underlying"]))
    for row in (
        list(trade.get("opened") or [])
        + list(trade.get("closed") or [])
        + list(trade.get("changed") or [])
    ):
        if row.get("underlying"):
            tickers.add(str(row["underlying"]))
        if row.get("strike") is not None:
            strikes.add(f"{float(row['strike']):g}")
    for row in trade.get("rolled") or []:
        if row.get("underlying"):
            tickers.add(str(row["underlying"]))
        for key in ("from_strike", "to_strike"):
            if row.get(key) is not None:
                strikes.add(f"{float(row[key]):g}")
    return sorted(tickers), sorted(strikes, key=float)


def generate_report_html(
    ibkr_snap: dict,
    one_snap: dict,
    cfg: dict,
    reconciliation_result: dict | None = None,
) -> tuple[str, str]:
    """Return a complete report that works without access to the local server."""
    now = datetime.now().astimezone()
    result = reconciliation_result or reconcile.reconcile_snapshots(
        ibkr_snap, one_snap, cfg)
    activity = result.get("activity") or {}
    fills = ibkr_snap.get("fills_today", [])

    one_path = one_snap.get("source_file") or one_reader.find_default_csv(
        cfg.get("one_export_dirs"))
    strategies = optionstrat_url.generate(
        one_path, ibkr_legs=ibkr_snap.get("legs", []))["strategies"]
    os_path = optionstrat_reader.find_default_xlsx(cfg.get("one_export_dirs"))
    os_result = None
    if os_path and os.path.exists(os_path):
        try:
            os_result = recon_one_os.reconcile(one_path, os_path, cfg)
        except Exception as exc:
            os_result = {"error": f"{type(exc).__name__}: {exc}"}

    actionable = [
        (account, finding)
        for account, findings in result.get("accounts", {}).items()
        for finding in findings
        if reconcile.is_actionable_finding(finding)
    ]
    expected = [
        (account, finding)
        for account, findings in result.get("accounts", {}).items()
        for finding in findings
        if reconcile.is_expected_finding(finding)
    ]
    basis_info = [
        (account, finding)
        for account, findings in result.get("accounts", {}).items()
        for finding in findings
        if finding.get("status") == "MATCH_FIFO_AVG"
        and not finding.get("acknowledged")
    ]
    ignored = set(result.get("ignore_one_accounts", []))
    unmapped = {
        one_reader.normalize_account_name(name): count
        for name, count in result.get("unmapped_one_groups", {}).items()
        if one_reader.normalize_account_name(name) not in ignored
    }
    ghosts = result.get("ghost_trades", []) or []
    unsettled = result.get("unsettled_trades", []) or []
    issue_count = (len(actionable) + sum(unmapped.values()) + len(ghosts)
                   + len(unsettled))
    one_mtime = result.get("one_source_mtime") or one_snap.get("source_mtime")
    if not one_mtime and one_path and os.path.exists(one_path):
        one_mtime = os.path.getmtime(one_path)
    one_age_seconds = time.time() - one_mtime if one_mtime else None
    one_stale = one_age_seconds is None or one_age_seconds > 3600
    if issue_count:
        state_label, state_color = f"DIFF {issue_count}", "#cf222e"
    elif one_stale:
        state_label, state_color = "STALE ONE EXPORT", "#9a6700"
    else:
        state_label, state_color = "MATCH", "#1a7f37"

    os_mtime = os.path.getmtime(os_path) if os_path and os.path.exists(os_path) else None
    os_age_seconds = time.time() - os_mtime if os_mtime else None
    os_stale = os_age_seconds is None or os_age_seconds > 3600
    if not os_result:
        os_label, os_color = "NO OPTIONSTRAT EXPORT", "#9a6700"
    elif os_result.get("error"):
        os_label, os_color = "OPTIONSTRAT ERROR", "#cf222e"
    else:
        os_diffs = [row for row in os_result["matched"] if not row["clean"]]
        os_issue_count = (
            len(os_diffs)
            + len(os_result["one_only"])
            + len(os_result["os_only"])
        )
        if os_issue_count:
            os_label, os_color = f"OPTIONSTRAT DIFF {os_issue_count}", "#cf222e"
        elif os_stale:
            os_label, os_color = "OPTIONSTRAT STALE", "#9a6700"
        else:
            os_label, os_color = "OPTIONSTRAT MATCH", "#1a7f37"

    n_rolled = len(activity.get("rolled", []))
    n_opened = len(activity.get("opened", []))
    n_closed = len(activity.get("closed", []))
    n_changed = len(activity.get("changed", []))
    change_count = n_rolled + n_opened + n_closed + n_changed
    subject = (
        f"TWS Matcher IBKR↔ONE {state_label} | {os_label} — "
        f"{now.strftime('%d %b %Y %H:%M')} "
        f"({len(fills)} fills, {change_count} changes)"
    )

    parts = [f"""<!doctype html>
<html><head><meta charset="utf-8"><title>{_escape(subject)}</title>
<style>
body{{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;background:#f6f8fa;color:#24292e;margin:0;padding:10px}}
.card{{background:#fff;border:1px solid #d0d7de;border-radius:8px;padding:14px 18px;max-width:980px;margin:0 auto 12px;box-sizing:border-box}}
h1{{font-size:18px;margin:0 0 4px;color:#0969da}}h2{{font-size:15px;margin:18px 0 6px;border-bottom:1px solid #d8dee4;padding-bottom:4px;color:#0969da}}
.badge{{font-weight:700;font-size:12px;padding:3px 8px;border-radius:12px;color:#fff;display:inline-block}}
.note{{background:#ddf4ff;border:1px solid #54aeff;border-radius:6px;padding:9px 12px;margin:10px 0}}
.warnbox{{background:#ffebe9;border:1px solid #ff8182;border-radius:6px;padding:9px 12px;margin:10px 0}}
.info{{background:#f6f8fa;border:1px solid #d8dee4;border-radius:6px;padding:9px 12px;margin:8px 0}}
.hint{{font-size:12px;color:#0969da;background:#ddf4ff;padding:4px 7px;border-radius:4px;margin-top:5px}}
.muted{{color:#57606a;font-size:12px}}.bad{{color:#cf222e}}
table{{width:100%;border-collapse:collapse;margin-top:5px;font-size:12px}}th,td{{padding:5px 7px;text-align:left;border-bottom:1px solid #d8dee4;white-space:nowrap}}th{{background:#f6f8fa;color:#57606a}}
.num{{text-align:right;font-variant-numeric:tabular-nums}}.btn{{display:inline-block;background:#1a7f37;color:#fff!important;padding:4px 9px;text-decoration:none;border-radius:4px;font-weight:700;font-size:11px;margin:2px}}
.report-controls{{position:sticky;top:0;z-index:10;display:flex;align-items:center;gap:6px;flex-wrap:wrap;background:#ddf4ff;border:1px solid #54aeff;border-radius:7px;padding:8px 10px;margin:10px 0;box-shadow:0 2px 5px #0001}}
.report-control{{background:#fff;color:#24292e;border:1px solid #8c959f;padding:5px 7px;border-radius:5px;font-size:12px;max-width:180px}}
.report-clear{{background:#1f6feb;color:#fff;border:0;padding:6px 10px;border-radius:5px;font-weight:700;cursor:pointer}}
.report-hidden{{display:none!important}}.adjustment-card{{transition:opacity .12s}}
table.report-sortable th{{cursor:pointer;user-select:none}}table.report-sortable th:hover{{color:#0969da}}
table.report-sortable th::after{{content:' ↕';opacity:.35}}table.report-sortable th.report-sorted::after{{opacity:1;color:#0969da}}
@media(max-width:650px){{body{{padding:2px}}.card{{padding:10px}}th,td{{white-space:normal}}}}
</style></head><body><div class="card">
<h1>TWS Matcher — portable reconciliation snapshot</h1>
<div class="muted">Generated {_escape(now.strftime('%d %b %Y %H:%M %Z'))} ·
IBKR snapshot {_escape(result.get('ibkr_source') or 'unknown')} ·
ONE source {_escape(os.path.basename(one_path or 'unknown'))}</div>
<div class="note"><b>This report is self-contained.</b> Read it directly in the
email or open the attached HTML file on any computer. It does not connect to
TWS, ONE, or the trading laptop. Sorting and filters work when the attached
HTML file is opened in a browser.</div>
<div class="report-controls" id="report-controls">
<b style="color:#0969da">Filter snapshot:</b>
<select id="report-account" class="report-control"><option value="">All accounts</option></select>
<select id="report-account-type" class="report-control"><option value="">All account types</option></select>
<select id="report-ticker" class="report-control"><option value="">All tickers</option></select>
<select id="report-strike" class="report-control"><option value="">All strikes</option></select>
<select id="report-trade" class="report-control"><option value="">All ONE trade IDs</option></select>
<select id="report-status" class="report-control"><option value="">All statuses</option></select>
<input id="report-search" class="report-control" type="search" placeholder="Search; space = AND">
<select id="report-adjustment-sort" class="report-control">
<option value="time-desc">Adjustments: newest first</option>
<option value="time-asc">Adjustments: oldest first</option>
<option value="trade">Adjustments: ONE trade ID</option>
<option value="account">Adjustments: account</option>
</select>
<button type="button" class="report-clear" id="report-clear">Clear filters</button>
<span class="muted" id="report-visible-count"></span>
</div>
<div class="info"><span class="badge" style="background:{state_color}">{_escape(state_label)}</span>
&nbsp; <span class="badge" style="background:{os_color}">{_escape(os_label)}</span>
&nbsp; <b>{len(actionable)}</b> actionable finding(s) ·
<b>{sum(unmapped.values())}</b> unmapped ONE leg(s) ·
<b>{len(basis_info)}</b> cost-basis note(s) ·
<b>{len(expected)}</b> expected stock/ETF holding(s)</div>"""]

    if one_stale:
        age_text = (
            "unknown age" if one_age_seconds is None
            else f"{one_age_seconds / 60:.0f} minutes old"
        )
        parts.append(
            "<div class='warnbox'><b>ONE export is stale "
            f"({_escape(age_text)}).</b> Export ONE again and run Check now "
            "before treating this snapshot as current.</div>")

    if unmapped:
        group_text = ", ".join(
            f"{_escape(name)} ({count} legs)"
            for name, count in sorted(unmapped.items())
        )
        parts.append(
            "<div class='warnbox'><b>Unmapped ONE account group(s):</b> "
            f"{group_text}. The comparison is not trustworthy until mapped.</div>")

    for trade in unsettled:
        legs_text = " · ".join(
            f'{leg["qty"]:+.0f} {leg["tradingClass"]} '
            f'{one_reader._pretty_expiry(leg["expiry"])} '
            f'{leg["strike"]:g}{leg["right"]} @ {leg["open_price"]:.2f}'
            for leg in trade["legs"])
        parts.append(
            "<div class='warnbox'><b>Unsettled expiry in ONE &mdash; "
            f"{_escape(str(trade.get('ibkr_account') or trade['account']))} "
            f"#{_escape(str(trade['trade_id']))} "
            f"{_escape(str(trade['trade_name']))}.</b> "
            "These legs expired in the market but are still open in ONE, so "
            "ONE's realised P&amp;L for the trade is wrong. The broker dropped "
            "them at expiry and the position check goes quiet with them. "
            f"<br><span style='font-family:monospace'>{_escape(legs_text)}</span>"
            f"<br>Settles <b>{trade['pnl_if_worthless']:+,.2f}</b> if expired "
            "worthless; an in-the-money leg was exercised or assigned and "
            "settles at intrinsic instead.</div>")

    for ghost in ghosts:
        legs_text = " · ".join(
            f'{leg["qty"]:+.0f} {leg["tradingClass"]} '
            f'{one_reader._pretty_expiry(leg["expiry"])} '
            f'{leg["strike"]:g}{leg["right"]} @ {leg["open_price"]:.2f}'
            for leg in ghost["legs"])
        parts.append(
            "<div class='warnbox'><b>Ghost trade in ONE &mdash; "
            f"{_escape(str(ghost.get('ibkr_account') or ghost['account']))} "
            f"#{_escape(str(ghost['trade_id']))} "
            f"{_escape(str(ghost['trade_name']))}.</b> "
            "It reports as open but ONE holds no position for it, so it cannot "
            "be selected in the Analysis window and its legs can never be "
            f"adjusted. Flagged because {_escape(', and '.join(ghost['reasons']))}. "
            f"<br><span style='font-family:monospace'>{_escape(legs_text)}</span>"
            "<br>Fix: rebuild it as a new trade in ONE, then delete "
            f"#{_escape(str(ghost['trade_id']))} so its legs are not counted "
            "twice.</div>")

    parts.append("<h2>IBKR ↔ ONE reconciliation</h2>")
    for account, findings in sorted(result.get("accounts", {}).items()):
        flags = [f for f in findings if reconcile.is_actionable_finding(f)]
        account_expected = [f for f in findings
                            if reconcile.is_expected_finding(f)]
        account_basis = [
            f for f in findings
            if f.get("status") == "MATCH_FIFO_AVG"
            and not f.get("acknowledged")
        ]
        matched = sum(1 for f in findings
                      if f.get("status") in ("MATCH", "MATCH_FIFO_AVG"))
        label = "MATCH" if not flags else f"DIFF {len(flags)}"
        color = "#1a7f37" if not flags else "#cf222e"
        account_statuses = sorted({
            str(f.get("status") or "") for f in findings if f.get("status")
        })
        account_attrs = _data_attrs(
            _class="info reconciliation-card",
            account=account,
            account_type=(cfg.get("account_codes") or {}).get(account, ""),
            status=account_statuses,
        )
        parts.append(
            f"<div {account_attrs}><b>{_escape(account)}</b> "
            f"<span class='badge' style='background:{color}'>{_escape(label)}</span>"
            f"<span class='muted'> · {matched} modeled options verified"
            f" · {len(account_basis)} cost-basis note(s)"
            f" · {len(account_expected)} expected holding(s)</span>")
        if flags:
            parts.append(
                "<table><tr><th>Instrument</th><th>Status</th>"
                "<th class='num'>IBKR qty</th><th class='num'>ONE qty</th>"
                "<th class='num'>IBKR px</th><th class='num'>ONE px</th></tr>")
            for finding in flags:
                parts.append(
                    f"<tr><td>{_escape(finding.get('label'))}</td>"
                    f"<td class='bad'><b>{_escape(finding.get('status'))}</b></td>"
                    f"<td class='num'>{_escape(finding.get('ibkr_qty'))}</td>"
                    f"<td class='num'>{_escape(finding.get('one_qty'))}</td>"
                    f"<td class='num'>{_num(finding.get('ibkr_px'))}</td>"
                    f"<td class='num'>{_num(finding.get('one_px'))}</td></tr>")
            parts.append("</table>")
        if account_basis:
            rows = "; ".join(
                f"{_escape(f['label'])} IBKR {_num(f.get('ibkr_px'))} / "
                f"ONE {_num(f.get('one_px'))}"
                for f in account_basis
            )
            parts.append(
                "<div class='muted' style='margin-top:6px'>"
                f"<b>Cost-basis information:</b> {rows}. Quantities match; "
                "aged IBKR positions use FIFO while ONE uses weighted average.</div>")
        if account_expected:
            holdings = ", ".join(_escape(f["label"]) for f in account_expected)
            parts.append(
                "<div class='muted' style='margin-top:6px'>"
                f"<b>Excluded stock/ETF holdings:</b> {holdings}.</div>")
        parts.append("</div>")

    parts.append("<h2>ONE ↔ OptionStrat reconciliation</h2>")
    if not os_result:
        parts.append(
            "<div class='warnbox'>No OptionStrat <i>all active</i> export was "
            "found, so the backup mirror was not verified.</div>")
    elif os_result.get("error"):
        parts.append(
            f"<div class='warnbox'><b>OptionStrat comparison failed:</b> "
            f"{_escape(os_result['error'])}</div>")
    else:
        os_diffs = [row for row in os_result["matched"] if not row["clean"]]
        os_age_text = (
            "unknown age" if os_age_seconds is None
            else f"{os_age_seconds / 60:.0f} minutes old"
        )
        parts.append(
            f"<div class='info'><b>{len(os_result['matched'])}</b> combo(s) paired · "
            f"<b>{len(os_diffs)}</b> combo diff(s) · "
            f"<b>{len(os_result['one_only'])}</b> only in ONE · "
            f"<b>{len(os_result['os_only'])}</b> only in OptionStrat · "
            f"source {_escape(os.path.basename(os_path or ''))} "
            f"({_escape(os_age_text)})</div>")
        if os_stale:
            parts.append(
                "<div class='warnbox'><b>OptionStrat export is stale.</b> "
                "Download a fresh all-active report before relying on this "
                "comparison.</div>")
        for combo in os_diffs:
            parts.append(
                f"<div class='info'><b>{_escape(combo['one_name'])}</b>"
                f"<span class='muted'> ↔ {_escape(combo['os_name'])}</span>"
                "<table><tr><th>Leg</th><th>Status</th>"
                "<th class='num'>ONE qty</th><th class='num'>OS qty</th>"
                "<th class='num'>ONE px</th><th class='num'>OS px</th></tr>")
            for leg in combo["legs"]:
                if leg["status"] == "MATCH":
                    continue
                parts.append(
                    f"<tr><td>{_escape(leg['label'])}</td>"
                    f"<td class='bad'><b>{_escape(leg['status'])}</b></td>"
                    f"<td class='num'>{_escape(leg.get('one_qty'))}</td>"
                    f"<td class='num'>{_escape(leg.get('os_qty'))}</td>"
                    f"<td class='num'>{_num(leg.get('one_px'))}</td>"
                    f"<td class='num'>{_num(leg.get('os_px'))}</td></tr>")
            parts.append("</table></div>")
        if os_result["one_only"] or os_result["os_only"]:
            one_names = ", ".join(
                _escape(row["name"]) for row in os_result["one_only"]) or "none"
            os_names = ", ".join(
                _escape(row["name"]) for row in os_result["os_only"]) or "none"
            parts.append(
                f"<div class='warnbox'><b>Only in ONE:</b> {one_names}<br>"
                f"<b>Only in OptionStrat:</b> {os_names}</div>")

    if change_count:
        parts.append(
            "<h2>Session trade changes and ONE import guidance</h2>"
            "<div id='report-adjustments'>"
        )
        for trade in activity.get("by_trade", []):
            trade_id = trade.get("trade_id")
            title = (
                f"ONE Trade #{trade_id} ({trade.get('trade_name') or ''})"
                if trade_id else f"[New Trade] {trade.get('trade_name') or ''}"
            )
            tickers, strikes = _trade_facets(trade)
            account = str(trade.get("account") or "")
            trade_attrs = _data_attrs(
                _class="info adjustment-card",
                account=account,
                account_type=(cfg.get("account_codes") or {}).get(account, ""),
                ticker=tickers,
                strike=strikes,
                trade=(str(trade_id) if trade_id else "[New Trade]"),
                status=(trade.get("status_label") or trade.get("status") or ""),
                time=(trade.get("timestamp") or ""),
            )
            parts.append(
                f"<div {trade_attrs}><b>[{_escape(trade.get('account'))}] "
                f"{_escape(title)}</b> <span class='muted'>"
                f"{_escape(trade.get('status_label') or trade.get('status'))} "
                f"{_escape(trade.get('timestamp') or '')}</span><table>")
            for row in trade.get("rolled", []):
                parts.append(
                    f"<tr><td>ROLLED</td><td>{_escape(row.get('from'))} → "
                    f"{_escape(row.get('to'))}</td><td class='num'>"
                    f"×{_escape(row.get('qty'))}</td></tr>")
            for row in trade.get("opened", []):
                parts.append(
                    f"<tr><td>OPENED</td><td>{_escape(row.get('label'))}</td>"
                    f"<td class='num'>{_escape(row.get('qty'))} @ "
                    f"{_num(row.get('px'))}</td></tr>")
            for row in trade.get("closed", []):
                parts.append(
                    f"<tr><td>CLOSED</td><td>{_escape(row.get('label'))}</td>"
                    f"<td class='num'>was {_escape(row.get('qty'))}</td></tr>")
            for row in trade.get("changed", []):
                parts.append(
                    f"<tr><td>{_escape(row.get('type') or 'ADJUSTED')}</td>"
                    f"<td>{_escape(row.get('label'))}</td><td class='num'>"
                    f"{_escape(row.get('qty'))}</td></tr>")
            parts.append("</table>")
            if trade.get("wizard_hint"):
                parts.append(
                    f"<div class='hint'>ONE Wizard: "
                    f"{_escape(trade['wizard_hint'])}</div>")
            parts.append("</div>")
        parts.append("</div>")
    else:
        parts.append(
            "<h2>Session trade changes</h2><p class='muted'>"
            "No fills after the selected ONE export.</p>")

    if fills:
        parts.append(
            "<h2>Today's IBKR fills</h2><table><tr><th>Time</th>"
            "<th>Account</th><th>Side</th><th class='num'>Qty</th>"
            "<th>Instrument</th><th class='num'>Price</th></tr>")
        for fill in fills:
            account = str(fill.get("account") or "")
            ticker = str(
                fill.get("underlying") or fill.get("tradingClass") or ""
            )
            label = (
                f"{fill.get('tradingClass') or fill.get('underlying') or ''} "
                f"{fill.get('expiry') or ''} {fill.get('strike') or ''}"
                f"{fill.get('right') or ''}"
            ).strip()
            fill_attrs = _data_attrs(
                _class="fill-row",
                account=account,
                account_type=(cfg.get("account_codes") or {}).get(account, ""),
                ticker=ticker,
                strike=(
                    f"{float(fill['strike']):g}"
                    if fill.get("strike") is not None else ""
                ),
                status=(fill.get("side") or ""),
                time=(fill.get("time") or ""),
            )
            parts.append(
                f"<tr {fill_attrs}><td>{_escape(fill.get('time'))}</td>"
                f"<td>{_escape(fill.get('account'))}</td>"
                f"<td>{_escape(fill.get('side'))}</td>"
                f"<td class='num'>{_escape(fill.get('shares'))}</td>"
                f"<td>{_escape(label)}</td>"
                f"<td class='num'>{_num(fill.get('price'))}</td></tr>")
        parts.append("</table>")

    if strategies:
        parts.append(
            "<h2>OptionStrat update links</h2><p class='muted'>"
            "These internet links work from any computer or phone.</p>")
        for strategy in strategies:
            links = " ".join(
                f"<a class='btn' href='{_escape(url.get('url'))}' "
                f"target='_blank' rel='noopener'>"
                f"Open {_escape(url.get('underlying'))}</a>"
                for url in strategy.get("create_urls", [])
            )
            parts.append(
                f"<div class='info'><b>{_escape(strategy.get('account'))} · "
                f"{_escape(strategy.get('name'))}</b> "
                f"({len(strategy.get('legs', []))} legs)<div>{links}</div></div>")

    parts.append(
        "<div class='muted' style='margin-top:18px;border-top:1px solid "
        "#d8dee4;padding-top:8px'>Frozen snapshot only; use the trading-laptop "
        "dashboard for a new live check.</div>"
    )
    parts.append(r"""
<script>
(function(){
  var facetConfig = [
    ['report-account', 'account'],
    ['report-account-type', 'accountType'],
    ['report-ticker', 'ticker'],
    ['report-strike', 'strike'],
    ['report-trade', 'trade'],
    ['report-status', 'status']
  ];
  var filterable = Array.prototype.slice.call(
    document.querySelectorAll('.report-filterable')
  );

  function splitValues(value) {
    return String(value || '').split('|').map(function(item) {
      return item.trim();
    }).filter(Boolean);
  }

  function populateSelect(id, key) {
    var select = document.getElementById(id);
    if (!select) return;
    var values = [];
    filterable.forEach(function(item) {
      values = values.concat(splitValues(item.dataset[key]));
    });
    values = Array.from(new Set(values)).sort(function(a, b) {
      return a.localeCompare(b, undefined, {numeric:true});
    });
    values.forEach(function(value) {
      var option = document.createElement('option');
      option.value = value;
      option.textContent = value;
      select.appendChild(option);
    });
  }

  function applyFilters() {
    var query = (document.getElementById('report-search').value || '')
      .toLowerCase().trim().split(/\s+/).filter(Boolean);
    var active = {};
    facetConfig.forEach(function(pair) {
      active[pair[1]] = document.getElementById(pair[0]).value;
    });
    var visible = 0;
    filterable.forEach(function(item) {
      var show = facetConfig.every(function(pair) {
        var wanted = active[pair[1]];
        return !wanted || splitValues(item.dataset[pair[1]]).indexOf(wanted) !== -1;
      });
      if (show && query.length) {
        var text = item.textContent.toLowerCase();
        show = query.every(function(term) { return text.indexOf(term) !== -1; });
      }
      item.classList.toggle('report-hidden', !show);
      if (show) visible += 1;
    });
    var count = document.getElementById('report-visible-count');
    if (count) count.textContent = visible + ' matching row(s) / card(s)';
  }

  function sortAdjustments() {
    var container = document.getElementById('report-adjustments');
    var select = document.getElementById('report-adjustment-sort');
    if (!container || !select) return;
    var cards = Array.prototype.slice.call(
      container.querySelectorAll('.adjustment-card')
    );
    var mode = select.value;
    cards.sort(function(a, b) {
      if (mode === 'trade') {
        return String(a.dataset.trade || '').localeCompare(
          String(b.dataset.trade || ''), undefined, {numeric:true}
        );
      }
      if (mode === 'account') {
        var accountCmp = String(a.dataset.account || '').localeCompare(
          String(b.dataset.account || ''), undefined, {numeric:true}
        );
        if (accountCmp) return accountCmp;
      }
      var aTime = Date.parse(a.dataset.time || '') || 0;
      var bTime = Date.parse(b.dataset.time || '') || 0;
      return mode === 'time-asc' ? aTime - bTime : bTime - aTime;
    });
    cards.forEach(function(card) { container.appendChild(card); });
  }

  function sortableValue(cell, header) {
    var raw = cell.getAttribute('data-sort-value') || cell.textContent.trim();
    if (/time|date/i.test(header)) {
      var epoch = Date.parse(raw);
      if (!isNaN(epoch)) return {kind:'number', value:epoch};
    }
    var cleaned = raw.replace(/[$,%×]/g, '').trim();
    if (/^[+-]?\d+(?:\.\d+)?$/.test(cleaned)) {
      return {kind:'number', value:Number(cleaned)};
    }
    return {kind:'text', value:raw.toLowerCase()};
  }

  document.querySelectorAll('table').forEach(function(table) {
    var rows = Array.prototype.slice.call(table.rows);
    if (!rows.length || !rows[0].querySelector('th')) return;
    table.classList.add('report-sortable');
    Array.prototype.slice.call(rows[0].cells).forEach(function(th, column) {
      th.addEventListener('click', function() {
        var ascending = th.dataset.direction !== 'asc';
        Array.prototype.slice.call(rows[0].cells).forEach(function(cell) {
          delete cell.dataset.direction;
          cell.classList.remove('report-sorted');
        });
        th.dataset.direction = ascending ? 'asc' : 'desc';
        th.classList.add('report-sorted');
        var body = rows[1] ? rows[1].parentNode : null;
        if (!body) return;
        rows.slice(1).sort(function(a, b) {
          var av = sortableValue(a.cells[column], th.textContent);
          var bv = sortableValue(b.cells[column], th.textContent);
          var cmp = av.kind === 'number' && bv.kind === 'number'
            ? av.value - bv.value
            : String(av.value).localeCompare(String(bv.value), undefined, {numeric:true});
          return ascending ? cmp : -cmp;
        }).forEach(function(row) { body.appendChild(row); });
      });
    });
  });

  facetConfig.forEach(function(pair) {
    populateSelect(pair[0], pair[1]);
    document.getElementById(pair[0]).addEventListener('change', applyFilters);
  });
  document.getElementById('report-search').addEventListener('input', applyFilters);
  document.getElementById('report-adjustment-sort').addEventListener(
    'change', sortAdjustments
  );
  document.getElementById('report-clear').addEventListener('click', function() {
    facetConfig.forEach(function(pair) {
      document.getElementById(pair[0]).value = '';
    });
    document.getElementById('report-search').value = '';
    applyFilters();
  });
  sortAdjustments();
  applyFilters();
})();
</script>
</div></body></html>""")
    return subject, "".join(parts)


def build_email_message(
    subject: str,
    html_body: str,
    cfg: dict,
    attachment_html: str | None = None,
) -> tuple[MIMEMultipart | None, str]:
    """Build the MIME message separately so it can be verified without sending."""
    email_cfg = cfg.get("email", {})
    username = email_cfg.get("username")
    password = email_cfg.get("password")
    to_email = email_cfg.get("to_email") or username
    if not email_cfg.get("enabled"):
        return None, "Email reporting is disabled in config.json."
    if not username or not password:
        return None, "SMTP username or password is missing in config.json."

    message = MIMEMultipart("mixed")
    message["Subject"] = subject
    message["From"] = username
    message["To"] = to_email
    alternative = MIMEMultipart("alternative")
    alternative.attach(MIMEText(html_body, "html", "utf-8"))
    message.attach(alternative)

    if attachment_html:
        filename = (
            f"TWS_Matcher_Portable_Report_"
            f"{datetime.now().strftime('%Y-%m-%d_%H%M')}.html"
        )
        attachment = MIMEText(attachment_html, "html", "utf-8")
        attachment.add_header(
            "Content-Disposition", "attachment", filename=filename)
        message.attach(attachment)
    return message, to_email


def send_email(
    subject: str,
    html_body: str,
    cfg: dict,
    attachment_html: str | None = None,
) -> tuple[bool, str]:
    """Send the portable report using the configured SMTP account."""
    message, target_or_error = build_email_message(
        subject, html_body, cfg, attachment_html)
    if message is None:
        return False, target_or_error

    email_cfg = cfg.get("email", {})
    username = email_cfg["username"]
    try:
        with smtplib.SMTP(
            email_cfg.get("smtp_server", "smtp.gmail.com"),
            int(email_cfg.get("smtp_port", 587)),
        ) as server:
            server.starttls()
            server.login(username, email_cfg["password"])
            server.sendmail(
                username, [target_or_error], message.as_string())
        return True, f"Portable session report emailed to {target_or_error}."
    except Exception as exc:
        return False, f"Failed to send email: {type(exc).__name__}: {exc}"


def main():
    cfg = reconcile.load_config(CONFIG_PATH) if os.path.exists(CONFIG_PATH) else {}
    ibkr_snap = (
        json.load(open(IBKR_JSON)) if os.path.exists(IBKR_JSON)
        else {"legs": [], "fills_today": []}
    )
    one_snap = (
        json.load(open(ONE_JSON)) if os.path.exists(ONE_JSON)
        else {"positions": []}
    )
    subject, html_body = generate_report_html(ibkr_snap, one_snap, cfg)

    if len(sys.argv) > 1 and sys.argv[1] == "--send":
        success, message = send_email(
            subject, html_body, cfg, attachment_html=html_body)
        print(f"[{'OK' if success else 'ERROR'}] {message}")
    else:
        out_path = os.path.join(HERE, "session_report_preview.html")
        with open(out_path, "w", encoding="utf-8") as file:
            file.write(html_body)
        print(f"Generated portable report preview -> {out_path}")


if __name__ == "__main__":
    main()
