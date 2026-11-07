"""Metra static GTFS: download/refresh, parse (selected routes only), service-day logic."""
import csv
import io
import logging
import os
import re
import urllib.request
import zipfile
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from .trainid import parse_trip_id

log = logging.getLogger(__name__)

ZIP_URL = "https://schedules.metrarail.com/gtfs/schedule.zip"
PUBLISHED_URL = "https://schedules.metrarail.com/gtfs/published.txt"
NO_SERVICE = "1"  # pickup_type / drop_off_type value meaning "not available"


@dataclass
class StopTime:
    seq: int
    stop_id: str
    arr: int  # seconds after service-day start (may exceed 24h)
    dep: int
    pickup: str
    drop_off: str


@dataclass
class Trip:
    trip_id: str
    route_id: str
    service_id: str
    direction_id: str
    headsign: str
    train: str
    stops: list = field(default_factory=list)
    shape_id: str = ""

    def stop_index(self, stop_id):
        for i, st in enumerate(self.stops):
            if st.stop_id == stop_id:
                return i
        return None


def _rows(zf, name):
    """Yield dict rows with whitespace-trimmed keys/values (Metra writes ', ' separators + CRLF)."""
    with zf.open(name) as fh:
        reader = csv.reader(io.TextIOWrapper(fh, encoding="utf-8-sig", newline=""), skipinitialspace=True)
        header = [h.strip() for h in next(reader)]
        for row in reader:
            if row:
                yield dict(zip(header, (v.strip() for v in row)))


def _secs(hms):
    h, m, s = (int(x) for x in hms.split(":"))
    return h * 3600 + m * 60 + s


_ABBREV = [(r"\bavenue\b", "ave"), (r"\bav\b", "ave"), (r"\broad\b", "rd"), (r"\bstreet\b", "st"),
           (r"\bsaint\b", "st"), (r"\bla grange\b", "lagrange"), (r"\bnorth\b", "n"), (r"\bsouth\b", "s"),
           (r"\beast\b", "e"), (r"\bwest\b(?= \w)", "w"), (r"\bboulevard\b", "blvd"), (r"\bdrive\b", "dr"),
           (r"\bchicago\b ", "")]


def _norm(name):
    s = re.sub(r"[^a-z0-9 ]+", " ", name.lower().replace("&", " and "))
    s = re.sub(r"\s+", " ", s).strip()
    for pat, rep in _ABBREV:
        s = re.sub(pat, rep, s)
    return re.sub(r"\s+", " ", s).strip()


def _simplify(pts, min_m=40):
    """Drop points closer than min_m to the last kept one (keeps maps light)."""
    out = []
    for la, lo in pts:
        if not out or abs(la - out[-1][0]) * 111000 + abs(lo - out[-1][1]) * 83000 >= min_m:
            out.append((round(la, 5), round(lo, 5)))
    if pts and out[-1] != (round(pts[-1][0], 5), round(pts[-1][1], 5)):
        out.append((round(pts[-1][0], 5), round(pts[-1][1], 5)))
    return out


def _ymd(s):
    return date(int(s[:4]), int(s[4:6]), int(s[6:8]))


