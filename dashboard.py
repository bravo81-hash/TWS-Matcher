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

HERE = os.path.dirname(os.path.abspath(__file__))

from ib_async import IB

import canonical_engine as eng
import email_report
import flex_export
import market_metrics
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
                "one_mtime": None, "ibkr_snap": None, "one_snap": None,
                "metrics": None, "metrics_at": None, "metrics_error": None}
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


# Greeks and per-position P&L cost ~40s and consume market-data lines, so they
# run on their own slower clock rather than every reconciliation cycle.
METRICS_INTERVAL = 300
NAV_HISTORY = os.path.join(HERE, "nav_history.json")
_metrics_last = [0.0]


def _log_nav(metrics: dict) -> None:
    """Append today's NAV to the equity curve, one row per calendar day.

    IBKR has no historical-NAV API, so the curve can only be built forward from
    the first day this runs. Re-running the same day overwrites that day's row
    rather than adding a second one.
    """
    try:
        try:
            with open(NAV_HISTORY) as fh:
                hist = json.load(fh)
        except (OSError, ValueError):
            hist = []
        day = datetime.now(ET).strftime("%Y-%m-%d")
        row = {
            "date": day,
            "nav_total": metrics.get("nav_total"),
            "accounts": {a: d.get("NetLiquidation")
                         for a, d in (metrics.get("accounts") or {}).items()},
            "daily_pnl": sum(t.get("daily_pnl") or 0.0
                             for t in metrics.get("by_ticker") or []),
        }
        hist = [r for r in hist if r.get("date") != day] + [row]
        hist.sort(key=lambda r: r["date"])
        with open(NAV_HISTORY, "w") as fh:
            json.dump(hist, fh, indent=2)
    except Exception:
        pass          # the equity log must never take the daemon down


def maybe_collect_metrics(ib: IB, cfg: dict, force: bool = False) -> None:
    if not force and time.time() - _metrics_last[0] < METRICS_INTERVAL:
        return
    _metrics_last[0] = time.time()
    try:
        _set(status="collecting greeks")
        accounts = set(cfg.get("account_map", {}).values()) or None
        m = market_metrics.collect_all(ib, accounts)
        _log_nav(m)
        _set(metrics=m, metrics_at=datetime.now(timezone.utc).isoformat(),
             metrics_error=None)
    except Exception as exc:
        _set(metrics_error=f"{type(exc).__name__}: {exc}")


def run_cycle(ib: IB, cfg: dict):
    _set(status="refreshing")
    ibkr_snap = eng.build_snapshot(ib)
    ibkr_snap, flex_completeness = flex_export.update_execution_journal(
        ibkr_snap)

    one_path = one_reader.find_default_csv(cfg.get("one_export_dirs"))
    one_mtime = os.path.getmtime(one_path) if os.path.exists(one_path) else None
    one_snap = one_reader.build_one_snapshot(one_path)

    result = reconcile.reconcile_snapshots(ibkr_snap, one_snap, cfg)

    # derived outputs (OptionStrat mirror URLs + ONE Flex-import rows)
    os_strategies = optionstrat_url.generate(
        one_path, ibkr_legs=ibkr_snap["legs"])["strategies"]
    flex_by_acct, flex_skipped = flex_export.generate(
        ibkr_snap, cfg.get("flex_timezone"))

    # A normal import exists only when every observed option-position change
    # is explained by persisted executions. Incomplete files are quarantined.
    flex_paths = flex_export.save_account_files(
        flex_by_acct, flex_completeness)

    # classify broker activity since the last ONE export (rolled/opened/closed/...)
    one_raw_legs = one_reader.read_summary_report(one_path) if (one_path and os.path.exists(one_path)) else []
    fills_since = [f for f in ibkr_snap.get("fills_today", [])
                   if one_mtime and (_fill_epoch(f.get("time")) or 0) > one_mtime]
    activity = reconcile.classify_activity(fills_since, ibkr_snap["legs"],
                                           one_legs=one_raw_legs,
                                           account_map=cfg.get("account_map"))

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
         ibkr_snap=ibkr_snap, one_snap=one_snap,
         os_strategies=os_strategies,
         flex={a: rows for a, rows in flex_by_acct.items()},
         flex_skipped=flex_skipped,
         flex_completeness=flex_completeness,
         flex_paths=flex_paths,
         activity=activity,
         account_codes=cfg.get("account_codes") or {},
         naming=naming_rows,
         oneos=oneos, oneos_file=os_path, oneos_mtime=os_mtime)
    # persist latest for other tools
    with open(reconcile.OUTPUT_JSON, "w") as fh:
        json.dump(result, fh, indent=2, default=str)

    maybe_collect_metrics(ib, cfg)


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
    "MATCH": "#1a7f37", "MATCH_FIFO_AVG": "#0969da", "PRICE_DRIFT": "#9a6700",
    "COST_BASIS_DRIFT": "#bc4c00", "QTY_MISMATCH": "#cf222e",
    "IBKR_ONLY": "#0969da", "ONE_ONLY": "#8250df",
}
ORDER = ["QTY_MISMATCH", "ONE_ONLY", "IBKR_ONLY", "PRICE_DRIFT",
         "COST_BASIS_DRIFT", "MATCH_FIFO_AVG", "MATCH"]

PAGE_STYLE = (
    "body{font:12.5px/1.35 -apple-system,BlinkMacSystemFont,Segoe UI,Roboto,sans-serif;"
    "margin:0;background:#0d1117;color:#e6edf3}"
    ".wrap{width:98%;max-width:1800px;margin:0 auto;padding:8px 12px;box-sizing:border-box}"
    "h1{font-size:16px;margin:0 0 2px} .sub{color:#8b949e;margin-bottom:8px;font-size:12px}"
    ".acct{background:#161b22;border:1px solid #30363d;border-radius:6px;"
    "padding:8px 10px;margin:6px 0;box-sizing:border-box}"
    ".acct h2{font-size:13px;margin:0 0 6px;display:flex;justify-content:space-between;align-items:center}"
    ".pill{border-radius:10px;padding:1px 7px;font-size:11px;font-weight:600}"
    ".MATCHpill{background:#1a7f3733;color:#3fb950}"
    ".DIFFpill{background:#cf222e33;color:#ff7b72}"
    ".table-wrap{overflow-x:auto;-webkit-overflow-scrolling:touch}"
    "table{border-collapse:collapse;width:100%}"
    "td,th{padding:3px 6px;text-align:right;border-bottom:1px solid #21262d;white-space:nowrap}"
    "td.l,th.l{text-align:left} .mono{font-variant-numeric:tabular-nums}"
    ".adj-filter{background:#0d1117;color:#e6edf3;border:1px solid #30363d;"
    "padding:3px 8px;border-radius:4px;font-size:12px;outline:none}"
    ".tag{font-weight:700;font-size:10.5px;padding:1px 5px;border-radius:4px;color:#fff;white-space:nowrap}"
    ".muted{color:#8b949e} a.btn{display:inline-block;background:#238636;color:#fff;"
    "padding:4px 10px;border-radius:5px;text-decoration:none;font-weight:600;font-size:12px}"
    ".warn{color:#d29922}"
    # top-level page nav
    ".pagenav{margin-bottom:8px;display:flex;gap:4px}"
    ".pagenav a{color:#8b949e;text-decoration:none;padding:4px 10px;border-radius:5px;"
    "font-weight:600;font-size:12px}"
    ".pagenav a.active{color:#e6edf3;background:#161b22;border:1px solid #30363d}"
    ".pagenav a:hover:not(.active){color:#e6edf3}"
    # in-page tab groups
    ".tabnav{display:flex;gap:4px;flex-wrap:wrap;border-bottom:1px solid #30363d;"
    "margin-bottom:8px}"
    ".tabbtn{padding:4px 12px;border-radius:5px 5px 0 0;cursor:pointer;color:#8b949e;"
    "text-decoration:none;font-size:12px;font-weight:600;border:1px solid transparent;"
    "margin-bottom:-1px;background:none}"
    ".tabbtn.active{color:#e6edf3;background:#161b22;border-color:#30363d;"
    "border-bottom-color:#161b22}"
    ".tabpanel{display:none}.tabpanel.active{display:block}"
    # sortable table headers
    "th.sortable{cursor:pointer;user-select:none}"
    "th.sortable:hover{color:#e6edf3}"
    "th.sortable::after{content:'\\2195';opacity:0.35;margin-left:4px}"
    "th.sortable.sorted::after{opacity:1}"
    ".kpis{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:8px;margin:8px 0 12px}"
    ".kpi{background:#161b22;border:1px solid #30363d;border-radius:6px;padding:7px 10px}"
    ".kpi-l{color:#8b949e;font-size:10.5px;text-transform:uppercase;letter-spacing:.04em}"
    ".kpi-v{font-size:19px;margin:2px 0 1px;font-variant-numeric:tabular-nums}"
    ".kpi-n{color:#8b949e;font-size:10.5px}"
    ".card{background:#161b22;border:1px solid #30363d;border-radius:6px;padding:9px 11px;margin:0 0 10px;box-sizing:border-box;min-width:0}"
    ".card h3{font-size:12.5px;margin:0 0 3px;font-weight:600}"
    ".chartgrid{display:grid;grid-template-columns:repeat(auto-fit,minmax(400px,1fr));gap:10px}"
    ".chart{display:block;max-width:100%;height:auto;overflow:visible}"
    ".chart rect:hover{opacity:1}"
    ".meter{display:inline-block;width:100%;min-width:60px;height:7px;background:#21262d;border-radius:4px;overflow:hidden;vertical-align:middle}"
    ".meter-fill{display:block;height:100%;border-radius:4px}"
    ".dot{display:inline-block;width:7px;height:7px;border-radius:50%;margin-right:5px;vertical-align:middle}"
    "tfoot td{border-top:1px solid #30363d;border-bottom:none}"
    "@media(max-width:768px){.chartgrid{grid-template-columns:1fr}}"
    ".grid-accts{display:grid;grid-template-columns:repeat(auto-fit,minmax(380px,1fr));gap:10px;margin:6px 0}"
    ".grid-accts > .acct{margin:0;min-width:0;overflow-x:auto}"
    "@media(max-width:768px){.grid-accts{grid-template-columns:1fr}.wrap{width:100%;padding:4px 6px}}"
)

