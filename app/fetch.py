"""Fetch estate sale listings and write sales.json.

Source: the marker feed behind estatesale-finder.com's map, plus each sale's
detail page for dates, hours and photos. Standard library only.
"""

import html
import json
import math
import os
import re
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone

BASE = "https://www.estatesale-finder.com"
FEED_URL = BASE + "/map_gen_xml_test.php"
SALE_URL = BASE + "/viewsale.php?saleid={id}"
USER_AGENT = "estate-sales-nearby/1.0 (personal use)"
REQUEST_PAUSE_SECONDS = 1.0

WEEKDAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
CATEGORY_BY_TYPE = {"1": "estate", "10": "auction"}


def http_get(url, attempts=3):
    last_error = None
    for attempt in range(attempts):
        try:
            request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(request, timeout=30) as response:
                return response.read().decode("utf-8", errors="replace")
        except Exception as error:  # network errors, timeouts, HTTP errors
            last_error = error
            time.sleep(2 * (attempt + 1))
    raise RuntimeError(f"GET {url} failed: {last_error}")


def to_text(fragment):
    """Turn an HTML fragment into plain text with sensible line breaks."""
    fragment = re.sub(r"<!--.*?-->", "", fragment, flags=re.S)
    fragment = re.sub(r"</?br\s*/?>|</p>|</li>|</div>", "\n", fragment, flags=re.I)
    fragment = re.sub(r"<[^>]+>", "", fragment)
    text = html.unescape(fragment).replace("\xa0", " ")
    lines = [re.sub(r"[ \t]+", " ", line).strip() for line in text.split("\n")]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


def miles_between(lat1, lng1, lat2, lng2):
    """Straight-line (great circle) distance in miles."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    a = (math.sin((p2 - p1) / 2) ** 2
         + math.cos(p1) * math.cos(p2) * math.sin(math.radians(lng2 - lng1) / 2) ** 2)
    return 3958.8 * 2 * math.asin(math.sqrt(a))


def parse_feed(xml_text):
    sales = []
    for marker in ET.fromstring(xml_text).iter("marker"):
        try:
            lat, lng = float(marker.get("lat", "")), float(marker.get("lng", ""))
        except (TypeError, ValueError):
            continue
        sale_id = (marker.get("id") or "").strip()
        if not sale_id.isdigit():
            continue
        type_code = (marker.get("type") or "").strip()
        sales.append({
            "id": sale_id,
            "type_code": type_code,
            "category": CATEGORY_BY_TYPE.get(type_code, "other"),
            "title": to_text(marker.get("description") or "") or "Sale",
            "address": (marker.get("address") or "").strip(),
            "city": (marker.get("city") or "").strip(),
            "state": (marker.get("state") or "").strip(),
            "zip": (marker.get("zip") or "").strip(),
            "lat": lat,
            "lng": lng,
        })
    return sales


def _field(page, label):
    """Inner HTML of the value column that follows a bold row label."""
    pattern = (r"<p><strong>\s*" + re.escape(label) + r"\s*</strong></p>\s*</div>\s*"
               r'<div class="small-9 columns[^"]*">(.*?)</div>')
    match = re.search(pattern, page, flags=re.S)
    return match.group(1) if match else ""


def _parse_when(day, month, year, clock):
    try:
        return datetime.strptime(f"{day} {month} {year} {clock}", "%d %B %Y %I:%M %p")
    except ValueError:
        return None


def parse_detail(page):
    """Extract title, organizer, dates, hours, description and photos."""
    detail = {}

    title = re.search(r'<h3 class="text-center darkbg">(.*?)</h3>', page, flags=re.S)
    if title:
        detail["title"] = to_text(title.group(1))

    head = page.split("<strong>Address</strong>")[0]
    organizer = re.search(r"<h4>(.*?)</h4>", head[head.find("salelist"):], flags=re.S)
    if organizer:
        name = to_text(organizer.group(1))
        name = re.sub(r"^Professionally Run (Sale )?by\s*", "", name, flags=re.I).strip()
        if name:
            detail["organizer"] = name

    region = re.search(r"Region:\s*([^<]+)", page)
    if region:
        detail["region"] = html.unescape(region.group(1)).strip()

    dates_html = _field(page, "Dates")
    stamps = re.findall(
        r"(\d{1,2})(?:st|nd|rd|th) of (\w+) (\d{4}) (\d{1,2}:\d{2} [AP]M)", dates_html)
    start = _parse_when(*stamps[0]) if len(stamps) >= 1 else None
    end = _parse_when(*stamps[1]) if len(stamps) >= 2 else start
    detail["recurring"] = start is None
    detail["start"] = start.isoformat(timespec="minutes") if start else None
    detail["end"] = end.isoformat(timespec="minutes") if end else None

    # Hours are listed one line per day, in order from the first day.
    days = []
    if start:
        cursor = start.date()
        last = end.date() if end else None
        for line in to_text(dates_html).split("\n"):
            match = re.match(r"(%s)\s*:\s*(.*)$" % "|".join(WEEKDAYS), line.strip())
            if not match:
                continue
            for _ in range(7):
                if WEEKDAYS[cursor.weekday()] == match.group(1):
                    break
                cursor += timedelta(days=1)
            if last and cursor > last:
                break
            days.append({
                "date": cursor.isoformat(),
                "weekday": match.group(1),
                "hours": match.group(2).strip(),
            })
            cursor += timedelta(days=1)
    detail["days"] = days

    detail["directions_note"] = to_text(_field(page, "Directions"))
    detail["description"] = to_text(_field(page, "Sale Details"))

    photos, seen = [], set()
    for tag in re.findall(r"<a\b[^>]*>", page):
        if 'data-fancybox-group="gallery"' not in tag:
            continue
        href = re.search(r'href="([^"]+)"', tag)
        if not href:
            continue
        path = html.unescape(href.group(1)).split("?")[0].lstrip("/")
        if path in seen or not re.match(r"useruploads/[\w./-]+$", path) or ".." in path:
            continue
        seen.add(path)
        folder, _, name = path.rpartition("/")
        photos.append({
            "full": f"{BASE}/{path}",
            "thumb": f"{BASE}/{folder}/thumb_{name}",
        })
    detail["photos"] = photos
    return detail


def apply_home(document, config):
    """Set each sale's distance and nearby flag from the settings, nearest first."""
    radius = float(config["nearby_radius_miles"])
    home_lat, home_lng = float(config["home_lat"]), float(config["home_lng"])
    for sale in document["sales"]:
        sale["distance_miles"] = round(
            miles_between(home_lat, home_lng, sale["lat"], sale["lng"]), 1)
        sale["nearby"] = sale["distance_miles"] <= radius
    document["sales"].sort(key=lambda s: s["distance_miles"])
    document["nearby_radius_miles"] = radius
    return document


