"""Tracked trips: resolve each day's train from the schedule, merge realtime, compute status + events.

Departure and arrival are only declared on evidence (stop rows leaving the feed, predictions in the past, GPS at
the station, or a terminal "ghost" whose prediction slides with the clock). Evidence is kept per run in a persisted
dict, so restarts don't lose it. Stop lists without times (NO_DATA) are never used as evidence of progress, because
Metra's NO_DATA lists can run ahead of where the train actually is.
"""
import logging
import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from . import alerts as al
from .realtime import ymd

log = logging.getLogger(__name__)

DAY_FILTERS = {"weekdays": {0, 1, 2, 3, 4}, "weekends": {5, 6}, "daily": set(range(7))}

# Status values (sensor states)
NOT_RUNNING = "Not running today"
SCHEDULED = "Scheduled"
UNCONFIRMED = "Unconfirmed"
NO_PREDICTION = "No prediction"
ON_TIME = "On time"
LATE = "Late"
DISRUPTED = "Delayed"  # a Metra alert says the train/line is held, stopped or halted; minutes unknown
DEPARTED = "Departed"
ARRIVED = "Arrived"
CANCELLED = "Cancelled"
NOT_STOPPING = "Not stopping"
NO_SCHEDULE = "Schedule unavailable"
NO_MATCH = "No matching train"

PROBLEM_STATES = {UNCONFIRMED, CANCELLED, NOT_STOPPING, DISRUPTED, NO_SCHEDULE, NO_MATCH}

LIVE_LEAD = timedelta(minutes=30)  # poll fast from origin departure - LIVE_LEAD ...
LIVE_TAIL = timedelta(minutes=15)  # ... until arrival + LIVE_TAIL
ALERT_LEAD = timedelta(minutes=90)  # train-specific alerts apply from origin departure - ALERT_LEAD ...
ALERT_TAIL = timedelta(minutes=60)  # ... until scheduled arrival + ALERT_TAIL (alerts have no active period)
UNCONFIRMED_GRACE = timedelta(minutes=2)  # no data this long after origin departure => unconfirmed
KEEP_AFTER_ARRIVAL = timedelta(minutes=60)  # keep showing a finished trip this long
NOT_RUNNING_HOLD = timedelta(hours=2)  # show "not running today" until preferred time + this
TURNAROUND = timedelta(minutes=10)  # minimum time to turn inbound equipment at the origin
AT_STATION_KM = 0.4  # GPS within this of a stop = at the stop
TERMINAL_KM = 0.8  # GPS within this of a terminal after scheduled arrival = arrived (CUS-Halsted is 2.2 km)
POS_FRESH_S = 120  # a vehicle position older than this isn't used as evidence
GHOST_SLIDE_S = 600  # without GPS, a terminal prediction must slide with the clock this long to count as a ghost


def km(lat1, lon1, lat2, lon2):
    p = math.pi / 180
    a = 0.5 - math.cos((lat2 - lat1) * p) / 2 + math.cos(lat1 * p) * math.cos(lat2 * p) * (1 - math.cos((lon2 - lon1) * p)) / 2
    return 12742 * math.asin(math.sqrt(a))


@dataclass
class TripConfig:
    name: str
    line: str
    from_station: str
    to_station: str
    departure: str
    days: str = "weekdays"

    @property
    def slug(self):
        return "".join(c if c.isalnum() else "_" for c in self.name.lower()).strip("_") or "trip"


@dataclass
class Instance:
    """One concrete run of a tracked trip on a service date."""
    service_date: object
    trip: object = None
    i_from: int = None
    i_to: int = None
    sched_dep: datetime = None
    sched_arr: datetime = None
    origin_dep: datetime = None

    @property
    def key(self):
        return f"{ymd(self.service_date)}:{self.trip.train if self.trip else '-'}"


@dataclass
class TripState:
    status: str
    train: str = ""
    service_date: str = ""
    delay_min: int = None
    departure: datetime = None  # predicted (or scheduled) departure from from_station
    arrival: datetime = None  # predicted (or scheduled) arrival at to_station
    sched_dep: datetime = None
    sched_arr: datetime = None
    live: bool = False
    data_age_s: int = None
    position: dict = None
    alerts: list = field(default_factory=list)
    alternatives: list = field(default_factory=list)
    detail: str = ""
    problem: bool = False
    active: bool = False  # this run is today's (in its window) => eligible for notifications
    departed: bool = False  # evidence-based
    arrived: bool = False  # evidence-based
    extra: dict = field(default_factory=dict)


