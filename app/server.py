"""Serve the estate sales page and JSON, and keep the data fresh.

Routes:
  /              the page
  /sales.json    everything, one entry per sale
  /api/summary   small summary for Home Assistant (this weekend, nearby first)
  /api/refresh   POST: fetch again now (at most once every few minutes)
"""

import json
import os
import threading
import time
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from zoneinfo import ZoneInfo

import fetch

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.environ.get("CONFIG", os.path.join(HERE, "..", "config.json"))
DATA_DIR = os.environ.get("DATA_DIR", os.path.join(HERE, "..", "data"))
SALES_PATH = os.path.join(DATA_DIR, "sales.json")
PAGE_PATH = os.path.join(HERE, "static", "index.html")
MANUAL_REFRESH_GAP_SECONDS = 300
RETRY_SECONDS = 900  # wait after a failed scheduled refresh before trying again

state = {"last_attempt": 0.0, "last_error": None}
refresh_lock = threading.Lock()


def log(message):
    print(f"{datetime.now().isoformat(timespec='seconds')} {message}", flush=True)


def load_config():
    config = fetch.load_json(CONFIG_PATH)
    if not config:
        raise SystemExit(f"Could not read config at {CONFIG_PATH}")
    config.setdefault("nearby_radius_miles", 3)
    config.setdefault("refresh_hours", 6)
    config.setdefault("timezone", "America/Los_Angeles")
    config.setdefault("port", 8787)
    return config


def do_refresh():
    """Run one fetch. Returns False if another one is already running."""
    if not refresh_lock.acquire(blocking=False):
        return False
    try:
        state["last_attempt"] = time.time()
        fetch.refresh(load_config(), DATA_DIR, log=log)
        state["last_error"] = None
    except (Exception, SystemExit) as error:  # SystemExit: config unreadable
        state["last_error"] = f"{type(error).__name__}: {error}"
        log(f"refresh failed: {state['last_error']}")
    finally:
        refresh_lock.release()
    return True


def refresh_once_if_due():
    """Refresh if the data is old enough. Returns seconds until the next check."""
    config = load_config()
    interval = max(0.5, float(config["refresh_hours"])) * 3600
    try:
        age = time.time() - os.path.getmtime(SALES_PATH)
    except OSError:
        age = interval
    if age < interval:
        return max(60, interval - age)
    if do_refresh() and state["last_error"] is None:
        return interval
    # Failed, or a manual refresh was already running: look again soon.
    return RETRY_SECONDS


def refresh_loop():
    while True:
        try:
            delay = refresh_once_if_due()
        except (Exception, SystemExit) as error:  # e.g. config.json caught mid-edit
            log(f"refresh loop: {type(error).__name__}: {error}; retrying in "
                f"{RETRY_SECONDS // 60} min")
            delay = RETRY_SECONDS
        time.sleep(delay)


def load_document():
    """sales.json with distances and the nearby flag worked out from the current settings."""
    document = fetch.load_json(SALES_PATH)
    if document is not None:
        try:
            fetch.apply_home(document, load_config())
        except (Exception, SystemExit):
            pass  # settings unreadable right now: serve the values stored at fetch time
    return document


def summarize(document, config):
    """What is on between today and Sunday, nearest first."""
    today = datetime.now(ZoneInfo(config["timezone"])).date()
    sunday = today + timedelta(days=(6 - today.weekday()))
    window = []
    for sale in document.get("sales", []):
        if sale.get("recurring") or not sale.get("start"):
            continue
        start, end = sale["start"][:10], (sale.get("end") or sale["start"])[:10]
        if end < today.isoformat() or start > sunday.isoformat():
            continue
        hours = {d["date"]: d["hours"] for d in sale.get("days", [])}
        window.append({
            "id": sale["id"],
            "title": sale["title"],
            "address": sale["full_address"],
            "distance_miles": sale["distance_miles"],
            "nearby": sale["nearby"],
            "open_today": start <= today.isoformat() <= end,
            "hours_today": hours.get(today.isoformat()),
            "days": [d for d in sale.get("days", [])
                     if today.isoformat() <= d["date"] <= sunday.isoformat()],
            "photo_count": sale.get("photo_count", 0),
            "photo": sale["photos"][0]["thumb"] if sale.get("photos") else None,
            "url": sale["url"],
            "maps_url": sale["maps_url"],
        })
    nearby = [s for s in window if s["nearby"]]
    return {
        "generated_at": document.get("generated_at"),
        "last_error": state["last_error"],
        "window": {"from": today.isoformat(), "to": sunday.isoformat()},
        "nearby_radius_miles": document.get("nearby_radius_miles"),
        "nearby_count": len(nearby),
        "total_count": len(window),
        "nearest": window[0] if window else None,
        "nearby": nearby,
        "farther": [s for s in window if not s["nearby"]],
    }


class Handler(BaseHTTPRequestHandler):
    server_version = "EstateSales/1.0"

    def log_message(self, *args):
        pass

    def send_body(self, status, content_type, body):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def send_json(self, status, payload):
        self.send_body(status, "application/json; charset=utf-8",
                       json.dumps(payload, ensure_ascii=False).encode("utf-8"))

    def do_GET(self):
        path = self.path.split("?")[0]
        if path in ("/", "/index.html"):
            with open(PAGE_PATH, "rb") as handle:
                self.send_body(200, "text/html; charset=utf-8", handle.read())
        elif path == "/sales.json":
            document = load_document()
            if document is None:
                self.send_json(503, {"error": "No data yet; the first fetch is still running.",
                                     "last_error": state["last_error"]})
            else:
                document["last_error"] = state["last_error"]
                self.send_json(200, document)
        elif path == "/api/summary":
            document = load_document()
            if document is None:
                self.send_json(503, {"error": "No data yet.", "last_error": state["last_error"]})
            else:
                self.send_json(200, summarize(document, load_config()))
        else:
            self.send_json(404, {"error": "Not found"})

    do_HEAD = do_GET

    def do_POST(self):
        if self.path.split("?")[0] != "/api/refresh":
            self.send_json(404, {"error": "Not found"})
            return
        wait = MANUAL_REFRESH_GAP_SECONDS - (time.time() - state["last_attempt"])
        if wait > 0:
            self.send_json(429, {"error": f"Refreshed recently; try again in {int(wait // 60) + 1} min."})
        elif not do_refresh():
            self.send_json(409, {"error": "A refresh is already running."})
        elif state["last_error"]:
            self.send_json(502, {"error": state["last_error"]})
        else:
            self.send_json(200, {"ok": True})


def main():
    config = load_config()
    threading.Thread(target=refresh_loop, daemon=True).start()
    server = ThreadingHTTPServer(("0.0.0.0", int(config["port"])), Handler)
    log(f"serving on port {config['port']}")
    server.serve_forever()


if __name__ == "__main__":
    main()