def build(config, previous=None, log=print):
    """Fetch everything and return the sales.json document."""
    previous_by_id = {s["id"]: s for s in (previous or {}).get("sales", [])}
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")

    sales = parse_feed(http_get(FEED_URL))
    log(f"feed: {len(sales)} listings")
    if not sales and previous_by_id:
        # An empty feed is far more likely a fault at the source than every sale
        # ending at once; failing here keeps the last good file and its history.
        raise RuntimeError(
            f"feed returned no listings (had {len(previous_by_id)}); keeping the last good data")

    for sale in sales:
        old = previous_by_id.get(sale["id"])
        time.sleep(REQUEST_PAUSE_SECONDS)
        try:
            detail = parse_detail(http_get(SALE_URL.format(id=sale["id"])))
        except Exception as error:
            log(f"sale {sale['id']}: detail page failed ({error}); keeping last known details")
            detail = {k: old[k] for k in (
                "title", "organizer", "region", "recurring", "start", "end", "days",
                "directions_note", "description", "photos") if k in old} if old else {}
            detail["detail_stale"] = True
        sale.update({k: v for k, v in detail.items() if v not in ("", None) or k in ("start", "end")})
        sale.setdefault("recurring", True)
        sale.setdefault("start", None)
        sale.setdefault("end", None)
        sale.setdefault("days", [])
        sale.setdefault("photos", [])

        # The feed sometimes puts the zip code in the city field.
        if sale["city"].isdigit():
            sale["city"] = sale.get("region", "")

        sale["photo_count"] = len(sale["photos"])
        sale["url"] = SALE_URL.format(id=sale["id"])
        place = ", ".join(p for p in (sale["address"], sale["city"], sale["state"]) if p)
        sale["full_address"] = (place + " " + sale["zip"]).strip()
        sale["maps_url"] = ("https://www.google.com/maps/dir/?api=1&destination="
                            + urllib.parse.quote(sale["full_address"]))
        # first_seen stays empty on the very first run so nothing is flagged as new.
        sale["first_seen"] = old.get("first_seen") if old else (now if previous else None)

    return apply_home({
        "generated_at": now,
        "source": BASE + "/all_sales_map.php",
        "sales": sales,
    }, config)


def load_json(path):
    try:
        with open(path, encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return None


def refresh(config, data_dir, log=print):
    """Rebuild sales.json in data_dir. Leaves the old file in place on failure."""
    os.makedirs(data_dir, exist_ok=True)
    target = os.path.join(data_dir, "sales.json")
    document = build(config, previous=load_json(target), log=log)
    temp = target + ".tmp"
    with open(temp, "w", encoding="utf-8") as handle:
        json.dump(document, handle, indent=2, ensure_ascii=False)
    os.replace(temp, target)
    log(f"wrote {target}: {len(document['sales'])} sales")
    return document


if __name__ == "__main__":
    here = os.path.dirname(os.path.abspath(__file__))
    config_path = os.environ.get("CONFIG", os.path.join(here, "..", "config.json"))
    refresh(load_json(config_path), os.environ.get("DATA_DIR", os.path.join(here, "..", "data")))
