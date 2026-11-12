"""UK Sport on TV: televised UK football listings (live-footballontv.com), kept in this plugin's settings."""
import datetime
import html
import json
import logging
import re
import time
import urllib.request
from pathlib import Path
from zoneinfo import ZoneInfo

LOGGER = logging.getLogger("plugins.uk_sport_on_tv")
# The plugin's key in Dispatcharr: its folder's name ("uk_sport_on_tv").
KEY = Path(__file__).parent.name
SOURCE = "https://www.live-footballontv.com/"
UA = "Mozilla/5.0 (compatible; UK-Sport-on-TV/0.1; +https://github.com/petebutty/uk-sport-on-tv)"
MAX_AGE = 6 * 3600
LONDON = ZoneInfo("Europe/London")
MANIFEST = json.loads((Path(__file__).parent / "plugin.json").read_text())

DATE_RE = re.compile(r'<div class="fixture-date"[^>]*>(.*?)<', re.S)
FIXTURE_RE = re.compile(r'<div class="fixture">(.*?)</div></div></div>', re.S)


def _field(block, name):
    m = re.search(r'fixture__%s">(.*?)<' % name, block, re.S)
    return html.unescape(m.group(1)).replace("\xa0", " ").strip() if m else ""


def _day(text):
    # "Saturday 21st November 2026"
    m = re.search(r"(\d{1,2})(?:st|nd|rd|th)?\s+([A-Za-z]+)\s+(\d{4})", text)
    if not m:
        return None
    try:
        return datetime.datetime.strptime(f"{m.group(1)} {m.group(2)} {m.group(3)}", "%d %B %Y").date()
    except ValueError:
        return None


def parse(page, days_ahead):
    """[[start ms (UTC), home, away, competition, [channels]], …] for matches from today to [days_ahead]."""
    out = []
    today = datetime.datetime.now(LONDON).date()
    last = today + datetime.timedelta(days=days_ahead)
    starts = [(m.start(), m.group(1)) for m in DATE_RE.finditer(page)]
    for i, (pos, label) in enumerate(starts):
        day = _day(html.unescape(label))
        if day is None or day < today or day > last:
            continue
        chunk = page[pos: starts[i + 1][0] if i + 1 < len(starts) else len(page)]
        for f in FIXTURE_RE.findall(chunk):
            teams = re.sub(r"\s*\(.*?\)\s*", " ", _field(f, "teams")).strip()
            if " v " not in teams:
                continue
            home, away = [t.strip() for t in teams.split(" v ", 1)]
            hm = re.match(r"(\d{1,2}):(\d{2})", _field(f, "time"))
            if not hm:
                continue
            local = datetime.datetime(day.year, day.month, day.day, int(hm.group(1)), int(hm.group(2)), tzinfo=LONDON)
            channels = [html.unescape(c).strip() for c in re.findall(r"channel-pill[^>]*>(.*?)<", f)]
            channels = [c for c in channels if c]
            if channels:
                out.append([int(local.timestamp() * 1000), home, away, _field(f, "competition"), channels])
    return out


class Plugin:
    name = MANIFEST["name"]
    version = MANIFEST["version"]
    description = MANIFEST["description"]
    fields = MANIFEST.get("fields", [])
    actions = MANIFEST.get("actions", [])

    def run(self, action, params, context):
        settings = dict((context or {}).get("settings") or {})
        if action == "refresh":
            return self._refresh(settings, force=True)
        if action == "on_epg_refresh":
            return self._refresh(settings, force=False)
        if action == "status":
            return self._status()
        return {"success": False, "message": f"Unknown action {action}"}

    def _stored(self):
        from apps.plugins.models import PluginConfig
        cfg = PluginConfig.objects.filter(key=KEY).first()
        return dict(cfg.settings or {}) if cfg else {}

    def _refresh(self, settings, force):
        stored = self._stored()
        age = time.time() - float(stored.get("listings_fetched") or 0)
        if not force and age < MAX_AGE and stored.get("listings"):
            return {"success": True, "message": f"Listings are {int(age / 60)} min old: not fetched again yet."}
        try:
            days = int(settings.get("days_ahead") or stored.get("days_ahead") or 30)
        except (TypeError, ValueError):
            days = 30
        try:
            req = urllib.request.Request(SOURCE, headers={"User-Agent": UA, "Accept": "text/html"})
            with urllib.request.urlopen(req, timeout=40) as r:
                page = r.read().decode("utf-8", "replace")
        except Exception as e:
            LOGGER.warning("UK Sport on TV: fetch failed: %s", e)
            return {"success": False, "message": f"Couldn't fetch the listings: {e}"}
        matches = parse(page, days)
        if not matches:
            # The page changed or came back empty: keep what we had rather than wipe it.
            return {"success": False, "message": "No matches found on the page (its layout may have changed); kept the previous listings."}
        from apps.plugins.models import PluginConfig
        cfg = PluginConfig.objects.get(key=KEY)
        merged = dict(cfg.settings or {})
        merged.update({
            "listings": json.dumps(matches, separators=(",", ":"), ensure_ascii=False),
            "listings_fetched": int(time.time()),
            "listings_source": SOURCE,
        })
        PluginConfig.objects.filter(pk=cfg.pk).update(settings=merged)
        return {"success": True, "message": f"{len(matches)} televised matches over the next {days} days."}

    def _status(self):
        stored = self._stored()
        try:
            matches = json.loads(stored.get("listings") or "[]")
        except ValueError:
            matches = []
        fetched = stored.get("listings_fetched")
        when = datetime.datetime.fromtimestamp(int(fetched), LONDON).strftime("%a %d %b %H:%M") if fetched else "never"
        now = time.time() * 1000
        nxt = [m for m in matches if m[0] >= now][:8]
        lines = [f"Last fetched: {when}. {len(matches)} matches."] + [
            datetime.datetime.fromtimestamp(m[0] / 1000, LONDON).strftime("%a %d %b %H:%M") + f"  {m[1]} v {m[2]}  ({m[3]}): {', '.join(m[4])}"
            for m in nxt
        ]
        return {"success": True, "message": "\n".join(lines)}