TAB_JS = r"""
<script>
(function(){
  function initTabs(){
    document.querySelectorAll('[data-tabgroup]').forEach(function(group){
      var gid = group.getAttribute('data-tabgroup');
      var nav = group.querySelector(':scope > .tabnav');
      var buttons = nav ? Array.prototype.slice.call(nav.querySelectorAll(':scope > .tabbtn')) : Array.prototype.slice.call(group.querySelectorAll(':scope > .tabbtn'));
      var panels = Array.prototype.slice.call(group.querySelectorAll(':scope > .tabpanel'));
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
  document.addEventListener('DOMContentLoaded', function(){ initTabs(); initSort(); if (window.initAdjustments) window.initAdjustments(); });
})();

window.initAdjustments = function() {
  var sortBySelect = document.getElementById('adj-sort-by');
  var qInput = document.getElementById('adj-filter-search');
  if (!sortBySelect || !window.ADJ_DATA) return;

  var savedSort = sessionStorage.getItem('adj_sort_by');
  if (sessionStorage.getItem('adj_sort_version') !== '2') {
    savedSort = 'time';
    sessionStorage.setItem('adj_sort_by', savedSort);
    sessionStorage.setItem('adj_sort_version', '2');
  }
  if (savedSort && sortBySelect.querySelector('option[value="' + savedSort + '"]')) {
    sortBySelect.value = savedSort;
  }

  var savedQuery = sessionStorage.getItem('adj_filter_query');
  if (savedQuery !== null && qInput) {
    qInput.value = savedQuery;
  }

  var trades = window.TRADE_ADJ_DATA || [];
  var flat = window.ADJ_DATA || [];
  var unique = function(values) {
    return Array.from(new Set(values.filter(function(v) {
      return v !== null && v !== undefined && String(v).trim() !== '';
    }).map(String))).sort(function(a, b) {
      return a.localeCompare(b, undefined, {numeric:true});
    });
  };
  var setOptions = function(id, values, allLabel) {
    var select = document.getElementById(id);
    if (!select) return;
    var saved = sessionStorage.getItem(id) || '';
    select.innerHTML = "<option value=''>" + allLabel + "</option>" +
      unique(values).map(function(v) {
        return "<option value='" + escapeHtml(v) + "'>" + escapeHtml(v) + "</option>";
      }).join('');
    if (saved && Array.from(select.options).some(function(o) { return o.value === saved; })) {
      select.value = saved;
    }
  };
  setOptions('adj-filter-ticker',
    trades.flatMap(function(t) { return t.tickers || []; })
      .concat(flat.map(function(i) { return i.ticker; })), 'All tickers');
  setOptions('adj-filter-strike',
    trades.flatMap(function(t) { return t.strikes || []; })
      .concat(flat.flatMap(function(i) { return i.strikes || []; })), 'All strikes');
  setOptions('adj-filter-trade',
    trades.map(function(t) { return t.trade_id || '[New Trade]'; })
      .concat(flat.map(function(i) { return i.trade_id || '[New Trade]'; })),
    'All ONE trade IDs');
  setOptions('adj-filter-account-type',
    trades.map(function(t) { return t.account_type || 'Unclassified'; })
      .concat(flat.map(function(i) { return i.account_type || 'Unclassified'; })),
    'All account types');
  setOptions('adj-filter-account',
    trades.map(function(t) { return t.account; })
      .concat(flat.map(function(i) { return i.account; })), 'All accounts');
  setOptions('adj-filter-status',
    trades.map(function(t) { return t.status_label || t.status; })
      .concat(flat.map(function(i) { return i.category; })), 'All statuses');

  window.renderAdjustments();

  var wasFocused = sessionStorage.getItem('adj_filter_focused') === 'true';
  if (wasFocused && qInput) {
    qInput.focus();
    var pos = qInput.value.length;
    try { qInput.setSelectionRange(pos, pos); } catch(e) {}
  }
};

window.clearAdjFilters = function() {
  ['adj-filter-search', 'adj-filter-ticker', 'adj-filter-strike',
   'adj-filter-trade', 'adj-filter-account-type', 'adj-filter-account',
   'adj-filter-status'].forEach(function(id) {
    var el = document.getElementById(id);
    if (el) el.value = '';
    sessionStorage.removeItem(id);
  });
  sessionStorage.removeItem('adj_filter_query');
  window.renderAdjustments();
};

window.setAdjSort = function(mode) {
  var sel = document.getElementById('adj-sort-by');
  if (sel) {
    sel.value = mode;
    sessionStorage.setItem('adj_sort_by', mode);
    window.renderAdjustments();
  }
};

window.renderAdjustments = function() {
  var container = document.getElementById('adj-content');
  var sel = document.getElementById('adj-sort-by');
  var qInput = document.getElementById('adj-filter-search');
  if (!container || (!window.ADJ_DATA && !window.TRADE_ADJ_DATA)) return;

  var mode = sel ? sel.value : (sessionStorage.getItem('adj_sort_by') || 'time');
  if (sel && sel.value !== mode) sel.value = mode;
  sessionStorage.setItem('adj_sort_by', mode);

  var rawQ = qInput ? qInput.value : '';
  sessionStorage.setItem('adj_filter_query', rawQ);
  var isFocused = (document.activeElement === qInput);
  sessionStorage.setItem('adj_filter_focused', isFocused ? 'true' : 'false');

  var qTerms = rawQ.toLowerCase().trim().split(/\s+/).filter(Boolean);
  var filterValue = function(id) {
    var el = document.getElementById(id);
    var value = el ? el.value : '';
    sessionStorage.setItem(id, value);
    return value;
  };
  var filters = {
    ticker: filterValue('adj-filter-ticker'),
    strike: filterValue('adj-filter-strike'),
    trade: filterValue('adj-filter-trade'),
    accountType: filterValue('adj-filter-account-type'),
    account: filterValue('adj-filter-account'),
    status: filterValue('adj-filter-status')
  };
  var matches = function(item, isTrade) {
    var tickers = isTrade ? (item.tickers || []) : [item.ticker];
    var strikes = isTrade ? (item.strikes || []) : (item.strikes || []);
    var tradeId = item.trade_id || '[New Trade]';
    var accountType = item.account_type || 'Unclassified';
    var status = isTrade ? (item.status_label || item.status) : item.category;
    if (filters.ticker && tickers.map(String).indexOf(filters.ticker) === -1) return false;
    if (filters.strike && strikes.map(String).indexOf(filters.strike) === -1) return false;
    if (filters.trade && String(tradeId) !== filters.trade) return false;
    if (filters.accountType && String(accountType) !== filters.accountType) return false;
    if (filters.account && String(item.account) !== filters.account) return false;
    if (filters.status && String(status) !== filters.status) return false;
    var haystack = JSON.stringify(item).toLowerCase();
    return qTerms.every(function(term) { return haystack.indexOf(term) !== -1; });
  };

  // Trade cards, ordered by broker execution time by default.
  if ((mode === 'time' || mode === 'trade_id') && window.TRADE_ADJ_DATA && window.TRADE_ADJ_DATA.length > 0) {
    var trades = window.TRADE_ADJ_DATA.filter(function(t) {
      return matches(t, true);
    });
    trades.sort(function(a, b) {
      if (mode === 'trade_id') {
        var aId = a.trade_id === null || a.trade_id === undefined ? Number.MAX_SAFE_INTEGER : Number(a.trade_id);
        var bId = b.trade_id === null || b.trade_id === undefined ? Number.MAX_SAFE_INTEGER : Number(b.trade_id);
        if (aId !== bId) return aId - bId;
      }
      return Number(b.timestamp_epoch || 0) - Number(a.timestamp_epoch || 0);
    });

    if (trades.length === 0) {
      container.innerHTML = "<div class='muted' style='padding:6px 0;'>No adjustments match all selected filters.</div>";
      return;
    }

    var html = [];
    trades.forEach(function(t) {
      var badgeColor = '#58a6ff';
      if (t.status === 'ROLLED' || t.status === 'ADJUSTED') badgeColor = '#d29922';
      else if (t.status === 'NEW_TRADE') badgeColor = '#3fb950';
      else if (t.status === 'TRADE_CLOSED' || t.status === 'LEG_CLOSED') badgeColor = '#ff7b72';

      var tTitle = t.trade_id ? ('ONE Trade #' + t.trade_id + (t.trade_name ? ' (' + t.trade_name + ')' : '')) : ('[New Trade] ' + (t.trade_name || ''));
      var timeStr = "";
      if (t.timestamp) {
        var parsedTime = new Date(t.timestamp);
        var displayTime = isNaN(parsedTime.getTime()) ? t.timestamp : parsedTime.toLocaleString();
        timeStr = " <span class='muted' style='font-weight:normal;font-size:11px;' title='" +
          escapeHtml(t.timestamp) + "'>Executed " + escapeHtml(displayTime) + "</span>";
      }
      var accountType = t.account_type ? " <span class='tag' style='background:#57606a;margin-left:5px;'>" +
        escapeHtml(t.account_type) + "</span>" : "";

      html.push("<div style='border:1px solid #30363d;border-radius:6px;padding:8px 12px;margin-bottom:10px;background:#161b22;'>");
      html.push("<div style='display:flex;justify-content:space-between;align-items:center;margin-bottom:6px;'>");
      html.push("<div><span style='font-weight:700;color:#e6edf3;font-size:13px;'>[" + escapeHtml(t.account) + "] " + escapeHtml(tTitle) + "</span>" + accountType + timeStr + "</div>");
      html.push("<span class='tag' style='background:" + badgeColor + ";font-weight:600;'>" + escapeHtml(t.status_label || t.status) + "</span>");
      html.push("</div>");

      html.push("<div class='table-wrap'><table style='table-layout:fixed;width:100%;'>");
      html.push("<colgroup><col style='width:120px;'><col style='width:auto;'><col style='width:105px;'></colgroup>");

      (t.rolled || []).forEach(function(r) {
        html.push("<tr>");
        html.push("<td style='color:#d29922;font-weight:600;'>🔄 Rolled</td>");
        html.push("<td class='l mono' style='color:#d29922;'>" + escapeHtml(r.from) + " &rarr; " + escapeHtml(r.to) + "</td>");
        html.push("<td class='mono' style='text-align:right;color:#d29922;'>x" + escapeHtml(r.qty) + "</td>");
        html.push("</tr>");
      });

      (t.opened || []).forEach(function(o) {
        var pxStr = (o.px !== null && o.px !== undefined) ? (" @ " + Number(o.px).toFixed(4)) : "";
        html.push("<tr>");
        html.push("<td style='color:#3fb950;font-weight:600;'>➕ Leg Opened</td>");
        html.push("<td class='l mono' style='color:#3fb950;'>" + escapeHtml(o.label) + "</td>");
        html.push("<td class='mono' style='text-align:right;color:#3fb950;'>" + (o.qty > 0 ? '+' : '') + escapeHtml(o.qty) + pxStr + "</td>");
        html.push("</tr>");
      });

      (t.closed || []).forEach(function(c) {
        html.push("<tr>");
        html.push("<td style='color:#ff7b72;font-weight:600;'>❌ Leg Closed</td>");
        html.push("<td class='l mono' style='color:#ff7b72;'>" + escapeHtml(c.label) + "</td>");
        html.push("<td class='mono' style='text-align:right;color:#ff7b72;'>was " + (c.qty > 0 ? '+' : '') + escapeHtml(c.qty) + "</td>");
        html.push("</tr>");
      });

      (t.changed || []).forEach(function(ch) {
        html.push("<tr>");
        html.push("<td style='color:#d29922;font-weight:600;'>⚡ Adjusted</td>");
        html.push("<td class='l mono' style='color:#d29922;'>" + escapeHtml(ch.label) + " (" + escapeHtml(ch.type ? ch.type.toLowerCase() : '') + ")</td>");
        html.push("<td class='mono' style='text-align:right;color:#d29922;'>" + (ch.qty > 0 ? '+' : '') + escapeHtml(ch.qty) + "</td>");
        html.push("</tr>");
      });

      html.push("</table></div>");

      if (t.wizard_hint) {
        html.push("<div style='margin-top:6px;padding:4px 8px;background:#0d1117;border-radius:4px;border:1px solid #21262d;font-size:12px;'>");
        html.push("<span style='color:#58a6ff;font-weight:600;'>👉 ONE Wizard:</span> <span style='color:#e6edf3;'>" + escapeHtml(t.wizard_hint) + "</span>");
        html.push("</div>");
      }

      html.push("</div>");
    });

    container.innerHTML = html.join('');
    return;
  }

  // Grouped modes use flat adjustment rows and keep each group newest-first.
  var items = (window.ADJ_DATA || []).filter(function(item) {
    return matches(item, false);
  });
  items.sort(function(a, b) {
    return Number(b.timestamp_epoch || 0) - Number(a.timestamp_epoch || 0);
  });

  if (items.length === 0) {
    container.innerHTML = "<div class='muted' style='padding:6px 0;'>No adjustments match all selected filters.</div>";
    return;
  }

  var groups = {};
  var groupOrder = [];

  items.forEach(function(item) {
    var gKey = '';
    var gTitle = '';

    if (mode === 'ticker') {
      gKey = item.ticker || 'Other';
      gTitle = 'Ticker: ' + gKey;
    } else if (mode === 'account') {
      gKey = item.account || 'Unknown';
      gTitle = 'Account: ' + gKey;
    } else {
      gKey = item.category;
      gTitle = item.category;
    }

    if (!groups[gKey]) {
      groups[gKey] = { title: gTitle, key: gKey, items: [], order: item.category_order };
      groupOrder.push(gKey);
    }
    groups[gKey].items.push(item);
  });

  groupOrder.sort(function(a, b) {
    if (mode === 'category') {
      return groups[a].order - groups[b].order;
    } else {
      return a.localeCompare(b);
    }
  });

  var html = [];
  groupOrder.forEach(function(gKey) {
    var grp = groups[gKey];
    var headerColor = '#f2cc60';
    if (gKey === 'Rolled' || gKey === 'Adjusted') headerColor = '#d29922';
    else if (gKey === 'New / opened') headerColor = '#3fb950';
    else if (gKey === 'Closed') headerColor = '#ff7b72';
    else if (mode !== 'category') headerColor = '#58a6ff';

    html.push("<div style='color:" + headerColor + ";font-weight:600;margin-top:6px;margin-bottom:2px;font-size:12px;display:flex;justify-content:space-between;'>");
    html.push("<span>" + escapeHtml(grp.title) + "</span>");
    html.push("<span class='muted' style='font-weight:normal;font-size:11px;'>" + grp.items.length + " item(s)</span>");
    html.push("</div>");

    html.push("<div class='table-wrap'><table style='table-layout:fixed;width:100%;'>");
    html.push("<colgroup><col style='width:145px;'><col style='width:90px;'><col style='width:auto;'><col style='width:105px;'></colgroup>");
    grp.items.forEach(function(r) {
      var hintHtml = r.wizard_hint ? " &nbsp;<span style='color:#58a6ff;font-size:11.5px'>👉 " + escapeHtml(r.wizard_hint) + "</span>" : "";
      var parsedTime = r.timestamp ? new Date(r.timestamp) : null;
      var rowTime = parsedTime && !isNaN(parsedTime.getTime()) ? parsedTime.toLocaleString() : (r.timestamp || '');
      html.push("<tr>");
      html.push("<td class='l mono muted' title='" + escapeHtml(r.timestamp || '') + "'>" + escapeHtml(rowTime) + "</td>");
      html.push("<td class='l' style='width:90px;font-weight:600;color:#e6edf3;'>" + escapeHtml(r.account) + "</td>");
      html.push("<td class='l mono' style='color:" + r.color + ";'>" + r.details_html + hintHtml + "</td>");
      html.push("<td class='mono' style='width:105px;text-align:right;'>" + escapeHtml(r.qty_str) + "</td>");
      html.push("</tr>");
    });
    html.push("</table></div>");
  });

  container.innerHTML = html.join('');
};

window.copyAdjustmentsText = function() {
  if (window.TRADE_ADJ_DATA && window.TRADE_ADJ_DATA.length > 0) {
    var lines = ["=== TWS Matcher - Today's Trade Adjustments Checklist ==="];
    window.TRADE_ADJ_DATA.forEach(function(t) {
      var header = "[" + t.account + "] " + (t.trade_id ? ("Trade #" + t.trade_id + " (" + t.trade_name + ")") : "[New Trade]") + " - " + t.status_label;
      lines.push("\n" + header);
      (t.rolled || []).forEach(function(r) {
        lines.push("  * ROLLED: " + r.from + " -> " + r.to + " (x" + r.qty + ")");
      });
      (t.opened || []).forEach(function(o) {
        lines.push("  * OPENED: " + o.label + " (" + (o.qty > 0 ? '+' : '') + o.qty + ")");
      });
      (t.closed || []).forEach(function(c) {
        lines.push("  * CLOSED: " + c.label + " (was " + c.qty + ")");
      });
      if (t.wizard_hint) {
        lines.push("  -> HINT: " + t.wizard_hint);
      }
    });
    var text = lines.join("\n");
    navigator.clipboard.writeText(text).then(function() {
      alert("Copied trade adjustments checklist to clipboard!");
    }).catch(function(err) {
      console.error("Copy failed", err);
    });
  } else if (window.ADJ_DATA) {
    var lines = ["=== TWS Matcher - Today's Trade Adjustments Checklist ==="];
    window.ADJ_DATA.forEach(function(r) {
      lines.push("[" + r.account + "] " + r.category.toUpperCase() + ": " + r.label + " | " + r.qty_str + " | " + r.wizard_hint);
    });
    var text = lines.join("\n");
    navigator.clipboard.writeText(text).then(function() {
      alert("Copied " + window.ADJ_DATA.length + " adjustment item(s) to clipboard!");
    }).catch(function(err) {
      console.error("Copy failed", err);
    });
  }
};

function escapeHtml(str) {
  if (!str) return '';
  return String(str).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
}
</script>
"""