class TrackedTrip:
    def __init__(self, cfg, schedule, runs=None):
        self.cfg = cfg
        self.runs = runs if runs is not None else {}  # persisted per-run evidence, keyed "slug:YYYYMMDD:train"
        self.bind(schedule)

    def bind(self, schedule):
        self.schedule = schedule
        self.from_id = schedule.find_stop(self.cfg.from_station, self.cfg.line)
        self.to_id = schedule.find_stop(self.cfg.to_station, self.cfg.line)
        if not self.from_id or not self.to_id:
            log.error("%s: could not resolve stations %r -> %r on %s (check spelling against Metra's stop names)",
                      self.cfg.name, self.cfg.from_station, self.cfg.to_station, self.cfg.line)
        else:
            log.info("%s: %s %s (%s) -> %s (%s) at %s, %s", self.cfg.name, self.cfg.line,
                     schedule.stop_name(self.from_id), self.from_id, schedule.stop_name(self.to_id), self.to_id,
                     self.cfg.departure, self.cfg.days)

    @property
    def stations(self):
        return [(sid, self.schedule.stop_name(sid)) for sid in (self.from_id, self.to_id) if sid]

    def _runs_on(self, d):
        return d.weekday() in DAY_FILTERS.get(self.cfg.days, DAY_FILTERS["weekdays"])

    def _hm(self):
        h, m = (int(x) for x in self.cfg.departure.split(":"))
        return h, m

    def _make(self, d):
        """Instance for the train departing at the preferred time on calendar day d (trains after midnight belong to
        the previous service day in GTFS, e.g. 00:33 is 24:33 of yesterday's service)."""
        h, m = self._hm()
        s = self.schedule
        leg, sd = s.resolve_leg(self.cfg.line, self.from_id, self.to_id, f"{h}:{m:02d}", d), d
        if not leg and h < 4:
            sd = d - timedelta(days=1)
            leg = s.resolve_leg(self.cfg.line, self.from_id, self.to_id, f"{h + 24}:{m:02d}", sd)
        if not leg:
            return None
        trip, i, j = leg
        return Instance(sd, trip, i, j, s.service_epoch(sd, trip.stops[i].dep), s.service_epoch(sd, trip.stops[j].arr),
                        s.service_epoch(sd, trip.stops[0].dep))

    def preferred_time(self, d):
        h, m = self._hm()
        return self.schedule.service_epoch(d, h * 3600 + m * 60)

    def current_instance(self, now):
        """The run to show now: today's (until it's done), else the next one. Returns (Instance|None, reason)."""
        if not (self.from_id and self.to_id):
            return None, NO_MATCH
        today = now.date()
        covered = False
        for offset in range(-1, 10):
            d = today + timedelta(days=offset)
            if not self._runs_on(d):
                continue
            if not self.schedule.covers(d):
                if d < today:
                    continue
                return Instance(d), NO_SCHEDULE
            covered = True
            inst = self._make(d)
            if inst is None:
                if d == today and now < self.preferred_time(d) + NOT_RUNNING_HOLD and self._ever_matches(today):
                    return Instance(d), NOT_RUNNING
                continue
            if now < inst.sched_arr + KEEP_AFTER_ARRIVAL:
                return inst, None
        return None, (NO_MATCH if covered else NO_SCHEDULE)

    def _ever_matches(self, around):
        """True if the preferred time matches a real train on some day near `around` (else it's a config mistake)."""
        return any(self._runs_on(around + timedelta(days=k)) and self.schedule.covers(around + timedelta(days=k))
                   and self._make(around + timedelta(days=k)) for k in range(1, 15))

    def alternatives(self, inst, live_index, now, count=2):
        """Trains just before/after this one between the same stations, with live departure if known."""
        if inst is None or inst.trip is None:
            return []
        legs = self.schedule.legs(self.cfg.line, self.from_id, self.to_id, inst.service_date)
        idx = next((k for k, (t, _, _) in enumerate(legs) if t.trip_id == inst.trip.trip_id), None)
        if idx is None:
            return []
        out = []
        for t, i, j in legs[max(0, idx - count):idx] + legs[idx + 1:idx + 1 + count]:
            dep = self.schedule.service_epoch(inst.service_date, t.stops[i].dep)
            arr = self.schedule.service_epoch(inst.service_date, t.stops[j].arr)
            live = live_index.get((self.cfg.line, t.train, ymd(inst.service_date)))
            pred = None
            if live and self.from_id in live.stops:
                a, dpt, _ = live.stops[self.from_id]
                pred = dpt or a
            out.append({"train": t.train, "departure": dep.isoformat(), "arrival": arr.isoformat(),
                        "minutes": int((arr - dep).total_seconds() // 60), "stops_between": j - i - 1,
                        "delay_min": max(0, round((pred - dep.timestamp()) / 60)) if pred else None,
                        "upcoming": dep > now})
        return out

    def run_memory(self, inst):
        return self.runs.setdefault(f"{self.cfg.slug}:{inst.key}", {})

    # ------------------------------------------------------------------ status
    def compute(self, now, tu_index, pos_index, line_alerts, feed_age, feed_fresh=True):
        inst, reason = self.current_instance(now)
        if reason:
            return self._not_running(now, inst, reason), inst

        s, trip, sd = self.schedule, inst.trip, ymd(inst.service_date)
        mem = self.run_memory(inst)
        key = (self.cfg.line, trip.train, sd)
        live, pos = tu_index.get(key), pos_index.get(key)
        if pos and pos.timestamp and now.timestamp() - pos.timestamp > POS_FRESH_S:
            pos = None  # stale GPS is not evidence
        st = TripState(SCHEDULED, train=trip.train, service_date=inst.service_date.isoformat(),
                       sched_dep=inst.sched_dep, sched_arr=inst.sched_arr, departure=inst.sched_dep,
                       arrival=inst.sched_arr,
                       active=inst.origin_dep - timedelta(hours=12) <= now <= inst.sched_arr + timedelta(hours=6))
        st.alternatives = self.alternatives(inst, tu_index, now)
        if pos:
            st.position = {"lat": pos.lat, "lon": pos.lon, "bearing": pos.bearing, "timestamp": pos.timestamp}

        # ---- alerts (only inside this run's window: alerts carry no active period)
        in_alert_window = inst.origin_dep - ALERT_LEAD <= now <= inst.sched_arr + ALERT_TAIL
        infos = []
        direction = "inbound" if trip.direction_id == "1" else "outbound" if trip.direction_id == "0" else None
        for a in line_alerts:
            info = al.classify(a, self.cfg.line, trip.train, self.stations, now, direction)
            if info.relevance:
                infos.append((info, a))
        infos.sort(key=lambda x: ["train", "station", "line", "other_train"].index(x[0].relevance))
        # Alerts have no active period: only attach them to today's run (or one inside its alert window).
        if in_alert_window or inst.service_date == now.date():
            st.alerts = [{"id": a.id, "relevance": i.relevance, "header": a.header, "url": a.url} for i, a in infos]
        ours = [(i, a) for i, a in infos if i.relevance == "train" and in_alert_window]
        alert_cancel = next((a for i, a in ours if i.cancelled), None)
        alert_skip = next(((i, a) for i, a in ours if i.not_stopping), None)
        alert_delay = max((i.delay_min for i, _ in ours if i.delay_min), default=None)
        disruption = next((a for i, a in ours if i.disrupted and not i.delay_min), None)
        halt_window = inst.origin_dep - LIVE_LEAD <= now <= inst.sched_arr
        halt = next((a for i, a in infos if i.halt), None) if halt_window else None
        advisory = max((i.advisory_delay_min for i, _ in infos if i.advisory_delay_min), default=None)
        if advisory and in_alert_window:
            st.extra["advisory_delay_min"] = advisory

        # ---- live evidence
        has_pred = bool(live and live.has_predictions)
        pred_dep = pred_arr = None
        if not feed_fresh:
            st.extra["stale"] = True
        if pos and self.from_id:
            c = s.coords(self.from_id)
            if c and km(pos.lat, pos.lon, *c) <= AT_STATION_KM:
                mem["at_from"] = True
        if has_pred:
            mem["live_seen"] = True
            f, t = live.stops.get(self.from_id), live.stops.get(self.to_id)
            pred_dep = (f[1] or f[0]) if f and (f[0] or f[1]) else None
            pred_arr = (t[0] or t[1]) if t and (t[0] or t[1]) else None
            if pred_dep:
                mem["pred_dep"] = pred_dep
            if pred_arr:
                mem["pred_arr"] = pred_arr
                hist = mem.setdefault("arr_hist", [])
                hist.append([int(now.timestamp()), pred_arr])
                del hist[:-40]
                if len(hist) >= 2 and now >= inst.sched_arr - timedelta(minutes=2):
                    (t0, p0), (t1, p1) = hist[-2], hist[-1]
                    if t1 - t0 >= 25 and p1 - p0 >= 0.75 * (t1 - t0):
                        st.extra["sliding"] = True  # prediction moving with the clock: don't announce new delays
            self._departure_evidence(now, inst, live, pos, pred_dep, mem)
            self._arrival_evidence(now, inst, live, pos, pred_arr, mem, s)
        elif live is None and mem.get("live_seen") and mem.get("departed") and not mem.get("arrived") and feed_fresh:
            # The trip left the feed after departing: arrived if we're near/after its last predicted arrival.
            if now.timestamp() >= (mem.get("pred_arr") or inst.sched_arr.timestamp()) - 120:
                mem["arrived"], mem["arrived_how"] = int(now.timestamp()), "left the feed"
        if pos and not mem.get("arrived") and now >= inst.sched_arr and self.to_id and \
                inst.i_to == len(trip.stops) - 1 and mem.get("departed"):
            c = s.coords(self.to_id)
            if c and km(pos.lat, pos.lon, *c) <= TERMINAL_KM:
                mem["arrived"], mem["arrived_how"] = int(now.timestamp()), "GPS at terminal"

        st.departed, st.arrived = bool(mem.get("departed")), bool(mem.get("arrived"))
        st.live = has_pred
        if has_pred:
            st.data_age_s = int(feed_age) if feed_age is not None else None

        # ---- status
        rel = live.relationship if live else None
        skipped = [sid for sid in (self.from_id, self.to_id) if live and live.stops.get(sid, (0, 0, ""))[2] == "SKIPPED"]
        if st.arrived:
            st.status = ARRIVED
            st.delay_min = mem.get("final_delay")
            if mem.get("arrived_at_pred"):
                st.arrival = datetime.fromtimestamp(mem["arrived_at_pred"], now.tzinfo)
            st.detail = f"Arrived at {s.stop_name(self.to_id)}"
        elif rel == "CANCELED":
            st.status, st.detail = CANCELLED, "Metra marked this train cancelled"
        elif skipped and not (st.departed and skipped[0] == self.from_id):
            st.status, st.detail = NOT_STOPPING, f"Train will skip {s.stop_name(skipped[0])}"
        elif has_pred:
            if pred_arr:
                st.arrival = datetime.fromtimestamp(pred_arr, now.tzinfo)
            if st.departed:
                st.status = DEPARTED
                if mem.get("dep_actual"):
                    st.departure = datetime.fromtimestamp(mem["dep_actual"], now.tzinfo)
                st.delay_min = max(0, round((pred_arr - inst.sched_arr.timestamp()) / 60)) if pred_arr \
                    else mem.get("delay")
                st.detail = f"Arriving {s.stop_name(self.to_id)} {st.arrival:%-I:%M %p}" if pred_arr else ""
            elif pred_dep:
                # Still at/before our station. If the predicted time has passed without departure evidence, the
                # train is at least that late (Metra often leaves the origin row at the scheduled time while held).
                dep_ts = pred_dep
                if feed_fresh and now.timestamp() > pred_dep + 60:
                    dep_ts = now.timestamp()
                st.departure = datetime.fromtimestamp(dep_ts, now.tzinfo)
                st.delay_min = max(0, round((dep_ts - inst.sched_dep.timestamp()) / 60))
                st.status = ON_TIME if st.delay_min <= 1 else LATE
                if pred_arr and st.delay_min:
                    st.arrival = max(st.arrival, inst.sched_arr + timedelta(minutes=st.delay_min))
            else:
                st.status, st.detail = NO_PREDICTION, "Metra has no time for your station right now"
        elif st.departed:
            st.status, st.delay_min = DEPARTED, mem.get("delay")
            if mem.get("pred_arr"):
                st.arrival = datetime.fromtimestamp(mem["pred_arr"], now.tzinfo)
            st.detail = "Live prediction paused; showing last known arrival"
        elif live is not None and now < inst.origin_dep:
            st.detail = "Train assigned by Metra; live predictions start at departure"
        elif live is not None:
            if inst.i_from == 0 and self.from_id in live.remaining:
                st.detail = "Waiting to depart (no live prediction yet)"
                if feed_fresh and now > inst.sched_dep + timedelta(minutes=1):
                    st.status = LATE
                    st.delay_min = round((now - inst.sched_dep).total_seconds() / 60)
                    st.departure = now
            else:
                st.status, st.detail = NO_PREDICTION, "Running, but Metra has no prediction right now"
        elif mem.get("live_seen"):
            # Pre-departure trip dropped out of the feed: keep the last known prediction.
            st.status, st.delay_min = (LATE if (mem.get("delay") or 0) > 1 else ON_TIME), mem.get("delay")
            if mem.get("pred_dep"):
                st.departure = datetime.fromtimestamp(mem["pred_dep"], now.tzinfo)
            st.detail = "Live data paused; showing last known prediction"
        else:
            assumed_arr = inst.sched_arr + timedelta(minutes=(alert_delay or 0) + 10)
            if now >= assumed_arr:
                st.status, st.detail = ARRIVED, "No live data; assumed arrived (not confirmed)"
            elif not feed_fresh and now >= inst.origin_dep:
                st.detail = "Metra's live feed is unavailable; showing the schedule"
            elif now >= inst.origin_dep + UNCONFIRMED_GRACE:
                st.status = UNCONFIRMED
                st.detail = "No live data from Metra for this train" + (" (GPS only)" if pos else "")

        # ---- incoming equipment (train starting at our station)
        if live is not None and not has_pred and not st.departed and st.status in (SCHEDULED, LATE, NO_PREDICTION):
            self._incoming(now, live, inst, tu_index, pos_index, st)

        # ---- alert overlays
        if alert_cancel is not None and not has_pred and not st.departed and st.status != ARRIVED:
            st.status, st.detail = CANCELLED, f"Metra alert: {alert_cancel.header}"
        if alert_skip and not st.departed and st.status not in (ARRIVED, CANCELLED):
            st.status, st.detail = NOT_STOPPING, f"Metra alert: train will not stop at {alert_skip[0].not_stopping[0]}"
        if alert_delay and not has_pred and st.status in (SCHEDULED, UNCONFIRMED, NO_PREDICTION) \
                and now >= inst.origin_dep - LIVE_LEAD:
            st.status, st.delay_min, st.detail = LATE, alert_delay, ""
            st.departure = inst.sched_dep + timedelta(minutes=alert_delay)
            st.arrival = inst.sched_arr + timedelta(minutes=alert_delay)
            st.extra["delay_source"] = "alert"
        if alert_delay:
            st.extra["alert_delay_min"] = alert_delay
        dis = disruption or halt
        if dis is not None and st.status not in (ARRIVED, CANCELLED, NOT_STOPPING):
            st.extra["disruption"] = {"id": dis.id, "header": dis.header, "description": dis.description[:400],
                                      "scope": "train" if dis is disruption else "line"}
            if not has_pred and st.status in (SCHEDULED, UNCONFIRMED, NO_PREDICTION):
                st.status, st.detail = DISRUPTED, dis.header

        if st.status == LATE and not st.detail:
            src = " (per Metra alert)" if st.extra.get("delay_source") == "alert" else ""
            st.detail = f"{st.delay_min} min late at {s.stop_name(self.from_id)}{src}"
        if st.delay_min is not None and st.status in (ON_TIME, LATE, DEPARTED):
            mem["delay"] = st.delay_min
        if st.arrived and "final_delay" not in mem:
            mem["final_delay"] = mem.get("delay")
            st.delay_min = mem["final_delay"]
        st.problem = st.status in PROBLEM_STATES or bool(st.extra.get("disruption"))
        return st, inst

    def _not_running(self, now, inst, reason):
        st = TripState(reason, service_date=inst.service_date.isoformat() if inst else "",
                       active=bool(inst and inst.service_date == now.date()))
        if reason == NOT_RUNNING:
            for offset in range(1, 10):
                d = now.date() + timedelta(days=offset)
                nxt = self._runs_on(d) and self.schedule.covers(d) and self._make(d)
                if nxt:
                    st.train, st.sched_dep, st.sched_arr = nxt.trip.train, nxt.sched_dep, nxt.sched_arr
                    st.departure, st.arrival = nxt.sched_dep, nxt.sched_arr
                    st.detail = f"Next: train {nxt.trip.train} on {nxt.sched_dep:%a %b %-d} at {nxt.sched_dep:%-I:%M %p}"
                    break
            h, m = self._hm()
            legs = self.schedule.legs(self.cfg.line, self.from_id, self.to_id, inst.service_date)
            if legs:
                t0, i0, _ = min(legs, key=lambda x: abs(x[0].stops[x[1]].dep - (h * 3600 + m * 60)))
                dep0 = self.schedule.service_epoch(inst.service_date, t0.stops[i0].dep)
                st.extra["closest_today"] = {"train": t0.train, "departure": dep0.isoformat()}
                st.detail = f"Closest today: train {t0.train} at {dep0:%-I:%M %p}. " + st.detail
        elif reason == NO_SCHEDULE:
            st.detail = "Metra's schedule doesn't cover this date yet (waiting for a new schedule)"
        else:
            st.detail = (f"No {self.cfg.line} train from {self.cfg.from_station} to {self.cfg.to_station} "
                         f"within 5 min of {self.cfg.departure}; check the trip settings")
        st.problem = reason in PROBLEM_STATES
        return st

    def _departure_evidence(self, now, inst, live, pos, pred_dep, mem):
        if mem.get("departed"):
            return
        trip = inst.trip
        later = [i for i in (trip.stop_index(sid) for sid, (a, d, _) in live.stops.items() if a or d) if i is not None]
        listed = self.from_id in live.remaining
        evidence = None
        if not listed and later and min(later) > inst.i_from:
            evidence = "left the stop list"
        elif pred_dep and pos and mem.get("at_from") and now.timestamp() >= pred_dep:
            c = self.schedule.coords(self.from_id)
            if c and km(pos.lat, pos.lon, *c) > AT_STATION_KM:
                evidence = "GPS left the station"
        elif pred_dep and inst.i_from > 0 and not pos and now.timestamp() > pred_dep + 180:
            evidence = "predicted departure passed"  # mid-line station, no GPS: rows normally vanish; time fallback
        if evidence:
            mem["departed"], mem["departed_how"] = int(now.timestamp()), evidence
            if evidence == "GPS left the station":
                mem["dep_actual"] = int(now.timestamp())
            elif mem.get("pred_dep"):
                mem["dep_actual"] = min(int(now.timestamp()), int(mem["pred_dep"]))

    def _arrival_evidence(self, now, inst, live, pos, pred_arr, mem, s):
        if mem.get("arrived"):
            return
        trip = inst.trip
        predicted = [i for i in (trip.stop_index(sid) for sid, (a, d, _) in live.stops.items() if a or d) if i is not None]
        how, arr_ts = None, int(now.timestamp())
        if predicted and min(predicted) > inst.i_to and self.to_id not in live.remaining:
            how = "passed the station"
        elif pred_arr and pred_arr <= now.timestamp() - 30 and mem.get("departed"):
            how, arr_ts = "predicted arrival passed", pred_arr
        elif pred_arr and mem.get("departed") and now >= inst.sched_arr and inst.i_to == len(trip.stops) - 1:
            # Terminal ghost: the prediction keeps sliding with the clock while the train sits in the yard.
            # A late train crawling in looks the same, so require GPS at the terminal, or a long slide without GPS.
            run = self._slide_start(mem.get("arr_hist", []))
            c = s.coords(self.to_id)
            if run is not None:
                if pos and c:
                    if km(pos.lat, pos.lon, *c) <= TERMINAL_KM and now.timestamp() - run >= 60:
                        how, arr_ts = "GPS at terminal, prediction sliding", run
                elif now.timestamp() - run >= GHOST_SLIDE_S:
                    how, arr_ts = "prediction sliding at terminal", run
        if how:
            mem["arrived"], mem["arrived_how"] = int(now.timestamp()), how
            mem["arrived_at_pred"] = int(min(arr_ts, now.timestamp()))
            mem["final_delay"] = max(0, round((mem["arrived_at_pred"] - inst.sched_arr.timestamp()) / 60))

    @staticmethod
    def _slide_start(hist):
        """Start time of the latest run where predicted arrival moved with the clock (gap roughly constant)."""
        if len(hist) < 3:
            return None
        start = None
        for (t0, p0), (t1, p1) in zip(hist, hist[1:]):
            moving = t1 - t0 > 0 and p1 - p0 >= 0.75 * (t1 - t0) and abs((p1 - t1) - (p0 - t0)) < 90
            if moving:
                start = t0 if start is None else start
            else:
                start = None
        return start

    def _incoming(self, now, live, inst, tu_index, pos_index, st):
        """Before departure Metra assigns equipment: vehicle label = the inbound train that becomes this one.
        If that inbound train is still running late into our origin, our departure is at risk."""
        label = live.vehicle_label
        if not label or label == inst.trip.train:
            return
        origin = inst.trip.stops[0].stop_id
        st.extra["incoming_train"] = label
        key = (self.cfg.line, label, ymd(inst.service_date))
        inc, inc_pos = tu_index.get(key), pos_index.get(key)
        inc_trip = self.schedule.trip_by_train(self.cfg.line, label, inst.service_date)
        if not inc or not inc_trip or inc_trip.stop_index(origin) is None:
            return
        row = inc.stops.get(origin)
        arr = (row[0] or row[1]) if row else None
        oc = self.schedule.coords(origin)
        if not arr or arr <= now.timestamp() or (inc_pos and oc and km(inc_pos.lat, inc_pos.lon, *oc) <= TERMINAL_KM):
            st.extra["incoming_status"] = "arrived"
            return
        sched = self.schedule.service_epoch(inst.service_date, inc_trip.stops[inc_trip.stop_index(origin)].arr)
        if now > sched + timedelta(minutes=5) and arr - now.timestamp() < 240 and not inc_pos:
            return  # likely a ghost prediction sliding with the clock and no GPS to confirm; don't guess
        inc_delay = max(0, round((arr - sched.timestamp()) / 60))
        ready = datetime.fromtimestamp(arr, now.tzinfo) + TURNAROUND
        risk = max(0, round((ready - inst.origin_dep).total_seconds() / 60))
        st.extra.update(incoming_delay_min=inc_delay, incoming_arrival=ready - TURNAROUND, departure_risk_min=risk,
                        incoming_status="en route")
        if risk > 0 and st.status == SCHEDULED:
            why = f"{inc_delay} min late" if inc_delay else f"due {ready - TURNAROUND:%-I:%M}, tight turnaround"
            st.detail = f"Equipment arrives on train {label} ({why}); departure could slip about {risk} min"

    def live_window(self, inst, now):
        return bool(inst and inst.trip and inst.origin_dep - LIVE_LEAD <= now <= inst.sched_arr + LIVE_TAIL)

    def prune(self, now, keep_days=3):
        cutoff = ymd(now.date() - timedelta(days=keep_days))
        for k in [k for k in self.runs if k.startswith(self.cfg.slug + ":") and k.split(":")[1] < cutoff]:
            del self.runs[k]


class EventGate:
    """Decides which notifications to emit; remembers what was sent per run (persisted)."""

    QUIET_ON_SEED = {"departed", "arrived", "confirmed", "recovered", "reinstated"}

    def __init__(self, store, threshold, change):
        self.store = store  # dict persisted by caller
        self.threshold = threshold
        self.change = change

    def evaluate(self, tracked, inst, st, seed=False):
        """seed=True (commuting just switched on): record state, but don't announce stale facts."""
        if inst is None or inst.trip is None or not st.active:
            return []
        mem = self.store.setdefault(f"{tracked.cfg.slug}:{inst.key}", {})
        base = {"trip": tracked.cfg.name, "trip_slug": tracked.cfg.slug, "train": st.train, "line": tracked.cfg.line,
                "from": tracked.schedule.stop_name(tracked.from_id), "to": tracked.schedule.stop_name(tracked.to_id),
                "scheduled": st.sched_dep.strftime("%-I:%M %p") if st.sched_dep else None,
                "status": st.status, "delay_min": st.delay_min}
        events = []

        def emit(kind, message, **extra):
            if not (seed and kind in self.QUIET_ON_SEED):
                events.append(dict(base, event_type=kind, message=message, **extra))

        label = f"Train {st.train} ({base['scheduled']})"
        if st.status == CANCELLED and not mem.get("cancelled"):
            mem["cancelled"] = True
            emit("cancelled", f"{label}: {st.detail}")
        elif mem.get("cancelled") and st.status not in (CANCELLED, ARRIVED) and st.live:
            mem["cancelled"] = False
            emit("reinstated", f"{label} is running after all: {st.status}")
        if st.status == NOT_STOPPING and not mem.get("not_stopping"):
            mem["not_stopping"] = True
            emit("not_stopping", f"{label}: {st.detail}")
        if st.status == UNCONFIRMED and not mem.get("unconfirmed"):
            mem["unconfirmed"] = True
            emit("unconfirmed", f"{label} isn't reporting live data yet")
        if st.live and mem.get("unconfirmed") and not mem.get("confirmed"):
            mem["confirmed"] = True
            emit("confirmed", f"Train {st.train} is now reporting: {st.status}")
        dis = st.extra.get("disruption")
        if dis and mem.get("disruption") != dis["id"]:
            mem["disruption"] = dis["id"]
            emit("disruption", f"{label}: {dis['header']}", description=dis["description"], scope=dis["scope"])

        d = st.delay_min
        quiet = st.extra.get("stale") or st.extra.get("sliding")
        if d is not None and st.status in (LATE, ON_TIME, DEPARTED) and not st.arrived and not quiet:
            last = mem.get("delay_notified")
            if d >= self.threshold and (last is None or abs(d - last) >= self.change):
                mem["delay_notified"], mem["recover_polls"] = d, 0
                where = base["from"] if st.status != DEPARTED else base["to"]
                emit("delay", f"{label} is {d} min late at {where}", source=st.extra.get("delay_source", "live"))
            elif last is not None and d <= self.threshold - self.change:
                mem["recover_polls"] = mem.get("recover_polls", 0) + 1
                if mem["recover_polls"] >= 2:
                    mem["delay_notified"] = None
                    emit("recovered", f"Train {st.train} has made up time: now {d} min late")
            else:
                mem["recover_polls"] = 0
        risk = st.extra.get("departure_risk_min")
        if risk and risk >= self.threshold and not mem.get("delay_risk") and not mem.get("delay_notified"):
            mem["delay_risk"] = True
            emit("delay_risk", f"{label} may leave about {risk} min late: its equipment is arriving on "
                 f"train {st.extra.get('incoming_train')}", risk_min=risk)
        if st.departed and not mem.get("departed"):
            mem["departed"] = True
            emit("departed", f"Train {st.train} left {base['from']}"
                 + (f", arriving {base['to']} {st.arrival:%-I:%M %p}" if st.arrival else ""))
        if st.arrived and not mem.get("arrived"):
            mem["arrived"] = True
            emit("arrived", f"Train {st.train} arrived at {base['to']}"
                 + (f", {st.delay_min} min late" if st.delay_min else ""))
        return events

    def prune(self, now, keep_days=3):
        cutoff = ymd(now.date() - timedelta(days=keep_days))
        for k in list(self.store):
            parts = k.split(":")
            if len(parts) >= 2 and parts[1] < cutoff:
                del self.store[k]
