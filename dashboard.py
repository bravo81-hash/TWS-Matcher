#!/usr/bin/env python3
"""
TWS Matcher dashboard + daemon  —  the one local service.

Holds a single persistent TWS connection, and on a market-hours-aware interval
(or on-demand) re-pulls IBKR truth, re-reads the newest ONE Summary Report CSV
from ~/Downloads, runs the reconciliation, and serves a small local web page
that shows per-account MATCH / DIFF, the flagged differences, and today's fills.

No push alerts (by config): diffs are highlighted on the page.

RUN
    python dashboard.py
    then open  http://127.0.0.1:8787/

It reuses:
    canonical_engine.build_snapshot   (IBKR)
    one_reader.build_one_snapshot     (ONE)
    reconcile.reconcile_snapshots     (diff)
    config.json                       (account map + tolerances)

NOTE on ONE freshness: ONE positions only change when you re-export the Summary
Report to Downloads (Reports -> Export -> CSV). The page shows the ONE file's
age so you know when it's stale. IBKR truth refreshes live every cycle.
"""

from __future__ import annotations

import csv
import html
import io
import json
import os
import threading
import time
from collections import defaultdict
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from zoneinfo import ZoneInfo

from ib_async import IB

import canonical_engine as eng
import flex_export
import naming_check
import one_reader
import optionstrat_reader
import optionstrat_url
import recon_one_os
import reconcile

HOST = "127.0.0.1"
WEB_PORT = 8787
POLL_RTH = 30          # seconds between cycles during US market hours
POLL_OFF = 300         # seconds between cycles outside market hours
ET = ZoneInfo("America/New_York")

# shared state (guarded by _lock)
_lock = threading.Lock()
_state: dict = {"status": "starting", "result": None, "error": None,
                "last_cycle": None, "ibkr_connected": False, "one_file": None,
                "one_mtime": None}
_check_now = threading.Event()


# --------------------------------------------------------------- market hours
def is_rth(now_et: datetime) -> bool:
    """Roughly US index-option regular trading hours (Mon-Fri 09:30-16:15 ET)."""
    if now_et.weekday() >= 5:
        return False
    start = now_et.replace(hour=9, minute=30, second=0, microsecond=0)
    end = now_et.replace(hour=16, minute=15, second=0, microsecond=0)
    return start <= now_et <= end


# --------------------------------------------------------------- worker loop
# client ids to try, in order, if one is stuck "already in use" (e.g. a
# previous daemon instance that didn't fully release its TWS session).
CLIENT_IDS = [eng.CLIENT_ID, 18, 19, 20, 21]


def connect_tws(ib: IB):
    """Connect trying a few client ids; tolerates a stale session holding one."""
    last = None
    for cid in CLIENT_IDS:
        try:
            ib.connect(eng.HOST, eng.PORT, clientId=cid,
                       timeout=eng.CONNECT_TIMEOUT)
            _set(status=f"connected (clientId {cid})", client_id=cid)
            return
        except Exception as exc:
            last = exc
            if "client id is already in use" in str(exc).lower() or \
               "326" in str(exc):
                try:
                    ib.disconnect()
                except Exception:
                    pass
                continue          # try next id
            raise
    raise RuntimeError(f"all client ids in use {CLIENT_IDS}: {last}")


def worker():
    cfg = reconcile.load_config()
    ib = IB()
    while True:
        try:
            if not ib.isConnected():
                _set(status="connecting TWS", ibkr_connected=False)
                connect_tws(ib)
                _set(ibkr_connected=True)
            run_cycle(ib, cfg)
        except Exception as exc:           # keep the daemon alive
            _set(status="error", error=f"{type(exc).__name__}: {exc}")
            try:
                ib.disconnect()
            except Exception:
                pass
            time.sleep(5)
            continue

        # wait until next cycle or an on-demand check
        now_et = datetime.now(ET)
        delay = POLL_RTH if is_rth(now_et) else POLL_OFF
        _wait(ib, delay)


def run_cycle(ib: IB, cfg: dict):
    _set(status="refreshing")
    ibkr_snap = eng.build_snapshot(ib)

    one_path = one_reader.find_default_csv(cfg.get("one_export_dirs"))
    one_mtime = os.path.getmtime(one_path) if os.path.exists(one_path) else None
    one_snap = one_reader.build_one_snapshot(one_path)

    result = reconcile.reconcile_snapshots(ibkr_snap, one_snap, cfg)

    # derived outputs (OptionStrat mirror URLs + ONE Flex-import rows)
    os_strategies = optionstrat_url.generate(
        one_path, ibkr_legs=ibkr_snap["legs"])["strategies"]
    flex_by_acct, flex_skipped = flex_export.generate(
        ibkr_snap, cfg.get("flex_timezone"))

    # classify broker activity since the last ONE export (rolled/opened/closed/...)
    fills_since = [f for f in ibkr_snap.get("fills_today", [])
                   if one_mtime and (_fill_epoch(f.get("time")) or 0) > one_mtime]
    activity = reconcile.classify_activity(fills_since, ibkr_snap["legs"])

    # naming-convention migration status per ONE combo
    try:
        naming_rows = naming_check.report(one_path, cfg.get("account_map"),
                                          cfg.get("account_codes"))
    except Exception:
        naming_rows = []

    # ONE <-> OptionStrat reconciliation (OptionStrat report is a manual download)
    os_path = optionstrat_reader.find_default_xlsx(cfg.get("one_export_dirs"))
    oneos = None
    os_mtime = None
    if os_path and os.path.exists(os_path):
        os_mtime = os.path.getmtime(os_path)
        try:
            oneos = recon_one_os.reconcile(one_path, os_path, cfg)
        except Exception as exc:
            oneos = {"error": f"{type(exc).__name__}: {exc}"}

    _set(status="ok", result=result, error=None,
         last_cycle=datetime.now(timezone.utc).isoformat(),
         one_file=one_path, one_mtime=one_mtime,
         os_strategies=os_strategies,
         flex={a: rows for a, rows in flex_by_acct.items()},
         flex_skipped=flex_skipped,
         activity=activity,
         naming=naming_rows,
         oneos=oneos, oneos_file=os_path, oneos_mtime=os_mtime)
    # persist latest for other tools
    with open(reconcile.OUTPUT_JSON, "w") as fh:
        json.dump(result, fh, indent=2, default=str)


