# Estate sales near home

Pulls the Portland-area listings from [Estate Sale Finder](https://www.estatesale-finder.com/all_sales_map.php),
works out how far each one is from home, and serves a phone-friendly page plus JSON.

## Run on the NUC

Copy this folder to the NUC, then create your settings file and start it:

```sh
cp config.example.json config.json   # then set home_lat / home_lng
docker compose up -d --build
```

`config.json` is not kept in git (it holds your home coordinates). Create it before the first
`docker compose up`, or Docker will make an empty folder with that name.

Open `http://<nuc-address>:8787/`.

## Run without Docker

```sh
cp config.example.json config.json   # first time only
python3 app/server.py                # needs Python 3.9+, no packages to install
```

## Settings (`config.json`)

| Key | Meaning |
|---|---|
| `home_lat`, `home_lng` | Home coordinates used for distances |
| `nearby_radius_miles` | What counts as "Nearby" (default 3) |
| `refresh_hours` | How often to re-fetch the listings (default 6) |
| `timezone` | Used to decide what "today" and "this weekend" are |
| `port` | Port the server listens on (if you change it, change `docker-compose.yml` too) |

Changes to the radius, coordinates and timezone apply the next time the page or JSON is loaded; no restart
needed. A new `refresh_hours` is picked up at the next scheduled check, and `port` needs a restart
(`docker compose restart`).

If a scheduled fetch fails, it is retried every 15 minutes until it works; the last good data stays up meanwhile.

## What it serves

| Path | What |
|---|---|
| `/` | The page: Nearby, Rest of town, Coming up, and recurring shops/markets/auctions |
| `/sales.json` | Every listing: address, distance, dates, hours per day, photo URLs, links |
| `/api/summary` | Sales open between today and Sunday, nearest first, with counts |
| `POST /api/refresh` | Fetch again now (limited to once every 5 minutes) |

Distances are straight-line, not driving distance. Photos load directly from estatesale-finder.com.

## Home Assistant

```yaml
rest:
  - resource: http://<nuc-address>:8787/api/summary
    scan_interval: 1800
    sensor:
      - name: Estate sales nearby
        value_template: "{{ value_json.nearby_count }}"
        json_attributes:
          - nearby
          - farther
          - total_count
          - window
          - generated_at
```

The sensor's state is the number of sales within the radius through Sunday; the `nearby` attribute lists
each one with its address, hours, listing link and a directions link.
