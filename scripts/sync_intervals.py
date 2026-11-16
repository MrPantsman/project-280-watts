#!/usr/bin/env python3
"""Pull training and wellness from Intervals.icu and write data/intervals.json.

Output shape (what the Fuel dashboards read): {"planned": [...], "completed": [...], "Wellness": [...]}

Only the fields the dashboards use are kept. That keeps the public file small and
leaves out ride descriptions, device names, GPS-related fields and similar.

Environment:
  INTERVALS_API_KEY   required (GitHub secret)
  INTERVALS_ATHLETE_ID  default "0" (means "the owner of the key")
  OUT_PATH            default data/intervals.json
Test options:
  INTERVALS_BASE_URL  default https://intervals.icu
  --from-file FILE    trim an existing full export instead of calling the API
"""
import base64
import datetime as dt
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

BASE = os.environ.get("INTERVALS_BASE_URL", "https://intervals.icu").rstrip("/")
ATHLETE = os.environ.get("INTERVALS_ATHLETE_ID", "0")
OUT = os.environ.get("OUT_PATH", "data/intervals.json")

DAYS_BACK_ACTIVITIES = 70   # completed sessions
DAYS_BACK_EVENTS = 70       # planned sessions
DAYS_AHEAD_EVENTS = 14      # planned sessions still to come
DAYS_BACK_WELLNESS = 84     # weight / body fat / food

# Fields kept. Add a name here if a dashboard ever needs it.
ACTIVITY_KEYS = [
    "id", "name", "type", "start_date_local", "moving_time", "icu_joules",
    "icu_training_load", "icu_intensity", "icu_zone_times", "icu_ftp",
    "icu_weight", "icu_average_watts", "carbs_ingested", "carbs_used",
    "calories", "paired_event_id",
]
EVENT_KEYS = [
    "id", "name", "type", "category", "start_date_local", "moving_time",
    "time_target", "joules", "icu_training_load", "icu_intensity",
    "carbs_used", "paired_activity_id",
]
WORKOUT_DOC_KEYS = ["duration", "average_watts", "zoneTimes"]
WELLNESS_KEYS = [
    "id", "weight", "bodyFat", "kcalConsumed", "carbohydrates", "protein", "fatTotal",
]


def pick(src, keys):
    return {k: src[k] for k in keys if k in src and src[k] is not None}


def trim_activity(a):
    return pick(a, ACTIVITY_KEYS)


def trim_event(e):
    out = pick(e, EVENT_KEYS)
    doc = e.get("workout_doc")
    if isinstance(doc, dict):
        d = pick(doc, WORKOUT_DOC_KEYS)
        if d:
            out["workout_doc"] = d
    return out


def trim_wellness(w):
    return pick(w, WELLNESS_KEYS)


def day(e):
    return str(e.get("start_date_local") or e.get("id") or "")[:10]


def trim_all(planned, completed, wellness):
    return {
        "planned": sorted((trim_event(e) for e in planned if isinstance(e, dict)),
                          key=lambda e: (day(e), str(e.get("id", "")))),
        "completed": sorted((trim_activity(a) for a in completed if isinstance(a, dict)),
                            key=lambda a: (day(a), str(a.get("id", "")))),
        "Wellness": sorted((trim_wellness(w) for w in wellness if isinstance(w, dict)),
                           key=lambda w: str(w.get("id", ""))),
    }


def get(path, params, auth):
    url = "%s/api/v1/athlete/%s/%s?%s" % (BASE, ATHLETE, path, urllib.parse.urlencode(params))
    req = urllib.request.Request(url, headers={
        "Authorization": auth,
        "Accept": "application/json",
        # Python's default User-Agent is often rejected (403) by sites behind Cloudflare.
        "User-Agent": "intervals-sync-dashboard/1.0 (personal fuel dashboard)",
    })
    last = "unknown error"
    for attempt in range(4):
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            if e.code in (429, 500, 502, 503, 504):
                last = "HTTP %d" % e.code
                time.sleep(3 * (attempt + 1))
                continue
            try:
                snippet = e.read(300).decode("utf-8", "replace").replace("\n", " ").strip()
            except Exception:
                snippet = ""
            sys.exit("Intervals %s request failed: HTTP %d. 401 = API key secret wrong; "
                     "403 = request blocked or key lacks access. Reply said: %s" % (path, e.code, snippet or "(empty)"))
        except (urllib.error.URLError, TimeoutError) as e:
            last = str(e)
            time.sleep(3 * (attempt + 1))
    sys.exit("Intervals %s request failed after retries: %s" % (path, last))


def main():
    if "--from-file" in sys.argv:
        src = json.load(open(sys.argv[sys.argv.index("--from-file") + 1]))
        data = trim_all(src.get("planned", []), src.get("completed", []),
                        src.get("Wellness") or src.get("wellness") or [])
    else:
        key = os.environ.get("INTERVALS_API_KEY", "").strip()
        if not key:
            sys.exit("INTERVALS_API_KEY is not set. Add it under Settings > Secrets and variables > Actions.")
        auth = "Basic " + base64.b64encode(("API_KEY:" + key).encode()).decode()
        today = dt.datetime.now(dt.timezone.utc).date()
        iso = lambda d: d.isoformat()
        acts = get("activities", {"oldest": iso(today - dt.timedelta(days=DAYS_BACK_ACTIVITIES)),
                                  "newest": iso(today + dt.timedelta(days=1))}, auth)
        evs = get("events", {"oldest": iso(today - dt.timedelta(days=DAYS_BACK_EVENTS)),
                             "newest": iso(today + dt.timedelta(days=DAYS_AHEAD_EVENTS))}, auth)
        well = get("wellness", {"oldest": iso(today - dt.timedelta(days=DAYS_BACK_WELLNESS)),
                                "newest": iso(today + dt.timedelta(days=1))}, auth)
        for name, val in (("activities", acts), ("events", evs), ("wellness", well)):
            if not isinstance(val, list):
                sys.exit("Unexpected reply for %s (not a list); leaving %s unchanged." % (name, OUT))
        if not well:
            sys.exit("Intervals returned no wellness records; leaving %s unchanged." % OUT)
        data = trim_all(evs, acts, well)

    text = json.dumps(data, separators=(",", ":"), ensure_ascii=False) + "\n"
    old = None
    try:
        with open(OUT, encoding="utf-8") as f:
            old = f.read()
    except FileNotFoundError:
        pass
    if old == text:
        print("No change.")
        return
    os.makedirs(os.path.dirname(OUT) or ".", exist_ok=True)
    tmp = OUT + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
    os.replace(tmp, OUT)
    print("Wrote %s: %d planned, %d completed, %d wellness (%d bytes)" % (
        OUT, len(data["planned"]), len(data["completed"]), len(data["Wellness"]), len(text)))


if __name__ == "__main__":
    main()