def _wait(ib: IB, seconds: float):
    """Sleep in small slices so the asyncio loop stays alive and a 'check now'
    request interrupts promptly."""
    end = time.time() + seconds
    while time.time() < end:
        if _check_now.is_set():
            _check_now.clear()
            return
        ib.sleep(0.5)        # services the ib_async event loop


def _set(**kw):
    with _lock:
        _state.update(kw)


# --------------------------------------------------------------- rendering
STATUS_COLORS = {
    "MATCH": "#1a7f37", "PRICE_DRIFT": "#9a6700", "QTY_MISMATCH": "#cf222e",
    "IBKR_ONLY": "#0969da", "ONE_ONLY": "#8250df",
}
ORDER = ["QTY_MISMATCH", "ONE_ONLY", "IBKR_ONLY", "PRICE_DRIFT", "MATCH"]

PAGE_STYLE = (
    "body{font:13px/1.45 -apple-system,Segoe UI,Roboto,sans-serif;"
    "margin:0;background:#0d1117;color:#e6edf3}"
    ".wrap{max-width:1080px;margin:0 auto;padding:18px}"
    "h1{font-size:18px;margin:0 0 4px} .sub{color:#8b949e;margin-bottom:14px}"
    ".acct{background:#161b22;border:1px solid #30363d;border-radius:8px;"
    "padding:12px 14px;margin:12px 0}"
    ".acct h2{font-size:14px;margin:0 0 8px;display:flex;justify-content:space-between}"
    ".pill{border-radius:12px;padding:1px 9px;font-size:12px;font-weight:600}"
    ".MATCHpill{background:#1a7f3733;color:#3fb950}"
    ".DIFFpill{background:#cf222e33;color:#ff7b72}"
    "table{border-collapse:collapse;width:100%}"
    "td,th{padding:3px 8px;text-align:right;border-bottom:1px solid #21262d}"
    "td.l,th.l{text-align:left} .mono{font-variant-numeric:tabular-nums}"
    ".tag{font-weight:700;font-size:11px;padding:1px 6px;border-radius:5px;color:#fff}"
    ".muted{color:#8b949e} a.btn{display:inline-block;background:#238636;color:#fff;"
    "padding:6px 14px;border-radius:6px;text-decoration:none;font-weight:600}"
    ".warn{color:#d29922}"
    # top-level page nav
    ".pagenav{margin-bottom:14px;display:flex;gap:4px}"
    ".pagenav a{color:#8b949e;text-decoration:none;padding:6px 12px;border-radius:6px;"
    "font-weight:600;font-size:13px}"
    ".pagenav a.active{color:#e6edf3;background:#161b22;border:1px solid #30363d}"
    ".pagenav a:hover:not(.active){color:#e6edf3}"
    # in-page tab groups (OptionStrat mirror, Fills)
    ".tabnav{display:flex;gap:4px;flex-wrap:wrap;border-bottom:1px solid #30363d;"
    "margin-bottom:10px}"
    ".tabbtn{padding:5px 12px;border-radius:6px 6px 0 0;cursor:pointer;color:#8b949e;"
    "text-decoration:none;font-size:12.5px;border:1px solid transparent;"
    "margin-bottom:-1px;background:none}"
    ".tabbtn.active{color:#e6edf3;background:#0d1117;border-color:#30363d;"
    "border-bottom-color:#0d1117}"
    ".tabpanel{display:none}.tabpanel.active{display:block}"
    # sortable table headers
    "th.sortable{cursor:pointer;user-select:none}"
    "th.sortable:hover{color:#e6edf3}"
    "th.sortable::after{content:'\\2195';opacity:0.35;margin-left:4px}"
    "th.sortable.sorted::after{opacity:1}"
)

