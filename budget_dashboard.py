"""EVBudget dashboard: local web app for importing, reviewing, and analyzing transactions.

Run this file (or launch_dashboard.bat). It opens the dashboard in your browser.

Tabs:
- Analytics: income and spending charts, filters, transaction editor, audit tool
- Import: load bank CSV files, review how each row was classified, and save

Everything runs on this computer. The server only accepts connections from
127.0.0.1, and your data stays in DataFiles/LocalStore/budget_store.db.
"""

import threading
import webbrowser
from http.server import HTTPServer

from evbudget import VERSION, api, http_util, paths, store


def make_server(start_port: int = 8060, app: "api.App" = None):
    """Bind to the first free port at or after start_port."""
    app = app or api.App()
    router = app.build_router()
    for port in range(start_port, start_port + 50):
        handler = http_util.make_handler(router, {f"127.0.0.1:{port}", f"localhost:{port}"})
        try:
            return HTTPServer(("127.0.0.1", port), handler), port
        except OSError:
            continue
    raise RuntimeError(f"No free port found in {start_port}-{start_port + 49}")


def main() -> None:
    report = store.ensure_schema()
    store.sync_master_csv()
    httpd, port = make_server(8060)
    url = f"http://127.0.0.1:{port}/"

    print("=" * 65)
    print(f" EVBudget Dashboard (v{VERSION:.1f})")
    print("=" * 65)
    print(f" * Web Interface:  {url}")
    print(f" * Database:       {paths.DB_PATH}")
    if report.get("action") == "migrated":
        print(f" * Database upgraded to schema v{report['version']}. Backup: {report['backup']}")
    print(" Press Ctrl+C in this window to stop the server.\n")

    threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping dashboard server.")
        httpd.server_close()


if __name__ == "__main__":
    main()
