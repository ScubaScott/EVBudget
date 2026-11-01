"""EVBudget dashboard: local web app for importing, reviewing, and analyzing transactions.

Run this file (or launch_dashboard.bat). It opens the dashboard in your browser.

Tabs:
- Envelopes: how full each envelope is, paycheck fills, transfers, bills due
- Spending: income and spending charts, filters, transaction editor, audit tool
- Income: income by person over time, pay sources, planning income
- Budget: envelope budgets and suggestions, bills, fill templates, go-live setup
- Import: load bank CSV files, review how each row was classified, and save

It opens in its own app-style window (Chrome, Edge or Brave, no address bar) when one is installed, and in your
normal browser otherwise. Closing the window (or the Quit button) stops the server.

Options (for troubleshooting):
  --no-app-window   open in the normal browser instead (or set EVBUDGET_NO_APP_WINDOW=1)
  --keep-running    leave the server running after the page closes

Each person keeps their own budget (a profile) in DataFiles/Profiles/<name>/. The page opens on a budget
picker; rules that everyone shares live in DataFiles/Shared/.

Everything runs on this computer. The server only accepts connections from 127.0.0.1.
"""

import sys
import threading
import time
from http.server import HTTPServer

from evbudget import VERSION, api, browser, http_util, paths, profiles, updater


def make_server(start_port: int = 8060, app: "api.App" = None, take_over_port: int = None):
    """Bind to the first free port at or after start_port. The app is available as server.app.

    take_over_port: a restarted copy waits a few seconds for the old copy to let go of its port, so the
    window that is already open can reload into the new server at the same address."""
    app = app or api.App(profiles=profiles.Profiles())
    router = app.build_router()
    if take_over_port:
        handler = http_util.make_handler(router, {f"127.0.0.1:{take_over_port}", f"localhost:{take_over_port}"})
        for _ in range(40):
            try:
                httpd = HTTPServer(("127.0.0.1", take_over_port), handler)
            except OSError:
                time.sleep(0.25)
                continue
            return _ready(httpd, app, take_over_port)
    for port in range(start_port, start_port + 50):
        handler = http_util.make_handler(router, {f"127.0.0.1:{port}", f"localhost:{port}"})
        try:
            httpd = HTTPServer(("127.0.0.1", port), handler)
        except OSError:
            continue
        return _ready(httpd, app, port)
    raise RuntimeError(f"No free port found in {start_port}-{start_port + 49}")


def _ready(httpd, app, port: int):
    httpd.app = app
    app.port = port
    # shutdown() waits for serve_forever to finish its current request, so it can't be called from that request.
    app.request_shutdown = lambda: threading.Thread(target=httpd.shutdown, daemon=True).start()
    return httpd, port


def stop_if_never_opened(app: "api.App", seconds: float = 90.0) -> None:
    """A restarted copy has no window of its own. If the old window is gone and nothing reconnects, stop."""
    time.sleep(seconds)
    if not app._page_ever_seen:
        print("\nNo page reconnected after the restart. Stopping.")
        app.request_shutdown()


def stop_when_pages_close(app: "api.App", interval: float = 3.0) -> None:
    """Stop the server once the browser page(s) have been closed. Runs in a background thread."""
    while True:
        time.sleep(interval)
        if app.should_exit():
            print("\nThe page was closed. Stopping the dashboard server.")
            app.request_shutdown()
            return


def main() -> None:
    keep_running = "--keep-running" in sys.argv
    app_browser = None if browser.app_window_disabled(sys.argv) else browser.find_app_browser()
    took_over = updater.restart_port(sys.argv)
    httpd, port = make_server(8060, take_over_port=took_over)
    restarted_in_place = bool(took_over) and port == took_over
    url = f"http://127.0.0.1:{port}/"

    print("=" * 65)
    print(f" EVBudget Dashboard (v{VERSION:.1f})")
    print("=" * 65)
    print(f" * Web Interface:  {url}")
    print(f" * Budgets:        {paths.PROFILES_DIR}")
    print(f" * Opens in:       {app_browser[0] + ' app window' if app_browser else 'your default browser'}")
    if keep_running:
        print(" Press Ctrl+C in this window to stop the server (it keeps running when the page closes).\n")
    else:
        print(" Close the page or use Quit to stop the server. Ctrl+C in this window also works.\n")
        threading.Thread(target=stop_when_pages_close, args=(httpd.app,), daemon=True).start()

    if restarted_in_place:
        # The window that asked for the restart reloads itself into this server, so don't open another
        threading.Thread(target=stop_if_never_opened, args=(httpd.app,), daemon=True).start()
    else:
        threading.Timer(1.0, lambda: browser.launch(url, app_browser)).start()
    httpd.app.updater.start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping dashboard server.")
    finally:
        httpd.server_close()


if __name__ == "__main__":
    main()
