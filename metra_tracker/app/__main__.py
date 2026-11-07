"""Metra Tracker add-on entry point: schedule refresh, realtime polling, status + MQTT publishing."""
import json
import logging
import os
import signal
import time
import urllib.request
from datetime import date, datetime, timedelta

from . import alerts as al
from .gtfs_static import Schedule, download_zip, fetch_published
from .mqtt_pub import Publisher
from .realtime import RealtimeClient, index_positions, index_trip_updates
from .tracker import DAY_FILTERS, EventGate, TrackedTrip, TripConfig
from .web import Web

DATA = os.environ.get("METRA_DATA", "/data")
VERSION = "0.4.2"
FAST, SLOW = 30, 300  # tripupdates/positions poll seconds (in / outside a live window)
ALERTS_FAST, ALERTS_SLOW = 60, 180
PUBLISHED_EVERY = 15 * 60
STALE_AFTER = 180  # seconds without a good tripupdates fetch => feed considered down
COMMUTE_RESET_HOUR = 3

log = logging.getLogger("metra")


def load_options():
    path = os.environ.get("METRA_OPTIONS", os.path.join(DATA, "options.json"))
    with open(path) as fh:
        return json.load(fh)


def mqtt_settings():
    if os.environ.get("MQTT_HOST"):
        return {"host": os.environ["MQTT_HOST"], "port": int(os.environ.get("MQTT_PORT", 1883)),
                "username": os.environ.get("MQTT_USER"), "password": os.environ.get("MQTT_PASS")}
    req = urllib.request.Request("http://supervisor/services/mqtt",
                                 headers={"Authorization": f"Bearer {os.environ['SUPERVISOR_TOKEN']}"})
    with urllib.request.urlopen(req, timeout=10) as r:
        d = json.load(r)["data"]
    return {"host": d["host"], "port": int(d["port"]), "username": d.get("username"), "password": d.get("password")}


class Store:
    def __init__(self, path):
        self.path = path
        try:
            with open(path) as fh:
                self.data = json.load(fh)
        except (OSError, ValueError):
            self.data = {}
        self._saved = json.dumps(self.data, sort_keys=True)

    def save(self):
        s = json.dumps(self.data, sort_keys=True)
        if s != self._saved:
            tmp = self.path + ".tmp"
            with open(tmp, "w") as fh:
                fh.write(s)
            os.replace(tmp, self.path)
            self._saved = s


