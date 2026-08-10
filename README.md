# TWS Matcher

Local Windows service for reconciling options positions across Interactive
Brokers TWS, OptionNet Explorer (ONE), and OptionStrat.

IBKR is treated as the source of truth. The dashboard compares live TWS
positions with the newest ONE Summary or Detail Report and compares ONE
strategies with OptionStrat's `all-active` workbook.

## Setup

1. Install Python 3.11 or newer.
2. Install dependencies:

   ```powershell
   pip install -r requirements.txt
   ```

3. Copy `config.example.json` to `config.json` and add your ONE-to-IBKR account
   mappings.
4. In `canonical_engine.py`, confirm the TWS host, port, and dedicated client
   ID. The defaults are `127.0.0.1`, live TWS port `7496`, and client ID `17`.
5. In TWS, enable socket clients and trust `127.0.0.1`.
6. Export ONE's open-position Summary or Detail Report as CSV and OptionStrat's
   `all-active` report as XLSX to Downloads or Documents.

## Run

Double-click `launch_dashboard.bat`, or run:

```powershell
python dashboard.py
```

Open <http://127.0.0.1:8787/>. The service refreshes automatically and also
supports an on-demand **Check now** action.

The dashboard address is intentionally local to the trading laptop. **Email
Report** sends a frozen, self-contained reconciliation in the email body and as
an attached HTML file; either can be opened on another computer without TWS,
ONE, or access to the local dashboard.

ONE report-order prefixes such as `1.A14+HV7` are normalized automatically.
Broker-only stock and ETF holdings are shown as expected information because
ONE models the option strategies, while option identity/quantity differences
and current-fill price drift remain actionable.

Generated snapshots, account configuration, broker exports, and Flex files are
excluded from Git because they contain private trading data.