def nav_html(active: str) -> str:
    items = [("dashboard", "/", "Dashboard"),
             ("risk", "/risk", "Risk & Greeks"),
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
        return reconcile.is_actionable_finding(f)

    ignored_groups = set((result or {}).get("ignore_one_accounts", []))
    unmapped = (result or {}).get("unmapped_one_groups", {})
    active_unmapped = {
        name: count for name, count in unmapped.items()
        if one_reader.normalize_account_name(name) not in ignored_groups
    }
    one_stale = bool(
        st.get("one_mtime")
        and time.time() - st["one_mtime"] > 3600
    )
    ghosts = (result or {}).get("ghost_trades", []) or []
    unsettled = (result or {}).get("unsettled_trades", []) or []
    prob_n = (
        sum(1 for finds in result["accounts"].values() for f in finds
            if _alerting(f))
        + sum(active_unmapped.values())
        + len(ghosts)
        + len(unsettled)
        if result else 0
    )
    if not result:
        title = "TWS Matcher"
    elif act_n:
        title = f"⚠{act_n} · TWS Matcher"        # broker activity since export
    elif prob_n or one_stale:
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
      f"&nbsp;|&nbsp; <a class='btn' href='/check'>Check now</a> "
      f"&nbsp;|&nbsp; <a class='btn' style='background:#1f6feb' href='/send_email'>📧 Email Report</a></div>")

    if result and prob_n == 0 and not one_stale:
        p("<div class='acct' style='border:2px solid #238636;background:#0d2818;margin-bottom:14px'>"
          "<h2 style='color:#3fb950;margin:0 0 4px'>🟢 ALL POSITIONS MATCHED &mdash; IBKR TRUTH IN SYNC</h2>"
          "<div class='muted'>Zero unhedged legs and zero quantity mismatches across all accounts. Position sizing is 100% aligned.</div>"
          "</div>")
    elif result and prob_n == 0 and one_stale:
        p("<div class='acct' style='border:2px solid #d29922;background:#2d250d;margin-bottom:14px'>"
          "<h2 style='color:#f2cc60;margin:0 0 4px'>POSITION STRUCTURE MATCHES, "
          "BUT THE ONE EXPORT IS STALE</h2>"
          "<div class='muted'>Export a fresh open-position report from ONE and "
          "click Check now before relying on this assurance.</div></div>")

    if unsettled:
        total = sum(t["pnl_if_worthless"] for t in unsettled)
        p("<div class='acct' style='border:2px solid #d29922;background:#2d250d;"
          "margin-bottom:14px'>"
          f"<h2 style='color:#f2cc60;margin:0 0 4px'>⏰ {len(unsettled)} "
          f"UNSETTLED EXPIR{'IES' if len(unsettled) > 1 else 'Y'} IN ONE</h2>"
          "<div class='muted'>These legs expired in the market but are still "
          "open in ONE, so ONE&rsquo;s realised P&amp;L for those trades is "
          "wrong. The broker dropped them at expiry and the position check goes "
          "quiet with it &mdash; accounts below can read MATCH while this money "
          "is unbooked. Settle each trade in ONE.</div>")
        for t in unsettled:
            acct = t.get("ibkr_account") or t["account"]
            legs = " &nbsp;·&nbsp; ".join(
                f'{l["qty"]:+.0f} {html.escape(str(l["tradingClass"]))} '
                f'{one_reader._pretty_expiry(l["expiry"])} '
                f'{l["strike"]:g}{l["right"]} @ {l["open_price"]:.2f}'
                for l in t["legs"])
            p(f"<div style='margin-top:10px'><b>{html.escape(str(acct))}</b> "
              f"&mdash; ONE trade <b>#{html.escape(str(t['trade_id']))}</b> "
              f"{html.escape(str(t['trade_name']))} "
              f"<span class='muted'>({html.escape(str(t['underlying']))}, "
              f"expired {one_reader._pretty_expiry(t['expiry'])})</span>"
              f"<div style='font-family:monospace;font-size:12px'>{legs}</div>"
              f"<div class='muted' style='margin-top:2px'>Settles "
              f"<b>{t['pnl_if_worthless']:+,.2f}</b> if expired worthless. "
              f"Check moneyness first &mdash; an in-the-money leg was exercised "
              f"or assigned and settles at intrinsic instead.</div></div>")
        p(f"<div style='margin-top:10px'><b>Total if all expired worthless: "
          f"{total:+,.2f}</b></div></div>")

    if ghosts:
        p("<div class='acct' style='border:2px solid #d29922;background:#2d250d;"
          "margin-bottom:14px'>"
          f"<h2 style='color:#f2cc60;margin:0 0 4px'>👻 {len(ghosts)} GHOST "
          f"TRADE{'S' if len(ghosts) > 1 else ''} IN ONE</h2>"
          "<div class='muted'>These report as open in ONE but ONE holds no "
          "position for them &mdash; they are not selectable in the Analysis "
          "window, so their legs can never be adjusted, and ONE models no risk "
          "for them. Their legs still reconcile against IBKR, so the account "
          "checks below can read clean while the trade is unmanageable.</div>")
        for g in ghosts:
            acct = g.get("ibkr_account") or g["account"]
            legs = " &nbsp;·&nbsp; ".join(
                f'{l["qty"]:+.0f} {html.escape(str(l["tradingClass"]))} '
                f'{one_reader._pretty_expiry(l["expiry"])} '
                f'{l["strike"]:g}{l["right"]}'
                f' @ {l["open_price"]:.2f}'
                for l in g["legs"])
            p(f"<div style='margin-top:10px'><b>{html.escape(str(acct))}</b> "
              f"&mdash; ONE trade <b>#{html.escape(str(g['trade_id']))}</b> "
              f"{html.escape(str(g['trade_name']))} "
              f"<span class='muted'>({html.escape(str(g['underlying']))}, "
              f"opened {html.escape(str(g['open_date']))})</span>"
              f"<div class='muted' style='margin:2px 0'>Flagged because "
              f"{html.escape(', and '.join(g['reasons']))}.</div>"
              f"<div style='font-family:monospace;font-size:12px'>{legs}</div>"
              f"<div class='muted' style='margin-top:2px'>Fix: rebuild it as a "
              f"new trade in ONE&rsquo;s Analysis window "
              f"(<i>Start New Trade</i>), then delete #"
              f"{html.escape(str(g['trade_id']))} in the Trade Log so its legs "
              f"are not counted twice.</div></div>")
        p("</div>")

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
        warn = (" <span class='warn'>(stale &mdash; re-export from ONE)</span>"
                if one_stale else "")
        p(f"<div class='sub'>ONE export: {html.escape(one_file)} &middot; "
          f"{one_age}{warn}</div>")

    if active_unmapped:
        group_text = ", ".join(
            f"{html.escape(name)} ({count} legs)"
            for name, count in sorted(active_unmapped.items())
        )
        p("<div class='acct' style='border:2px solid #cf222e;background:#2d1117'>"
          "<b class='warn'>Unmapped ONE account group(s):</b> "
          f"{group_text}. Add these groups to config.json before trusting the "
          "reconciliation.</div>")

    # -------- BUILD ADJUSTMENT DATA OBJECTS --------
    adj_list = []
    account_codes = st.get("account_codes") or {}

    def account_type(account):
        return str(account_codes.get(str(account), ""))

    def timestamp_fields(item):
        timestamp = item.get("timestamp")
        return {
            "timestamp": timestamp,
            "timestamp_epoch": _fill_epoch(timestamp) or 0,
        }

    exp_t = ""
    if act_n:
        if st.get("one_mtime"):
            exp_t = " " + datetime.fromtimestamp(st["one_mtime"]).strftime("%H:%M")

        if act.get("rolled"):
            for r in act["rolled"]:
                tid = r.get("one_trade_id")
                tname = r.get("one_trade_name") or ""
                hint = r.get("wizard_hint", "")
                adj_list.append({
                    "account": str(r.get("account", "")),
                    "account_type": account_type(r.get("account", "")),
                    "category": "Rolled",
                    "category_order": 1,
                    "ticker": r.get("underlying") or str(r.get("from", "")).split()[0],
                    "strikes": [s for s in (r.get("from_strike"), r.get("to_strike")) if s is not None],
                    "label": f"{r['from']} -> {r['to']}",
                    "trade_id": tid,
                    "trade_id_str": f"Trade #{tid}" if tid else "[New Trade]",
                    "trade_name": tname,
                    "wizard_hint": hint,
                    "qty_str": f"x{r['qty']:.0f}",
                    "color": "#d29922",
                    "details_html": f"{html.escape(r['from'])} &rarr; {html.escape(r['to'])}",
                    **timestamp_fields(r),
                })

        if act.get("opened"):
            for o in act["opened"]:
                tid = o.get("one_trade_id")
                tname = o.get("one_trade_name") or ""
                hint = o.get("wizard_hint", "")
                adj_list.append({
                    "account": str(o.get("account", "")),
                    "account_type": account_type(o.get("account", "")),
                    "category": "New / opened",
                    "category_order": 2,
                    "ticker": o.get("underlying") or str(o.get("label", "")).split()[0],
                    "strikes": [o["strike"]] if o.get("strike") is not None else [],
                    "label": o.get("label", ""),
                    "trade_id": tid,
                    "trade_id_str": f"Trade #{tid}" if tid else "[New Trade]",
                    "trade_name": tname,
                    "wizard_hint": hint,
                    "qty_str": f"{o['qty']:+.0f} @ {o['px']:.4f}",
                    "color": "#3fb950",
                    "details_html": html.escape(o.get("label", "")),
                    **timestamp_fields(o),
                })

        if act.get("closed"):
            for c in act["closed"]:
                tid = c.get("one_trade_id")
                tname = c.get("one_trade_name") or ""
                hint = c.get("wizard_hint", "")
                adj_list.append({
                    "account": str(c.get("account", "")),
                    "account_type": account_type(c.get("account", "")),
                    "category": "Closed",
                    "category_order": 3,
                    "ticker": c.get("underlying") or str(c.get("label", "")).split()[0],
                    "strikes": [c["strike"]] if c.get("strike") is not None else [],
                    "label": c.get("label", ""),
                    "trade_id": tid,
                    "trade_id_str": f"Trade #{tid}" if tid else "[New Trade]",
                    "trade_name": tname,
                    "wizard_hint": hint,
                    "qty_str": f"was {c['qty']:+.0f}",
                    "color": "#ff7b72",
                    "details_html": html.escape(c.get("label", "")),
                    **timestamp_fields(c),
                })

        if act.get("changed"):
            for ch in act["changed"]:
                tid = ch.get("one_trade_id")
                tname = ch.get("one_trade_name") or ""
                hint = ch.get("wizard_hint", "")
                adj_list.append({
                    "account": str(ch.get("account", "")),
                    "account_type": account_type(ch.get("account", "")),
                    "category": "Adjusted",
                    "category_order": 4,
                    "ticker": ch.get("underlying") or str(ch.get("label", "")).split()[0],
                    "strikes": [ch["strike"]] if ch.get("strike") is not None else [],
                    "label": f"{ch.get('label', '')} ({ch.get('type', '').lower()})",
                    "trade_id": tid,
                    "trade_id_str": f"Trade #{tid}" if tid else "[New Trade]",
                    "trade_name": tname,
                    "wizard_hint": hint,
                    "qty_str": f"{ch['qty']:+.0f}",
                    "color": "#d29922",
                    "details_html": f"{html.escape(ch.get('label', ''))} ({html.escape(ch.get('type', '').lower())})",
                    **timestamp_fields(ch),
                })

    trade_adj_list = []
    for trade in act.get("by_trade", []):
        item = dict(trade)
        tickers = set()
        strikes = set()
        if trade.get("underlying"):
            tickers.add(str(trade["underlying"]))
        for leg in (
            list(trade.get("opened") or [])
            + list(trade.get("closed") or [])
            + list(trade.get("changed") or [])
        ):
            if leg.get("underlying"):
                tickers.add(str(leg["underlying"]))
            if leg.get("strike") is not None:
                strikes.add(float(leg["strike"]))
        for roll in trade.get("rolled") or []:
            if roll.get("underlying"):
                tickers.add(str(roll["underlying"]))
            for key in ("from_strike", "to_strike"):
                if roll.get(key) is not None:
                    strikes.add(float(roll[key]))
        item.update({
            "account_type": account_type(trade.get("account", "")),
            "tickers": sorted(tickers),
            "strikes": sorted(strikes),
            **timestamp_fields(trade),
        })
        trade_adj_list.append(item)
    trade_adj_list.sort(key=lambda item: item["timestamp_epoch"], reverse=True)

    # -------- MAIN DASHBOARD TAB GROUP --------
    fills = result.get("fills_today", [])
    os_strats = st.get("os_strategies") or []
    flex = st.get("flex") or {}
    flex_completeness = st.get("flex_completeness") or {}
    flex_problem_accounts = {
        account for account, check in flex_completeness.items()
        if account != "UNASSIGNED"
        and check.get("status") != flex_export.COMPLETE
    }
    flex_accounts = sorted(set(flex) | flex_problem_accounts)
    accts_count = len(result["accounts"])

    p("<div data-tabgroup='main-dash' style='margin-top:10px;'>")
    p("<div class='tabnav' style='font-size:13px;'>")
    p(f"<a href='#' class='tabbtn active' data-tab='tab-reconcile'>🏦 Account Reconciliations ({accts_count})</a>")
    if act_n:
        p(f"<a href='#' class='tabbtn' data-tab='tab-adj'>⚠️ Trade Adjustments ({act_n})</a>")
    if fills:
        p(f"<a href='#' class='tabbtn' data-tab='tab-fills'>📋 Today's IBKR Fills ({len(fills)})</a>")
    if os_strats:
        p(f"<a href='#' class='tabbtn' data-tab='tab-optionstrat'>📱 OptionStrat Mirror ({len(os_strats)})</a>")
    if flex_accounts:
        p(f"<a href='#' class='tabbtn' data-tab='tab-flex'>📥 ONE Flex Import ({len(flex_accounts)})</a>")
    p("</div>")

    # --- TABPANEL 1: ACCOUNT RECONCILIATIONS ---
    p("<div class='tabpanel active' data-tab='tab-reconcile'>")
    def problem(f):
        return reconcile.is_actionable_finding(f)

    grand = {s: 0 for s in ORDER}
    ack_total = 0
    expected_total = 0
    for findings in result["accounts"].values():
        for f in findings:
            if f.get("acknowledged"):
                ack_total += 1
            elif reconcile.is_expected_finding(f):
                expected_total += 1
            else:
                grand[f["status"]] = grand.get(f["status"], 0) + 1
    tot = " &nbsp; ".join(
        f"<span class='tag' style='background:{STATUS_COLORS[s]}'>{s} {grand[s]}</span>"
        for s in ORDER if grand[s])
    if ack_total:
        tot += f" &nbsp; <span class='tag' style='background:#57606a'>ACK {ack_total}</span>"
    if expected_total:
        tot += (f" &nbsp; <span class='tag' style='background:#57606a'>"
                f"EXPECTED STOCK/ETF {expected_total}</span>")
    p(f"<div class='sub' style='margin-bottom:10px;'>{tot}</div>")

    p("<div class='grid-accts'>")
    for acct in sorted(result["accounts"]):
        findings = result["accounts"][acct]
        counts = {s: 0 for s in ORDER}
        for f in findings:
            if not f.get("acknowledged") and not reconcile.is_expected_finding(f):
                counts[f["status"]] += 1
        clean = not any(problem(f) for f in findings)
        pill = ("<span class='pill MATCHpill'>MATCH</span>" if clean
                else "<span class='pill DIFFpill'>DIFF</span>")
        expected = [f for f in findings if reconcile.is_expected_finding(f)]
        basis_info = [f for f in findings
                      if f["status"] == "MATCH_FIFO_AVG"
                      and not f.get("acknowledged")]
        summ_parts = [f"{s}:{counts[s]}" for s in ORDER
                      if counts[s] and s != "MATCH_FIFO_AVG"]
        if basis_info:
            summ_parts.append(f"COST_BASIS_INFO:{len(basis_info)}")
        if expected:
            summ_parts.append(f"EXPECTED_HOLDINGS:{len(expected)}")
        n_ack = sum(1 for f in findings if f.get("acknowledged"))
        if n_ack:
            summ_parts.append(f"ACK:{n_ack}")
        summ = " ".join(summ_parts)
        p(f"<div class='acct'><h2><span>{html.escape(acct)} "
          f"<span class='muted'>{html.escape(summ)}</span></span>{pill}</h2>")

        flags = sorted((f for f in findings if problem(f)),
                       key=lambda f: ORDER.index(f["status"]))
        acked = [f for f in findings if f.get("acknowledged")]
        if not flags:
            p(f"<div class='muted'>All {counts['MATCH'] + len(basis_info)} "
              "modeled option instruments reconcile on identity and quantity."
              + (f" ({n_ack} acknowledged)" if n_ack else "") + "</div>")
        else:
            p("<div class='table-wrap'><table>"
              "<tr><th class='l'>instrument</th><th>IBKR qty</th>"
              "<th>IBKR px</th><th>ONE qty</th><th>ONE px</th>"
              "<th>px delta</th><th>P&amp;L impact</th>"
              "<th class='l'>flag</th></tr>")
            for f in flags:
                iq = "" if f["ibkr_qty"] is None else f"{f['ibkr_qty']:+.0f}"
                oq = "" if f["one_qty"] is None else f"{f['one_qty']:+.0f}"
                ip = "" if f["ibkr_px"] is None else f"{f['ibkr_px']:.2f}"
                op = "" if f.get("one_px") is None else f"{f['one_px']:.2f}"
                dx = ("" if f.get("px_delta") is None
                      else f"{f['px_delta']:+.2f}")
                # A price flag the user cannot price is a flag they cannot act
                # on, so carry ONE's price and the dollars through to the table.
                pl = f.get("pnl_divergence")
                plc = ("" if pl is None else
                       f"<b style='color:{'#ff7b72' if pl < 0 else '#3fb950'}'>"
                       f"{pl:+,.0f}</b>")
                color = STATUS_COLORS.get(f["status"], "#8b949e")
                tag_label = "MATCH (FIFO)" if f["status"] == "MATCH_FIFO_AVG" else f["status"]
                tooltip = " title='Quantity matched. IBKR uses FIFO cost basis while ONE uses Weighted Avg entry.'" if f["status"] == "MATCH_FIFO_AVG" else ""
                p(f"<tr><td class='l mono'>{html.escape(f['label'])}</td>"
                  f"<td class='mono'>{iq}</td><td class='mono'>{ip}</td>"
                  f"<td class='mono'>{oq}</td><td class='mono'>{op}</td>"
                  f"<td class='mono'>{dx}</td><td class='mono'>{plc}</td>"
                  f"<td class='l'><span class='tag' style='background:{color}'{tooltip}>"
                  f"{tag_label}</span></td></tr>")
            p("</table></div>")
            drifts = [f for f in flags if f["status"] == "COST_BASIS_DRIFT"]
            if drifts:
                p("<div class='muted' style='margin-top:6px'>COST_BASIS_DRIFT: "
                  "quantity agrees but ONE's entry price does not. Set ONE's "
                  "price to the IBKR price above; the P&amp;L impact column is "
                  "what the trade's reported profit is wrong by until you "
                  "do.</div>")
        if basis_info:
            p(f"<details class='muted' style='margin-top:6px'><summary>"
              f"{len(basis_info)} quantity-matched cost-basis difference(s) "
              "(IBKR FIFO versus ONE weighted average)</summary>"
              "<div class='table-wrap'><table><tr><th class='l'>instrument</th>"
              "<th>IBKR px</th><th>ONE px</th></tr>")
            for f in basis_info:
                p(f"<tr><td class='l mono'>{html.escape(f['label'])}</td>"
                  f"<td class='mono'>{f['ibkr_px']:.4f}</td>"
                  f"<td class='mono'>{f['one_px']:.4f}</td></tr>")
            p("</table></div></details>")
        if expected:
            p(f"<details class='muted' style='margin-top:6px'><summary>"
              f"{len(expected)} expected broker-only stock/ETF holding(s), "
              "excluded because ONE models options only</summary>"
              "<div class='table-wrap'><table><tr><th class='l'>holding</th>"
              "<th>IBKR qty</th></tr>")
            for f in expected:
                p(f"<tr><td class='l mono'>{html.escape(f['label'])}</td>"
                  f"<td class='mono'>{f['ibkr_qty']:+.0f}</td></tr>")
            p("</table></div></details>")
        if acked:
            p("<div class='muted' style='margin-top:6px'>Acknowledged (reviewed-OK):</div>")
            p("<div class='table-wrap'><table>")
            for f in acked:
                ip = "" if f["ibkr_px"] is None else f"{f['ibkr_px']:.2f}"
                op = "" if f["one_px"] is None else f"{f['one_px']:.2f}"
                p(f"<tr><td class='l mono muted'>{html.escape(f['label'])}</td>"
                  f"<td class='mono muted'>IBKR {ip}</td>"
                  f"<td class='mono muted'>ONE {op}</td>"
                  f"<td class='l'><span class='tag' style='background:#57606a'>"
                  f"ACK {html.escape(f['status'])}</span></td></tr>")
            p("</table></div>")
        p("</div>")
    p("</div></div>") # close grid-accts AND close tabpanel tab-reconcile

    # --- TABPANEL 2: TRADE ADJUSTMENTS MODE ---
    if act_n:
        p("<div class='tabpanel' data-tab='tab-adj'>")
        p("<div class='acct' style='border:2px solid #1f6feb;background:#1f6feb22;margin-bottom:12px'>"
          "<h2 style='color:#58a6ff'>&#9888; ADJUSTMENT MODE &mdash; "
          f"{act_n} change(s) since your last ONE export{exp_t}</h2>"
          "<div class='muted' style='margin-bottom:8px'>Replicate these in ONE "
          "(re-export &rarr; Check now) and edit the same-named OptionStrat combos."
          "</div>")

        p("<div style='display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:8px;margin-bottom:10px;background:#161b22;padding:6px 10px;border-radius:6px;border:1px solid #30363d;'>"
          "<div style='display:flex;align-items:center;gap:6px;flex-wrap:wrap;'>"
          "<span style='font-weight:600;font-size:12px;color:#58a6ff;'>Group / Sort by:</span>"
          "<select id='adj-sort-by' onchange='window.renderAdjustments()' style='background:#0d1117;color:#e6edf3;border:1px solid #30363d;padding:3px 8px;border-radius:4px;font-size:12px;outline:none;'>"
          "<option value='time' selected>⏱ Execution time (newest first)</option>"
          "<option value='trade_id'>🆔 ONE Trade ID (Trade #74, #100, #115...)</option>"
          "<option value='category'>📁 Category (Rolled &rarr; New &rarr; Closed &rarr; Adjusted)</option>"
          "<option value='ticker'>🏷️ Ticker / Symbol (ADBE, RUT, SPX, UAL...)</option>"
          "<option value='account'>🏦 Account ID (F244..., U232..., U455...)</option>"
          "</select>"
          "<div style='display:flex;gap:4px;margin-left:4px;'>"
          "<button onclick='window.setAdjSort(\"time\")' style='background:#21262d;color:#c9d1d9;border:1px solid #30363d;padding:2px 8px;border-radius:4px;font-size:11px;cursor:pointer;'>Execution time</button>"
          "<button onclick='window.setAdjSort(\"trade_id\")' style='background:#21262d;color:#c9d1d9;border:1px solid #30363d;padding:2px 8px;border-radius:4px;font-size:11px;cursor:pointer;'>ONE Trade ID</button>"
          "<button onclick='window.setAdjSort(\"category\")' style='background:#21262d;color:#c9d1d9;border:1px solid #30363d;padding:2px 8px;border-radius:4px;font-size:11px;cursor:pointer;'>Category</button>"
          "<button onclick='window.setAdjSort(\"ticker\")' style='background:#21262d;color:#c9d1d9;border:1px solid #30363d;padding:2px 8px;border-radius:4px;font-size:11px;cursor:pointer;'>Ticker</button>"
          "<button onclick='window.setAdjSort(\"account\")' style='background:#21262d;color:#c9d1d9;border:1px solid #30363d;padding:2px 8px;border-radius:4px;font-size:11px;cursor:pointer;'>Account</button>"
          "</div>"
          "</div>"
          "<div style='display:flex;align-items:center;gap:6px;flex-wrap:wrap;'>"
          "<select id='adj-filter-ticker' onchange='window.renderAdjustments()' class='adj-filter'><option value=''>All tickers</option></select>"
          "<select id='adj-filter-strike' onchange='window.renderAdjustments()' class='adj-filter'><option value=''>All strikes</option></select>"
          "<select id='adj-filter-trade' onchange='window.renderAdjustments()' class='adj-filter'><option value=''>All ONE trade IDs</option></select>"
          "<select id='adj-filter-account-type' onchange='window.renderAdjustments()' class='adj-filter'><option value=''>All account types</option></select>"
          "<select id='adj-filter-account' onchange='window.renderAdjustments()' class='adj-filter'><option value=''>All accounts</option></select>"
          "<select id='adj-filter-status' onchange='window.renderAdjustments()' class='adj-filter'><option value=''>All statuses</option></select>"
          "<input type='text' id='adj-filter-search' oninput='window.renderAdjustments()' placeholder='🔍 Search; space = AND' class='adj-filter' style='width:175px;' />"
          "<button onclick='window.clearAdjFilters()' style='background:#21262d;color:#c9d1d9;border:1px solid #30363d;padding:3px 8px;border-radius:4px;font-size:11px;cursor:pointer;'>Clear filters</button>"
          "<button onclick='window.copyAdjustmentsText()' style='background:#238636;color:#ffffff;border:none;padding:3px 10px;border-radius:4px;font-size:12px;font-weight:600;cursor:pointer;' title='Copy checklist to clipboard'>📋 Copy</button>"
          "</div>"
          "</div>")

        p("<div id='adj-content'></div></div></div>")
        p(f"<script>window.ADJ_DATA = {json.dumps(adj_list)}; window.TRADE_ADJ_DATA = {json.dumps(trade_adj_list)};</script>")

    # --- TABPANEL 3: TODAY'S IBKR FILLS ---
    if fills:
        p("<div class='tabpanel' data-tab='tab-fills'>")
        p("<div class='acct'><h2>Today's IBKR Fills</h2>")
        by_acct: dict = defaultdict(list)
        for fl in fills:
            by_acct[str(fl.get("account", ""))].append(fl)
        accts = sorted(by_acct)
        p("<div data-tabgroup='fills-sub'><div class='tabnav' style='margin-bottom:6px;'>")
        for i, acct in enumerate(accts):
            p(f"<a href='#' class='tabbtn{' active' if i == 0 else ''}' "
              f"data-tab='{html.escape(acct)}'>{html.escape(acct)} "
              f"({len(by_acct[acct])})</a>")
        p("</div>")
        for i, acct in enumerate(accts):
            cls = "tabpanel active" if i == 0 else "tabpanel"
            p(f"<div class='{cls}' data-tab='{html.escape(acct)}'>")
            p("<div class='table-wrap'><table class='sortable'><thead><tr>"
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
            p("</tbody></table></div></div>")
        p("</div></div></div>")

    # --- TABPANEL 3: OPTIONSTRAT MIRROR ---
    if os_strats:
        p("<div class='tabpanel' data-tab='tab-optionstrat'>")
        p("<div class='acct'><h2>OptionStrat Mirror "
          "<span class='muted'>edit your saved combo of the same name to match "
          "these legs</span></h2>")
        by_acct = {}
        for s in os_strats:
            by_acct.setdefault(s["account"], []).append(s)
        accts = sorted(by_acct)
        p("<div data-tabgroup='optionstrat-sub'><div class='tabnav' style='margin-bottom:6px;'>")
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
                p("<div style='margin:6px 0;padding:6px 8px;border:1px solid "
                  "#30363d;border-radius:6px'>"
                  f"<div style='margin-bottom:4px'><b>{html.escape(s['name'])}</b> "
                  f"<span class='muted'>&middot; {len(s['legs'])} legs</span> "
                  f"<span class='muted' style='float:right'>{create}</span></div>")
                p("<div class='table-wrap'><table>")
                for lg in s["legs"]:
                    side_col = "#3fb950" if lg["side"] == "Buy" else "#ff7b72"
                    p(f"<tr><td class='l' style='color:{side_col};width:48px'>"
                      f"{lg['side']}</td><td class='mono' style='width:40px'>"
                      f"{lg['qty']}</td><td class='l mono'>{html.escape(lg['label'])}</td>"
                      f"<td class='mono'>{lg['price']:.4f}</td></tr>")
                p("</table></div></div>")
            p("</div>")
        p("</div></div></div>")

    # --- TABPANEL 4: ONE FLEX IMPORT ---
    if flex_accounts:
        p("<div class='tabpanel' data-tab='tab-flex'>")
        skipped = st.get("flex_skipped", 0)
        out_folder = os.path.join(HERE, "flex_export")
        incomplete = sorted(flex_problem_accounts)
        p("<div class='acct'><h2>ONE Flex Import "
          f"<span class='muted'>today's fills &middot; {skipped} non-option skipped</span></h2>")
        if incomplete:
            p("<div style='border:2px solid #d29922;background:#2d250d;padding:7px 9px;"
              "border-radius:5px;margin-bottom:8px;color:#f2cc60'><b>IMPORT BLOCKED:</b> "
              "TWS positions moved without matching captured executions. Review the "
              "listed discrepancies and click Check now; normal import files remain "
              "quarantined until execution history catches up.</div>")
        else:
            p("<div style='border:1px solid #238636;background:#0d2818;padding:6px 9px;"
              "border-radius:5px;margin-bottom:8px;color:#3fb950'><b>IMPORT COMPLETE:</b> "
              "Every observed option-position change is explained by persisted, "
              "deduplicated TWS executions.</div>")
        p(f"<div class='sub' style='margin-bottom:8px;color:#3fb950;font-size:12.5px;'>"
          f"📁 <b>Saved automatically to local folder:</b> <code class='mono' style='background:#21262d;padding:2px 6px;border-radius:4px;color:#e6edf3;'>{html.escape(out_folder)}</code>"
          "</div>")
        p("<div class='table-wrap'><table><tr><th class='l'>account</th>"
          "<th class='l'>status</th><th>fill rows</th>"
          "<th class='l'>local file path</th><th class='l'>download link</th></tr>")
        for acct in flex_accounts:
            n = len(flex.get(acct) or [])
            check = flex_completeness.get(str(acct)) or {}
            complete = check.get("status") == flex_export.COMPLETE
            status = (flex_export.COMPLETE if complete
                      else flex_export.POSSIBLY_INCOMPLETE)
            color = "#238636" if complete else "#d29922"
            f_path = (st.get("flex_paths") or {}).get(acct, "")
            download = (
                f"<a href='/flex/{html.escape(acct)}.csv' class='btn' "
                "style='font-size:11px;padding:2px 8px;text-decoration:none;'>"
                f"Download ONEImport_{html.escape(acct)}.csv</a>"
                if complete else
                "<span class='warn'>blocked pending complete fills</span>"
            )
            p(f"<tr><td class='l'>{html.escape(acct)}</td>"
              f"<td class='l'><span class='tag' style='background:{color}'>{status}</span></td>"
              f"<td class='mono'>{n}</td>"
              f"<td class='l mono muted' style='font-size:11.5px;'>{html.escape(f_path)}</td>"
              f"<td class='l'>{download}</td></tr>")
            for issue in check.get("discrepancies") or []:
                if "expected_qty" not in issue:
                    continue
                p("<tr><td></td><td colspan='4' class='l warn mono'>"
                  f"{html.escape(issue['instrument'])}: position "
                  f"{issue['position_qty']:+g}, executions imply "
                  f"{issue['expected_qty']:+g} (unexplained "
                  f"{issue['unexplained_qty']:+g})</td></tr>")
        p("</table></div><div class='muted' style='margin-top:6px'>"
          "Point ONE's import wizard directly to these files, then run ONE's link-trades step.</div></div></div>")

    p("</div>")  # close main-dash tabgroup

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


def _m(v, spec=",.0f", dash="&mdash;", suffix=""):
    """Format a metric, colouring the sign, or an em dash when absent."""
    if v is None:
        return f"<span class='muted'>{dash}</span>"
    try:
        txt = format(v, spec) + suffix
    except (TypeError, ValueError):
        return html.escape(str(v))
    if isinstance(v, (int, float)) and v < 0:
        return f"<span style='color:#ff7b72'>{txt}</span>"
    return txt


def _sv(v):
    """data-sort-value on a cell so the column header sorts numerically, not as text."""
    return "" if v is None else f" data-sort-value='{v}'"


def _headroom_cell(pct):
    """Liquidity headroom, coloured by how close a margin call is."""
    if pct is None:
        return "<span class='muted'>&mdash;</span>"
    colour = "#3fb950" if pct >= 40 else "#f2cc60" if pct >= 20 else "#ff7b72"
    return f"<b style='color:{colour}'>{pct:.1f}%</b>"


def _iv_cell(rank):
    """IV rank, coloured for a premium seller: low rank is poor conditions."""
    if rank is None:
        return "<span class='muted'>&mdash;</span>"
    colour = "#ff7b72" if rank < 20 else "#f2cc60" if rank < 40 else "#3fb950"
    return f"<b style='color:{colour}'>{rank:.0f}</b>"


# --------------------------------------------------------------------------
# Small inline-SVG charts.  Everything is self-contained: this dashboard is
# served from a local socket with no internet, so no charting library exists
# to lean on and none is wanted.
# --------------------------------------------------------------------------

C_POS, C_NEG, C_AXIS, C_GRID, C_TEXT = "#3fb950", "#ff7b72", "#8b949e", "#21262d", "#e6edf3"
TICKER_COLOURS = ["#58a6ff", "#bc8cff", "#f2cc60", "#3fb950", "#ff7b72",
                  "#79c0ff", "#ffa657", "#7ee787"]


def _colour_for(key, order):
    """Stable colour per ticker so a ticker keeps its colour across every chart."""
    try:
        return TICKER_COLOURS[order.index(key) % len(TICKER_COLOURS)]
    except ValueError:
        return "#58a6ff"


def _diverging_bars(rows, width=520, row_h=24, pad_left=110, fmt="{:+,.0f}",
                    empty="no data"):
    """Horizontal bars either side of a zero line.

    rows: (label, value, tooltip, colour).  Zero always sits at the centre so
    long and short read as mirror images, which is the point for a book that is
    meant to be delta neutral.
    """
    rows = [r for r in rows if r[1] is not None]
    if not rows:
        return f"<div class='muted' style='padding:6px'>{empty}</div>"
    span = max(abs(r[1]) for r in rows) or 1.0
    pad_right = 76
    plot = max(width - pad_left - pad_right, 60)
    mid = pad_left + plot / 2.0
    height = len(rows) * row_h + 14
    s = [f"<svg class='chart' viewBox='0 0 {width} {height}' width='100%' "
         f"height='{height}' preserveAspectRatio='xMidYMid meet' role='img'>"]
    s.append(f"<line x1='{mid:.1f}' y1='2' x2='{mid:.1f}' y2='{height - 12:.1f}' "
             f"stroke='{C_AXIS}' stroke-width='1' opacity='0.5'/>")
    for i, (label, value, tip, colour) in enumerate(rows):
        y = i * row_h + 4
        w = abs(value) / span * (plot / 2.0)
        x = mid if value >= 0 else mid - w
        col = colour or (C_POS if value >= 0 else C_NEG)
        s.append(
            f"<rect x='{x:.1f}' y='{y:.1f}' width='{max(w, 1.0):.1f}' "
            f"height='{row_h - 8}' rx='2' fill='{col}' opacity='0.85'>"
            f"<title>{html.escape(str(tip))}</title></rect>")
        s.append(f"<text x='{pad_left - 6}' y='{y + row_h - 11:.1f}' "
                 f"text-anchor='end' font-size='11' fill='{C_TEXT}'>"
                 f"{html.escape(str(label))}</text>")
        vx = mid + w + 6 if value >= 0 else mid - w - 6
        anchor = "start" if value >= 0 else "end"
        s.append(f"<text x='{vx:.1f}' y='{y + row_h - 11:.1f}' text-anchor='{anchor}' "
                 f"font-size='10.5' fill='{C_AXIS}' "
                 f"font-variant-numeric='tabular-nums'>{fmt.format(value)}</text>")
    s.append("</svg>")
    return "".join(s)


def _line_chart(points, width=760, height=200, fmt="{:,.0f}", empty=""):
    """Line + area chart of (label, value) pairs, with min/max/last annotated."""
    points = [(str(a), b) for a, b in points if b is not None]
    if len(points) < 2:
        return f"<div class='muted' style='padding:6px'>{empty}</div>"
    vals = [v for _, v in points]
    lo, hi = min(vals), max(vals)
    if hi == lo:
        hi, lo = hi + 1, lo - 1
    pad_l, pad_r, pad_t, pad_b = 62, 12, 12, 22
    pw, ph = width - pad_l - pad_r, height - pad_t - pad_b
    n = len(points)

    def xy(i, v):
        x = pad_l + (pw * i / (n - 1))
        y = pad_t + ph - (ph * (v - lo) / (hi - lo))
        return x, y

    pts = [xy(i, v) for i, (_, v) in enumerate(points)]
    line = " ".join(f"{x:.1f},{y:.1f}" for x, y in pts)
    area = (f"{pad_l:.1f},{pad_t + ph:.1f} " + line +
            f" {pad_l + pw:.1f},{pad_t + ph:.1f}")
    rising = vals[-1] >= vals[0]
    colour = C_POS if rising else C_NEG
    s = [f"<svg class='chart' viewBox='0 0 {width} {height}' width='100%' "
         f"height='{height}' preserveAspectRatio='xMidYMid meet' role='img'>"]
    for frac in (0.0, 0.5, 1.0):
        y = pad_t + ph * frac
        val = hi - (hi - lo) * frac
        s.append(f"<line x1='{pad_l}' y1='{y:.1f}' x2='{pad_l + pw}' y2='{y:.1f}' "
                 f"stroke='{C_GRID}' stroke-width='1'/>")
        s.append(f"<text x='{pad_l - 6}' y='{y + 3.5:.1f}' text-anchor='end' "
                 f"font-size='10' fill='{C_AXIS}' "
                 f"font-variant-numeric='tabular-nums'>{fmt.format(val)}</text>")
    s.append(f"<polygon points='{area}' fill='{colour}' opacity='0.10'/>")
    s.append(f"<polyline points='{line}' fill='none' stroke='{colour}' "
             f"stroke-width='2' stroke-linejoin='round'/>")
    for (x, y), (label, v) in zip(pts, points):
        s.append(f"<circle cx='{x:.1f}' cy='{y:.1f}' r='2.8' fill='{colour}'>"
                 f"<title>{html.escape(label)}: {fmt.format(v)}</title></circle>")
    s.append(f"<text x='{pad_l}' y='{height - 6}' font-size='10' fill='{C_AXIS}'>"
             f"{html.escape(points[0][0])}</text>")
    s.append(f"<text x='{pad_l + pw}' y='{height - 6}' text-anchor='end' "
             f"font-size='10' fill='{C_AXIS}'>{html.escape(points[-1][0])}</text>")
    s.append("</svg>")
    return "".join(s)


def _meter(value, lo=0, hi=100, colour="#58a6ff", label=""):
    """A thin 0-100 gauge, used for IV rank and margin utilisation."""
    if value is None:
        return "<span class='muted'>&mdash;</span>"
    frac = max(0.0, min(1.0, (value - lo) / (hi - lo) if hi > lo else 0))
    return (f"<span class='meter' title='{html.escape(str(label))}'>"
            f"<span class='meter-fill' style='width:{frac * 100:.0f}%;"
            f"background:{colour}'></span></span>")


def _kpi(label, value_html, note=""):
    return (f"<div class='kpi'><div class='kpi-l'>{label}</div>"
            f"<div class='kpi-v'>{value_html}</div>"
            f"<div class='kpi-n'>{note}</div></div>")


def render_risk_html() -> str:
    with _lock:
        st = dict(_state)
    m = st.get("metrics")
    out = []
    p = out.append
    p(_page_head("Risk &amp; Greeks &mdash; TWS Matcher", refresh_secs=60))
    p(nav_html("risk"))

    if st.get("metrics_error"):
        p(f"<div class='acct' style='border-color:#cf222e'><b class='warn'>"
          f"Metrics error:</b> {html.escape(str(st['metrics_error']))}</div>")
    if not m:
        p("<h1>Risk &amp; Greeks</h1>")
        p("<p class='muted'>Waiting for the first Greeks collection "
          "(runs every 5 minutes; it needs option market data)&hellip;</p>")
        return "".join(out) + "</div></body></html>"

    tick = m.get("by_ticker") or []
    expiries = m.get("by_expiry") or []
    accounts = m.get("accounts") or {}
    radar = m.get("radar") or {}
    risk = radar.get("assignment_risk") or []
    order = [t["underlying"] for t in
             sorted(tick, key=lambda t: -abs(t.get("delta_dollars") or 0))]

    book_theta = sum(t.get("theta") or 0 for t in tick)
    book_vega = sum(t.get("vega") or 0 for t in tick)
    book_delta = sum(t.get("delta_dollars") or 0 for t in tick)
    book_daily = sum(t.get("daily_pnl") or 0 for t in tick)
    book_unreal = sum(t.get("unrealized_pnl") or 0 for t in tick)
    nav = m.get("nav_total") or 0
    worst_headroom = min((d.get("headroom_pct") for d in accounts.values()
                          if d.get("headroom_pct") is not None), default=None)

    p("<h1>Risk &amp; Greeks</h1>")
    p(f"<div class='sub'>collected {html.escape(str(st.get('metrics_at')))} UTC "
      f"&nbsp;|&nbsp; refreshed every {METRICS_INTERVAL // 60} min "
      f"&nbsp;|&nbsp; {sum(t.get('positions') or 0 for t in tick)} legs "
      f"across {len(accounts)} account(s)</div>")

    # ---- KPI strip: the six numbers worth knowing before anything else
    p("<div class='kpis'>")
    p(_kpi("Net liquidation", f"<b>{_m(nav)}</b>",
           f"{len(accounts)} account(s), AUD"))
    p(_kpi("Day P&amp;L", f"<b>{_m(book_daily, '+,.0f')}</b>",
           f"{_m(book_daily / nav * 100 if nav else None, '+.2f', suffix='% of NAV')}"))
    p(_kpi("Theta / day", f"<b>{_m(book_theta, '+,.0f')}</b>",
           "positive = time decay pays you"))
    p(_kpi("Net delta $", f"<b>{_m(book_delta, '+,.0f')}</b>",
           f"notional; {_m(book_delta / 100.0, '+,.0f')} per 1% move"))
    p(_kpi("Vega", f"<b>{_m(book_vega, '+,.0f')}</b>",
           "P&amp;L per 1 vol point"))
    p(_kpi("Tightest headroom", _headroom_cell(worst_headroom),
           "excess liquidity over NAV"))
    p("</div>")

    # ---- assignment risk sits above the tabs: it can happen tonight and must
    #      never be hidden behind a tab the user did not click.
    if risk:
        p("<div class='acct' style='border:2px solid #cf222e;background:#2d1416;"
          "margin-bottom:12px'>"
          f"<h2 style='color:#ff7b72;margin:0 0 4px'>&#9888; {len(risk)} SHORT "
          f"OPTION{'S' if len(risk) > 1 else ''} AT EARLY-ASSIGNMENT RISK</h2>"
          "<div class='muted'>In the money with almost no extrinsic value left, "
          "so the holder gives up nothing by exercising. Assignment turns these "
          "into stock overnight, without any execution reaching this tool. "
          "Index options are excluded &mdash; they are cash settled and cannot "
          "be assigned early.</div>")
        p("<div class='table-wrap'><table><tr><th class='l'>account</th>"
          "<th class='l'>contract</th><th>qty</th><th>DTE</th>"
          "<th>spot</th><th>intrinsic</th><th>extrinsic</th><th>delta</th></tr>")
        for r in risk:
            p(f"<tr><td class='l'>{html.escape(str(r['account']))}</td>"
              f"<td class='l'><b>{html.escape(str(r['underlying']))} "
              f"{html.escape(str(r['expiry_label']))} "
              f"{r['strike']:g}{r['right']}</b></td>"
              f"<td>{r['qty']:+.0f}</td><td>{r['dte']}</td>"
              f"<td>{_m(r.get('spot'), ',.2f')}</td>"
              f"<td>{_m(r.get('intrinsic'), ',.2f')}</td>"
              f"<td><b style='color:#ff7b72'>{_m(r.get('extrinsic'), ',.2f')}</b></td>"
              f"<td>{_m(r.get('delta'), '.3f')}</td></tr>")
        p("</table></div></div>")

    div = (st.get("result") or {}).get("pnl_divergence") or {}
    exp = radar.get("expiring") or []
    n_div = sum(1 for r in div.get("worst") or []
                if r.get("status") in ("COST_BASIS_DRIFT", "PRICE_DRIFT"))

    # ---- tabs
    p("<div data-tabgroup='risk'><div class='tabnav'>")
    for key, label in (("overview", "Overview"),
                       ("greeks", f"Greeks by ticker ({len(tick)})"),
                       ("expiry", f"Expiry ladder ({len(expiries)})"),
                       ("margin", f"Margin &amp; accounts ({len(accounts)})"),
                       ("basis", f"Cost basis{f' ({n_div})' if n_div else ''}")):
        p(f"<a href='#' class='tabbtn' data-tab='risk-{key}'>{label}</a>")
    p("</div>")

    # ================= OVERVIEW =================
    p("<div class='tabpanel' data-tab='risk-overview'>")
    p("<div class='chartgrid'>")

    p("<div class='card'><h3>Theta per day by ticker</h3>"
      "<div class='muted' style='margin-bottom:4px'>What the book earns from a "
      "day passing with nothing else changing. Short premium should sit on the "
      "green side.</div>")
    p(_diverging_bars(
        [(t["underlying"], t.get("theta"),
          f"{t['underlying']}: {(t.get('theta') or 0):+,.0f} per day",
          None)
         for t in sorted(tick, key=lambda t: -(t.get("theta") or 0))]))
    p("</div>")

    p("<div class='card'><h3>Delta exposure by ticker</h3>"
      "<div class='muted' style='margin-bottom:4px'>Delta &times; underlying "
      "price: the dollar notional the book is effectively long or short, so "
      "tickers compare directly. A 1% move in the underlying moves P&amp;L by "
      "1% of the bar. Near zero is the goal.</div>")
    p(_diverging_bars(
        [(t["underlying"], t.get("delta_dollars"),
          f"{t['underlying']}: {(t.get('delta_dollars') or 0):+,.0f} notional, "
          f"{(t.get('delta_dollars') or 0) / 100.0:+,.0f} per 1% move",
          None)
         for t in sorted(tick, key=lambda t: -(t.get("delta_dollars") or 0))]))
    p("</div>")

    p("<div class='card'><h3>Today's P&amp;L by ticker</h3>"
      "<div class='muted' style='margin-bottom:4px'>Change in mark since "
      "yesterday's close, from IBKR's own per-position daily P&amp;L.</div>")
    p(_diverging_bars(
        [(t["underlying"], t.get("daily_pnl"),
          f"{t['underlying']}: {(t.get('daily_pnl') or 0):+,.0f} today",
          None)
         for t in sorted(tick, key=lambda t: -(t.get("daily_pnl") or 0))]))
    p("</div>")

    p("<div class='card'><h3>Vega by ticker</h3>"
      "<div class='muted' style='margin-bottom:4px'>P&amp;L for a 1-point rise "
      "in implied volatility. Negative is the normal state for a seller &mdash; "
      "a volatility spike costs this much per point.</div>")
    p(_diverging_bars(
        [(t["underlying"], t.get("vega"),
          f"{t['underlying']}: {(t.get('vega') or 0):+,.0f} per vol point",
          None)
         for t in sorted(tick, key=lambda t: -(t.get("vega") or 0))]))
    p("</div>")
    p("</div>")  # chartgrid

    # ---- equity curve
    try:
        with open(NAV_HISTORY) as fh:
            hist = json.load(fh)
    except (OSError, ValueError):
        hist = []
    p("<div class='card'><h3>Equity curve</h3>")
    if len(hist) < 2:
        p(f"<div class='muted'>Logging NAV daily &mdash; {len(hist)} day(s) so "
          "far. IBKR has no historical-NAV API, so this curve can only build "
          "forward from the day logging started. For history before that, a Flex "
          "Web Service token and a NAV query are needed.</div>")
    else:
        first, last = hist[0], hist[-1]
        base = first.get("nav_total") or 0
        chg = (last.get("nav_total") or 0) - base
        p(_line_chart([(r.get("date"), r.get("nav_total")) for r in hist[-120:]]))
        p(f"<div class='muted'>{len(hist)} days logged &mdash; "
          f"{html.escape(str(first.get('date')))} to "
          f"{html.escape(str(last.get('date')))}: {_m(chg, '+,.0f')} "
          f"({_m(chg / base * 100 if base else None, '+.2f', suffix='%')}). "
          "Hover a point for its date and NAV.</div>")
    p("</div>")
    p("</div>")  # overview panel

    # ================= GREEKS BY TICKER =================
    p("<div class='tabpanel' data-tab='risk-greeks'>")
    p("<div class='card'><h3>Per ticker</h3>"
      "<div class='muted' style='margin-bottom:6px'>Click any column heading to "
      "sort. Delta $ is delta &times; underlying price &mdash; the notional the "
      "book is effectively long or short; divide by 100 for the P&amp;L of a 1% "
      "move. Daily % is measured against gross prior value (a spread's net value "
      "nets toward zero and would give nonsense); % NAV is the contribution to "
      "account equity.</div>")
    p("<div class='table-wrap'><table class='sortable'><thead><tr>"
      "<th class='l sortable'>ticker</th><th class='sortable'>delta $</th>"
      "<th class='sortable'>theta / day</th><th class='sortable'>vega</th>"
      "<th class='sortable'>gamma</th><th class='sortable'>daily P&amp;L</th>"
      "<th class='sortable'>daily %</th><th class='sortable'>% NAV</th>"
      "<th class='sortable'>unrealised</th><th class='sortable'>IV</th>"
      "<th class='sortable'>IVR</th><th class='l'>&nbsp;</th>"
      "<th class='sortable'>IVP</th><th class='sortable'>IV 1d</th>"
      "<th class='sortable'>legs</th></tr></thead><tbody>")
    for t in tick:
        iv = t.get("iv")
        rank = t.get("iv_rank")
        meter_col = ("#ff7b72" if (rank or 0) < 20
                     else "#f2cc60" if (rank or 0) < 40 else "#3fb950")
        lo, hi = t.get("low_1y"), t.get("high_1y")
        note = (f"IV {iv * 100:.1f}% within 1y range "
                f"{lo * 100:.1f}%-{hi * 100:.1f}%") if iv and lo and hi else ""
        p(f"<tr><td class='l'><b>{html.escape(str(t['underlying']))}</b></td>"
          f"<td class='mono'{_sv(t.get('delta_dollars'))}>{_m(t.get('delta_dollars'))}</td>"
          f"<td class='mono'{_sv(t.get('theta'))}>{_m(t.get('theta'))}</td>"
          f"<td class='mono'{_sv(t.get('vega'))}>{_m(t.get('vega'))}</td>"
          f"<td class='mono'{_sv(t.get('gamma'))}>{_m(t.get('gamma'), ',.1f')}</td>"
          f"<td class='mono'{_sv(t.get('daily_pnl'))}>{_m(t.get('daily_pnl'))}</td>"
          f"<td class='mono'{_sv(t.get('daily_pnl_pct'))}>"
          f"{_m(t.get('daily_pnl_pct'), '.2f', suffix='%')}</td>"
          f"<td class='mono'{_sv(t.get('daily_pnl_pct_nav'))}>"
          f"{_m(t.get('daily_pnl_pct_nav'), '.2f', suffix='%')}</td>"
          f"<td class='mono'{_sv(t.get('unrealized_pnl'))}>{_m(t.get('unrealized_pnl'))}</td>"
          f"<td class='mono'{_sv(iv)}>{_m(iv * 100 if iv else None, '.1f', suffix='%')}</td>"
          f"<td class='mono'{_sv(rank)}>{_iv_cell(rank)}</td>"
          f"<td class='l'>{_meter(rank, colour=meter_col, label=note)}</td>"
          f"<td class='mono'{_sv(t.get('iv_percentile'))}>"
          f"{_m(t.get('iv_percentile'), '.0f')}</td>"
          f"<td class='mono'{_sv(t.get('chg_pct'))}>"
          f"{_m(t.get('chg_pct'), '+.1f', suffix='%')}</td>"
          f"<td class='mono'{_sv(t.get('positions'))}>{t.get('positions')}</td></tr>")
    p("</tbody><tfoot>")
    p(f"<tr><td class='l'><b>BOOK</b></td><td class='mono'><b>{_m(book_delta)}</b></td>"
      f"<td class='mono'><b>{_m(book_theta)}</b></td>"
      f"<td class='mono'><b>{_m(book_vega)}</b></td><td></td>"
      f"<td class='mono'><b>{_m(book_daily)}</b></td><td></td>"
      f"<td class='mono'><b>"
      f"{_m(book_daily / nav * 100 if nav else None, '.2f', suffix='%')}</b></td>"
      f"<td class='mono'><b>{_m(book_unreal)}</b></td>"
      f"<td colspan='6'></td></tr>")
    p("</tfoot></table></div></div>")

    p("<div class='card'><h3>Implied volatility &mdash; where each ticker sits "
      "in its own 1-year range</h3>"
      "<div class='muted' style='margin-bottom:4px'>IV rank is the position of "
      "today's implied volatility between its 1-year low and high. Below 20 is "
      "red: options are cheap, which is poor conditions to be selling them.</div>")
    p("<div class='table-wrap'><table><tr><th class='l'>ticker</th>"
      "<th>1y low</th><th class='l'>rank</th><th>1y high</th><th>IV now</th>"
      "<th>IVR</th><th>IVP</th><th>1d change</th><th>samples</th></tr>")
    for t in sorted(tick, key=lambda t: -(t.get("iv_rank") or 0)):
        rank, iv = t.get("iv_rank"), t.get("iv")
        if iv is None:
            continue
        meter_col = ("#ff7b72" if (rank or 0) < 20
                     else "#f2cc60" if (rank or 0) < 40 else "#3fb950")
        lo, hi = t.get("low_1y"), t.get("high_1y")
        p(f"<tr><td class='l'><b>{html.escape(str(t['underlying']))}</b></td>"
          f"<td class='mono'>{_m(lo * 100 if lo else None, '.1f', suffix='%')}</td>"
          f"<td class='l' style='width:200px'>"
          f"{_meter(rank, colour=meter_col, label=f'IV rank {rank:.0f}' if rank is not None else '')}</td>"
          f"<td class='mono'>{_m(hi * 100 if hi else None, '.1f', suffix='%')}</td>"
          f"<td class='mono'><b>{_m(iv * 100, '.1f', suffix='%')}</b></td>"
          f"<td class='mono'>{_iv_cell(rank)}</td>"
          f"<td class='mono'>{_m(t.get('iv_percentile'), '.0f')}</td>"
          f"<td class='mono'>{_m(t.get('chg_pct'), '+.1f', suffix='%')}</td>"
          f"<td class='mono muted'>{t.get('samples') or ''}</td></tr>")
    p("</table></div></div>")

    missing = sum(t.get("greeks_missing") or 0 for t in tick)
    if missing:
        p(f"<div class='warnbox'><b>{missing} leg(s) returned no Greeks</b> and "
          "are excluded from the delta/theta/vega totals, so those totals "
          "understate the book. Usually a missing market-data permission or a "
          "contract that was not quoting when the snapshot was taken.</div>")
    p("</div>")  # greeks panel

    # ================= EXPIRY LADDER =================
    p("<div class='tabpanel' data-tab='risk-expiry'>")
    if exp:
        itm = [r for r in exp if r["itm"]]
        p("<div class='card'><h3>Expiring within "
          f"{radar.get('within_days', 7)} days</h3>"
          f"<div class='muted' style='margin-bottom:6px'>{len(exp)} leg(s), "
          f"{len(itm)} in the money. In-the-money legs settle for cash or become "
          "stock; either way ONE will not book it for you.</div>")
        p("<div class='table-wrap'><table class='sortable'><thead><tr>"
          "<th class='l sortable'>account</th><th class='l sortable'>contract</th>"
          "<th class='sortable'>qty</th><th class='sortable'>DTE</th>"
          "<th class='sortable'>spot</th><th class='l sortable'>moneyness</th>"
          "<th class='sortable'>delta</th><th class='l sortable'>settles</th>"
          "</tr></thead><tbody>")
        for r in exp:
            tag = ("<b style='color:#f2cc60'>ITM</b>" if r["itm"]
                   else "<span class='muted'>OTM</span>")
            p(f"<tr><td class='l'>{html.escape(str(r['account']))}</td>"
              f"<td class='l mono'>{html.escape(str(r['underlying']))} "
              f"{html.escape(str(r['expiry_label']))} "
              f"{r['strike']:g}{r['right']}</td>"
              f"<td class='mono'{_sv(r['qty'])}>{r['qty']:+.0f}</td>"
              f"<td class='mono'{_sv(r['dte'])}>{r['dte']}</td>"
              f"<td class='mono'{_sv(r.get('spot'))}>{_m(r.get('spot'), ',.2f')}</td>"
              f"<td class='l'{_sv(1 if r['itm'] else 0)}>{tag}</td>"
              f"<td class='mono'{_sv(r.get('delta'))}>{_m(r.get('delta'), '.3f')}</td>"
              f"<td class='l muted'>{'cash' if r['cash_settled'] else 'shares'}</td>"
              f"</tr>")
        p("</tbody></table></div></div>")

    p("<div class='chartgrid'>")
    p("<div class='card'><h3>Theta by expiry</h3>"
      "<div class='muted' style='margin-bottom:4px'>Where the decay is actually "
      "earned. Colour is the ticker; expiries run nearest first.</div>")
    p(_diverging_bars(
        [(f"{e['underlying']} {str(e.get('expiry_label'))[5:]}", e.get("theta"),
          f"{e['underlying']} {e.get('expiry_label')}: "
          f"{(e.get('theta') or 0):+,.0f}/day, {e.get('positions')} legs",
          _colour_for(e["underlying"], order))
         for e in expiries], row_h=20))
    p("</div>")
    p("<div class='card'><h3>Vega by expiry</h3>"
      "<div class='muted' style='margin-bottom:4px'>Volatility exposure "
      "concentrates in the far months, where a vol move is worth most.</div>")
    p(_diverging_bars(
        [(f"{e['underlying']} {str(e.get('expiry_label'))[5:]}", e.get("vega"),
          f"{e['underlying']} {e.get('expiry_label')}: "
          f"{(e.get('vega') or 0):+,.0f} per vol point",
          _colour_for(e["underlying"], order))
         for e in expiries], row_h=20))
    p("</div>")
    p("</div>")

    p("<div class='card'><h3>Per expiry</h3>"
      "<div class='muted' style='margin-bottom:6px'>Click any column heading to "
      "sort &mdash; sorting by theta or unrealised finds the expiry carrying the "
      "book.</div>")
    p("<div class='table-wrap'><table class='sortable'><thead><tr>"
      "<th class='l sortable'>ticker</th><th class='l sortable'>expiry</th>"
      "<th class='sortable'>delta $</th><th class='sortable'>theta / day</th>"
      "<th class='sortable'>vega</th><th class='sortable'>gamma</th>"
      "<th class='sortable'>daily P&amp;L</th><th class='sortable'>daily %</th>"
      "<th class='sortable'>unrealised</th><th class='sortable'>legs</th>"
      "</tr></thead><tbody>")
    for e in expiries:
        col = _colour_for(e["underlying"], order)
        p(f"<tr><td class='l'><span class='dot' style='background:{col}'></span>"
          f"{html.escape(str(e['underlying']))}</td>"
          f"<td class='l mono'>{html.escape(str(e.get('expiry_label')))}</td>"
          f"<td class='mono'{_sv(e.get('delta_dollars'))}>{_m(e.get('delta_dollars'))}</td>"
          f"<td class='mono'{_sv(e.get('theta'))}>{_m(e.get('theta'))}</td>"
          f"<td class='mono'{_sv(e.get('vega'))}>{_m(e.get('vega'))}</td>"
          f"<td class='mono'{_sv(e.get('gamma'))}>{_m(e.get('gamma'), ',.1f')}</td>"
          f"<td class='mono'{_sv(e.get('daily_pnl'))}>{_m(e.get('daily_pnl'))}</td>"
          f"<td class='mono'{_sv(e.get('daily_pnl_pct'))}>"
          f"{_m(e.get('daily_pnl_pct'), '.2f', suffix='%')}</td>"
          f"<td class='mono'{_sv(e.get('unrealized_pnl'))}>{_m(e.get('unrealized_pnl'))}</td>"
          f"<td class='mono'{_sv(e.get('positions'))}>{e.get('positions')}</td></tr>")
    p("</tbody></table></div></div>")
    p("</div>")  # expiry panel

    # ================= MARGIN =================
    p("<div class='tabpanel' data-tab='risk-margin'>")
    p("<div class='card'><h3>Accounts &amp; margin</h3>"
      "<div class='muted' style='margin-bottom:6px'>Headroom is excess liquidity "
      "over net liquidation: how far the account can fall before a margin call. "
      "Amber under 40%, red under 20%.</div>")
    p("<div class='table-wrap'><table class='sortable'><thead><tr>"
      "<th class='l sortable'>account</th><th class='sortable'>NAV</th>"
      "<th class='sortable'>cash</th><th class='sortable'>gross position</th>"
      "<th class='sortable'>init margin</th><th class='sortable'>maint margin</th>"
      "<th class='sortable'>excess liquidity</th><th class='sortable'>headroom</th>"
      "<th class='l'>&nbsp;</th><th class='sortable'>init util</th>"
      "</tr></thead><tbody>")
    for acct, d in sorted(accounts.items()):
        cur = d.get("currency") or ""
        hp = d.get("headroom_pct")
        col = ("#3fb950" if (hp or 0) >= 40 else
               "#f2cc60" if (hp or 0) >= 20 else "#ff7b72")
        p(f"<tr><td class='l'><b>{html.escape(acct)}</b></td>"
          f"<td class='mono'{_sv(d.get('NetLiquidation'))}>"
          f"{_m(d.get('NetLiquidation'))} <span class='muted'>{cur}</span></td>"
          f"<td class='mono'{_sv(d.get('TotalCashValue'))}>{_m(d.get('TotalCashValue'))}</td>"
          f"<td class='mono'{_sv(d.get('GrossPositionValue'))}>"
          f"{_m(d.get('GrossPositionValue'))}</td>"
          f"<td class='mono'{_sv(d.get('FullInitMarginReq'))}>"
          f"{_m(d.get('FullInitMarginReq'))}</td>"
          f"<td class='mono'{_sv(d.get('FullMaintMarginReq'))}>"
          f"{_m(d.get('FullMaintMarginReq'))}</td>"
          f"<td class='mono'{_sv(d.get('ExcessLiquidity'))}>"
          f"{_m(d.get('ExcessLiquidity'))}</td>"
          f"<td class='mono'{_sv(hp)}>{_headroom_cell(hp)}</td>"
          f"<td class='l' style='width:120px'>"
          f"{_meter(hp, hi=60, colour=col, label='headroom')}</td>"
          f"<td class='mono'{_sv(d.get('init_margin_util_pct'))}>"
          f"{_m(d.get('init_margin_util_pct'), '.0f', suffix='%')}</td></tr>")
    p("</tbody></table></div></div>")

    p("<div class='chartgrid'>")
    p("<div class='card'><h3>Headroom by account</h3>"
      "<div class='muted' style='margin-bottom:4px'>Excess liquidity as a "
      "percentage of net liquidation.</div>")
    p(_diverging_bars(
        [(a, d.get("headroom_pct"),
          f"{a}: {(d.get('headroom_pct') or 0):.1f}% headroom",
          ("#3fb950" if (d.get("headroom_pct") or 0) >= 40 else
           "#f2cc60" if (d.get("headroom_pct") or 0) >= 20 else "#ff7b72"))
         for a, d in sorted(accounts.items())], fmt="{:.1f}%"))
    p("</div>")
    p("<div class='card'><h3>Net liquidation by account</h3>"
      "<div class='muted' style='margin-bottom:4px'>Equity per account.</div>")
    p(_diverging_bars(
        [(a, d.get("NetLiquidation"), f"{a}: {_m(d.get('NetLiquidation'))}", "#58a6ff")
         for a, d in sorted(accounts.items())], fmt="{:,.0f}"))
    p("</div>")
    p("</div>")
    p("</div>")  # margin panel

    # ================= COST BASIS =================
    p("<div class='tabpanel' data-tab='risk-basis'>")
    if div.get("worst"):
        p("<div class='card'><h3>Cost-basis P&amp;L divergence &mdash; ONE vs "
          "IBKR</h3>")
        p(f"<div class='sub'>Net <b>{_m(div.get('net'), '+,.0f')}</b> "
          f"&nbsp;|&nbsp; gross <b>{_m(div.get('gross'))}</b></div>")
        p("<div class='muted' style='margin-bottom:6px'>ONE reports no P&amp;L "
          "for an open leg, so this prices the consequence instead: when these "
          "positions close, ONE will report this many dollars more profit than "
          "IBKR purely because the two cost bases disagree. Gross matters more "
          "than net &mdash; offsetting legs can hide a large disagreement inside "
          "a small net.</div>")
        p("<div class='table-wrap'><table class='sortable'><thead><tr>"
          "<th class='l sortable'>account</th><th class='l sortable'>instrument</th>"
          "<th class='l sortable'>status</th><th class='sortable'>px delta</th>"
          "<th class='sortable'>P&amp;L divergence</th></tr></thead><tbody>")
        for r in div["worst"]:
            colour = STATUS_COLORS.get(r.get("status"), "#8b949e")
            p(f"<tr><td class='l'>{html.escape(str(r['account']))}</td>"
              f"<td class='l mono'>{html.escape(str(r['label']))}</td>"
              f"<td class='l'><span class='tag' style='background:{colour}'>"
              f"{html.escape(str(r['status']))}</span></td>"
              f"<td class='mono'{_sv(r.get('px_delta'))}>"
              f"{_m(r.get('px_delta'), '+.4f')}</td>"
              f"<td class='mono'{_sv(r.get('pnl_divergence'))}>"
              f"<b>{_m(r.get('pnl_divergence'), '+,.0f')}</b></td></tr>")
        p("</tbody></table></div>")
        if div.get("by_ticker"):
            p("</div><div class='card'><h3>Divergence by ticker</h3>")
            p(_diverging_bars(
                [(k, v, f"{k}: {v:+,.0f}", None)
                 for k, v in sorted(div["by_ticker"].items(),
                                    key=lambda x: -abs(x[1]))]))
        p("</div>")
    else:
        p("<div class='card'><div class='muted'>No cost-basis divergence "
          "recorded yet &mdash; run a reconciliation first.</div></div>")
    p("</div>")  # basis panel

    p("</div>")  # tabgroup
    p(TAB_JS)
    return "".join(out) + "</div></body></html>"


def render_oneos_html() -> str:
    with _lock:
        st = dict(_state)
    r = st.get("oneos")
    os_file = os.path.basename(st.get("oneos_file") or "")
    os_age = _age(st.get("oneos_mtime"))
    os_stale = bool(
        st.get("oneos_mtime")
        and time.time() - st["oneos_mtime"] > 3600
    )

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
    if os_stale:
        p("<div class='acct' style='border:2px solid #d29922;background:#2d250d'>"
          "<b class='warn'>OptionStrat export is stale.</b> Download a fresh "
          "<i>all active</i> report before relying on this comparison.</div>")
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
        check = (_state.get("flex_completeness") or {}).get(account) or {}
    if rows is None:
        return None
    if check.get("status") != flex_export.COMPLETE:
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
        if self.path.startswith("/send_email"):
            cfg = reconcile.load_config()
            with _lock:
                st = dict(_state)
                result = st.get("result") or {}
            ibkr_snap = st.get("ibkr_snap") or {
                "legs": [], "fills_today": result.get("fills_today", [])}
            one_snap = st.get("one_snap") or {"positions": []}
            subject, html_body = email_report.generate_report_html(
                ibkr_snap, one_snap, cfg, reconciliation_result=result)
            success, msg = email_report.send_email(
                subject, html_body, cfg, attachment_html=html_body)
            _set(status=msg)
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
        if self.path.startswith("/risk"):
            body = render_risk_html().encode("utf-8")
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