class App:
    def __init__(self, opts):
        self.opts = opts
        self.trip_cfgs = [TripConfig(**t) for t in opts["trips"]]
        self.lines = sorted({t.line for t in self.trip_cfgs})
        self.store = Store(os.path.join(DATA, "state.json"))
        self.zip_path = os.path.join(DATA, "schedule.zip")
        self.rt = RealtimeClient(opts["api_token"])
        dry = os.environ.get("DRY_RUN") == "1"
        self.pub = Publisher(dry_run=dry, version=VERSION, **({} if dry else mqtt_settings()))
        self.gate = EventGate(self.store.data.setdefault("events", {}), opts.get("delay_threshold_min", 5),
                              opts.get("delay_change_min", 3))
        self.schedule = None
        self.trips = []
        self.next_due = {"tripupdates": 0, "positions": 0, "alerts": 0, "published": 0, "compute": 0}
        self.running = True
        self.fast = False
        self.seed_events = False  # commuting just switched on: don't announce stale facts
        self.geometry = {}
        self.snap = {"trips": [], "trains": [], "lines": [], "generated": None}

    # ---------- schedule ----------
    def refresh_schedule(self, force=False):
        try:
            published = fetch_published()
        except Exception as e:
            log.warning("published.txt check failed: %s", e)
            published = None
        have = os.path.exists(self.zip_path)
        if force or not have or (published and published != self.store.data.get("published")):
            log.info("Downloading schedule (published %s)", published)
            part = self.zip_path + ".new"
            try:
                download_zip(part)
                sched = Schedule(part, self.lines)  # must parse before it replaces the last good schedule
                os.replace(part, self.zip_path)
                self.store.data["published"] = published
                self.load_schedule(sched)
                return True
            except Exception as e:
                log.error("New schedule rejected (keeping the previous one): %s", e)
                if os.path.exists(part):
                    os.remove(part)
        if self.schedule is None and os.path.exists(self.zip_path):
            try:
                self.load_schedule()
            except Exception as e:
                log.error("Saved schedule unreadable (%s); will download again", e)
                os.remove(self.zip_path)
                return False
        return self.schedule is not None

    def load_schedule(self, sched=None):
        self.schedule = sched or Schedule(self.zip_path, self.lines)
        self.geometry = {line: dict(self.schedule.line_geometry(line), id=line,
                                    **self.schedule.route_info.get(line, {"name": line, "color": "#888888"}))
                         for line in self.lines}
        if self.trips:
            for t in self.trips:
                t.bind(self.schedule)
        else:
            runs = self.store.data.setdefault("runs", {})
            self.trips = [TrackedTrip(c, self.schedule, runs, self.opts.get("delay_threshold_min", 5))
                          for c in self.trip_cfgs]
        self.pub.publish("metra_tracker/schedule/state", {
            "valid_until": self.schedule.feed_end.isoformat() if self.schedule.feed_end else None,
            "valid_from": self.schedule.feed_start.isoformat() if self.schedule.feed_start else None,
            "published": self.store.data.get("published"),
            "days_left": (self.schedule.feed_end - date.today()).days if self.schedule.feed_end else None})

    # ---------- MQTT ----------
    def discover(self):
        self.pub.forget()
        self.pub.discover_hub(self.lines)
        current = {t.cfg.slug for t in self.trips}
        for old in set(self.store.data.get("slugs", [])) - current:
            self.pub.remove_trip(old)
            for k in [k for k in self.store.data.get("runs", {}) if k.startswith(old + ":")]:
                del self.store.data["runs"][k]
        for t in self.trips:
            self.pub.discover_trip(t)
        self.store.data["slugs"] = sorted(current)
        self.publish_commuting()
        self.next_due["compute"] = 0

    def commuting(self):
        return self.store.data.get("commuting", True)

    def publish_commuting(self):
        self.pub.publish("metra_tracker/commuting/state", "ON" if self.commuting() else "OFF")

    def handle_commands(self):
        while not self.pub.commands.empty():
            topic, payload = self.pub.commands.get()
            if topic == "__connected__" or (topic == "homeassistant/status" and payload == "online"):
                self.discover()
            elif topic == "metra_tracker/commuting/set":
                on = payload.upper() == "ON"
                if on and not self.commuting():
                    self.seed_events = True
                self.store.data["commuting"] = on
                log.info("Commuting today -> %s", payload)
                self.publish_commuting()
                self.next_due["compute"] = 0

    def daily_commute_reset(self, now):
        today = now.date().isoformat()
        if now.hour >= COMMUTE_RESET_HOUR and self.store.data.get("commute_reset") != today:
            self.store.data["commute_reset"] = today
            on = now.weekday() in DAY_FILTERS.get(self.opts.get("commute_days", "weekdays"), DAY_FILTERS["weekdays"])
            if on and not self.commuting():
                self.seed_events = True
            self.store.data["commuting"] = on
            log.info("Daily reset: commuting today = %s", self.store.data["commuting"])
            self.publish_commuting()

    # ---------- main cycle ----------
    def cycle(self):
        now_ts = time.time()
        now = self.schedule.local_now()
        self.handle_commands()
        self.daily_commute_reset(now)
        if now_ts >= self.next_due["published"]:
            self.next_due["published"] = now_ts + PUBLISHED_EVERY
            self.refresh_schedule()
        before = {k: v.ok_at for k, v in self.rt.state.items()}
        self.poll(now_ts, self.fast)
        fetched = any(v.ok_at != before[k] for k, v in self.rt.state.items())
        if fetched or now_ts >= self.next_due["compute"]:
            self.next_due["compute"] = now_ts + 30
            self.compute(now)
            self.gate.prune(now)
            for t in self.trips:
                t.prune(now)
            self.store.save()

    def poll(self, now_ts, fast):
        for name in ("tripupdates", "positions"):
            if now_ts >= self.next_due[name]:
                self.rt.fetch(name)
                self.next_due[name] = now_ts + (FAST if fast else SLOW)
        if now_ts >= self.next_due["alerts"]:
            self.rt.fetch("alerts")
            self.next_due["alerts"] = now_ts + (ALERTS_FAST if fast else ALERTS_SLOW)

    def compute(self, now):
        routes = set(self.lines)
        tu = index_trip_updates(self.rt.state["tripupdates"].message, routes)
        pos = index_positions(self.rt.state["positions"].message, routes)
        all_alerts = al.parse_alerts(self.rt.state["alerts"].message)
        age = self.rt.age("tripupdates")
        fresh = age is not None and age < STALE_AFTER
        commuting = self.commuting()
        events = []
        fast = False
        states = []
        for t in self.trips:
            st, inst = t.compute(now, tu, pos, al.for_line(all_alerts, t.cfg.line), age, fresh)
            states.append((t, st))
            fast = fast or t.live_window(inst, now)
            self.pub.trip_state(t, st, {"commuting": commuting})
            if commuting:
                events += self.gate.evaluate(t, inst, st, seed=self.seed_events)
        self.seed_events = False
        for line in self.lines:
            la = al.dedupe(al.for_line(all_alerts, line))
            self.pub.publish(f"metra_tracker/line/{line}/alerts", {
                "count": len(la),
                "alerts": [{"id": a.id, "header": a.header, "description": a.description[:500], "url": a.url}
                           for a in la]})
        if self.rt.state["alerts"].message is not None:
            events += self.alert_events(all_alerts, commuting)
        for e in events:
            e["commuting"] = commuting
            log.info("EVENT %s: %s", e["event_type"], e.get("message"))
            self.pub.event(e)
        self.snap = self.snapshot(now, tu, pos, states, all_alerts, fresh, age)
        was_fast, self.fast = self.fast, fast and commuting
        if self.fast and not was_fast:  # entering a live window: poll right away
            self.next_due.update(tripupdates=0, positions=0, alerts=0)
        self.pub.publish("metra_tracker/feed/state", {
            "ok": "ON" if fresh else "OFF",
            "tripupdates_age_s": int(age) if age is not None else None,
            "positions_age_s": _int(self.rt.age("positions")),
            "alerts_age_s": _int(self.rt.age("alerts")),
            "last_error": "; ".join(f"{k}: {v.last_error}" for k, v in self.rt.state.items() if v.last_error) or None,
            "rate_limit": self.rt.rate_limit, "rate_remaining": self.rt.rate_remaining})

    def snapshot(self, now, tu, pos, states, all_alerts, fresh, age):
        """Everything the map page needs, built once per compute cycle."""
        s = self.schedule
        tracked = {(t.cfg.line, st.train, st.service_date.replace("-", "")): t.cfg.name for t, st in states if st.train}
        trains = []
        for (route, train, sd), p in pos.items():
            if p.timestamp and now.timestamp() - p.timestamp > 600:
                continue
            try:
                d = date(int(sd[:4]), int(sd[4:6]), int(sd[6:8])) if sd else now.date()
            except ValueError:
                d = now.date()
            trip = s.trip_by_train(route, train, d)
            if trip and now > s.service_epoch(d, trip.stops[-1].arr) + timedelta(minutes=20):
                continue  # finished trip still assigned to a parked train set
            delay = next_stop = next_time = None
            live = tu.get((route, train, sd))
            if live and trip:
                rows = [(trip.stop_index(sid), a or dp) for sid, (a, dp, _) in live.stops.items() if a or dp]
                rows = [r for r in rows if r[0] is not None]
                if rows:
                    idx, pred = min(rows)
                    delay = round((pred - s.service_epoch(d, trip.stops[idx].arr).timestamp()) / 60)
                    next_stop, next_time = s.stop_name(trip.stops[idx].stop_id), pred
            trains.append({
                "line": route, "train": train, "lat": p.lat, "lon": p.lon, "bearing": p.bearing,
                "age_s": int(now.timestamp() - p.timestamp) if p.timestamp else None,
                "headsign": trip.headsign if trip else "", "direction": trip.direction_id if trip else "",
                "delay_min": delay, "next_stop": next_stop,
                "next_time": datetime.fromtimestamp(next_time, now.tzinfo).isoformat() if next_time else None,
                "tracked": tracked.get((route, train, sd))})
        trips = []
        for t, st in states:
            trips.append({
                "name": t.cfg.name, "slug": t.cfg.slug, "line": t.cfg.line, "train": st.train, "status": st.status,
                "delay_min": st.delay_min, "from": s.stop_name(t.from_id), "to": s.stop_name(t.to_id),
                "from_id": t.from_id, "to_id": t.to_id, "detail": st.detail, "problem": st.problem,
                "departure": st.departure.isoformat() if st.departure else None,
                "arrival": st.arrival.isoformat() if st.arrival else None,
                "service_date": st.service_date, "live": st.live})
        return {
            "generated": now.isoformat(), "tz": str(s.tz), "feed_ok": fresh, "feed_age_s": int(age) if age is not None else None,
            "commuting": self.commuting(), "lines": list(self.geometry.values()), "trains": trains, "trips": trips,
            "alerts": {line: [{"header": a.header, "description": a.description[:400]}
                              for a in al.dedupe(al.for_line(all_alerts, line))] for line in self.lines}}

    def alert_events(self, all_alerts, commuting):
        current = {}
        for a in al.dedupe([a for a in all_alerts if any(line in a.routes or a.agency_wide for line in self.lines)]):
            current[a.id] = (next((line for line in self.lines if line in a.routes), self.lines[0]), a)
        seen = self.store.data.get("alerts_seen")
        self.store.data["alerts_seen"] = {k: v[1].header for k, v in current.items()}
        if seen is None or not commuting:  # first run: seed silently
            return []
        out = []
        for aid, (line, a) in current.items():
            if aid not in seen:
                out.append({"event_type": "alert_new", "line": line, "alert_id": aid, "message": a.header,
                            "description": a.description[:500], "url": a.url})
        for aid, header in seen.items():
            if aid not in current:
                out.append({"event_type": "alert_cleared", "alert_id": aid, "message": f"Cleared: {header}"})
        return out

    def sleep(self, seconds):
        end = time.time() + seconds
        while self.running and time.time() < end:
            time.sleep(1)

    def run(self, stop=None):
        def on_term(*_):
            self.running = False
            if stop is not None:
                stop["now"] = True
        signal.signal(signal.SIGTERM, on_term)
        while self.running and not self.refresh_schedule():
            log.error("No schedule available yet; retrying in 5 minutes")
            self.sleep(300)
        if not self.running:
            return
        self.next_due["published"] = time.time() + PUBLISHED_EVERY
        if self.pub.dry_run:
            self.discover()
        errors = 0
        while self.running:
            try:
                self.cycle()
                errors = 0
            except Exception:
                errors += 1
                log.exception("Cycle failed (%d in a row); continuing", errors)
                self.sleep(min(300, 10 * errors))
            if os.environ.get("RUN_ONCE") == "1":
                break
            time.sleep(1)
        self.store.save()
        self.pub.stop()
        log.info("Stopped")


