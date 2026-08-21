import os
import tempfile
import time
import unittest
from datetime import date, datetime, timezone

import dashboard
import email_report
import flex_export
import one_reader
import recon_one_os
import reconcile


def option(account="A14+HV7", *, qty=1.0, price=10.0):
    return {
        "account": account,
        "secType": "OPT",
        "underlying": "SPX",
        "tradingClass": "SPXW",
        "expiry": "20260807",
        "strike": 7400.0,
        "right": "P",
        "qty": qty,
        "avg_price": price,
    }


def one_leg(account="SMSF", **over):
    """A raw ONE report leg, as read_summary_report() emits it."""
    return {
        "account": account, "trade_id": "1", "underlying": "SPX",
        "tradingClass": "SPXW", "expiry": "20260808", "expiry_listed": "20260807",
        "strike": 7400.0, "right": "P", "multiplier": 100.0, "qty": -1.0,
        "open_price": 10.0, "is_open": True, "is_expired": False,
        "unsettled_in_one": False, "trade_name": None,
        **over,
    }


def trade_row(account="SMSF", *, trade_id="1", name="A Trade",
              margin=1000.0, status="Open"):
    """A TRADE summary row, as parse_trade_row() emits it."""
    return {
        "account": account, "trade_id": trade_id, "trade_name": name,
        "status": status, "underlying": "SPX", "expiration": "20260807",
        "open_date": "8/08/2026 6:11 AM", "margin": margin,
        "has_margin_column": True,
    }


def config():
    return {
        "account_map": {"A14+HV7": "U1"},
        "ignore_one_accounts": [],
        "one_export_dirs": [r"Z:\tws-matcher-test-no-files"],
        "tolerances": {
            "price_abs": 0.05,
            "price_pct": 0.005,
            "expiry_days": 1,
        },
        "email": {
            "enabled": True,
            "username": "sender@example.com",
            "password": "test-password",
            "to_email": "recipient@example.com",
        },
    }


class FlexExportHousekeepingTests(unittest.TestCase):
    def _row(self):
        return ["Trades", "U1", "SPX", "OPT", "SPXW  260904P07400000"]

    def test_superseded_export_is_deleted_not_left_beside_the_current_one(self):
        with tempfile.TemporaryDirectory() as d:
            # a COMPLETE run replaces a previously quarantined file
            blocked = os.path.join(
                d, f"ONEImport_U1_{flex_export.POSSIBLY_INCOMPLETE}.csv")
            open(blocked, "w").close()
            flex_export.save_account_files(
                {"U1": [self._row()]},
                {"U1": {"status": flex_export.COMPLETE}}, out_dir=d)
            self.assertEqual(sorted(os.listdir(d)), ["ONEImport_U1.csv"])

    def test_incomplete_run_removes_the_stale_complete_file(self):
        with tempfile.TemporaryDirectory() as d:
            normal = os.path.join(d, "ONEImport_U1.csv")
            open(normal, "w").close()
            flex_export.save_account_files(
                {"U1": [self._row()]},
                {"U1": {"status": flex_export.POSSIBLY_INCOMPLETE}}, out_dir=d)
            self.assertEqual(
                sorted(os.listdir(d)),
                [f"ONEImport_U1_{flex_export.POSSIBLY_INCOMPLETE}.csv"])

    def test_existing_dot_stale_leftovers_are_swept(self):
        """Folders from the rename era heal themselves on the next save."""
        with tempfile.TemporaryDirectory() as d:
            for name in ("ONEImport_U1.csv.stale",
                         "ONEImport_U9_POSSIBLY_INCOMPLETE.csv.stale"):
                open(os.path.join(d, name), "w").close()
            keep = os.path.join(d, "notes.txt")
            open(keep, "w").close()
            flex_export.save_account_files(
                {"U1": [self._row()]},
                {"U1": {"status": flex_export.COMPLETE}}, out_dir=d)
            self.assertEqual(sorted(os.listdir(d)),
                             ["ONEImport_U1.csv", "notes.txt"])