TAB_JS = """
<script>
(function(){
  function initTabs(){
    document.querySelectorAll('[data-tabgroup]').forEach(function(group){
      var gid = group.getAttribute('data-tabgroup');
      var buttons = Array.prototype.slice.call(group.querySelectorAll('.tabbtn'));
      var panels = Array.prototype.slice.call(group.querySelectorAll('.tabpanel'));
      var keys = buttons.map(function(b){ return b.getAttribute('data-tab'); });
      if (!keys.length) return;
      var saved = sessionStorage.getItem('tab:' + gid);
      var active = (saved && keys.indexOf(saved) !== -1) ? saved : keys[0];
      function show(key){
        buttons.forEach(function(b){
          b.classList.toggle('active', b.getAttribute('data-tab') === key);
        });
        panels.forEach(function(pnl){
          pnl.classList.toggle('active', pnl.getAttribute('data-tab') === key);
        });
        sessionStorage.setItem('tab:' + gid, key);
      }
      buttons.forEach(function(b){
        b.addEventListener('click', function(e){
          e.preventDefault();
          show(b.getAttribute('data-tab'));
        });
      });
      show(active);
    });
  }
  function initSort(){
    document.querySelectorAll('table.sortable').forEach(function(table){
      var headers = Array.prototype.slice.call(table.querySelectorAll('th.sortable'));
      headers.forEach(function(th, idx){
        th.addEventListener('click', function(){
          var tbody = table.querySelector('tbody');
          if (!tbody) return;
          var rows = Array.prototype.slice.call(tbody.querySelectorAll('tr'));
          var asc = th.getAttribute('data-sort-dir') !== 'asc';
          headers.forEach(function(h){
            h.removeAttribute('data-sort-dir');
            h.classList.remove('sorted');
          });
          th.setAttribute('data-sort-dir', asc ? 'asc' : 'desc');
          th.classList.add('sorted');
          rows.sort(function(a, b){
            var ac = a.children[idx], bc = b.children[idx];
            var av = ac.getAttribute('data-sort-value');
            var bv = bc.getAttribute('data-sort-value');
            var cmp;
            if (av !== null && bv !== null) {
              cmp = parseFloat(av) - parseFloat(bv);
            } else {
              cmp = ac.textContent.trim().localeCompare(bc.textContent.trim());
            }
            return asc ? cmp : -cmp;
          });
          rows.forEach(function(r){ tbody.appendChild(r); });
        });
      });
    });
  }
  document.addEventListener('DOMContentLoaded', function(){ initTabs(); initSort(); });
})();
</script>
"""


def nav_html(active: str) -> str:
    items = [("dashboard", "/", "Dashboard"),
             ("oneos", "/oneos", "ONE ↔ OptionStrat"),
             ("guide", "/guide", "Guide"),
             ("naming", "/naming", "Naming convention")]
    parts = ["<div class='pagenav'>"]
    for key, href, label in items:
        cls = " class='active'" if key == active else ""
        parts.append(f"<a href='{href}'{cls}>{label}</a>")
    parts.append("</div>")
    return "".join(parts)


def _page_head(title: str, refresh_secs: int | None) -> str:
    refresh = (f"<meta http-equiv='refresh' content='{refresh_secs}'>"
               if refresh_secs else "")
    return (f"<!doctype html><html><head><meta charset='utf-8'>{refresh}"
            f"<title>{title}</title><style>{PAGE_STYLE}</style></head>"
            f"<body><div class='wrap'>")


GUIDE_BODY_HTML = """
<div style='line-height:1.5'>
<p class='muted'>TWS/IBKR is the source of truth (live, automatic). ONE updates when you
re-export. OptionStrat you edit by hand. This app observes &amp; compares &mdash; it never
places or changes orders.</p>

<b>Start of session</b>
<ul>
<li>Make sure <b>TWS is running &amp; logged in</b> (live, port 7496).</li>
<li>Double-click the <b>TWS Matcher</b> desktop button &rarr; opens this page and connects to TWS.</li>
<li>Tab title shows state: <b>&#10003;</b> in sync &middot; <b>&#9888;N</b> needs attention.</li>
</ul>

<b>1. Place a trade or adjustment</b>
<ul>
<li>Execute it as usual &mdash; in <b>TWS</b> directly, or in <b>ONE &rarr; Send Order to Broker</b>.
Either way it fills into IBKR (the truth).</li>
</ul>

<b>2. The app reacts automatically (every ~30s in market hours)</b>
<ul>
<li>Re-pulls IBKR positions + today's fills; re-reads your latest ONE export; reconciles per account.</li>
<li>An amber <b>&#9888; Adjustment Mode</b> banner classifies broker activity since your last ONE
export into <b>Rolled / Opened / Closed / Adjusted</b> &mdash; a ready-made checklist of what to
replicate in ONE and OptionStrat.</li>
</ul>

<b>3. Bring ONE into line</b>
<ul>
<li>In <b>ONE</b>, enter/adjust the trade in the <b>same combo</b> (skip if you traded via ONE).</li>
<li><b>Export ONE's Summary Report</b> to Downloads or Documents (Reports &rarr; Export &rarr; CSV).</li>
<li>Click <b>Check now</b> &rarr; newest export auto-selected &rarr; banner clears, account returns to
<b>MATCH</b> (or flags a real <b>PRICE_DRIFT</b> if ONE's price &ne; your actual fill).</li>
<li><i>Optional:</i> use the <b>ONE Flex import</b> download to import the day's fills instead of
retyping, then run ONE's &ldquo;link trades&rdquo; step.</li>
</ul>

<b>4. Bring OptionStrat into line</b>
<ul>
<li>In the <b>OptionStrat mirror</b> panel below, find the combo by <b>name</b> (same name as ONE).</li>
<li>Open your <b>existing saved OptionStrat combo of that name</b> and <b>edit its legs in place</b>
to match the legs shown &mdash; this keeps its entry prices and running P&amp;L.</li>
<li>Only for a <b>brand-new</b> combo: use the small <b>create new &#8599;</b> link, then save/name it
to match ONE.</li>
</ul>

<b>Steady state</b>
<ul>
<li><b>TWS</b> = truth (auto) &middot; <b>ONE</b> = matches after re-export &middot;
<b>OptionStrat</b> = matches after you edit the same-named combos.</li>
<li>Tab shows <b>&#10003;</b> and every account shows <b>MATCH</b> &mdash; aside from stock/ETF
holdings (expected IBKR-only) and any acknowledged legs.</li>
</ul>

<p class='muted'><b>Flags:</b> QTY_MISMATCH = qty differs &middot; PRICE_DRIFT = ONE's price differs
&middot; IBKR_ONLY = in broker not ONE &middot; ONE_ONLY = in ONE not broker &middot; ACK = reviewed-OK,
muted.</p>
</div>
"""