def _int(v):
    return int(v) if v is not None else None


def main():
    opts = load_options()
    logging.basicConfig(level=getattr(logging, opts.get("log_level", "info").upper(), logging.INFO),
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s", datefmt="%H:%M:%S")
    log.info("Metra Tracker %s starting: %d trips on %s", VERSION, len(opts["trips"]),
             ",".join(sorted({t["line"] for t in opts["trips"]})))
    stop = {"now": False}
    signal.signal(signal.SIGTERM, lambda *_: stop.update(now=True))
    if not opts.get("trips") or not opts.get("api_token"):
        log.error("Nothing to do yet: add your Metra API token and at least one trip on the add-on's Configuration "
                  "tab, then restart the add-on. See the Documentation tab for an example.")
        while not stop["now"]:
            time.sleep(5)
        return
    current = {"app": None}
    try:  # started once; survives in-process restarts of the tracker
        Web(int(os.environ.get("METRA_WEB_PORT", 8099)),
            lambda: current["app"].snap if current["app"] else {"trips": [], "trains": [], "lines": []}).start()
    except OSError as e:
        log.error("Map page unavailable: %s", e)
    while not stop["now"]:
        app = None
        try:
            app = App(opts)
            current["app"] = app
            app.run(stop)
            return
        except Exception:
            log.exception("Tracker crashed; restarting in 60 s")
            if app is not None:
                try:
                    app.pub.stop()
                except Exception:
                    pass
            for _ in range(60):
                if stop["now"]:
                    return
                time.sleep(1)


if __name__ == "__main__":
    main()
