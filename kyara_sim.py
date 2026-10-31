import json
import os
import random
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

PACK_INTERVAL = 600
MAX_PACKS = 10

# Packs are opened in random batches: wait a random 10-100 minutes, then open
# everything that has built up (1 pack after 10 min, 10 after 100 min).
BATCH_MIN_MINUTES = 10
BATCH_MAX_MINUTES = 100

SKIP_CHANCE = 0.1  # chance an opening is skipped; the packs wait for the next one
LONG_PAUSE_CHANCE = 0.002  # rolled every loop (30-120 s): about one 6-7 h pause a day

# Night rule, in local time. GitHub's servers run on UTC, so the zone is explicit.
LOCAL_TZ = timezone(timedelta(hours=1))  # Tunisia = UTC+1, no daylight saving
STOP_HOUR = 0  # 12 AM
RESUME_HOUR = 7
WAKE_JITTER_MINUTES = 30  # wakes up at a random time between 7:00 and 7:30
NIGHT_CONTINUE_CHANCE = 0.05  # on 5% of nights it doesn't stop

# A GitHub job is killed after 6 hours, so the workflow sets MAX_RUN_HOURS and
# the script exits cleanly before that. Not set (e.g. on your PC) = runs forever.
MAX_RUN_HOURS = float(os.environ.get("MAX_RUN_HOURS", "0"))

OPEN_URL = "https://kyara.games/api/packs/all/open"
OPEN_BODY = {"maxChakra": 0}  # JSON sent with the request (copied from the browser)
MAX_FAILURES = 3  # stop after this many failed openings in a row

# The token and device id come from environment variables (GitHub secrets),
# never from this file: in a public repo anyone could read them.
KYARA_TOKEN = os.environ.get("KYARA_TOKEN", "").strip()
if KYARA_TOKEN.lower().startswith("bearer "):
    KYARA_TOKEN = KYARA_TOKEN[7:].strip()
KYARA_DEVICE_ID = os.environ.get("KYARA_DEVICE_ID", "").strip()
LIVE = bool(KYARA_TOKEN)  # no token = simulation only, nothing is sent
TEST_OPEN_NOW = os.environ.get("TEST_OPEN_NOW") == "1"  # one real request, then stop

# Push notifications through ntfy. On the public server the topic name is the only
# protection (anyone who knows it can read it), so it is a secret too. No topic = no alerts.
NTFY_SERVER = "https://ntfy.sh"
NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "").strip().rstrip("/").rsplit("/", 1)[-1]
ALERT_RARITY = "mythic"  # a pull of this rarity sends a notification


def local_now():
    return datetime.fromtimestamp(time.time(), LOCAL_TZ)


def log(message):
    print(f"{local_now():%Y-%m-%d %H:%M:%S} - {message}", flush=True)


def random_wait():
    return random.uniform(BATCH_MIN_MINUTES, BATCH_MAX_MINUTES) * 60


def is_night(now):
    if STOP_HOUR < RESUME_HOUR:  # e.g. 0 -> 7
        return STOP_HOUR <= now.hour < RESUME_HOUR
    return now.hour >= STOP_HOUR or now.hour < RESUME_HOUR  # e.g. 23 -> 7


def night_id(now):
    return (now - timedelta(hours=RESUME_HOUR)).date()


def stays_awake_tonight(night):
    return random.Random(f"night-{night}").random() < NIGHT_CONTINUE_CHANCE


def next_resume(now):
    wake = now.replace(hour=RESUME_HOUR, minute=0, second=0, microsecond=0)
    return wake if wake > now else wake + timedelta(days=1)


