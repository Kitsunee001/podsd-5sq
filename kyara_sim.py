#!/usr/bin/env python3
"""Kyara free-pack opener, version 2.

How it works
------------
* The game server is the source of truth. Before every decision the script reads
  GET /api/me (freePacks, freePacksMax, nextFreeAt, boostersOpened), so a restart or a
  crash never loses anything: it just looks again. No pack count is guessed any more.
* Packs are opened in random batches (10-100 minutes apart) but never later than
  SAFETY_MARGIN_S before the stock would be full, so a pack is never wasted. Nothing is
  ever skipped at random.
* At night and during the random long breaks it goes "quiet": it then only opens when a
  pack would otherwise be lost (about once every 90 minutes).
* Every failure is retried with a growing delay; an unexpected error never ends the run.
"""

import json
import os
import random
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

VERSION = "2.0"


def load_env_file():
    """Read KEY=VALUE lines from a .env file next to the script (for running on your own PC)."""
    folder = os.path.dirname(os.path.abspath(__file__)) if "__file__" in globals() else os.getcwd()
    try:
        with open(os.path.join(folder, ".env"), encoding="utf-8") as handle:
            lines = handle.read().splitlines()
    except OSError:
        return
    for line in lines:
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip("\"'"))


load_env_file()

# --------------------------------------------------------------------------- settings
ME_URL = "https://kyara.games/api/me"
OPEN_URL = "https://kyara.games/api/packs/all/open"
OPEN_BODY = {"maxChakra": 0}  # copied from the browser

PACK_INTERVAL_S = 600  # the game adds a free pack every 10 minutes
SAFETY_MARGIN_S = 300  # open at the latest this long before the stock would be full
BATCH_MIN_MINUTES = 10  # random wait between two openings ...
BATCH_MAX_MINUTES = 100  # ... but never past the safety deadline above
MIN_POLL_S = 60  # routine checks: a third of the time until full, between 1 and 15 minutes
MAX_POLL_S = 900
MIN_WAKE_S = 5
MIN_REQUEST_GAP_S = 2  # pause between two requests to the site
MIN_OPEN_GAP_S = 20  # never two open requests closer together than this
HTTP_TIMEOUT_S = 30

# Quiet periods (night + random long breaks). "sparse" = only open when a pack would
# otherwise be lost. "off" = no quiet periods. A full stop is not offered: with a cap of 10
# packs a 7 hour stop wastes about 30 packs a night.
QUIET_STYLE = "sparse"
LOCAL_TZ = timezone(timedelta(hours=1))  # Tunisia = UTC+1, no daylight saving
STOP_HOUR = 0  # the quiet night starts at 12 AM ...
RESUME_HOUR = 7  # ... and ends around 7 AM
WAKE_JITTER_MINUTES = 30  # ... at a random time up to this long after RESUME_HOUR
NIGHT_CONTINUE_CHANCE = 0.05  # on 5% of nights there is no quiet period
LONG_BREAK_CHANCE = 0.04  # per opening: about one 6-7 hour quiet break a day
LONG_BREAK_HOURS = (6, 7)

# Run control. On GitHub a job is killed after 6 hours, so the workflow sets MAX_RUN_HOURS;
# on your own PC leave it unset and the script runs forever.
MAX_RUN_HOURS = float(os.environ.get("MAX_RUN_HOURS", "0"))
HANDOFF_S = 600  # open one last time this long before the run ends: the next run starts with an empty stock
GIVE_UP_AFTER_MIN = float(os.environ.get("GIVE_UP_AFTER_MIN", "0"))  # no answer from the site this long -> exit so a fresh run starts (0 = never)
MAX_CRASHES = 25  # unexpected errors in a row before exiting
ALERT_COOLDOWN_S = 6 * 3600

# Credentials and optional services come from environment variables (GitHub secrets or a
# .env file), never from this file: in a public repo anyone could read them.
def env_secret(name):
    """A value from the environment without spaces or quotes around it."""
    return os.environ.get(name, "").strip().strip("\"'").strip()


KYARA_TOKEN = env_secret("KYARA_TOKEN")
if KYARA_TOKEN.lower().startswith("bearer "):
    KYARA_TOKEN = KYARA_TOKEN[7:].strip()
KYARA_DEVICE_ID = env_secret("KYARA_DEVICE_ID")
LIVE = bool(KYARA_TOKEN)  # no token = simulation only, nothing is sent
TEST_OPEN_NOW = os.environ.get("TEST_OPEN_NOW") == "1"  # one real check + one real opening, then stop

