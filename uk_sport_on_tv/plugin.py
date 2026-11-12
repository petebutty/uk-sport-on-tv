"""UK Sport on TV: televised UK football listings (live-footballontv.com), kept in this plugin's settings."""
import datetime
import email.utils
import html
import json
import logging
import re
import time
import urllib.error
import urllib.request
from pathlib import Path
from zoneinfo import ZoneInfo

LOGGER = logging.getLogger("plugins.uk_sport_on_tv")
# The plugin's key in Dispatcharr: its folder's name ("uk_sport_on_tv").
KEY = Path(__file__).parent.name
SOURCE = "https://www.live-footballontv.com/"
UA = "Mozilla/5.0 (compatible; UK-Sport-on-TV/0.2; +https://github.com/petebutty/uk-sport-on-tv)"
MAX_AGE = 6 * 3600
# The Refresh button: at most this often.
MIN_MANUAL = 10 * 60
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
            res = self._refresh(settings, force=True)
            return self._with_guide(res, settings)
        if action == "on_epg_refresh":
            # (Re-added after every refresh: a refresh replaces a source's programmes, these included.)
            res = self._refresh(settings, force=False)
            return self._with_guide(res, settings)
        if action == "remove_guide":
            n = self._clear_guide()
            return {"success": True, "message": f"Removed {n} added match(es) from the guide."}
        if action == "status":
            return self._status()
        return {"success": False, "message": f"Unknown action {action}"}

    def _stored(self):
        from apps.plugins.models import PluginConfig
        cfg = PluginConfig.objects.filter(key=KEY).first()
        return dict(cfg.settings or {}) if cfg else {}

    def _save(self, values):
        from apps.plugins.models import PluginConfig
        cfg = PluginConfig.objects.get(key=KEY)
        merged = dict(cfg.settings or {})
        merged.update(values)
        PluginConfig.objects.filter(pk=cfg.pk).update(settings=merged)

    def _refresh(self, settings, force):
        """
        Fetches the listings when they're over 6 hours old (or on Refresh, at most every 10 minutes). Polite to the
        site: asks "only if changed" (a tiny "not modified" reply most times), and after a "slow down" or "unavailable"
        reply waits as long as it asks (an hour at least), keeping the listings it has meanwhile.
        """
        stored = self._stored()
        now = time.time()
        age = now - float(stored.get("listings_fetched") or 0)
        paused = float(stored.get("listings_paused_until") or 0)
        if now < paused:
            return {"success": True, "message": f"The listings site asked us to wait: next try in {int((paused - now) / 60) + 1} min. Keeping the current listings."}
        if force and age < MIN_MANUAL and stored.get("listings"):
            return {"success": True, "message": f"Fetched {int(age / 60)} min ago: try again in {int((MIN_MANUAL - age) / 60) + 1} min."}
        if not force and age < MAX_AGE and stored.get("listings"):
            return {"success": True, "message": f"Listings are {int(age / 60)} min old: not fetched again yet."}
        try:
            days = int(settings.get("days_ahead") or stored.get("days_ahead") or 30)
        except (TypeError, ValueError):
            days = 30
        headers = {"User-Agent": UA, "Accept": "text/html"}
        if stored.get("listings") and stored.get("listings_last_modified"):
            headers["If-Modified-Since"] = stored["listings_last_modified"]
        try:
            req = urllib.request.Request(SOURCE, headers=headers)
            with urllib.request.urlopen(req, timeout=40) as r:
                page = r.read().decode("utf-8", "replace")
                last_modified = r.headers.get("Last-Modified")
        except urllib.error.HTTPError as e:
            if e.code == 304:
                self._save({"listings_fetched": int(now)})
                return {"success": True, "message": "Listings unchanged since the last fetch."}
            if e.code in (429, 503):
                wait = 3600
                ra = (e.headers.get("Retry-After") or "").strip()
                if ra.isdigit():
                    wait = max(wait, int(ra))
                elif ra:
                    try:
                        wait = max(wait, int(email.utils.parsedate_to_datetime(ra).timestamp() - now))
                    except (TypeError, ValueError):
                        pass
                self._save({"listings_paused_until": int(now + wait)})
                LOGGER.warning("UK Sport on TV: the site answered %s; waiting %d min", e.code, wait // 60)
                return {"success": False, "message": f"The listings site is busy ({e.code}): waiting {wait // 60} min, keeping the current listings."}
            LOGGER.warning("UK Sport on TV: fetch failed: %s", e)
            return {"success": False, "message": f"Couldn't fetch the listings: {e}"}
        except Exception as e:
            LOGGER.warning("UK Sport on TV: fetch failed: %s", e)
            return {"success": False, "message": f"Couldn't fetch the listings: {e}"}
        matches = parse(page, days)
        if not matches:
            # The page changed or came back empty: keep what we had rather than wipe it.
            return {"success": False, "message": "No matches found on the page (its layout may have changed); kept the previous listings."}
        self._save({
            "listings": json.dumps(matches, separators=(",", ":"), ensure_ascii=False),
            "listings_fetched": int(now),
            "listings_last_modified": last_modified or "",
            "listings_paused_until": 0,
            "listings_source": SOURCE,
        })
        return {"success": True, "message": f"{len(matches)} televised matches over the next {days} days."}

    # ---- The guide: each match on the channels showing it, at its time.

    def _with_guide(self, res, settings):
        if str(settings.get("add_to_guide", True)).lower() in ("false", "0", "no"):
            return res
        try:
            added = self._fill_guide()
            res = dict(res)
            res["message"] = res.get("message", "") + f" Guide: {added} match(es) added to channels."
        except Exception as e:
            LOGGER.exception("UK Sport on TV: guide fill failed")
            res = dict(res)
            res["message"] = res.get("message", "") + f" Guide not updated: {e}"
        return res

    @staticmethod
    def _norm(name):
        n = html.unescape(name or "").lower().replace("&", " and ")
        n = re.sub(r"\b(uk|hd|fhd|uhd|4k|sd|hevc|backup)\b", " ", n)
        # ("+" kept: "Sky Sports+" is not every Sky Sports channel.)
        return re.sub(r"[^a-z0-9()+]+", " ", n).strip()

    def _clear_guide(self):
        from apps.epg.models import ProgramData
        n, _ = ProgramData.objects.filter(custom_properties__uk_sport_on_tv=True).delete()
        return n

    def _fill_guide(self):
        """Adds each listed match to the guide of every channel showing it, where the guide has nothing then."""
        from apps.channels.models import Channel
        from apps.epg.models import EPGData, ProgramData
        stored = self._stored()
        matches = json.loads(stored.get("listings") or "[]")
        self._clear_guide()
        if not matches:
            return 0
        # Channel name -> its guide entries ("Sky Sports Football" -> SkySportsFootball.sky). A listed name matches a
        # channel called that, or that followed by a region / note ("ITV1 (London)", "BBC One Wales"), never a longer
        # number ("Premier Sports 1" is not "Premier Sports 14").
        # (Not the provider's numbered event feeds, "SKY SPORTS+ B 04:": those carry whatever's on that day.)
        slot = re.compile(r"\s\d{1,3}\s*:")
        chans = [(n, e) for n, e in Channel.objects.exclude(epg_data=None).values_list("name", "epg_data_id") if not slot.search(n or "")]
        index = [(self._norm(n), e) for n, e in chans]

        def guides_for(listed):
            want = self._norm(listed)
            # "Sky Sports TBC": not a channel yet.
            if not want or "tbc" in want.split():
                return set()
            out = set()
            for n, e in index:
                if n == want or (n.startswith(want + " ") and not n[len(want) + 1:][:1].isdigit()) or n.startswith(want + "("):
                    out.add(e)
            return out

        epgs = {e.id: e for e in EPGData.objects.filter(id__in={e for _, e in index})}
        now = datetime.datetime.now(datetime.timezone.utc)
        rows = []
        for start_ms, home, away, comp, channels in matches:
            start = datetime.datetime.fromtimestamp(start_ms / 1000, datetime.timezone.utc)
            if start < now - datetime.timedelta(hours=2):
                continue
            end = start + datetime.timedelta(hours=2)
            done = set()
            for listed in channels:
                for eid in guides_for(listed) - done:
                    done.add(eid)
                    epg = epgs.get(eid)
                    if epg is None:
                        continue
                    # Only into a gap: the provider's own programme there (often the match itself) is left alone.
                    overlapping = ProgramData.objects.filter(epg_id=eid, start_time__lt=end, end_time__gt=start).exists()
                    if overlapping:
                        continue
                    # Up to the next programme, if that starts sooner than two hours.
                    nxt = ProgramData.objects.filter(epg_id=eid, start_time__gte=start).order_by("start_time").values_list("start_time", flat=True).first()
                    stop = min(end, nxt) if nxt else end
                    also = [c for c in channels if c != listed]
                    rows.append(ProgramData(
                        epg_id=eid, tvg_id=epg.tvg_id, start_time=start, end_time=stop,
                        title=f"{home} v {away}", sub_title=comp,
                        description=f"Live {comp}: {home} v {away}." + (f" Also on {', '.join(also)}." if also else "") + " (Added by UK Sport on TV from live-footballontv.com.)",
                        custom_properties={"uk_sport_on_tv": True},
                    ))
        ProgramData.objects.bulk_create(rows, batch_size=500)
        return len(rows)

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