def _age(mtime):
    if not mtime:
        return "n/a"
    secs = time.time() - mtime
    if secs < 90:
        return f"{int(secs)}s ago"
    if secs < 5400:
        return f"{int(secs // 60)}m ago"
    return f"{secs / 3600:.1f}h ago"


def _fill_epoch(tstr):
    """Parse a stored fill time ('2026-06-23 20:08:11+00:00') -> epoch, or None."""
    s = str(tstr or "").strip()
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        try:
            dt = datetime.strptime(s.split(".")[0], "%Y-%m-%d %H:%M:%S%z")
        except ValueError:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def render_html() -> str:
    with _lock:
        st = dict(_state)
        result = st["result"]
    parts = []
    p = parts.append

    # attention state -> tab title badge (visible when the tab is backgrounded)
    act = st.get("activity") or {}
    act_n = sum(len(act.get(k, [])) for k in ("rolled", "opened", "closed", "changed"))

    def _alerting(f):
        # actionable = real divergence, not acknowledged, not an expected
        # stock-only-in-IBKR line (ONE never models stocks, so those are noise).
        if f["status"] == "MATCH" or f.get("acknowledged"):
            return False
        if f["status"] == "IBKR_ONLY" and str(f.get("label", "")).endswith("(STK)"):
            return False
        return True

    prob_n = (sum(1 for finds in result["accounts"].values() for f in finds
                  if _alerting(f)) if result else 0)
    if not result:
        title = "TWS Matcher"
    elif act_n:
        title = f"⚠{act_n} · TWS Matcher"        # broker activity since export
    elif prob_n:
        title = f"⚠{prob_n} · TWS Matcher"       # structural diffs
    else:
        title = "✓ TWS Matcher"                       # all in sync

    p(_page_head(title, refresh_secs=15))
    p(nav_html("dashboard"))

    p("<h1>TWS Matcher &mdash; IBKR vs ONE</h1>")
    conn = ("<span style='color:#3fb950'>TWS connected</span>"
            if st["ibkr_connected"]
            else "<span style='color:#ff7b72'>TWS disconnected</span>")
    last = st.get("last_cycle") or "&mdash;"
    p(f"<div class='sub'>{conn} &nbsp;|&nbsp; status: {html.escape(str(st['status']))}"
      f" &nbsp;|&nbsp; last refresh: {html.escape(str(last))} UTC "
      f"&nbsp;|&nbsp; <a class='btn' href='/check'>Check now</a></div>")

    if st.get("error"):
        p(f"<div class='acct' style='border-color:#cf222e'><b class='warn'>"
          f"Error:</b> {html.escape(str(st['error']))}</div>")

    if not result:
        p("<p class='muted'>Waiting for first reconciliation cycle&hellip;</p>")
        p("</div></body></html>")
        return "".join(parts)

    one_age = _age(st.get("one_mtime"))
    one_path = st.get("one_file") or ""
    one_file = os.path.basename(one_path)
    if os.path.abspath(one_path) == os.path.abspath(one_reader.SAMPLE):
        p("<div class='acct' style='border-color:#cf222e'><b class='warn'>"
          "Using bundled SAMPLE export (24th)</b> &mdash; no real ONE Summary "
          "Report found in the configured folders. Export from ONE to Downloads "
          "or Documents.</div>")
    else:
        stale = (st.get("one_mtime") and (time.time() - st["one_mtime"]) > 3600)
        warn = " <span class='warn'>(stale &mdash; re-export from ONE)</span>" if stale else ""
        p(f"<div class='sub'>ONE export: {html.escape(one_file)} &middot; "
          f"{one_age}{warn}</div>")

    # -------- ADJUSTMENT MODE: classified broker activity since last ONE export --
    if act_n:
        exp_t = ""
        if st.get("one_mtime"):
            exp_t = " " + datetime.fromtimestamp(st["one_mtime"]).strftime("%H:%M")
        p("<div class='acct' style='border:2px solid #d29922;background:#3a2d0a'>"
          "<h2 style='color:#f2cc60'>&#9888; ADJUSTMENT MODE &mdash; "
          f"{act_n} change(s) since your last ONE export{exp_t}</h2>"
          "<div class='muted' style='margin-bottom:8px'>Replicate these in ONE "
          "(re-export &rarr; Check now) and edit the same-named OptionStrat combos."
          "</div>")

        def _act_row(c1, c2, c3, color="#f2cc60"):
            p(f"<tr><td class='l'>{c1}</td>"
              f"<td class='l mono' style='color:{color}'>{c2}</td>"
              f"<td class='mono'>{c3}</td></tr>")

        if act.get("rolled"):
            p("<div style='color:#d29922;font-weight:600;margin-top:4px'>Rolled</div>"
              "<table>")
            for r in act["rolled"]:
                _act_row(html.escape(str(r["account"])),
                         f"{html.escape(r['from'])} &rarr; {html.escape(r['to'])}",
                         f"&times;{r['qty']:.0f}")
            p("</table>")
        if act.get("opened"):
            p("<div style='color:#3fb950;font-weight:600;margin-top:4px'>New / opened</div>"
              "<table>")
            for o in act["opened"]:
                _act_row(html.escape(str(o["account"])), html.escape(o["label"]),
                         f"{o['qty']:+.0f} @ {o['px']:.4f}", "#3fb950")
            p("</table>")
        if act.get("closed"):
            p("<div style='color:#ff7b72;font-weight:600;margin-top:4px'>Closed</div>"
              "<table>")
            for c in act["closed"]:
                _act_row(html.escape(str(c["account"])), html.escape(c["label"]),
                         f"was {c['qty']:+.0f}", "#ff7b72")
            p("</table>")
        if act.get("changed"):
            p("<div style='color:#d29922;font-weight:600;margin-top:4px'>Adjusted</div>"
              "<table>")
            for c in act["changed"]:
                _act_row(html.escape(str(c["account"])),
                         f"{html.escape(c['label'])} ({c['type'].lower()})",
                         f"{c['qty']:+.0f}")
            p("</table>")
        p("</div>")

    # a finding is a real problem only if not MATCH and not acknowledged
    def problem(f):
        return f["status"] != "MATCH" and not f.get("acknowledged")

    # grand totals (acknowledged excluded from diff tags; shown as ACK)
    grand = {s: 0 for s in ORDER}
    ack_total = 0
    for findings in result["accounts"].values():
        for f in findings:
            if f.get("acknowledged"):
                ack_total += 1
            else:
                grand[f["status"]] = grand.get(f["status"], 0) + 1
    tot = " &nbsp; ".join(
        f"<span class='tag' style='background:{STATUS_COLORS[s]}'>{s} {grand[s]}</span>"
        for s in ORDER if grand[s])
    if ack_total:
        tot += (f" &nbsp; <span class='tag' style='background:#57606a'>"
                f"ACK {ack_total}</span>")
    p(f"<div class='sub'>{tot}</div>")

    for acct in sorted(result["accounts"]):
        findings = result["accounts"][acct]
        counts = {s: 0 for s in ORDER}
        for f in findings:
            if not f.get("acknowledged"):
                counts[f["status"]] += 1
        clean = all(counts[s] == 0 for s in
                    ("QTY_MISMATCH", "ONE_ONLY", "IBKR_ONLY", "PRICE_DRIFT"))
        pill = ("<span class='pill MATCHpill'>MATCH</span>" if clean
                else "<span class='pill DIFFpill'>DIFF</span>")
        summ = " ".join(f"{s}:{counts[s]}" for s in ORDER if counts[s])
        n_ack = sum(1 for f in findings if f.get("acknowledged"))
        if n_ack:
            summ += f" ACK:{n_ack}"
        p(f"<div class='acct'><h2><span>{html.escape(acct)} "
          f"<span class='muted'>{html.escape(summ)}</span></span>{pill}</h2>")

        flags = sorted((f for f in findings if problem(f)),
                       key=lambda f: ORDER.index(f["status"]))
        acked = [f for f in findings if f.get("acknowledged")]
        if not flags:
            p(f"<div class='muted'>All {counts['MATCH']} instruments reconcile."
              + (f" ({n_ack} acknowledged)" if n_ack else "") + "</div>")
        else:
            p("<table><tr><th class='l'>instrument</th><th>IBKR qty</th>"
              "<th>IBKR px</th><th>ONE qty</th><th>ONE px</th>"
              "<th>&Delta;px</th><th class='l'>flag</th></tr>")
            for f in flags:
                iq = "" if f["ibkr_qty"] is None else f"{f['ibkr_qty']:+.0f}"
                oq = "" if f["one_qty"] is None else f"{f['one_qty']:+.0f}"
                ip = "" if f["ibkr_px"] is None else f"{f['ibkr_px']:.4f}"
                op = "" if f["one_px"] is None else f"{f['one_px']:.4f}"
                dpx = f"{f['px_delta']:+.4f}" if f.get("px_delta") not in (None,) and \
                    f["status"] == "PRICE_DRIFT" else ""
                color = STATUS_COLORS[f["status"]]
                p(f"<tr><td class='l mono'>{html.escape(f['label'])}</td>"
                  f"<td class='mono'>{iq}</td><td class='mono'>{ip}</td>"
                  f"<td class='mono'>{oq}</td><td class='mono'>{op}</td>"
                  f"<td class='mono'>{dpx}</td>"
                  f"<td class='l'><span class='tag' style='background:{color}'>"
                  f"{f['status']}</span></td></tr>")
            p("</table>")
        if acked:
            p("<div class='muted' style='margin-top:6px'>Acknowledged (reviewed-OK):</div>")
            p("<table>")
            for f in acked:
                ip = "" if f["ibkr_px"] is None else f"{f['ibkr_px']:.4f}"
                op = "" if f["one_px"] is None else f"{f['one_px']:.4f}"
                p(f"<tr><td class='l mono muted'>{html.escape(f['label'])}</td>"
                  f"<td class='mono muted'>IBKR {ip}</td>"
                  f"<td class='mono muted'>ONE {op}</td>"
                  f"<td class='l'><span class='tag' style='background:#57606a'>"
                  f"ACK {html.escape(f['status'])}</span></td></tr>")
            p("</table>")
        p("</div>")

    # unmapped / ignored ONE groups
    if result.get("unmapped_one_groups"):
        ignore = set(result.get("ignore_one_accounts", []))
        rows = " &nbsp; ".join(
            f"{html.escape(g)}: {n}{' (ignored)' if g in ignore else ' <b class=warn>REVIEW</b>'}"
            for g, n in result["unmapped_one_groups"].items())
        p(f"<div class='acct'><h2>Unmapped ONE groups</h2><div class='muted'>{rows}</div></div>")

    # today's fills — tab per account, sortable columns
    fills = result.get("fills_today", [])
    if fills:
        by_acct: dict = defaultdict(list)
        for fl in fills:
            by_acct[str(fl.get("account", ""))].append(fl)
        accts = sorted(by_acct)
        p(f"<div class='acct'><h2>Today's IBKR fills ({len(fills)})</h2>")
        p("<div data-tabgroup='fills'><div class='tabnav'>")
        for i, acct in enumerate(accts):
            p(f"<a href='#' class='tabbtn{' active' if i == 0 else ''}' "
              f"data-tab='{html.escape(acct)}'>{html.escape(acct)} "
              f"({len(by_acct[acct])})</a>")
        p("</div>")
        for i, acct in enumerate(accts):
            cls = "tabpanel active" if i == 0 else "tabpanel"
            p(f"<div class='{cls}' data-tab='{html.escape(acct)}'>")
            p("<table class='sortable'><thead><tr>"
              "<th class='l sortable'>time</th><th class='sortable'>side</th>"
              "<th class='sortable'>qty</th><th class='l sortable'>instrument</th>"
              "<th class='sortable'>price</th></tr></thead><tbody>")
            rows = sorted(by_acct[acct], key=lambda f: str(f.get("time", "")),
                         reverse=True)
            for fl in rows[:100]:
                lbl = (f"{fl.get('tradingClass')} {fl.get('expiry')} "
                       f"{fl.get('strike')}{fl.get('right') or ''}")
                shares = fl.get("shares") or 0
                price = fl.get("price") or 0
                p(f"<tr><td class='l mono'>{html.escape(str(fl.get('time','')))}</td>"
                  f"<td>{html.escape(str(fl.get('side','')))}</td>"
                  f"<td class='mono' data-sort-value='{shares}'>{shares}</td>"
                  f"<td class='l mono'>{html.escape(lbl)}</td>"
                  f"<td class='mono' data-sort-value='{price}'>{price}</td></tr>")
            p("</tbody></table></div>")
        p("</div></div>")

    # OptionStrat mirror: tab per account, per-combo current legs to keep your
    # SAVED combos in sync
    os_strats = st.get("os_strategies") or []
    if os_strats:
        by_acct = {}
        for s in os_strats:
            by_acct.setdefault(s["account"], []).append(s)
        accts = sorted(by_acct)
        p("<div class='acct'><h2>OptionStrat mirror "
          "<span class='muted'>edit your saved combo of the same name to match "
          "these legs (keeps its P&amp;L)</span></h2>")
        p("<div data-tabgroup='optionstrat'><div class='tabnav'>")
        for i, acct in enumerate(accts):
            p(f"<a href='#' class='tabbtn{' active' if i == 0 else ''}' "
              f"data-tab='{html.escape(acct)}'>{html.escape(acct)} "
              f"({len(by_acct[acct])})</a>")
        p("</div>")
        for i, acct in enumerate(accts):
            cls = "tabpanel active" if i == 0 else "tabpanel"
            p(f"<div class='{cls}' data-tab='{html.escape(acct)}'>")
            for s in by_acct[acct]:
                create = " &nbsp; ".join(
                    f"<a href='{html.escape(u['url'])}' target='_blank' rel='noopener'>"
                    f"create new {html.escape(u['underlying'])} &#8599;</a>"
                    for u in s.get("create_urls", []))
                p("<div style='margin:8px 0;padding:8px 10px;border:1px solid "
                  "#30363d;border-radius:6px'>"
                  f"<div style='margin-bottom:4px'><b>{html.escape(s['name'])}</b> "
                  f"<span class='muted'>&middot; {len(s['legs'])} legs</span> "
                  f"<span class='muted' style='float:right'>{create}</span></div>")
                p("<table>")
                for lg in s["legs"]:
                    side_col = "#3fb950" if lg["side"] == "Buy" else "#ff7b72"
                    p(f"<tr><td class='l' style='color:{side_col};width:48px'>"
                      f"{lg['side']}</td><td class='mono' style='width:40px'>"
                      f"{lg['qty']}</td><td class='l mono'>{html.escape(lg['label'])}</td>"
                      f"<td class='mono'>{lg['price']:.4f}</td></tr>")
                p("</table></div>")
            p("</div>")
        p("</div></div>")

    # ONE Flex-import downloads (per account)
    flex = st.get("flex") or {}
    if flex:
        skipped = st.get("flex_skipped", 0)
        p("<div class='acct'><h2>ONE Flex import "
          f"<span class='muted'>today's fills &middot; {skipped} non-option skipped</span></h2>")
        p("<table><tr><th class='l'>account</th><th>fill rows</th>"
          "<th class='l'>download</th></tr>")
        for acct in sorted(flex):
            n = len(flex[acct])
            p(f"<tr><td class='l'>{html.escape(acct)}</td><td class='mono'>{n}</td>"
              f"<td class='l'><a href='/flex/{html.escape(acct)}.csv'>"
              f"ONEImport_{html.escape(acct)}.csv</a></td></tr>")
        p("</table><div class='muted' style='margin-top:6px'>"
          "Import into ONE (Flex Query format), then run ONE's link-trades step.</div></div>")

    p("<div class='sub muted'>Auto-refreshes every 15s. IBKR truth is live; "
      "ONE updates when you re-export the Summary Report. See the "
      "<b>Naming convention</b> tab for combo-name migration status.</div>")
    p(TAB_JS)
    p("</div></body></html>")
    return "".join(parts)