NTFY_SERVER = "https://ntfy.sh"
NTFY_TOPIC = env_secret("NTFY_TOPIC").rstrip("/").rsplit("/", 1)[-1]
HEALTHCHECK_URL = env_secret("HEALTHCHECK_URL")  # optional dead-man's switch (healthchecks.io)
ALERT_RARITY = "mythic"  # a pull of this rarity sends a notification
NOTIFY_EVERY_OPENING = False  # True = also send a quiet ntfy message after every opening
SHOW_ERROR_HINTS = True  # when the site answers with an error, log its server, type and first 100 characters

SECRETS = [s for s in (KYARA_TOKEN, KYARA_DEVICE_ID, NTFY_TOPIC, HEALTHCHECK_URL) if len(s) >= 4]


# --------------------------------------------------------------------------- small helpers
def now():
    return time.time()


def local_dt(t=None):
    return datetime.fromtimestamp(now() if t is None else t, LOCAL_TZ)


def log(message):
    text = str(message)
    for secret in SECRETS:  # safety net: a secret can never end up in the (public) log
        text = text.replace(secret, "***")
    try:
        print(f"{local_dt():%Y-%m-%d %H:%M:%S} - {text}", flush=True)
    except (OSError, ValueError):  # stdout closed or broken: logging must never stop the bot
        pass


def fmt(seconds):
    seconds = max(0, int(round(seconds)))
    hours, rest = divmod(seconds, 3600)
    minutes, secs = divmod(rest, 60)
    if hours:
        return f"{hours}h{minutes:02d}m"
    if minutes:
        return f"{minutes}m{secs:02d}s"
    return f"{secs}s"


def plural(n, word="pack"):
    return f"{n} {word}{'' if n == 1 else 's'}"


def random_wait():
    return random.uniform(BATCH_MIN_MINUTES, BATCH_MAX_MINUTES) * 60