class ReconciliationTests(unittest.TestCase):
    def test_one_expired_leg_is_not_treated_as_open(self):
        self.assertTrue(one_reader.is_expired("20260801", date(2026, 8, 4)))
        self.assertFalse(one_reader.is_expired("20260804", date(2026, 8, 4)))
        self.assertFalse(one_reader.is_expired("20260805", date(2026, 8, 4)))

        row = [
            "", "", "A14+HV7", "100", "", "Sell", "4",
            "SPXW  260801P07250000", "31/07/2026", "Put", "",
            "SPX", "10.0", "", "",
        ]
        leg = one_reader.parse_leg_row(row)
        self.assertTrue(leg["is_expired"])
        self.assertFalse(leg["is_open"])
        self.assertEqual(one_reader.net_positions([leg]), [])

    def test_one_report_order_prefix_is_not_part_of_account_name(self):
        self.assertEqual(
            one_reader.normalize_account_name("  12.A14+HV7 "), "A14+HV7")
        row = [
            "", "", "1.A14+HV7", "100", "", "Buy", "1",
            "SPXW  260808P07400000", "7/08/2026", "Put", "",
            "SPX", "10.0", "", "",
        ]
        self.assertEqual(one_reader.parse_leg_row(row)["account"], "A14+HV7")

    def test_numbered_one_account_matches_unprefixed_config(self):
        ibkr = {"captured_at": "now", "legs": [
            {**option(account="U1")}], "fills_today": []}
        one = {"source_file": "report.csv", "positions": [
            {**option(account="1.A14+HV7"), "avg_price": 10.0}]}
        result = reconcile.reconcile_snapshots(ibkr, one, config())
        finding = result["accounts"]["U1"][0]
        self.assertEqual(finding["status"], "MATCH")
        self.assertEqual(result["unmapped_one_groups"], {})

    def test_stock_only_is_expected_and_not_actionable(self):
        stock = {
            "account": "U1", "secType": "STK", "underlying": "EVN",
            "tradingClass": "EVN", "expiry": None, "strike": 0.0,
            "right": "", "qty": 250.0, "avg_price": 12.8,
        }
        result = reconcile.reconcile_snapshots(
            {"captured_at": "now", "legs": [stock], "fills_today": []},
            {"source_file": "report.csv", "positions": []},
            config(),
        )
        finding = result["accounts"]["U1"][0]
        self.assertTrue(finding["expected"])
        self.assertFalse(reconcile.is_actionable_finding(finding))

    def test_aged_price_difference_is_fifo_information(self):
        result = reconcile.reconcile_snapshots(
            {"captured_at": "now", "legs": [
                {**option(account="U1"), "avg_price": 12.0}],
             "fills_today": []},
            {"source_file": "report.csv", "positions": [
                {**option(account="A14+HV7"), "avg_price": 10.0}]},
            config(),
        )
        finding = result["accounts"]["U1"][0]
        self.assertEqual(finding["status"], "MATCH_FIFO_AVG")
        self.assertFalse(reconcile.is_actionable_finding(finding))

    def test_exact_current_fill_price_difference_is_actionable(self):
        fill = {
            "account": "U1", "secType": "OPT", "underlying": "SPX",
            "tradingClass": "SPXW", "expiry": "20260807",
            "strike": 7400.0, "right": "P", "shares": 1.0,
            "side": "BOT", "price": 9.0,
        }
        result = reconcile.reconcile_snapshots(
            {"captured_at": "now", "legs": [
                {**option(account="U1"), "avg_price": 9.1}],
             "fills_today": [fill]},
            {"source_file": "report.csv", "positions": [
                {**option(account="A14+HV7"), "avg_price": 10.0}]},
            config(),
        )
        finding = result["accounts"]["U1"][0]
        self.assertEqual(finding["status"], "PRICE_DRIFT")
        self.assertTrue(reconcile.is_actionable_finding(finding))

    def test_unmapped_group_is_retained_as_an_assurance_failure(self):
        result = reconcile.reconcile_snapshots(
            {"captured_at": "now", "legs": [], "fills_today": []},
            {"source_file": "report.csv", "positions": [
                {**option(account="UnknownBook"), "avg_price": 10.0}]},
            config(),
        )
        self.assertEqual(result["unmapped_one_groups"], {"UnknownBook": 1})

    def test_one_legs_are_netted_on_the_listed_expiry_not_the_osi_symbol(self):
        """ONE's OSI symbol runs a day late; the Expiry column is the truth."""
        leg = {
            "account": "A14+HV7", "underlying": "SPX", "tradingClass": "SPXW",
            "expiry": "20260822", "expiry_listed": "20260821",
            "strike": 7400.0, "right": "P", "multiplier": 100.0,
            "qty": -1.0, "open_price": 10.0, "is_open": True, "trade_id": "1",
        }
        [pos] = one_reader.net_positions([leg])
        self.assertEqual(pos["expiry"], "20260821")
        self.assertEqual(pos["expiry_osi"], "20260822")

    def test_am_monthly_still_pairs_within_the_one_day_window(self):
        """IBKR reports the Thursday last-trade date, ONE the Friday expiry."""
        result = reconcile.reconcile_snapshots(
            {"captured_at": "now", "fills_today": [], "legs": [
                {**option(account="U1", qty=-1.0), "expiry": "20260917"}]},
            {"source_file": "report.csv", "positions": [
                {**option(qty=-1.0), "expiry": "20260918",
                 "avg_price": 10.0}]},
            config(),
        )
        [finding] = result["accounts"]["U1"]
        self.assertEqual(finding["status"], "MATCH")
        self.assertEqual(finding["expiry_offset_days"], 1)

    def test_adjacent_weekly_expiries_do_not_cross_pair(self):
        """A 1-day window must not marry a Wednesday leg to a Friday one."""
        result = reconcile.reconcile_snapshots(
            {"captured_at": "now", "fills_today": [], "legs": [
                {**option(account="U1", qty=-1.0), "expiry": "20260819"}]},
            {"source_file": "report.csv", "positions": [
                {**option(qty=-1.0), "expiry": "20260821",
                 "avg_price": 10.0}]},
            config(),
        )
        self.assertEqual(
            sorted(f["status"] for f in result["accounts"]["U1"]),
            ["IBKR_ONLY", "ONE_ONLY"],
        )

    def test_expired_leg_still_open_in_one_is_reported_not_suppressed(self):
        """The regression that hid a real assignment: at expiry IBKR drops the
        contract and the reader stops counting it, so neither side can raise a
        finding. ONE's P&L stays wrong until the trade is settled."""
        legs = [
            one_leg(trade_id="104", strike=380.0, qty=2.0, open_price=14.56,
                    expiry_listed="20260821", unsettled_in_one=True,
                    is_open=False, is_expired=True, trade_name="MSFT"),
            one_leg(trade_id="104", strike=390.0, qty=-2.0, open_price=18.93,
                    expiry_listed="20260821", unsettled_in_one=True,
                    is_open=False, is_expired=True, trade_name="MSFT"),
        ]
        self.assertEqual(one_reader.net_positions(legs), [])   # not a position
        [t] = one_reader.find_unsettled_trades(legs)
        self.assertEqual(t["trade_id"], "104")
        self.assertEqual(t["expiry"], "20260821")
        # long 2 @14.56 costs 2912; short 2 @18.93 keeps 3786
        self.assertAlmostEqual(t["pnl_if_worthless"], 874.0, places=2)

    def test_leg_closed_in_one_before_expiry_is_not_unsettled(self):
        settled = one_leg(is_open=False, is_expired=True,
                          unsettled_in_one=False)
        live = one_leg(is_open=True, is_expired=False)
        self.assertEqual(one_reader.find_unsettled_trades([settled, live]), [])

    def test_close_date_clears_the_unsettled_flag(self):
        row = ["", "", "SMSF", "104", "", "Sell", "2",
               "MSFT  260801P00390000", "31/07/2026", "Put", "",
               "MSFT", "18.93", "", ""]
        expired = one_reader.parse_leg_row(row)
        self.assertTrue(expired["unsettled_in_one"])
        row[14] = "22/08/2026 5:00:00 AM"          # CloseDate present
        self.assertFalse(one_reader.parse_leg_row(row)["unsettled_in_one"])

    def test_unsettled_expiry_breaks_the_all_clear(self):
        result = reconcile.reconcile_snapshots(
            {"captured_at": "now", "fills_today": [], "legs": []},
            {"source_file": "report.csv", "positions": [],
             "unsettled_trades": [{
                 "account": "A14+HV7", "trade_id": "104",
                 "trade_name": "MSFT", "underlying": "MSFT",
                 "expiry": "20260821", "pnl_if_worthless": 874.0,
                 "legs": [{"tradingClass": "MSFT", "expiry": "20260821",
                           "strike": 390.0, "right": "P", "qty": -2.0,
                           "open_price": 18.93}],
             }]},
            config(),
        )
        self.assertEqual(result["accounts"], {})       # nothing reconcilable
        [t] = result["unsettled_trades"]
        self.assertEqual(t["ibkr_account"], "U1")

    def test_ghost_trade_is_detected_from_zero_risk(self):
        legs = [{**one_leg(), "trade_id": "154"}]
        trades = [trade_row(trade_id="154", name="", margin=0.0)]
        [ghost] = one_reader.find_ghost_trades(trades, legs)
        self.assertEqual(ghost["trade_id"], "154")
        self.assertEqual(ghost["leg_count"], 1)
        self.assertIn("no trade name", " ".join(ghost["reasons"]))

    def test_healthy_and_legless_trades_are_not_ghosts(self):
        legs = [{**one_leg(), "trade_id": "153"}]
        self.assertEqual(one_reader.find_ghost_trades([
            trade_row(trade_id="153", margin=11535.36),      # real risk
            trade_row(trade_id="900", margin=0.0),           # zero risk, no legs
            trade_row(trade_id="901", margin=0.0, status="Closed"),
        ], legs), [])

    def test_unnamed_trade_with_real_risk_is_not_a_ghost(self):
        """A blank name alone is too weak -- a hand-built trade may be unnamed."""
        legs = [{**one_leg(), "trade_id": "153"}]
        self.assertEqual(one_reader.find_ghost_trades(
            [trade_row(trade_id="153", name="", margin=8953.5)], legs), [])

    def test_missing_risk_column_flags_nothing(self):
        legs = [{**one_leg(), "trade_id": "153"}]
        row = trade_row(trade_id="153", margin=0.0)
        row.update(margin=None, has_margin_column=False)
        self.assertEqual(one_reader.find_ghost_trades([row], legs), [])

    def test_ghost_trade_breaks_the_all_clear_even_when_legs_reconcile(self):
        """A ghost's legs are in the report, so they match -- the trade is still
        unmanageable in ONE and must not be reported as all-clear."""
        result = reconcile.reconcile_snapshots(
            {"captured_at": "now", "fills_today": [],
             "legs": [option(account="U1", qty=-1.0)]},
            {"source_file": "report.csv",
             "positions": [{**option(qty=-1.0), "avg_price": 10.0}],
             "ghost_trades": [{
                 "account": "A14+HV7", "trade_id": "154", "trade_name": "x",
                 "underlying": "SPX", "expiration": "20260807",
                 "open_date": "8/08/2026", "margin": 0.0, "leg_count": 1,
                 "reasons": ["ONE models no risk for it"],
                 "legs": [{"tradingClass": "SPXW", "expiry": "20260807",
                           "strike": 7400.0, "right": "P", "qty": -1.0,
                           "open_price": 10.0}],
             }]},
            config(),
        )
        self.assertEqual(result["accounts"]["U1"][0]["status"], "MATCH")
        [ghost] = result["ghost_trades"]
        self.assertEqual(ghost["ibkr_account"], "U1")

    def test_monthly_expiry_offset_does_not_mark_live_trade_closed(self):
        current = {
            **option(account="U1", qty=-1.0),
            "expiry": "20260820",
        }
        fill = {
            "account": "U1", "secType": "OPT", "underlying": "SPX",
            "tradingClass": "SPXW", "expiry": "20260820",
            "strike": 7400.0, "right": "P", "shares": 1.0,
            "side": "BOT", "price": 9.0, "time": "2026-07-28T10:00:00+00:00",
        }
        one_leg = {
            **option(account="A14+HV7", qty=-1.0),
            "expiry": "20260822", "expiry_listed": "20260821",
            "trade_id": "100", "trade_name": "Monthly",
        }
        activity = reconcile.classify_activity(
            [fill], [current],
            one_trades=[{
                "account": "A14+HV7", "trade_id": "100",
                "trade_name": "Monthly", "legs": [one_leg],
            }],
            account_map=config()["account_map"],
        )
        self.assertEqual(len(activity["by_trade"]), 1)
        self.assertNotEqual(
            activity["by_trade"][0]["status"], "TRADE_CLOSED")

    def test_adjustments_are_sorted_by_latest_execution_time(self):
        early = {
            "account": "U1", "secType": "OPT", "underlying": "SPX",
            "tradingClass": "SPXW", "expiry": "20260807",
            "strike": 7400.0, "right": "P", "shares": 1.0,
            "side": "BOT", "price": 9.0,
            "time": "2026-07-28T10:00:00+00:00",
        }
        late = {
            **early,
            "account": "U2",
            "strike": 7500.0,
            "time": "2026-07-28T11:00:00+00:00",
        }
        positions = [
            {**option(account="U1"), "strike": 7400.0},
            {**option(account="U2"), "strike": 7500.0},
        ]
        activity = reconcile.classify_activity([early, late], positions)
        self.assertEqual(
            [item["timestamp"] for item in activity["by_trade"]],
            [late["time"], early["time"]],
        )
        self.assertEqual(activity["opened"][0]["timestamp"], late["time"])

    def test_separate_executions_for_same_one_trade_are_not_merged(self):
        first = {
            "account": "U1", "secType": "OPT", "underlying": "SPX",
            "tradingClass": "SPXW", "expiry": "20260814",
            "strike": 7200.0, "right": "P", "shares": 2.0,
            "side": "BOT", "price": 24.6, "permId": 100,
            "time": "2026-07-30T19:24:50+00:00",
        }
        second = {
            **first, "expiry": "20260828", "strike": 7575.0,
            "shares": 1.0, "side": "SLD", "price": 173.88,
            "permId": 200, "time": "2026-07-30T19:32:11+00:00",
        }
        one_trade = {
            "account": "A14+HV7", "trade_id": "122",
            "trade_name": "14 AUG.A14.MGN.SPX - 2 LOTS",
            "legs": [
                {**option(), "expiry": "20260814", "strike": 7200.0},
                {**option(), "expiry": "20260828", "strike": 7575.0},
            ],
        }
        positions = [
            {**option(account="U1", qty=2.0), "expiry": "20260814",
             "strike": 7200.0},
            {**option(account="U1", qty=-1.0), "expiry": "20260828",
             "strike": 7575.0},
        ]
        activity = reconcile.classify_activity(
            [first, second], positions, one_trades=[one_trade],
            account_map={"A14+HV7": "U1"},
        )
        self.assertEqual(len(activity["by_trade"]), 2)
        self.assertEqual(
            [item["timestamp"] for item in activity["by_trade"]],
            [second["time"], first["time"]],
        )
        self.assertEqual(
            [item["trade_id"] for item in activity["by_trade"]],
            ["122", "122"],
        )