def _naming_table_html(naming_rows: list) -> str:
    if not naming_rows:
        return "<p class='muted'>No naming data yet &mdash; waiting for the first " \
               "reconciliation cycle.</p>"
    ok = sum(1 for r in naming_rows if r.get("conforms"))
    parts = [f"<div class='sub'>{ok}/{len(naming_rows)} combos conform</div>"]
    parts.append("<div class='muted' style='margin-bottom:10px'>Rename ONE/OptionStrat "
                 "combos to the convention. STRATEGY shows <b>?</b> &mdash; supply it "
                 "(proprietary, not in broker data). Suggestions are advisory; refine "
                 "multi-leg ones.</div>")
    parts.append("<div class='acct'><table class='sortable'><thead><tr>"
                 "<th class='l sortable'>account</th>"
                 "<th class='l sortable'>current ONE name</th>"
                 "<th class='l sortable'>suggested</th>"
                 "<th class='l sortable'>geometry</th></tr></thead><tbody>")
    for r in sorted(naming_rows, key=lambda r: (str(r["code"]), r["current"])):
        mark = ("<span style='color:#3fb950'>&#10003;</span>"
                if r.get("conforms") else "<span style='color:#d29922'>&rarr;</span>")
        sug = "" if r.get("conforms") else html.escape(r.get("suggested", ""))
        parts.append(f"<tr><td class='l'>{html.escape(str(r['code']))}</td>"
                     f"<td class='l mono'>{mark} {html.escape(r['current'])}</td>"
                     f"<td class='l mono muted'>{sug}</td>"
                     f"<td class='muted'>{html.escape(r.get('geometry',''))}</td></tr>")
    parts.append("</tbody></table></div>")
    return "".join(parts)