def iso(epoch):
    return datetime.fromtimestamp(epoch, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def parse_time(value):
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


# --------------------------------------------------------------------------- night rule
def is_night(local):
    if STOP_HOUR < RESUME_HOUR:  # e.g. 0 -> 7
        return STOP_HOUR <= local.hour < RESUME_HOUR
    return local.hour >= STOP_HOUR or local.hour < RESUME_HOUR  # e.g. 23 -> 7


def night_id(local):
    return (local - timedelta(hours=RESUME_HOUR)).date()


def stays_awake_tonight(night):
    return random.Random(f"night-{night}").random() < NIGHT_CONTINUE_CHANCE


def next_resume(local):
    wake = local.replace(hour=RESUME_HOUR, minute=0, second=0, microsecond=0)
    return wake if wake > local else wake + timedelta(days=1)


# --------------------------------------------------------------------------- what the site tells us
class Me:
    """The pack state from GET /api/me."""

    def __init__(self, packs, cap, next_at, opened, synced_at):
        self.packs = packs
        self.cap = cap
        self.next_at = next_at  # epoch seconds of the next free pack, or None
        self.opened = opened  # boostersOpened, or None
        self.synced_at = synced_at

    def to_next(self, t, interval):
        return interval if self.next_at is None else max(0.0, self.next_at - t)

    def project(self, t, interval):
        """(packs, seconds to next pack) as they should be at time t if nothing was opened since the last look."""
        packs, nxt = self.packs, self.next_at
        if nxt is None:
            return packs, interval
        while nxt <= t and packs < self.cap:
            packs += 1
            nxt += interval
        return packs, max(0.0, nxt - t)


def parse_me(data, t):
    if not isinstance(data, dict):
        raise ValueError("not an object")
    packs = int(data["freePacks"])
    cap = int(data.get("freePacksMax") or 10)
    opened = data.get("boostersOpened")
    opened = int(opened) if isinstance(opened, (int, float)) and not isinstance(opened, bool) else None
    return Me(packs, cap, parse_time(data.get("nextFreeAt")), opened, t)


def full_in(me, t, interval):
    """Seconds until the stock would be full (0 if it already is)."""
    if me.packs >= me.cap:
        return 0.0
    return me.to_next(t, interval) + (me.cap - me.packs - 1) * interval


def opened_since(before, after):
    if before.opened is not None and after.opened is not None:
        return after.opened > before.opened
    return after.packs < before.packs


# --------------------------------------------------------------------------- talking to the site
class SimGame:
    """Stands in for the site when no KYARA_TOKEN is set: same fields, same rules, nothing is sent."""

    def __init__(self):
        self.packs, self.opened = 3, 0
        self.next_at = now() + PACK_INTERVAL_S * 0.6

    def handle(self, url):
        while self.next_at <= now():
            self.packs = min(self.packs + 1, 10)
            self.next_at += PACK_INTERVAL_S
        if url == ME_URL:
            return 200, {"freePacks": self.packs, "freePacksMax": 10, "nextFreeAt": iso(self.next_at),
                         "boostersOpened": self.opened}, None
        if url == OPEN_URL and self.packs > 0:
            count, self.packs = self.packs, 0
            self.opened += count
            cards = [{"name": "Simulated card", "rarity": "mythic" if random.random() < 0.01 else "common"}
                     for _ in range(count)]
            return 200, {"cards": cards}, None
        return 400, None, None


_sim = []
_last_request = [0.0]
_error_hint = [""]


def describe_error(error):
    """A short, safe description of an error answer (server, type, first characters) so the log
    shows WHY a request failed. Successful answers are never logged."""
    try:
        headers = getattr(error, "headers", None)
        parts = []
        for name in ("Server", "Content-Type", "cf-mitigated"):
            value = headers.get(name) if headers is not None else None
            if value:
                parts.append(f"{name.lower()}: {str(value)[:40]}")
        body = error.read(200).decode("utf-8", "replace") if hasattr(error, "read") else ""
        body = " ".join(body.split())[:100]
        if body:
            parts.append(f"body: {body!r}")
        return "; ".join(parts)
    except Exception:
        return ""


def take_hint():
    hint, _error_hint[0] = _error_hint[0], ""
    return f" - {hint}" if hint else ""


def retry_after_seconds(headers):
    try:
        return int(float(headers.get("Retry-After")))
    except (AttributeError, TypeError, ValueError):
        return None


def request(method, url, body=None):
    """One request to the site. Returns (HTTP status or 0 if nothing came back, parsed JSON or None,
    Retry-After seconds or None). Never raises; answers to successful requests are never logged
    (Actions logs are public)."""
    _error_hint[0] = ""
    gap = _last_request[0] + MIN_REQUEST_GAP_S - now()
    if gap > 0:
        time.sleep(gap)
    _last_request[0] = now()
    if not LIVE:
        if not _sim:
            _sim.append(SimGame())
        return _sim[0].handle(url)
    headers = {"accept": "*/*", "authorization": f"Bearer {KYARA_TOKEN}"}
    if KYARA_DEVICE_ID:
        headers["x-device-id"] = KYARA_DEVICE_ID
    data = None
    if body is not None:
        headers["content-type"] = "application/json"
        data = json.dumps(body, separators=(",", ":")).encode()  # compact, like the browser
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    retry_after = None
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT_S) as response:
            status, raw = response.status, response.read()
    except urllib.error.HTTPError as error:
        status, raw = error.code, b""
        retry_after = retry_after_seconds(getattr(error, "headers", None))
        if SHOW_ERROR_HINTS:
            _error_hint[0] = describe_error(error)
    except Exception:  # no connection, timeout, DNS error, reset ...
        return 0, None, None
    try:
        parsed = json.loads(raw) if raw else None
    except ValueError:
        parsed = None
    return status, parsed, retry_after


def kind_of(status):
    if 200 <= status < 300:
        return "ok"
    if status in (401, 403):
        return "auth"  # token rejected
    if status == 429:
        return "rate"  # slow down
    if status == 0 or status in (408, 425) or status >= 500:
        return "transient"  # network problem or server trouble
    return "rejected"  # other 4xx: the request itself is wrong


# --------------------------------------------------------------------------- notifications
def notify(title, message, priority="high", tags="tada"):
    """Push a notification through ntfy. Returns True if sent. Never raises."""
    if not NTFY_TOPIC:
        return False
    req = urllib.request.Request(
        f"{NTFY_SERVER}/{NTFY_TOPIC}",
        data=message[:1000].encode("utf-8"),
        headers={"Title": title, "Priority": priority, "Tags": tags},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=15):
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
    """One entry (the card's name, or None) for every object in the response that has
    ALERT_RARITY as one of its values, e.g. {"name": "...", "rarity": "mythic"}."""
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


