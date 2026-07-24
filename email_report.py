#!/usr/bin/env python3
"""
email_report.py — On-demand / End-of-session Email Fill & Trade Activity Report.

Pulls today's IBKR fills and current positions, formats a clean HTML email with:
  1. Executive Summary & IBKR Fills
  2. Classified Activity (What happened to which trade)
  3. ONE Import Checklist (which Trade ID to link each fill to)
  4. 1-Click OptionStrat Mobile Update Links

Can be run standalone:
    python email_report.py
Or triggered via the dashboard UI at http://127.0.0.1:8787/send_email
"""

from __future__ import annotations

import json
import os
import smtplib
import sys
from datetime import datetime, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

import canonical_engine as eng
import one_reader
import optionstrat_url
import reconcile

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(HERE, "config.json")
IBKR_JSON = os.path.join(HERE, "canonical_positions.json")
ONE_JSON = os.path.join(HERE, "one_positions.json")


def generate_report_html(ibkr_snap: dict, one_snap: dict, cfg: dict) -> tuple[str, str]:
    """Generates (subject, html_body) for today's session activity with link to interactive dashboard."""
    now_str = datetime.now().strftime("%b %d, %Y - %H:%M")
    fills = ibkr_snap.get("fills_today", [])
    account_map = cfg.get("account_map", {})
    dash_url = cfg.get("dashboard_url", "http://127.0.0.1:8787/")

    # Load raw ONE legs for Trade ID matching
    one_path = one_reader.find_default_csv(cfg.get("one_export_dirs"))
    one_raw_legs = one_reader.read_summary_report(one_path) if (one_path and os.path.exists(one_path)) else []

    activity = reconcile.classify_activity(fills, ibkr_snap.get("legs", []),
                                           one_legs=one_raw_legs,
                                           account_map=account_map)

    os_strats = optionstrat_url.generate(one_path, ibkr_legs=ibkr_snap.get("legs", []))["strategies"]

    n_rolled = len(activity.get("rolled", []))
    n_opened = len(activity.get("opened", []))
    n_closed = len(activity.get("closed", []))
    n_changed = len(activity.get("changed", []))
    act_n = n_rolled + n_opened + n_closed + n_changed

    all_accts = sorted(set(
        [r.get("account") for cat in activity.values() for r in cat if r.get("account")] +
        [fl.get("account") for fl in fills if fl.get("account")]
    ))
    acct_str = ", ".join(all_accts) if all_accts else "All accounts"

    subject = f"📊 TWS Matcher Session Report — {now_str} ({len(fills)} Fills / {act_n} Trade Changes)"

    html = [f"""
    <!DOCTYPE html>
    <html>
    <head>
    <style>
        body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; background-color: #f6f8fa; color: #24292e; margin: 0; padding: 6px; }}
        .card {{ background: #ffffff; border: 1px solid #e1e4e8; border-radius: 8px; padding: 12px 16px; max-width: min(800px, 96vw); margin: 0 auto 10px; color: #24292e; box-shadow: 0 1px 4px rgba(0,0,0,0.05); box-sizing: border-box; }}
        h1 {{ font-size: 16px; margin-top: 0; margin-bottom: 2px; color: #0366d6; }}
        h2 {{ font-size: 13.5px; margin-top: 10px; margin-bottom: 4px; border-bottom: 1px solid #e1e4e8; padding-bottom: 3px; color: #0366d6; }}
        .btn-primary {{ display: inline-block; background: #0366d6; color: #ffffff !important; padding: 8px 16px; text-decoration: none; border-radius: 6px; font-weight: bold; font-size: 13px; text-align: center; }}
        .btn-secondary {{ display: inline-block; background: #28a745; color: #ffffff !important; padding: 3px 8px; text-decoration: none; border-radius: 4px; font-weight: bold; font-size: 11px; margin-top: 2px; }}
        .badge {{ font-weight: bold; font-size: 11px; padding: 2px 7px; border-radius: 4px; color: #ffffff; display: inline-block; }}
        .stat-box {{ background: #f6f8fa; border: 1px solid #e1e4e8; border-radius: 6px; padding: 8px 12px; margin: 8px 0; }}
        .hint {{ font-size: 11.5px; color: #0366d6; background: #f1f8ff; padding: 2px 6px; border-radius: 4px; margin-top: 2px; }}
        .table-wrap {{ overflow-x: auto; -webkit-overflow-scrolling: touch; }}
        table {{ width: 100%; border-collapse: collapse; margin-top: 4px; font-size: 12px; table-layout: auto; }}
        th, td {{ padding: 3px 6px; text-align: left; border-bottom: 1px solid #e1e4e8; white-space: nowrap; }}
        th {{ background: #f6f8fa; color: #586069; }}
        .muted {{ color: #586069; font-size: 11.5px; }}
    </style>
    </head>
    <body>
        <div class="card">
            <h1>📊 TWS Matcher — Session Summary Report</h1>
            <p class="muted">Generated on {now_str} UTC &middot; Source: IBKR TWS Truth</p>

            <div style="text-align: center; margin: 20px 0; padding: 16px; background: #f1f8ff; border: 1px solid #c8e1ff; border-radius: 8px;">
                <div style="font-size: 14px; font-weight: bold; color: #0366d6; margin-bottom: 8px;">Interactive Dashboard &amp; Sorting Tools</div>
                <a href="{dash_url}" class="btn-primary" target="_blank">
                    🚀 Open Interactive Session Dashboard &rarr;
                </a>
                <div class="muted" style="margin-top: 10px; font-size: 12px;">
                    Link: <a href="{dash_url}" style="color: #0366d6;">{dash_url}</a><br>
                    📎 <b>Interactive HTML File Attached:</b> Open <code>TWS_Matcher_Interactive_Report.html</code> in any browser to sort by Ticker, ONE Trade ID, or Account.
                </div>
            </div>

            <div class="stat-box">
                <div style="display: flex; justify-content: space-between; align-items: center;">
                    <span style="font-size: 14px; font-weight: bold;">Session Activity Overview</span>
                    <span class="badge" style="background: {'#28a745' if act_n == 0 else '#b08800'};">
                        {'🟢 IN SYNC' if act_n == 0 else f'🟡 {act_n} ADJUSTMENTS'}
                    </span>
                </div>
                <div style="margin-top: 10px; font-size: 13px; color: #24292e; line-height: 1.6;">
                    &bull; <b>IBKR Fills Today:</b> {len(fills)} executed fill(s)<br>
                    &bull; <b>Trade Changes:</b> {act_n} total ({n_rolled} Rolled, {n_opened} Opened, {n_closed} Closed, {n_changed} Adjusted)<br>
                    &bull; <b>Accounts Affected:</b> {acct_str}
                </div>
            </div>
    """]

    # SECTION 1: WHAT HAPPENED TO WHICH TRADE
    if act_n > 0:
        html.append("<h2>🔄 Session Trade Changes &amp; ONE Import Links</h2>")

        if activity.get("rolled"):
            html.append("<div style='margin-top:10px;'><b>🔄 Rolled Positions</b></div><table>")
            rolled_sorted = sorted(activity["rolled"], key=lambda r: (str(r.get("account")), str(r.get("underlying")), r.get("one_trade_id") or 999))
            for r in rolled_sorted:
                html.append(f"""
                <tr>
                    <td><b>{r['account']}</b></td>
                    <td>{r['from']} &rarr; {r['to']} (&times;{r['qty']:.0f})</td>
                </tr>
                <tr><td colspan='2'><div class='hint'>👉 ONE Wizard: {r.get('wizard_hint', '')}</div></td></tr>
                """)
            html.append("</table>")

        if activity.get("opened"):
            html.append("<div style='margin-top:10px;'><b>🟢 New / Opened Legs</b></div><table>")
            opened_sorted = sorted(activity["opened"], key=lambda o: (str(o.get("account")), str(o.get("underlying")), o.get("one_trade_id") or 999))
            for o in opened_sorted:
                html.append(f"""
                <tr>
                    <td><b>{o['account']}</b></td>
                    <td><span style='color:#28a745;font-weight:bold;'>OPENED</span> {o['label']} ({o['qty']:+.0f} @ ${o['px']:.4f})</td>
                </tr>
                <tr><td colspan='2'><div class='hint'>👉 ONE Wizard: {o.get('wizard_hint', '')}</div></td></tr>
                """)
            html.append("</table>")

        if activity.get("closed"):
            html.append("<div style='margin-top:10px;'><b>🔴 Closed / Trimmed Legs</b></div><table>")
            closed_sorted = sorted(activity["closed"], key=lambda c: (str(c.get("account")), str(c.get("underlying")), c.get("one_trade_id") or 999))
            for c in closed_sorted:
                html.append(f"""
                <tr>
                    <td><b>{c['account']}</b></td>
                    <td><span style='color:#d73a49;font-weight:bold;'>CLOSED</span> {c['label']} (was {c['qty']:+.0f})</td>
                </tr>
                <tr><td colspan='2'><div class='hint'>👉 ONE Wizard: {c.get('wizard_hint', '')}</div></td></tr>
                """)
            html.append("</table>")

        if activity.get("changed"):
            html.append("<div style='margin-top:10px;'><b>🟡 Adjusted / Sized Legs</b></div><table>")
            changed_sorted = sorted(activity["changed"], key=lambda ch: (str(ch.get("account")), str(ch.get("underlying")), ch.get("one_trade_id") or 999))
            for c in changed_sorted:
                html.append(f"""
                <tr>
                    <td><b>{c['account']}</b></td>
                    <td><span style='color:#b08800;font-weight:bold;'>{c['type']}</span> {c['label']} ({c['qty']:+.0f})</td>
                </tr>
                <tr><td colspan='2'><div class='hint'>👉 ONE Wizard: {c.get('wizard_hint', '')}</div></td></tr>
                """)
            html.append("</table>")
    else:
        html.append("<p style='color:#586069; margin-top:15px;'>No new trades or adjustments executed during today's session.</p>")

    # SECTION 2: OPTIONSTRAT MOBILE LINKS
    if os_strats:
        html.append("<h2>📱 OptionStrat Mobile Update Links (1-Click)</h2>")
        html.append("<p class='muted'>Click below to open and update affected campaigns directly on your mobile device:</p>")
        for s in os_strats:
            urls_html = " &nbsp; ".join([f"<a href='{u['url']}' class='btn-secondary' target='_blank'>Update {u['underlying']} ↗</a>" for u in s.get("create_urls", [])])
            html.append(f"""
            <div style="margin-bottom:12px; padding:10px; background:#f6f8fa; border-radius:6px; border:1px solid #e1e4e8;">
                <b>{s['account']} &middot; {s['name']}</b> ({len(s['legs'])} legs)
                <div style="margin-top:6px;">{urls_html}</div>
            </div>
            """)

    html.append(f"""
            <div style="margin-top:24px; padding-top:12px; border-top:1px solid #e1e4e8; font-size:12px; color:#586069; text-align:center;">
                Sent automatically by TWS Matcher &middot; Single Source of Truth: IBKR TWS<br>
                <a href="{dash_url}" style="color:#0366d6;">Open Interactive Dashboard</a>
            </div>
        </div>
    </body>
    </html>
    """)

    return subject, "".join(html)