def open_all_packs():
    """Send the open-all request. Returns (HTTP status or 0, response as parsed JSON or None)."""
    headers = {
        "accept": "*/*",
        "content-type": "application/json",
        "authorization": f"Bearer {KYARA_TOKEN}",
    }
    if KYARA_DEVICE_ID:
        headers["x-device-id"] = KYARA_DEVICE_ID
    request = urllib.request.Request(
        OPEN_URL,
        data=json.dumps(OPEN_BODY, separators=(",", ":")).encode(),  # compact, like the browser
        headers=headers,
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            status, raw = response.status, response.read()
    except urllib.error.HTTPError as error:
        return error.code, None
    except OSError:  # no connection, timeout, DNS error...
        return 0, None
    try:
        return status, json.loads(raw)
    except ValueError:
        return status, None  # not JSON; the body is never logged (Actions logs are public)


def notify(title, message, priority="high", tags="tada"):
    """Push a notification through ntfy. Returns True if sent. Never raises."""
    if not NTFY_TOPIC:
        return False
    request = urllib.request.Request(
        f"{NTFY_SERVER}/{NTFY_TOPIC}",
        data=message[:1000].encode("utf-8"),
        headers={"Title": title, "Priority": priority, "Tags": tags},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=15):
            return True
    except Exception as error:  # a failed notification must never stop the script
        # Only the error type: the full text can contain the URL, and the topic is secret.
        log(f"ntfy notification failed ({getattr(error, 'code', type(error).__name__)})")
        return False


def card_name(card):
    """Best guess at a card's name; also looks one level down, e.g. {"card": {...}}."""
    for source in [card] + [v for v in card.values() if isinstance(v, dict)]:
        for key in ("name", "title", "cardName", "card_name"):
            value = source.get(key)
            if isinstance(value, str) and value.strip().lower() != ALERT_RARITY:
                return value.strip()
    return None


def find_alerts(data):
    """One entry (the card's name, or None) for every object in the response that
    has ALERT_RARITY as one of its values, e.g. {"name": "...", "rarity": "mythic"}."""
    found = []
    if isinstance(data, dict):
        if any(isinstance(v, str) and v.strip().lower() == ALERT_RARITY for v in data.values()):
            found.append(card_name(data))
        for value in data.values():
            found += find_alerts(value)
    elif isinstance(data, list):
        for item in data:
            found += find_alerts(item)
    return found


def shape(data):
    """Keys and types only, no values, so it is safe to print (test run only)."""
    if isinstance(data, dict):
        return {key: shape(value) for key, value in list(data.items())[:15]}
    if isinstance(data, list):
        return [shape(data[0])] if data else []
    return type(data).__name__


def rarities_seen(data, seen=None):
    """Values of every field with 'rarity' in its name (test run only)."""
    seen = set() if seen is None else seen
    if isinstance(data, dict):
        for key, value in data.items():
            if "rarity" in key.lower() and isinstance(value, (str, int)):
                seen.add(str(value))
            rarities_seen(value, seen)
    elif isinstance(data, list):
        for item in data:
            rarities_seen(item, seen)
    return seen


packs = 0
next_pack = time.time() + PACK_INTERVAL
next_open = time.time() + random_wait()
paused_until = 0
announced_night = None
failures = 0
started = time.time()

if LIVE:
    log("Live mode: packs will be opened on kyara.games")
else:
    log("No KYARA_TOKEN set: simulation only, nothing is sent")

if TEST_OPEN_NOW:
    if not LIVE:
        log("Test: no KYARA_TOKEN set, nothing to send")
        raise SystemExit(1)
    status, data = open_all_packs()
    log(f"Test: open request returned HTTP {status or 'no response'}")
    if data is not None:
        log(f"Test: response shape (keys and types only): {json.dumps(shape(data))[:800]}")
        log(f"Test: rarity values in the response: {str(sorted(rarities_seen(data)))[:300]}")
        log(f"Test: {len(find_alerts(data))} {ALERT_RARITY} found in the response")
    if not NTFY_TOPIC:
        log("Test: NTFY_TOPIC is not set, no notification sent")
    elif notify("Kyara test", "ntfy is working", priority="default", tags="white_check_mark"):
        log("Test: notification sent, check your phone")
    raise SystemExit(0 if 200 <= status < 300 else 1)

while True:
    now = time.time()

    if MAX_RUN_HOURS and now - started >= MAX_RUN_HOURS * 3600:
        log("Run time limit reached, exiting so the next scheduled run can take over")
        break

    # Simulate new packs arriving every 10 minutes.
    while now >= next_pack:
        packs = min(packs + 1, MAX_PACKS)
        next_pack += PACK_INTERVAL

    if now < paused_until:
        time.sleep(min(60, paused_until - now))
        continue

    # Night rule: stop at 12 AM, unless tonight is one of the 5% nights.
    local = local_now()
    if is_night(local):
        night = night_id(local)
        if not stays_awake_tonight(night):
            wake = next_resume(local) + timedelta(
                minutes=random.uniform(0, WAKE_JITTER_MINUTES)
            )
            paused_until = wake.timestamp()
            log(f"Night time, stopping until {wake:%H:%M}")
            continue
        if night != announced_night:
            announced_night = night
            log("Night time, but not stopping tonight")

    # Occasionally pause for 6-7 hours.
    if random.random() < LONG_PAUSE_CHANCE:
        pause_hours = random.uniform(6, 7)
        paused_until = now + pause_hours * 3600
        log(f"Pausing for {pause_hours:.2f} hours")
        continue

    # Open a batch once the random wait is over.
    if now >= next_open and packs > 0:
        wait = random_wait()
        next_open = now + wait
        if random.random() < SKIP_CHANCE:
            log(f"Randomly skipped this opening; next try in {wait / 60:.0f} min")
        elif not LIVE:
            log(
                f"Simulated opening {packs} pack{'s' if packs != 1 else ''}; "
                f"next opening in {wait / 60:.0f} min"
            )
            packs = 0
        else:
            status, data = open_all_packs()
            if 200 <= status < 300:
                failures = 0
                packs = 0
                log(f"Opened packs (HTTP {status}); next opening in {wait / 60:.0f} min")
                alerts = find_alerts(data)
                if alerts:
                    names = ", ".join(name for name in alerts if name)
                    text = f"Pulled {len(alerts)} {ALERT_RARITY}" + (f": {names}" if names else "")
                    log(text)
                    notify(f"Kyara: {ALERT_RARITY.title()}!", text)
            elif status in (401, 403):
                log(f"Kyara refused the token (HTTP {status}), stopping")
                raise SystemExit(1)
            elif status == 429:
                pause = random.uniform(1, 2) * 3600
                paused_until = now + pause
                log(f"Rate limited (HTTP 429), pausing for {pause / 3600:.1f} hours")
            else:
                failures += 1
                retry = random.uniform(5, 15) * 60
                next_open = now + retry
                log(
                    f"Open request failed (HTTP {status or 'no response'}), "
                    f"try {failures}/{MAX_FAILURES}; retrying in {retry / 60:.0f} min"
                )
                if failures >= MAX_FAILURES:
                    log("Too many failures in a row, stopping")
                    raise SystemExit(1)

    time.sleep(random.randint(30, 120))
