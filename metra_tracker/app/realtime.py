"""Fetch + decode Metra GTFS-Realtime feeds; tolerate the server's frequent HTTP 500 bursts."""
import logging
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime

from google.transit import gtfs_realtime_pb2

from .trainid import parse_trip_id

log = logging.getLogger(__name__)

BASE = "https://gtfspublic.metrarr.com/gtfs/public/"
FEEDS = ("tripupdates", "positions", "alerts")


@dataclass
class FeedState:
    message: object = None  # last good FeedMessage
    ok_at: float = 0.0  # wall time of last good fetch
    feed_ts: int = 0  # header.timestamp of last good message
    failures: int = 0  # consecutive failures
    last_error: str = ""


class RealtimeClient:
    def __init__(self, token):
        self.token = token
        self.state = {name: FeedState() for name in FEEDS}
        self.rate_limit = None
        self.rate_remaining = None

    def fetch(self, name, timeout=20):
        st = self.state[name]
        url = BASE + name + "?" + urllib.parse.urlencode({"api_token": self.token})
        try:
            with urllib.request.urlopen(url, timeout=timeout) as r:
                self.rate_limit = r.headers.get("x-ratelimit-limit", self.rate_limit)
                self.rate_remaining = r.headers.get("x-ratelimit-remaining", self.rate_remaining)
                ctype = r.headers.get("content-type", "")
                body = r.read()
            if "html" in ctype:
                raise ValueError("HTML response instead of protobuf")
            msg = gtfs_realtime_pb2.FeedMessage()
            msg.ParseFromString(body)
        except urllib.error.HTTPError as e:
            return self._fail(name, f"HTTP {e.code}")
        except Exception as e:  # timeouts, DNS, decode errors
            return self._fail(name, f"{type(e).__name__}: {e}")
        st.message, st.ok_at, st.feed_ts, st.failures, st.last_error = msg, time.time(), msg.header.timestamp, 0, ""
        return True

    def _fail(self, name, err):
        st = self.state[name]
        st.failures += 1
        st.last_error = err
        # Metra's feed fails in 20-45 s bursts; only escalate when it persists.
        (log.warning if st.failures >= 4 else log.debug)("%s fetch failed (%d in a row): %s", name, st.failures, err)
        return False

    def age(self, name):
        st = self.state[name]
        return None if not st.ok_at else time.time() - st.ok_at


@dataclass
class TripLive:
    trip_id: str
    route_id: str
    train: str
    start_date: str
    relationship: str
    vehicle_label: str = ""
    timestamp: int = 0
    stops: dict = field(default_factory=dict)  # stop_id -> (arr_epoch, dep_epoch, relationship)
    remaining: list = field(default_factory=list)  # every stop_id Metra still lists, incl. NO_DATA ones

    @property
    def has_predictions(self):
        return any(a or d for a, d, _ in self.stops.values())


@dataclass
class Position:
    route_id: str
    train: str
    start_date: str
    lat: float
    lon: float
    bearing: float
    timestamp: int
    vehicle_id: str


def _rel_name(enum_type, value):
    try:
        return enum_type.Name(value)
    except ValueError:
        return str(value)


def index_trip_updates(msg, routes):
    """{(route, train, start_date): TripLive} for the given routes."""
    out = {}
    if msg is None:
        return out
    TD = gtfs_realtime_pb2.TripDescriptor.ScheduleRelationship
    SU = gtfs_realtime_pb2.TripUpdate.StopTimeUpdate.ScheduleRelationship
    for ent in msg.entity:
        if not ent.HasField("trip_update"):
            continue
        tu = ent.trip_update
        route, train = parse_trip_id(tu.trip.trip_id)
        route = tu.trip.route_id or route
        if route not in routes or not train:
            continue
        live = TripLive(tu.trip.trip_id, route, train, tu.trip.start_date, _rel_name(TD, tu.trip.schedule_relationship),
                        tu.vehicle.label, tu.timestamp)
        for stu in tu.stop_time_update:
            arr = stu.arrival.time if stu.HasField("arrival") and stu.arrival.time else None
            dep = stu.departure.time if stu.HasField("departure") and stu.departure.time else None
            rel = _rel_name(SU, stu.schedule_relationship)
            live.remaining.append(stu.stop_id)
            if arr or dep or rel == "SKIPPED":  # NO_DATA rows (pre-departure assignments) carry no information
                live.stops[stu.stop_id] = (arr, dep, rel)
        out[(route, train, tu.trip.start_date)] = live
    return out


def index_positions(msg, routes):
    """{(route, train, start_date): Position} for trip-assigned vehicles on the given routes."""
    out = {}
    if msg is None:
        return out
    for ent in msg.entity:
        if not ent.HasField("vehicle"):
            continue
        v = ent.vehicle
        if not v.HasField("trip") or not v.trip.trip_id:
            continue
        route, train = parse_trip_id(v.trip.trip_id)
        route = v.trip.route_id or route
        train = train or v.vehicle.label
        if route not in routes or not train:
            continue
        out[(route, train, v.trip.start_date)] = Position(route, train, v.trip.start_date, v.position.latitude,
                                                          v.position.longitude, v.position.bearing, v.timestamp,
                                                          v.vehicle.id)
    return out


def ymd(d):
    return d.strftime("%Y%m%d")


def epoch_to_local(ts, tz):
    return datetime.fromtimestamp(ts, tz) if ts else None