class OptionStratReconciliationTests(unittest.TestCase):
    def test_price_difference_makes_combo_unclean(self):
        one_combo = {
            "name": "ONE", "code": "MGN",
            "legs": [{
                "tradingClass": "SPXW", "expiry": "20260807",
                "right": "P", "strike": 7400.0, "qty": -2.0,
                "price": 10.0,
            }],
        }
        os_combo = {
            "name": "OS", "code": "MGN",
            "legs": [{
                "tradingClass": "SPXW", "expiry": "20260807",
                "right": "P", "strike": 7400.0, "qty": -2.0,
                "price": 10.5,
            }],
        }
        diff = recon_one_os.diff_pair(one_combo, os_combo, 0.10)
        self.assertFalse(diff["clean"])
        self.assertEqual(diff["legs"][0]["status"], "PRICE_DIFF")


class DashboardAdjustmentTests(unittest.TestCase):
    def test_adjustment_view_has_time_sort_and_composable_filters(self):
        timestamp = "2026-07-28T11:00:00+00:00"
        opened = {
            "account": "U1", "underlying": "SPX",
            "tradingClass": "SPXW", "expiry": "20260807",
            "strike": 7400.0, "right": "P", "label": "SPXW 2026-08-07 7400P",
            "qty": -1.0, "px": 10.0, "timestamp": timestamp,
            "one_trade_id": "117", "one_trade_name": "MGN.SPX.TEST",
        }
        activity = {
            "rolled": [], "opened": [opened], "closed": [], "changed": [],
            "by_trade": [{
                "account": "U1", "trade_id": "117",
                "trade_name": "MGN.SPX.TEST", "status": "ADJUSTED",
                "status_label": "ADJUSTED", "timestamp": timestamp,
                "underlying": "SPX", "rolled": [], "opened": [opened],
                "closed": [], "changed": [], "wizard_hint": "Update Trade #117",
            }],
        }
        with dashboard._lock:
            original = dict(dashboard._state)
            dashboard._state.update({
                "status": "ok", "result": {
                    "accounts": {}, "ignore_one_accounts": [],
                    "unmapped_one_groups": {}, "fills_today": [],
                },
                "error": None, "last_cycle": timestamp,
                "ibkr_connected": True, "one_file": "report.csv",
                "one_mtime": time.time(), "activity": activity,
                "account_codes": {"U1": "MGN"}, "os_strategies": [],
                "flex": {}, "naming": [],
            })
        try:
            page = dashboard.render_html()
        finally:
            with dashboard._lock:
                dashboard._state.clear()
                dashboard._state.update(original)
        self.assertIn("<option value='time' selected>", page)
        for control_id in (
            "adj-filter-ticker", "adj-filter-strike", "adj-filter-trade",
            "adj-filter-account-type", "adj-filter-account",
            "adj-filter-status",
        ):
            self.assertIn(f"id='{control_id}'", page)
        self.assertIn('"account_type": "MGN"', page)
        self.assertIn('"strikes": [7400.0]', page)
        self.assertIn('"timestamp_epoch":', page)
        self.assertIn("border:2px solid #1f6feb", page)
        self.assertIn("background:#1f6feb22", page)
        self.assertNotIn("background:#3a2d0a", page)


