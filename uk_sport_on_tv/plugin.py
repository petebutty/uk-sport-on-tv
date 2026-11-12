"""UK Sport on TV: televised UK football listings (live-footballontv.com), kept in this plugin's settings."""
import datetime
import email.utils
import html
import json
import logging
import re
import time
import urllib.error
import urllib.parse
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
            res = self._with_clubs(res, settings, force=True)
            return self._with_guide(res, settings)
        if action == "on_epg_refresh":
            # (Re-added after every refresh: a refresh replaces a source's programmes, these included.)
            res = self._refresh(settings, force=False)
            res = self._with_clubs(res, settings, force=False)
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
        # ("Only if changed" only when the listings kept cover the same days: a changed "Days ahead" reads it again.)
        if stored.get("listings") and stored.get("listings_last_modified") and int(stored.get("listings_days") or 0) == days:
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
            "listings_uk": json.dumps(matches, separators=(",", ":"), ensure_ascii=False),
            "listings": json.dumps(matches, separators=(",", ":"), ensure_ascii=False),
            "listings_fetched": int(now),
            "listings_last_modified": last_modified or "",
            "listings_days": days,
            "listings_paused_until": 0,
            "listings_source": SOURCE,
        })
        return {"success": True, "message": f"{len(matches)} televised matches over the next {days} days."}

    # ---- Club channels: "SPFL - CELTIC TV" streams Celtic's league games outside the UK, the 3pm ones UK TV can't show
    # included. Their Scottish Premiership fixtures (ESPN's public data) go on those channels, where UK TV hasn't got them.

    # "<league> - <club> TV" channels ("SPFL - CELTIC TV", "EPL - LFC TV", "EFL - SUNDERLAND TV") and the clubs' own
    # channels known by a short name.
    CLUB_RE = re.compile(r"^\s*(?:SPFL|EPL|EFL|NIFL|WSL|SWPL|SERIE A|LALIGA|LIGUE 1|BUNDESLIGA)\s*[-:–]\s*(.+?)\s*TV\s*$", re.I)
    CLUB_ALIASES = {"lfc": "Liverpool", "mutv": "Manchester United", "rtv": "Rangers", "cfc": "Chelsea", "mcfc": "Manchester City",
                    "afc": "Arsenal", "thfc": "Tottenham Hotspur", "spurs": "Tottenham Hotspur", "hibs": "Hibernian"}
    # The provider's short names → ESPN's ("HEARTS TV" is Heart of Midlothian's).
    CLUB_NAMES = {"hearts": "Heart of Midlothian", "dundee utd": "Dundee United", "hamilton": "Hamilton Academical",
                  "milan": "AC Milan", "inter": "Internazionale", "man utd": "Manchester United", "man city": "Manchester City",
                  "queens park": "Queen's Park", "queen's park": "Queen's Park", "wolves": "Wolverhampton Wanderers"}
    LEAGUE_NAMES = {"sco.1": "Scottish Premiership", "sco.2": "Scottish Championship", "sco.3": "Scottish League One", "sco.4": "Scottish League Two",
                    "ita.1": "Serie A", "esp.1": "La Liga", "fra.1": "Ligue 1", "ger.1": "Bundesliga",
                    "eng.1": "Premier League", "eng.2": "Championship",
                    "eng.3": "League One", "eng.4": "League Two", "eng.5": "National League", "nir.1": "NIFL Premiership", "wal.1": "Cymru Premier"}

    def _ensure_club_guides(self):
        """Club channels without a guide get one of this plugin's own (an "UK Sport on TV" source, written by us)."""
        from apps.channels.models import Channel
        from apps.epg.models import EPGData, EPGSource
        todo = [c for c in Channel.objects.filter(epg_data=None) if self.CLUB_RE.match(c.name or "")]
        if not todo:
            return 0
        # "xmltv" with no address: Dispatcharr never fetches or rewrites it (as EPG & Sports Editor's own source).
        src, _ = EPGSource.objects.get_or_create(name="UK Sport on TV", defaults={"source_type": "xmltv", "custom_properties": {"uk_sport_on_tv": True}})
        for c in todo:
            tvg = "uk-sport-on-tv-" + re.sub(r"[^a-z0-9]+", "-", (c.name or "").lower()).strip("-")
            epg, _ = EPGData.objects.get_or_create(tvg_id=tvg, epg_source=src, defaults={"name": c.name})
            Channel.objects.filter(pk=c.pk).update(epg_data=epg)
        return len(todo)

    def _club_channels(self):
        """{club name: {"label": "Celtic TV", "ids": [guide entry ids]}} for the club channels in the lineup."""
        from apps.channels.models import Channel
        out = {}
        for name, eid in Channel.objects.exclude(epg_data=None).values_list("name", "epg_data_id"):
            n = (name or "").strip()
            m = self.CLUB_RE.match(n)
            short = m.group(1).strip() if m else (n if n.lower() in self.CLUB_ALIASES else None)
            if not short:
                continue
            club = self.CLUB_ALIASES.get(short.lower()) or self.CLUB_NAMES.get(short.lower()) or short.title().replace("'S ", "'s ")
            label = (short.upper() if short.lower() in self.CLUB_ALIASES else short.title().replace("'S ", "'s ").replace("'S", "'s")) + ("" if short.lower() == "mutv" else " TV")
            entry = out.setdefault(club, {"label": label, "ids": set()})
            entry["ids"].add(eid)
        return {k: {"label": v["label"], "ids": sorted(v["ids"])} for k, v in out.items()}

    @staticmethod
    def _get_json(url):
        req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read().decode("utf-8", "replace"))

    _league_teams = {}

    def _team_in_leagues(self, club):
        """[ESPN id, league] for [club] from the leagues' team lists (read once per run)."""
        want = club.lower()
        for league in self.LEAGUE_NAMES:
            if league not in self._league_teams:
                try:
                    data = self._get_json(f"https://site.api.espn.com/apis/site/v2/sports/soccer/{league}/teams")
                    teams = ((data.get("sports") or [{}])[0].get("leagues") or [{}])[0].get("teams") or []
                    self._league_teams[league] = [t.get("team") or {} for t in teams]
                except Exception:
                    self._league_teams[league] = []
            for t in self._league_teams[league]:
                names = {str(t.get(k) or "").lower() for k in ("displayName", "shortDisplayName", "name", "location")}
                if want in names:
                    return [t.get("id"), league]
        return None

    def _club_fixtures(self, club, ids):
        """A club's coming Scottish Premiership games: [[start ms, home, away, competition], …]."""
        # ESPN's id and league for the club (remembered): "Celtic" → 256 in sco.1.
        known = ids.get(club)
        if not isinstance(known, list):
            q = urllib.parse.quote(club)
            found = self._get_json(f"https://site.web.api.espn.com/apis/common/v3/search?query={q}&limit=10&type=team")
            known = None
            for item in found.get("items") or []:
                league = str(item.get("league") or "")
                if item.get("sport") == "soccer" and league in self.LEAGUE_NAMES and (item.get("displayName") or "").lower() == club.lower():
                    known = [item.get("id"), league]
                    break
            # Not found that way (ESPN's search misses some clubs, or files them under a cup): its league's team list.
            if not known:
                known = self._team_in_leagues(club)
            if not known:
                return None
            ids[club] = known
        tid, league = known
        data = self._get_json(f"https://site.api.espn.com/apis/site/v2/sports/soccer/{league}/teams/{tid}/schedule?fixture=true")
        out = []
        for e in data.get("events") or []:
            comp = (e.get("competitions") or [{}])[0]
            sides = comp.get("competitors") or []
            home = next((c for c in sides if c.get("homeAway") == "home"), None)
            away = next((c for c in sides if c.get("homeAway") == "away"), None)
            try:
                start = datetime.datetime.fromisoformat((e.get("date") or "").replace("Z", "+00:00"))
            except ValueError:
                continue
            if not home or not away:
                continue
            out.append([int(start.timestamp() * 1000), home["team"]["displayName"], away["team"]["displayName"], self.LEAGUE_NAMES.get(league, league)])
        return out

    def _with_clubs(self, res, settings, force):
        """Club channels' fixtures, refreshed with the listings (every 6 hours; ESPN, a request or two a club)."""
        if str(settings.get("club_channels", True)).lower() in ("false", "0", "no"):
            return res
        stored = self._stored()
        if not force and time.time() - float(stored.get("clubs_fetched") or 0) < MAX_AGE:
            return res
        try:
            self._ensure_club_guides()
        except Exception as e:
            LOGGER.warning("UK Sport on TV: club guides: %s", e)
        clubs = self._club_channels()
        ids = dict(stored.get("club_ids") or {})
        fixtures = {}
        for club in clubs:
            try:
                f = self._club_fixtures(club, ids)
                if f is not None:
                    fixtures[club] = f
            except Exception as e:
                LOGGER.warning("UK Sport on TV: fixtures for %s failed: %s", club, e)
        if fixtures or not clubs:
            self._save({"club_fixtures": json.dumps(fixtures, separators=(",", ":"), ensure_ascii=False), "club_ids": ids, "clubs_fetched": int(time.time())})
        res = dict(res)
        if clubs:
            res["message"] = res.get("message", "") + " Club channels: " + ", ".join(f"{v['label']} ({len(fixtures.get(c, []))} games)" for c, v in clubs.items()) + "."
        return res

    @staticmethod
    def _same_match(a_home, a_away, b_home, b_away):
        def words(n):
            return {w for w in re.split(r"[^a-z0-9]+", n.lower().replace("&", " ")) if len(w) > 2 and w not in ("the", "and", "club", "united", "city", "athletic", "fc")}
        hits = 0
        for x, y in ((a_home, b_home), (a_away, b_away), (a_home, b_away), (a_away, b_home)):
            wx, wy = words(x), words(y)
            if wx & wy or any(p.startswith(q) or q.startswith(p) for p in wx for q in wy):
                hits += 1
        return hits >= 1

    def _uk(self):
        """The UK TV listings alone (not the club-channel games added to "listings")."""
        stored = self._stored()
        try:
            if stored.get("listings_uk"):
                return json.loads(stored["listings_uk"])
            # (Before the first fetch that keeps them apart: "listings" without the club-channel games added to it.)
            labels = {[v["label"]][0] for v in self._club_channels().values()}
            return [m for m in json.loads(stored.get("listings") or "[]") if not (len(m[4]) == 1 and m[4][0] in labels)]
        except ValueError:
            return []

    def _publish(self):
        """"listings" (what players read): the UK TV listings plus club-channel games UK TV hasn't got, by time."""
        uk = self._uk()
        if uk:
            allm = sorted(uk + [m[:5] for m in self._club_listings(uk)], key=lambda m: m[0])
            self._save({"listings": json.dumps(allm, separators=(",", ":"), ensure_ascii=False)})

    def _club_listings(self, uk):
        """Club-channel games not on UK TV, as listings: [start, home, away, competition, ["Celtic TV"]]."""
        labels = {c: v["label"] for c, v in self._club_channels().items()}
        stored = self._stored()
        try:
            fixtures = json.loads(stored.get("club_fixtures") or "{}")
        except ValueError:
            return []
        out = []
        for club, games in fixtures.items():
            for start, home, away, comp in games:
                # The home club streams the match: only its own home games go on a club's channel (Kilmarnock v
                # Aberdeen is on Kilmarnock TV). An away game is on the home side's channel, if you have it.
                if home.lower() != club.lower():
                    continue
                # Premier League clubs' channels can't show live Premier League games at all.
                if comp == "Premier League":
                    continue
                televised = any(abs(m[0] - start) <= 30 * 60_000 and self._same_match(m[1], m[2], home, away) for m in uk)
                if not televised:
                    out.append([start, home, away, comp, [labels.get(club, f"{club} TV")], club])
        return out

    # ---- The guide: each match on the channels showing it, at its time.

    def _with_guide(self, res, settings):
        self._publish()
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
        matches = self._uk()
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

        club_ids = {e for v in self._club_channels().values() for e in v["ids"]}
        epgs = {e.id: e for e in EPGData.objects.filter(id__in={e for _, e in index} | club_ids)}
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
        # Club channels: their games UK TV hasn't got, on "SPFL - <club> TV".
        clubs = self._club_channels()
        for start_ms, home, away, comp, channels, club in self._club_listings(matches):
            start = datetime.datetime.fromtimestamp(start_ms / 1000, datetime.timezone.utc)
            if start < now - datetime.timedelta(hours=2):
                continue
            end = start + datetime.timedelta(hours=2)
            for eid in clubs.get(club, {}).get("ids", []):
                epg = epgs.get(eid)
                if epg is None or ProgramData.objects.filter(epg_id=eid, start_time__lt=end, end_time__gt=start).exists():
                    continue
                rows.append(ProgramData(
                    epg_id=eid, tvg_id=epg.tvg_id, start_time=start, end_time=end,
                    title=f"{home} v {away}", sub_title=comp,
                    description=f"{comp}: {home} v {away}. Not on UK TV; the home club usually streams its home games on {channels[0]}. (Added by UK Sport on TV.)",
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