def send_email(subject: str, html_body: str, cfg: dict, attachment_html: str | None = None) -> tuple[bool, str]:
    """Sends the report email using SMTP settings in config.json, with optional interactive HTML attachment."""
    email_cfg = cfg.get("email", {})
    if not email_cfg.get("enabled"):
        return False, "Email reporting is disabled in config.json ('email.enabled': false)."

    smtp_server = email_cfg.get("smtp_server", "smtp.gmail.com")
    smtp_port = int(email_cfg.get("smtp_port", 587))
    username = email_cfg.get("username")
    password = email_cfg.get("password")
    to_email = email_cfg.get("to_email") or username

    if not username or not password:
        return False, "SMTP username or password missing in config.json."

    try:
        msg = MIMEMultipart("mixed")
        msg["Subject"] = subject
        msg["From"] = username
        msg["To"] = to_email

        # Attach main HTML body
        msg_alternative = MIMEMultipart("alternative")
        msg.attach(msg_alternative)
        msg_alternative.attach(MIMEText(html_body, "html", "utf-8"))

        # Attach standalone interactive HTML file if provided
        if attachment_html:
            filename = f"TWS_Matcher_Interactive_Report_{datetime.now().strftime('%Y-%m-%d')}.html"
            part = MIMEText(attachment_html, "html", "utf-8")
            part.add_header("Content-Disposition", "attachment", filename=filename)
            msg.attach(part)

        with smtplib.SMTP(smtp_server, smtp_port) as server:
            server.starttls()
            server.login(username, password)
            server.sendmail(username, [to_email], msg.as_string())

        return True, f"Session report successfully emailed to {to_email}!"
    except Exception as exc:
        return False, f"Failed to send email: {type(exc).__name__}: {exc}"


def main():
    cfg = reconcile.load_config(CONFIG_PATH) if os.path.exists(CONFIG_PATH) else {}
    ibkr_snap = json.load(open(IBKR_JSON)) if os.path.exists(IBKR_JSON) else {"legs": [], "fills_today": []}
    one_snap = json.load(open(ONE_JSON)) if os.path.exists(ONE_JSON) else {"positions": []}

    subject, html_body = generate_report_html(ibkr_snap, one_snap, cfg)
    
    if len(sys.argv) > 1 and sys.argv[1] == "--send":
        try:
            import dashboard
            interactive_html = dashboard.render_html()
        except Exception:
            interactive_html = None
        success, msg = send_email(subject, html_body, cfg, attachment_html=interactive_html)
        print(f"[{'OK' if success else 'ERROR'}] {msg}")
    else:
        out_path = os.path.join(HERE, "session_report_preview.html")
        with open(out_path, "w", encoding="utf-8") as fh:
            fh.write(html_body)
        print(f"Generated Session Report Preview -> {out_path}")
        print("To send email via SMTP, configure 'email' in config.json and run with --send.")


if __name__ == "__main__":
    main()