class FlexImportCompletenessTests(unittest.TestCase):
    @staticmethod
    def fill(*, exec_id="E1", qty=1.0):
        return {
            "account": "U1", "secType": "OPT", "underlying": "SPX",
            "tradingClass": "SPXW", "expiry": "20260821",
            "strike": 7400.0, "right": "P", "shares": abs(qty),
            "side": "BOT" if qty > 0 else "SLD", "price": 10.0,
            "time": "2026-08-20T15:00:00+00:00", "execId": exec_id,
        }

    @staticmethod
    def snapshot(qty, fills):
        legs = [] if qty == 0 else [{
            **option(account="U1", qty=qty), "expiry": "20260821",
        }]
        return {
            "captured_at": "2026-08-20T16:00:00+00:00",
            "managed_accounts": ["U1"], "legs": legs,
            "fills_today": fills,
        }

    def test_journal_persists_deduplicates_and_detects_missing_fill(self):
        now = datetime(2026, 8, 20, 16, tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "journal.json")
            _, checks = flex_export.update_execution_journal(
                self.snapshot(0, []), path, now)
            self.assertEqual(checks["U1"]["status"], flex_export.COMPLETE)

            fill = self.fill()
            merged, checks = flex_export.update_execution_journal(
                self.snapshot(1, [fill]), path, now)
            self.assertEqual(checks["U1"]["status"], flex_export.COMPLETE)
            self.assertEqual(len(merged["fills_today"]), 1)

            merged, _ = flex_export.update_execution_journal(
                self.snapshot(1, [fill]), path, now)
            self.assertEqual(len(merged["fills_today"]), 1)

            _, checks = flex_export.update_execution_journal(
                self.snapshot(2, [fill]), path, now)
            self.assertEqual(
                checks["U1"]["status"], flex_export.POSSIBLY_INCOMPLETE)
            self.assertEqual(
                checks["U1"]["discrepancies"][0]["unexplained_qty"], 1)

    def test_incomplete_import_is_quarantined(self):
        with tempfile.TemporaryDirectory() as tmp:
            rows = [["20260821,010000", "BUY", "OPT", "symbol", "1",
                     "10", "", "SPX", "-1000"]]
            normal = os.path.join(tmp, "ONEImport_U1.csv")
            flex_export._write_csv(normal, rows)
            paths = flex_export.save_account_files(
                {},
                {"U1": {"status": flex_export.POSSIBLY_INCOMPLETE}},
                tmp,
            )
            # The superseded file must not remain importable. It is deleted
            # rather than parked as .stale: it is regenerable from the
            # execution journal, and a leftover copy beside the current export
            # is what gets imported into ONE by mistake.
            self.assertFalse(os.path.exists(normal))
            self.assertFalse(os.path.exists(normal + ".stale"))
            self.assertIn(flex_export.POSSIBLY_INCOMPLETE, paths["U1"])

    def test_non_option_fills_remain_visible_but_are_not_exported(self):
        now = datetime(2026, 8, 20, 16, tzinfo=timezone.utc)
        stock_fill = {
            "account": "U1", "secType": "STK", "underlying": "SPY",
            "tradingClass": "SPY", "shares": 1, "side": "BOT",
            "price": 650, "time": "2026-08-20T15:00:00+00:00",
            "execId": "STOCK1",
        }
        with tempfile.TemporaryDirectory() as tmp:
            merged, _ = flex_export.update_execution_journal(
                self.snapshot(0, [stock_fill]),
                os.path.join(tmp, "journal.json"), now)
            self.assertEqual(merged["fills_today"], [stock_fill])
            rows, skipped = flex_export.generate(merged)
            self.assertEqual(dict(rows), {})
            self.assertEqual(skipped, 1)