def render_guide_html() -> str:
    parts = [_page_head("Guide · TWS Matcher", refresh_secs=None)]
    parts.append(nav_html("guide"))
    parts.append("<h1>Workflow guide</h1>")
    parts.append(GUIDE_BODY_HTML)
    parts.append("</div></body></html>")
    return "".join(parts)


def render_naming_html() -> str:
    with _lock:
        naming_rows = list(_state.get("naming") or [])
    parts = [_page_head("Naming · TWS Matcher", refresh_secs=60)]
    parts.append(nav_html("naming"))
    parts.append("<h1>Naming convention</h1>")
    parts.append(_naming_table_html(naming_rows))
    parts.append(TAB_JS)
    parts.append("</div></body></html>")
    return "".join(parts)


_ONEOS_COLORS = {"QTY_MISMATCH": "#cf222e", "PRICE_DIFF": "#9a6700",
                 "ONE_ONLY": "#8250df", "OS_ONLY": "#0969da", "MATCH": "#1a7f37"}


def render_oneos_html() -> str:
    with _lock:
        st = dict(_state)
    r = st.get("oneos")
    os_file = os.path.basename(st.get("oneos_file") or "")
    os_age = _age(st.get("oneos_mtime"))

    parts = [_page_head("ONE ↔ OptionStrat · TWS Matcher", refresh_secs=60)]
    p = parts.append
    p(nav_html("oneos"))
    p("<h1>ONE &harr; OptionStrat</h1>")

    if not r:
        p("<div class='acct'><b class='warn'>No OptionStrat report found.</b>"
          "<div class='muted' style='margin-top:6px'>Download OptionStrat &rarr; "
          "<i>all active</i> to Downloads/Documents (file name starts "
          "<code>all-active</code>), then it appears here on the next cycle. "
          "This compares combos, legs and entry prices between ONE and OptionStrat.</div>"
          "</div>")
        p("</div></body></html>")
        return "".join(parts)
    if r.get("error"):
        p(f"<div class='acct' style='border-color:#cf222e'><b class='warn'>Error:</b> "
          f"{html.escape(str(r['error']))}</div></div></body></html>")
        return "".join(parts)

    matched = r["matched"]
    diffs = [m for m in matched if not m["clean"]]
    to_align = [m for m in matched if m["clean"] and not m["name_aligned"]]
    clean_aligned = sum(1 for m in matched if m["clean"] and m["name_aligned"])
    one_only, os_only = r["one_only"], r["os_only"]

    p(f"<div class='sub'>OptionStrat report: {html.escape(os_file)} &middot; {os_age} "
      f"&middot; price tol ${r['price_tol']:.2f}/sh</div>")
    tags = [("#cf222e", f"DIFFS {len(diffs)}"),
            ("#8250df", f"only in ONE {len(one_only)}"),
            ("#0969da", f"only in OptionStrat {len(os_only)}"),
            ("#9a6700", f"names to align {len(to_align)}"),
            ("#1a7f37", f"clean+aligned {clean_aligned}")]
    p("<div class='sub'>" + " &nbsp; ".join(
        f"<span class='tag' style='background:{c}'>{t}</span>" for c, t in tags)
      + "</div>")

    groups = [("diffs", f"Price / qty diffs ({len(diffs)})"),
              ("align", f"Names to align ({len(to_align)})"),
              ("one", f"Only in ONE ({len(one_only)})"),
              ("os", f"Only in OptionStrat ({len(os_only)})")]
    p("<div class='acct'><div data-tabgroup='oneos'><div class='tabnav'>")
    for i, (key, label) in enumerate(groups):
        p(f"<a href='#' class='tabbtn{' active' if i == 0 else ''}' "
          f"data-tab='{key}'>{html.escape(label)}</a>")
    p("</div>")

    # --- diffs tab
    p("<div class='tabpanel active' data-tab='diffs'>")
    if not diffs:
        p("<div class='muted'>All matched combos reconcile on legs and price.</div>")
    for m in diffs:
        p(f"<div style='margin:8px 0'><b>{html.escape(m['one_name'])}</b>"
          + ("" if m["name_aligned"] else
             f" <span class='muted'>&middot; OS: {html.escape(m['os_name'])}</span>")
          + "</div><table><tr><th class='l'>leg</th><th>ONE qty</th><th>ONE px</th>"
          "<th>OS qty</th><th>OS px</th><th class='l'>flag</th></tr>")
        for lg in m["legs"]:
            if lg["status"] == "MATCH":
                continue
            oq = "" if lg["one_qty"] is None else f"{lg['one_qty']:+g}"
            sq = "" if lg["os_qty"] is None else f"{lg['os_qty']:+g}"
            op = "" if lg["one_px"] is None else f"{lg['one_px']:.2f}"
            sp = "" if lg["os_px"] is None else f"{lg['os_px']:.2f}"
            col = _ONEOS_COLORS.get(lg["status"], "#8b949e")
            p(f"<tr><td class='l mono'>{html.escape(lg['label'])}</td>"
              f"<td class='mono'>{oq}</td><td class='mono'>{op}</td>"
              f"<td class='mono'>{sq}</td><td class='mono'>{sp}</td>"
              f"<td class='l'><span class='tag' style='background:{col}'>"
              f"{lg['status']}</span></td></tr>")
        p("</table>")
    p("</div>")

    # --- names-to-align tab
    p("<div class='tabpanel' data-tab='align'>")
    if not to_align:
        p("<div class='muted'>All matched combo names are aligned.</div>")
    else:
        p("<div class='muted' style='margin-bottom:6px'>Legs &amp; prices match; only "
          "the names differ. Rename in OptionStrat (or ONE) to converge.</div>"
          "<table class='sortable'><thead><tr><th class='l sortable'>ONE name</th>"
          "<th class='l sortable'>OptionStrat name</th></tr></thead><tbody>")
        for m in sorted(to_align, key=lambda m: m["one_name"]):
            p(f"<tr><td class='l mono'>{html.escape(m['one_name'])}</td>"
              f"<td class='l mono muted'>{html.escape(m['os_name'])}</td></tr>")
        p("</tbody></table>")
    p("</div>")

    # --- only in ONE / only in OptionStrat tabs
    for key, rows, note in (("one", one_only, "in ONE but not OptionStrat"),
                            ("os", os_only, "in OptionStrat but not ONE")):
        p(f"<div class='tabpanel' data-tab='{key}'>")
        if not rows:
            p("<div class='muted'>None.</div>")
        else:
            p(f"<div class='muted' style='margin-bottom:6px'>Combos {note}.</div>"
              "<table><tr><th class='l'>account</th><th class='l'>name</th>"
              "<th>legs</th></tr>")
            for c in rows:
                p(f"<tr><td class='l'>{html.escape(str(c.get('code') or ''))}</td>"
                  f"<td class='l mono'>{html.escape(c['name'])}</td>"
                  f"<td class='mono'>{c['legs']}</td></tr>")
            p("</table>")
        p("</div>")

    p("</div></div>")   # close tabgroup + acct
    p(TAB_JS)
    p("</div></body></html>")
    return "".join(parts)