# --------------------------------------------------------------------------- the bot
class Bot:
    def __init__(self):
        t = now()
        self.started = t
        self.interval = float(PACK_INTERVAL_S)  # only ever shrinks, if the site turns out to be faster
        self.me = None
        self.failures = 0  # failed requests since the last healthy cycle (drives the backoff)
        self.crashes = 0
        self.last_ok = t  # last successful look at the site
        self.auth_blocked = False
        self.next_open_at = t + random_wait()
        self.quiet_until = 0.0
        self.wake_at = 0.0
        self.last_open_sent = 0.0
        self.last_ping = 0.0
        self.alerted = {}
        if MAX_RUN_HOURS:
            run = MAX_RUN_HOURS * 3600
            self.handoff_at = t + max(run - HANDOFF_S, run / 2)
        else:
            self.handoff_at = None

    # ---- monitoring
    def alert(self, key, title, message, priority="high", tags="warning"):
        t = now()
        if t - self.alerted.get(key, -1e12) >= ALERT_COOLDOWN_S:
            self.alerted[key] = t
            notify(title, message, priority, tags)

    def ping_health(self):
        if not HEALTHCHECK_URL or now() - self.last_ping < 300:
            return
        self.last_ping = now()
        try:
            with urllib.request.urlopen(HEALTHCHECK_URL, timeout=10):
                pass
        except Exception as error:
            log(f"Health check ping failed ({type(error).__name__})")

    # ---- reading the pack state
    def learn_interval(self, prev, me):
        """If packs arrived between two looks (and nothing was opened), the move of nextFreeAt shows the
        real interval. Only a faster one is accepted, so a wrong reading can only make us open earlier."""
        if not prev or prev.opened is None or me.opened != prev.opened:
            return
        gained = me.packs - prev.packs
        if gained < 1 or me.packs >= me.cap or prev.next_at is None or me.next_at is None:
            return
        step = (me.next_at - prev.next_at) / gained
        if 30 <= step < self.interval - 1:
            log(f"Packs seem to arrive every {fmt(step)} instead of {fmt(self.interval)}; schedule adjusted")
            self.interval = step

    def fetch_me(self):
        """Returns (kind, Me or None, Retry-After). Keeps self.me up to date."""
        status, data, retry_after = request("GET", ME_URL)
        kind = kind_of(status)
        if kind != "ok":
            log(f"Could not read your packs (HTTP {status or 'no answer'}){take_hint()}")
            self.auth_blocked = kind == "auth"
            return kind, None, retry_after
        try:
            me = parse_me(data, now())
        except (KeyError, TypeError, ValueError):
            log("The answer from /api/me did not look as expected (no freePacks); will try again")
            return "transient", None, None
        self.learn_interval(self.me, me)
        self.me = me
        self.last_ok = now()
        self.auth_blocked = False
        self.ping_health()
        return "ok", me, None

    # ---- quiet periods
    def in_quiet_period(self, t):
        if QUIET_STYLE != "sparse":
            return False
        if t < self.quiet_until or t < self.wake_at:
            return True
        local = local_dt(t)
        if is_night(local) and not stays_awake_tonight(night_id(local)):
            wake = next_resume(local) + timedelta(minutes=random.uniform(0, WAKE_JITTER_MINUTES))
            self.wake_at = wake.timestamp()
            log(f"Quiet night until about {wake:%H:%M}: only opening when packs would otherwise be lost")
            return True
        return False

    def maybe_long_break(self, t, quiet):
        if QUIET_STYLE == "sparse" and not quiet and random.random() < LONG_BREAK_CHANCE:
            hours = random.uniform(*LONG_BREAK_HOURS)
            self.quiet_until = t + hours * 3600
            log(f"Long quiet break of {hours:.1f} h: only opening when packs would otherwise be lost")

    # ---- failures
    def failed(self, what, kind, retry_after):
        """Log a failure and return how many seconds to wait before trying again."""
        self.failures += 1
        t = now()
        stock_full = False
        about_to_fill = False
        if self.me is not None:
            packs, to_next = self.me.project(t, self.interval)
            stock_full = packs >= self.me.cap
            about_to_fill = stock_full or to_next + (self.me.cap - packs - 1) * self.interval <= SAFETY_MARGIN_S
        if kind == "auth":
            self.alert("auth", "Kyara: token rejected",
                       "The site refused the token (HTTP 401/403). Update the KYARA_TOKEN secret, then restart the workflow.",
                       "urgent", "rotating_light")
            delay = 900
        else:
            if retry_after:
                delay = min(retry_after + random.uniform(1, 5), 1800)
            elif kind == "rate":
                delay = min(60 * 2 ** (self.failures - 1), 900)
            elif kind in ("rejected", "noeffect"):
                delay = min(60 * 2 ** (self.failures - 1), 600)
            else:
                delay = min(15 * 2 ** (self.failures - 1), 300)
                if about_to_fill:
                    delay = min(delay, 30)  # packs are about to be lost: keep trying quickly
            if kind in ("rejected", "noeffect") and self.failures >= 3:
                self.alert("rejected", "Kyara: unexpected answers from the site",
                           f"{what} keeps failing in an unexpected way ({kind}). Check the log.")
        if stock_full and self.failures >= 2:
            self.alert("losing", "Kyara: packs are being lost",
                       "Your pack stock is full and I cannot open packs right now. Check the log.", "urgent", "warning")
        delay *= random.uniform(0.9, 1.1)
        log(f"{what} failed ({kind}), attempt {self.failures}; trying again in {fmt(delay)}")
        return delay

    # ---- opening
    def after_opening(self, before, data, after=None):
        """Book-keeping after an opening. Returns True if packs really were opened."""
        if after is None:
            kind, after, _ = self.fetch_me()
            if kind != "ok":
                after = None
                self.me = None  # state unknown: look again soon
        if before.opened is not None and after is not None and after.opened is not None:
            count = after.opened - before.opened
            effect = count > 0
        elif after is not None:
            count = before.packs
            effect = after.packs < before.packs
        else:
            count, effect = before.packs, True  # cannot tell; the request itself succeeded
        if not effect:
            log("The site said OK but your pack count did not go down")
            return False
        t = now()
        log(f"Opened {plural(count)}" + (f"; {after.packs}/{after.cap} left" if after else ""))
        self.next_open_at = t + random_wait()
        self.maybe_long_break(t, self.in_quiet_period(t))
        if NOTIFY_EVERY_OPENING:
            notify("Kyara", f"Opened {plural(count)}", priority="low", tags="package")
        alerts = find_alerts(data)
        if alerts:
            names = ", ".join(name for name in alerts if name)
            text = f"Pulled {len(alerts)} {ALERT_RARITY}" + (f": {names}" if names else "")
            log(text)
            notify(f"Kyara: {ALERT_RARITY.title()}!", text)
        return True

    def open_packs(self, me, why):
        """Returns (True, None) when packs were opened, else (False, seconds to wait before trying again)."""
        gap = self.last_open_sent + MIN_OPEN_GAP_S - now()
        if gap > 0:
            time.sleep(gap)
        self.last_open_sent = now()
        log(f"Opening {plural(me.packs)} ({why})")
        status, data, retry_after = request("POST", OPEN_URL, OPEN_BODY)
        kind = kind_of(status)
        if kind != "ok":
            log(f"The open request got HTTP {status or 'no answer'}{take_hint()}")
        if kind == "ok":
            if self.after_opening(me, data):
                self.failures = 0
                return True, None
            return False, self.failed("Opening", "noeffect", None)
        if kind == "transient":
            # The request may have gone through even though the answer was lost: look before sending it again.
            k2, me2, _ = self.fetch_me()
            if k2 == "ok" and opened_since(me, me2):
                log("The answer was lost, but the packs were opened; not sending the request again")
                self.after_opening(me, None, after=me2)
                self.failures = 0
                return True, None
        return False, self.failed("Opening", kind, retry_after)

    # ---- planning
    def plan_sleep(self, me, t, quiet):
        to_next = me.to_next(t, self.interval)
        full = full_in(me, t, self.interval)
        deadline = full - SAFETY_MARGIN_S
        options = {"routine check": min(MAX_POLL_S, max(MIN_POLL_S, full / 3))}
        if deadline > 0:
            options["safety deadline"] = deadline
        if quiet:
            end = max(self.quiet_until, self.wake_at) - t
            if end > 0:
                options["quiet period ends"] = end
        else:
            until = self.next_open_at - t
            if me.packs == 0:
                until = max(until, to_next + 2)  # nothing to open yet: wait for the next pack
            if until > 0:
                options["random opening"] = until
        if self.handoff_at:
            options["end of this run"] = max(0.0, self.handoff_at - t)
        reason, wake = min(options.items(), key=lambda item: item[1])
        wake = max(wake, MIN_WAKE_S)
        log(f"Packs {me.packs}/{me.cap}, next in {fmt(to_next)}, full in {fmt(full)}"
            f"{' [quiet]' if quiet else ''}; next check in {fmt(wake)} ({reason})")
        return wake

    def cycle(self):
        """One look at the site plus whatever it needs. Returns the seconds to sleep afterwards."""
        kind, me, retry_after = self.fetch_me()
        if kind != "ok":
            return self.failed("Check", kind, retry_after)
        t = now()
        quiet = self.in_quiet_period(t)
        deadline = full_in(me, t, self.interval) - SAFETY_MARGIN_S
        random_due = (not quiet) and t >= self.next_open_at
        if me.packs >= 1 and (deadline <= 0 or random_due):
            why = "the stock would soon be full" if deadline <= 0 else "random batch time"
            ok, delay = self.open_packs(me, why)
            if not ok:
                return delay
            me = self.me
            if me is None:
                return 15
            t = now()
            quiet = self.in_quiet_period(t)
        self.failures = 0
        return self.plan_sleep(me, t, quiet)

    # ---- running
    def gave_up_waiting(self):
        return (GIVE_UP_AFTER_MIN and not self.auth_blocked and now() - self.last_ok > GIVE_UP_AFTER_MIN * 60)

    def handoff(self):
        log("Run time almost up: opening any waiting packs so the next run starts with an empty stock")
        me = None
        for attempt in range(3):
            kind, me, _ = self.fetch_me()
            if kind in ("ok", "auth"):
                break
            time.sleep(15)
        if me is not None and me.packs >= 1:
            self.open_packs(me, "handing over to the next run")
        log("Handing over to the next run")
        return 0

    def run(self):
        while True:
            try:
                if self.handoff_at and now() >= self.handoff_at:
                    return self.handoff()
                if self.gave_up_waiting():
                    log(f"No answer from the site for {GIVE_UP_AFTER_MIN:.0f} minutes; exiting so a fresh run takes over")
                    self.alert("giveup", "Kyara: site unreachable", "No answer from the site for a long time; restarting the run.")
                    return 1
                sleep_s = self.cycle()
                self.crashes = 0
            except Exception as error:  # anything unexpected: log it, back off, carry on
                self.crashes += 1
                log(f"Unexpected error ({type(error).__name__}: {str(error)[:200]}), attempt {self.crashes}/{MAX_CRASHES}")
                if self.crashes >= MAX_CRASHES:
                    self.alert("crash", "Kyara: script crashed", "Too many unexpected errors in a row; restarting the run.", "urgent")
                    return 1
                sleep_s = min(30 * 2 ** (self.crashes - 1), 600)
            time.sleep(sleep_s)

    def test_run(self):
        kind, me, _ = self.fetch_me()
        if kind != "ok":
            log(f"Test: could not read /api/me ({kind})")
            return 1
        t = now()
        log(f"Test: you have {me.packs}/{me.cap} packs, next one in {fmt(me.to_next(t, self.interval))}, "
            f"full in {fmt(full_in(me, t, self.interval))}")
        ok = True
        if me.packs >= 1:
            status, data, _ = request("POST", OPEN_URL, OPEN_BODY)
            log(f"Test: open request returned HTTP {status or 'no response'}{take_hint()}")
            ok = kind_of(status) == "ok"
            if data is not None:
                log(f"Test: response shape (keys and types only): {json.dumps(shape(data))[:800]}")
                log(f"Test: rarity values in the response: {str(sorted(rarities_seen(data)))[:300]}")
                log(f"Test: {len(find_alerts(data))} {ALERT_RARITY} found in the response")
        else:
            log("Test: no packs waiting right now, so the open request itself was not tested")
        if not NTFY_TOPIC:
            log("Test: NTFY_TOPIC is not set, no notification sent")
        elif notify("Kyara test", "ntfy is working", priority="default", tags="white_check_mark"):
            log("Test: notification sent, check your phone")
        return 0 if ok else 1


def main():
    log(f"Kyara pack opener v{VERSION} started: {'live' if LIVE else 'simulation only (no KYARA_TOKEN set)'}, "
        f"quiet style '{QUIET_STYLE}', run limit {f'{MAX_RUN_HOURS:g} h' if MAX_RUN_HOURS else 'none'}")
    bot = Bot()
    return bot.test_run() if TEST_OPEN_NOW else bot.run()


if __name__ == "__main__":
    sys.exit(main())