class Schedule:
    def __init__(self, zip_path, routes):
        self.zip_path = zip_path
        self.routes = set(routes)
        self.tz = ZoneInfo("America/Chicago")
        self.stops = {}  # stop_id -> (name, lat, lon)
        self.trips = {}  # trip_id -> Trip
        self.calendar = {}  # service_id -> (weekday flags[7], start, end)
        self.exceptions = {}  # date -> {service_id: exception_type}
        self.feed_start = None
        self.feed_end = None
        self.route_info = {}  # route_id -> {"name", "color", "text_color"}
        self.shapes = {}  # shape_id -> [(lat, lon), ...] for loaded trips
        self._load()

    # ---------- loading ----------
    def _load(self):
        bad = 0

        def safe(fn, r):
            nonlocal bad
            try:
                fn(r)
            except (KeyError, ValueError, IndexError):
                bad += 1

        def stop(r):
            lat, lon = r.get("stop_lat", ""), r.get("stop_lon", "")
            self.stops[r["stop_id"]] = (r["stop_name"], float(lat) if lat else None, float(lon) if lon else None)

        def cal(r):
            flags = [r[d] == "1" for d in ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")]
            self.calendar[r["service_id"]] = (flags, _ymd(r["start_date"]), _ymd(r["end_date"]))

        def trip(r):
            if r["route_id"] in self.routes:
                _, train = parse_trip_id(r["trip_id"])
                self.trips[r["trip_id"]] = Trip(r["trip_id"], r["route_id"], r["service_id"],
                                                r.get("direction_id", ""), r.get("trip_headsign", ""), train or "",
                                                shape_id=r.get("shape_id", ""))

        def stop_time(r):
            t = self.trips.get(r["trip_id"])
            if t is not None:
                arr = r["arrival_time"] or r["departure_time"]
                dep = r["departure_time"] or r["arrival_time"]
                t.stops.append(StopTime(int(r["stop_sequence"]), r["stop_id"], _secs(arr), _secs(dep),
                                        r.get("pickup_type", "0") or "0", r.get("drop_off_type", "0") or "0"))

        with zipfile.ZipFile(self.zip_path) as zf:
            for r in _rows(zf, "agency.txt"):
                if r.get("agency_timezone"):
                    self.tz = ZoneInfo(r["agency_timezone"])
            for r in _rows(zf, "stops.txt"):
                safe(stop, r)
            for r in _rows(zf, "calendar.txt"):
                safe(cal, r)
            if "calendar_dates.txt" in zf.namelist():
                for r in _rows(zf, "calendar_dates.txt"):
                    safe(lambda r: self.exceptions.setdefault(_ymd(r["date"]), {}).__setitem__(r["service_id"],
                                                                                           r["exception_type"]), r)
            for r in _rows(zf, "trips.txt"):
                safe(trip, r)
            for r in _rows(zf, "stop_times.txt"):
                safe(stop_time, r)
            if "routes.txt" in zf.namelist():
                for r in _rows(zf, "routes.txt"):
                    if r.get("route_id") in self.routes:
                        self.route_info[r["route_id"]] = {
                            "name": r.get("route_long_name") or r.get("route_short_name") or r["route_id"],
                            "color": "#" + (r.get("route_color") or "888888"),
                            "text_color": "#" + (r.get("route_text_color") or "FFFFFF")}
            wanted = {t.shape_id for t in self.trips.values() if t.shape_id}
            if wanted and "shapes.txt" in zf.namelist():
                pts = {}
                for r in _rows(zf, "shapes.txt"):
                    if r.get("shape_id") in wanted:
                        try:
                            pts.setdefault(r["shape_id"], []).append(
                                (int(r["shape_pt_sequence"]), float(r["shape_pt_lat"]), float(r["shape_pt_lon"])))
                        except (KeyError, ValueError):
                            bad += 1
                self.shapes = {k: [(la, lo) for _, la, lo in sorted(v)] for k, v in pts.items()}
        for t in self.trips.values():
            t.stops.sort(key=lambda s: s.seq)
        self.trips = {k: t for k, t in self.trips.items() if len(t.stops) >= 2}
        if not self.trips or not self.stops:
            raise ValueError(f"schedule has no usable trips for {sorted(self.routes)}")
        dates = [c[1] for c in self.calendar.values()] + [c[2] for c in self.calendar.values()] + list(self.exceptions)
        if dates:
            self.feed_start, self.feed_end = min(dates), max(dates)
        if bad:
            log.warning("Skipped %d malformed schedule rows", bad)
        log.info("Loaded schedule: %d trips on %s, feed %s..%s", len(self.trips), ",".join(sorted(self.routes)),
                 self.feed_start, self.feed_end)

    def line_geometry(self, route_id):
        """Polylines + ordered stations for drawing a line on a map."""
        trips = [t for t in self.trips.values() if t.route_id == route_id]
        shape_ids = sorted({t.shape_id for t in trips if t.shape_id in self.shapes},
                           key=lambda s: -len(self.shapes[s]))
        polylines, covered = [], set()
        for sid in shape_ids:  # longest first; skip shapes whose points are already drawn
            pts = self.shapes[sid]
            key = {(round(a, 3), round(b, 3)) for a, b in pts}
            if key <= covered:
                continue
            covered |= key
            polylines.append(_simplify(pts))
        longest = max(trips, key=lambda t: len(t.stops), default=None)
        order = [st.stop_id for st in longest.stops] if longest else []
        served = {st.stop_id for t in trips for st in t.stops}
        order += sorted(served - set(order))
        stations = [{"id": s, "name": self.stop_name(s), "lat": self.stops[s][1], "lon": self.stops[s][2]}
                    for s in order if s in self.stops and self.stops[s][1] is not None]
        return {"polylines": polylines, "stations": stations}

    def coords(self, stop_id):
        _, lat, lon = self.stops.get(stop_id, (None, None, None))
        return (lat, lon) if lat is not None and lon is not None else None

    # ---------- time ----------
    def service_epoch(self, service_date, secs):
        """GTFS time -> aware datetime. Service day starts at noon minus 12h (DST-safe)."""
        noon = datetime.combine(service_date, time(12), tzinfo=self.tz)
        return (noon - timedelta(hours=12) + timedelta(seconds=secs)).astimezone(self.tz)

    def local_now(self):
        return datetime.now(self.tz)

    # ---------- calendar ----------
    def covers(self, d):
        return self.feed_start is not None and self.feed_start <= d <= self.feed_end

    def services_on(self, d):
        active = {sid for sid, (flags, start, end) in self.calendar.items() if start <= d <= end and flags[d.weekday()]}
        for sid, ex in self.exceptions.get(d, {}).items():
            if ex == "1":
                active.add(sid)
            elif ex == "2":
                active.discard(sid)
        return active

    def trips_on(self, route_id, d):
        services = self.services_on(d)
        return [t for t in self.trips.values() if t.route_id == route_id and t.service_id in services]

    def trip_by_train(self, route_id, train, d):
        for t in self.trips_on(route_id, d):
            if t.train == train:
                return t
        return None

    # ---------- stops ----------
    def find_stop(self, query, route_id=None):
        """Resolve a station name or stop_id to a stop_id (only stops served by route_id when given).
        Exact (normalised) name match wins; a partial match is used only when it is unambiguous."""
        if query.strip().upper() in self.stops:
            return query.strip().upper()
        served = None
        if route_id:
            served = {st.stop_id for t in self.trips.values() if t.route_id == route_id for st in t.stops}
        candidates = [sid for sid in self.stops if served is None or sid in served]
        q = _norm(query)
        exact = [s for s in candidates if _norm(self.stops[s][0]) == q]
        if exact:
            return exact[0]
        partial = [s for s in candidates if q and (q in _norm(self.stops[s][0]) or _norm(self.stops[s][0]) in q)]
        if len(partial) == 1:
            return partial[0]
        if partial:
            log.error("Station %r is ambiguous: %s", query, ", ".join(sorted(self.stops[s][0] for s in partial)))
        return None

    def stop_name(self, stop_id):
        return self.stops.get(stop_id, (stop_id, None, None))[0]

    # ---------- trips between two stations ----------
    def legs(self, route_id, from_id, to_id, d):
        """All trips on date d that pick up at from_id and later drop off at to_id, sorted by departure."""
        out = []
        for t in self.trips_on(route_id, d):
            i, j = t.stop_index(from_id), t.stop_index(to_id)
            if i is None or j is None or j <= i:
                continue
            if t.stops[i].pickup == NO_SERVICE or t.stops[j].drop_off == NO_SERVICE:
                continue
            out.append((t, i, j))
        out.sort(key=lambda x: x[0].stops[x[1]].dep)
        return out

    def resolve_leg(self, route_id, from_id, to_id, hhmm, d, tolerance_min=5):
        """Pick the trip departing from_id closest to hh:mm on service date d (within tolerance)."""
        h, m = (int(x) for x in hhmm.split(":"))
        target = h * 3600 + m * 60
        best = None
        for t, i, j in self.legs(route_id, from_id, to_id, d):
            diff = abs(t.stops[i].dep - target)
            if diff <= tolerance_min * 60 and (best is None or diff < best[3]):
                best = (t, i, j, diff)
        return best[:3] if best else None


def fetch_published(timeout=15):
    with urllib.request.urlopen(PUBLISHED_URL, timeout=timeout) as r:
        return r.read().decode("utf-8", "replace").strip()


def download_zip(path, timeout=120):
    tmp = path + ".part"
    with urllib.request.urlopen(ZIP_URL, timeout=timeout) as r, open(tmp, "wb") as fh:
        fh.write(r.read())
    zipfile.ZipFile(tmp).testzip()
    os.replace(tmp, path)