def flex_csv_bytes(account: str) -> bytes | None:
    """Build the IBKR-Flex CSV for one account from current state, in memory."""
    with _lock:
        rows = (_state.get("flex") or {}).get(account)
    if rows is None:
        return None
    buf = io.StringIO()
    w = csv.writer(buf, quoting=csv.QUOTE_ALL)
    w.writerow(flex_export.HEADER)
    w.writerows(rows)
    return buf.getvalue().encode("utf-8")


# --------------------------------------------------------------- http server
class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):           # quiet
        pass

    def do_GET(self):
        if self.path.startswith("/check"):
            _check_now.set()
            self.send_response(303)
            self.send_header("Location", "/")
            self.end_headers()
            return
        if self.path.startswith("/flex/"):
            acct = os.path.basename(self.path)[:-4] if self.path.endswith(".csv") \
                else os.path.basename(self.path)
            data = flex_csv_bytes(acct)
            if data is None:
                self.send_response(404)
                self.end_headers()
                return
            self.send_response(200)
            self.send_header("Content-Type", "text/csv")
            self.send_header("Content-Disposition",
                             f'attachment; filename="ONEImport_{acct}.csv"')
            self.end_headers()
            self.wfile.write(data)
            return
        if self.path.startswith("/api/state"):
            with _lock:
                body = json.dumps(_state.get("result") or {}, default=str).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(body)
            return
        if self.path.startswith("/guide"):
            body = render_guide_html().encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(body)
            return
        if self.path.startswith("/naming"):
            body = render_naming_html().encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(body)
            return
        if self.path.startswith("/oneos"):
            body = render_oneos_html().encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(body)
            return
        body = render_html().encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(body)


def _port_in_use(host, port):
    import socket
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.4)
        return s.connect_ex((host, port)) == 0


def main():
    if _port_in_use(HOST, WEB_PORT):
        print(f"TWS Matcher is already running at http://{HOST}:{WEB_PORT}/\n"
              f"(opened in your browser). Close that window first to restart.")
        return
    t = threading.Thread(target=worker, daemon=True)
    t.start()
    httpd = ThreadingHTTPServer((HOST, WEB_PORT), Handler)
    print(f"TWS Matcher dashboard -> http://{HOST}:{WEB_PORT}/   (Ctrl+C to stop)")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nstopping.")


if __name__ == "__main__":
    main()