class PortableEmailTests(unittest.TestCase):
    def test_report_has_no_localhost_dependency(self):
        ibkr = {"captured_at": "now", "legs": [
            {**option(account="U1")}], "fills_today": []}
        one = {"source_file": "missing-report.csv", "source_mtime": time.time(),
               "positions": [
            {**option(account="A14+HV7")}]}
        subject, body = email_report.generate_report_html(ibkr, one, config())
        self.assertIn("MATCH", subject)
        self.assertIn("self-contained", body)
        self.assertIn("OptionStrat reconciliation", body)
        self.assertNotIn("127.0.0.1", body)
        self.assertNotIn("localhost", body)
        self.assertNotIn("href=\"/", body)
        self.assertIn("id=\"report-controls\"", body)
        self.assertIn("id=\"report-account\"", body)
        self.assertIn("id=\"report-ticker\"", body)
        self.assertIn("id=\"report-strike\"", body)
        self.assertIn("id=\"report-trade\"", body)
        self.assertIn("id=\"report-status\"", body)
        self.assertIn("report-sortable", body)
        self.assertIn("function applyFilters()", body)

    def test_portable_report_adjustments_have_filter_facets(self):
        timestamp = "2026-07-30T19:24:50+00:00"
        opened = {
            "account": "U1", "underlying": "SPX",
            "tradingClass": "SPXW", "expiry": "20260814",
            "strike": 7200.0, "right": "P",
            "label": "SPXW 2026-08-14 7200P", "qty": 2.0,
            "px": 24.6, "timestamp": timestamp,
            "one_trade_id": "122", "one_trade_name": "MGN.SPX.TEST",
        }
        activity = {
            "rolled": [], "opened": [opened], "closed": [], "changed": [],
            "by_trade": [{
                "account": "U1", "trade_id": "122",
                "trade_name": "MGN.SPX.TEST", "status": "ADJUSTED",
                "status_label": "ADJUSTED", "timestamp": timestamp,
                "underlying": "SPX", "rolled": [], "opened": [opened],
                "closed": [], "changed": [], "wizard_hint": "Update Trade #122",
            }],
        }
        result = {
            "accounts": {"U1": []}, "unmapped_one_groups": {},
            "ignore_one_accounts": [], "activity": activity,
            "one_source_mtime": time.time(), "ibkr_source": timestamp,
        }
        cfg = config()
        cfg["account_codes"] = {"U1": "MGN"}
        _, body = email_report.generate_report_html(
            {"captured_at": timestamp, "legs": [], "fills_today": []},
            {"source_file": "missing-report.csv", "source_mtime": time.time(),
             "positions": []},
            cfg,
            reconciliation_result=result,
        )
        self.assertIn("class='report-filterable info adjustment-card'", body)
        self.assertIn("data-account='U1'", body)
        self.assertIn("data-account-type='MGN'", body)
        self.assertIn("data-ticker='SPX'", body)
        self.assertIn("data-strike='7200'", body)
        self.assertIn("data-trade='122'", body)
        self.assertIn("data-status='ADJUSTED'", body)
        message, _ = email_report.build_email_message(
            "subject", body, cfg, attachment_html=body)
        attachment_parts = [
            part for part in message.walk()
            if part.get_content_disposition() == "attachment"
        ]
        self.assertEqual(len(attachment_parts), 1)
        attached_html = attachment_parts[0].get_payload(
            decode=True).decode("utf-8")
        self.assertIn("function applyFilters()", attached_html)
        self.assertIn("id=\"report-adjustment-sort\"", attached_html)

    def test_message_contains_portable_html_attachment(self):
        message, target = email_report.build_email_message(
            "subject", "<html>report</html>", config(),
            attachment_html="<html>portable</html>",
        )
        self.assertEqual(target, "recipient@example.com")
        filenames = [part.get_filename() for part in message.walk()
                     if part.get_filename()]
        self.assertEqual(len(filenames), 1)
        self.assertTrue(filenames[0].startswith(
            "TWS_Matcher_Portable_Report_"))


if __name__ == "__main__":
    unittest.main()
