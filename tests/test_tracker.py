"""Offline tests for the tracker state machine using the real Metra schedule zip + synthetic realtime feeds.

Run:  METRA_TEST_ZIP=/path/to/schedule.zip PYTHONPATH=<gtfs-rt bindings>:metra_tracker python3 -m unittest discover tests
"""
import os
import unittest
from datetime import date, datetime, timedelta

from google.transit import gtfs_realtime_pb2 as rt

from app import alerts as al
from app.gtfs_static import Schedule
from app.realtime import index_positions, index_trip_updates
from app.trainid import parse_trip_id
from app.tracker import (ARRIVED, CANCELLED, DEPARTED, DISRUPTED, LATE, NO_MATCH, NO_PREDICTION, NO_SCHEDULE, NOT_RUNNING,
                         NOT_STOPPING, ON_TIME, SCHEDULED, UNCONFIRMED, EventGate, TrackedTrip, TripConfig)

ZIP = os.environ.get("METRA_TEST_ZIP")
WED = date(2026, 11, 4)  # a regular weekday in feed period A1A
ROUTES = {"BNSF"}


def feed():
    m = rt.FeedMessage()
    m.header.gtfs_realtime_version = "2.0"
    return m


def add_tu(m, trip, d, stops, label=None, rel=None, nodata=()):
    """stops: [(stop_id, arr_dt|None, dep_dt|None, rel)]"""
    e = m.entity.add(id=str(len(m.entity) + 1))
    tu = e.trip_update
    tu.trip.trip_id, tu.trip.route_id, tu.trip.start_date = trip.trip_id, "BNSF", d.strftime("%Y%m%d")
    if rel:
        tu.trip.schedule_relationship = rel
    tu.vehicle.label = label or trip.train
    for sid, arr, dep, srel in stops:
        s = tu.stop_time_update.add(stop_id=sid)
        if arr:
            s.arrival.time = int(arr.timestamp())
        if dep:
            s.departure.time = int(dep.timestamp())
        if srel:
            s.schedule_relationship = srel
    for sid in nodata:
        tu.stop_time_update.add(stop_id=sid, schedule_relationship=rt.TripUpdate.StopTimeUpdate.NO_DATA)
    return tu


def add_pos(m, trip, d, lat, lon, ts):
    e = m.entity.add(id=f"V{len(m.entity) + 1}")
    v = e.vehicle
    v.trip.trip_id, v.trip.route_id, v.trip.start_date = trip.trip_id, "BNSF", d.strftime("%Y%m%d")
    v.vehicle.id, v.vehicle.label = "8001", trip.train
    v.position.latitude, v.position.longitude, v.position.bearing = lat, lon, 90
    v.timestamp = int(ts.timestamp())


def add_alert(m, header, desc, route="BNSF", trip_id=None):
    e = m.entity.add(id=f"A{len(m.entity) + 1}")
    ie = e.alert.informed_entity.add(agency_id="METRA", route_id=route)
    if trip_id:
        ie.trip.trip_id = trip_id
    e.alert.header_text.translation.add(text=header, language="en")
    e.alert.description_text.translation.add(text=desc, language="en")


@unittest.skipUnless(ZIP and os.path.exists(ZIP), "set METRA_TEST_ZIP to the Metra schedule.zip")
class TrackerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.s = Schedule(ZIP, ROUTES)

    def setUp(self):
        # Example commute (any line/stations work): BNSF Naperville 07:32 -> Union Station, Union Station 17:20 -> Naperville
        self.am = TrackedTrip(TripConfig("Morning", "BNSF", "Naperville", "Chicago Union Station", "07:32"), self.s)
        self.pm = TrackedTrip(TripConfig("Evening", "BNSF", "Union Station", "Naperville", "17:20"), self.s)
        self.A, _ = self.am.current_instance(self.at(WED, 5, 0))
        self.P, _ = self.pm.current_instance(self.at(WED, 12, 0))
        self.T = self.A.trip.train  # our morning train number
        self.OTHER = next(t.train for t, _, _ in self.s.legs("BNSF", "NAPERVILLE", "CUS", WED) if t.train != self.T)

    # times relative to the morning run: o = origin departure, d = our departure, a = arrival; p = evening departure
    def o(self, m, s=0):
        return self.A.origin_dep + timedelta(minutes=m, seconds=s)

    def d(self, m, s=0):
        return self.A.sched_dep + timedelta(minutes=m, seconds=s)

    def a(self, m, s=0):
        return self.A.sched_arr + timedelta(minutes=m, seconds=s)

    def p(self, m, s=0):
        return self.P.sched_dep + timedelta(minutes=m, seconds=s)

    def at(self, d, hh, mm, ss=0):
        return datetime(d.year, d.month, d.day, hh, mm, ss, tzinfo=self.s.tz)

    def run_am(self, now, tu=None, alerts=None, fresh=True, trip=None, pos=None):
        trip = trip or self.am
        tu_idx = index_trip_updates(tu, ROUTES)
        line_alerts = al.for_line(al.parse_alerts(alerts), "BNSF")
        return trip.compute(now, tu_idx, index_positions(pos, ROUTES), line_alerts, 5, fresh)

    def stops_from(self, inst, delay_min, start_idx=0):
        d = timedelta(minutes=delay_min)
        out = []
        for st in inst.trip.stops[start_idx:]:
            out.append((st.stop_id, self.s.service_epoch(inst.service_date, st.arr) + d,
                        self.s.service_epoch(inst.service_date, st.dep) + d, None))
        return out

    # ---------- schedule ----------
    def test_resolves_commute_trains(self):
        inst, _ = self.am.current_instance(self.at(WED, 6, 0))
        self.assertEqual(inst.trip.train, "1230")
        self.assertEqual(inst.sched_dep.strftime("%H:%M"), "07:32")
        self.assertEqual(inst.sched_arr.strftime("%H:%M"), "08:11")
        inst, _ = self.pm.current_instance(self.at(WED, 12, 0))
        self.assertEqual((inst.trip.train, inst.sched_dep.strftime("%H:%M"), inst.sched_arr.strftime("%H:%M")),
                         ("1267", "17:20", "17:55"))

    def test_weekend_shows_next_weekday(self):
        st, inst = self.run_am(self.at(date(2026, 11, 7), 9, 0))
        self.assertEqual(st.status, SCHEDULED)
        self.assertEqual(inst.service_date, date(2026, 11, 9))

    def test_thanksgiving_not_running(self):
        st, _ = self.run_am(self.at(date(2026, 11, 26), 6, 30))
        self.assertEqual(st.status, NOT_RUNNING)
        self.assertIn("Fri Nov 27", st.detail)
        self.assertEqual(st.extra["closest_today"]["train"], "2004")  # Sunday schedule's 7:16

    def test_feed_expiry(self):
        st, _ = self.run_am(self.at(date(2027, 1, 4), 6, 30))
        self.assertEqual(st.status, NO_SCHEDULE)
        self.assertTrue(st.problem)

    def test_dst_fall_back_day(self):
        # 2026-11-01 is the DST change; a 07:00 GTFS time must be 07:00 local (CST).
        self.assertEqual(self.s.service_epoch(date(2026, 11, 1), 7 * 3600).strftime("%H:%M %Z"), "07:00 CST")
        self.assertEqual(self.s.service_epoch(date(2026, 11, 1), 25 * 3600).strftime("%m-%d %H:%M"), "11-02 01:00")

    def test_trip_id_formats(self):
        self.assertEqual(parse_trip_id("BNSF_BN1230_V2_AA"), ("BNSF", "1230"))
        self.assertEqual(parse_trip_id("UP-NW_UNW644_V2_AA"), ("UP-NW", "644"))
        self.assertEqual(parse_trip_id("BNSF_1230_8_2009_5682524"), ("BNSF", "1230"))

    # ---------- live ----------
    def test_scheduled_then_unconfirmed(self):
        st, _ = self.run_am(self.o(-12))  # before the train leaves its origin
        self.assertEqual(st.status, SCHEDULED)
        st, _ = self.run_am(self.o(3), tu=feed())
        self.assertEqual(st.status, UNCONFIRMED)
        self.assertTrue(st.problem)

    def test_feed_down_is_not_unconfirmed(self):
        st, _ = self.run_am(self.o(3), tu=feed(), fresh=False)
        self.assertEqual(st.status, SCHEDULED)
        self.assertFalse(st.problem)

    def test_on_time_late_departed_arrived(self):
        inst, _ = self.am.current_instance(self.o(-12))
        m = feed()
        add_tu(m, inst.trip, WED, self.stops_from(inst, 1))
        st, _ = self.run_am(self.o(3), tu=m)
        self.assertEqual((st.status, st.delay_min), (ON_TIME, 1))
        m = feed()
        add_tu(m, inst.trip, WED, self.stops_from(inst, 7))
        st, _ = self.run_am(self.d(-7), tu=m)
        self.assertEqual((st.status, st.delay_min), (LATE, 7))
        self.assertEqual(st.departure, self.d(7))
        m = feed()
        add_tu(m, inst.trip, WED, self.stops_from(inst, 7, start_idx=inst.i_from + 1))
        st, _ = self.run_am(self.d(13), tu=m)
        self.assertEqual((st.status, st.delay_min), (DEPARTED, 7))
        # Feed drops the trip after arrival -> Arrived, not Unconfirmed.
        st, _ = self.run_am(self.a(9), tu=feed())
        self.assertEqual(st.status, ARRIVED)

    def test_departed_survives_prediction_dropout(self):
        inst, _ = self.am.current_instance(self.o(-12))
        m = feed()
        add_tu(m, inst.trip, WED, self.stops_from(inst, 3, start_idx=inst.i_from + 1))
        st, _ = self.run_am(self.d(8), tu=m)
        self.assertEqual(st.status, DEPARTED)
        st, _ = self.run_am(self.d(13), tu=feed())  # trip gone, predicted arrival still ahead
        self.assertEqual(st.status, DEPARTED)

    def test_ghost_after_arrival_stays_arrived(self):
        inst, _ = self.am.current_instance(self.o(-12))
        m = feed()
        cus = inst.trip.stops[inst.i_to]
        add_tu(m, inst.trip, WED, [(cus.stop_id, self.a(-1), None, None)])
        self.run_am(self.a(2), tu=m)  # predicted arrival passed -> Arrived
        m = feed()
        add_tu(m, inst.trip, WED, [(cus.stop_id, self.a(34), None, None)])  # yard ghost
        st, _ = self.run_am(self.a(14), tu=m)
        self.assertEqual(st.status, ARRIVED)

    def test_no_data_midtrip_is_no_prediction(self):
        inst, _ = self.am.current_instance(self.o(-12))
        m = feed()
        add_tu(m, inst.trip, WED, [], nodata=[s.stop_id for s in inst.trip.stops[1:]])
        st, _ = self.run_am(self.o(4), tu=m)
        self.assertEqual(st.status, NO_PREDICTION)
        self.assertFalse(st.problem)
        # NO_DATA lists can run ahead of the train (seen live 2026-11-07): never evidence of departure.
        m = feed()
        add_tu(m, inst.trip, WED, [], nodata=[s.stop_id for s in inst.trip.stops[inst.i_from + 1:]])
        st, _ = self.run_am(self.d(6), tu=m)
        self.assertEqual(st.status, NO_PREDICTION)
        self.assertFalse(st.departed)

    def test_cancel_and_skip(self):
        inst, _ = self.am.current_instance(self.o(-12))
        m = feed()
        add_tu(m, inst.trip, WED, [], rel=rt.TripDescriptor.CANCELED)
        st, _ = self.run_am(self.o(-2), tu=m)
        self.assertEqual(st.status, CANCELLED)
        m = feed()
        stops = self.stops_from(inst, 0)
        stops[inst.i_from] = (stops[inst.i_from][0], None, None, rt.TripUpdate.StopTimeUpdate.SKIPPED)
        add_tu(m, inst.trip, WED, stops)
        st, _ = self.run_am(self.o(3), tu=m)
        self.assertEqual(st.status, NOT_STOPPING)

    def test_alert_delay_and_cancel_and_disruption(self):
        a = feed()
        add_alert(a, f"BNSF Train {self.T} Delayed", f"BNSF train #{self.T} may be operating 15 to 20 minutes behind schedule.")
        st, _ = self.run_am(self.o(3), tu=feed(), alerts=a)
        self.assertEqual((st.status, st.delay_min), (LATE, 15))
        a = feed()
        add_alert(a, f"BNSF Train {self.T} Stopped", f"BNSF train #{self.T} is stopped near Route 59 due to police activity.")
        st, _ = self.run_am(self.o(-7), tu=feed(), alerts=a)
        self.assertEqual(st.status, DISRUPTED)
        self.assertTrue(st.problem)
        a = feed()
        add_alert(a, f"Train {self.T} Cancelled", f"BNSF train #{self.T} has been cancelled due to mechanical problems.")
        st, _ = self.run_am(self.o(-12), alerts=a)
        self.assertEqual(st.status, CANCELLED)

    def test_other_train_alert_not_attached(self):
        a = feed()
        add_alert(a, f"BNSF Train {self.OTHER} Delayed", f"BNSF train #{self.OTHER} scheduled to arrive Chicago Union Station is late.")
        add_alert(a, "Union Station Great Hall closed", "The Great Hall at Chicago Union Station is closed tomorrow.")
        st, _ = self.run_am(self.o(-12), alerts=a)
        self.assertEqual([x["relevance"] for x in st.alerts], ["station", "other_train"])
        self.assertEqual(st.status, SCHEDULED)

    def test_incoming_equipment_risk(self):
        inst, _ = self.pm.current_instance(self.at(WED, 16, 30))
        incoming = next(t for t in self.s.trips_on("BNSF", WED)
                        if t.direction_id == "1" and t.stops[-1].stop_id == "CUS"
                        and t.stops[-1].arr < inst.trip.stops[0].dep)
        m = feed()
        add_tu(m, inst.trip, WED, [], label=incoming.train, nodata=[s.stop_id for s in inst.trip.stops])
        late_arr = inst.sched_dep - timedelta(minutes=2)  # equipment arrives 2 min before our departure
        add_tu(m, incoming, WED, [("CUS", late_arr, None, None)])
        st, _ = self.run_am(self.p(-15), tu=m, trip=self.pm)
        self.assertEqual(st.status, SCHEDULED)
        self.assertEqual(st.extra["incoming_train"], incoming.train)
        self.assertEqual(st.extra["departure_risk_min"], 8)


    # ---------- QA regressions (2026-11-07) ----------
    def test_c1_odd_snapshot_does_not_lock_arrived(self):
        inst, _ = self.am.current_instance(self.o(-12))
        m = feed()  # origin row predicted, every other stop NO_DATA
        add_tu(m, inst.trip, WED, [self.stops_from(inst, 0)[0]], nodata=[s.stop_id for s in inst.trip.stops[1:]])
        st, _ = self.run_am(self.o(-3), tu=m)
        self.assertNotEqual(st.status, ARRIVED)
        m = feed()
        add_tu(m, inst.trip, WED, self.stops_from(inst, 6))
        st, _ = self.run_am(self.o(3), tu=m)
        self.assertEqual((st.status, st.delay_min), (LATE, 6))

    def test_c1_outage_then_live_overrides_assumed_arrival(self):
        st, _ = self.run_am(self.a(10), tu=feed(), fresh=False)
        self.assertEqual(st.status, ARRIVED)
        self.assertFalse(st.arrived)  # assumed, not evidenced
        inst, _ = self.am.current_instance(self.a(10))
        m = feed()
        add_tu(m, inst.trip, WED, self.stops_from(inst, 20, start_idx=inst.i_from + 1))
        st, _ = self.run_am(self.a(11), tu=m)
        self.assertEqual((st.status, st.delay_min), (DEPARTED, 20))

    def test_h1_terminal_ghost_one_delay_then_arrived(self):
        gate = EventGate({}, 5, 3)
        inst, _ = self.am.current_instance(self.o(-12))
        evs = []
        def step(now, tu):
            st, i = self.run_am(now, tu=tu)
            evs.extend(e["event_type"] for e in gate.evaluate(self.am, i, st))
            return st
        m = feed()
        add_tu(m, inst.trip, WED, self.stops_from(inst, 0, start_idx=inst.i_from + 1))
        step(self.d(13), m)
        for k in range(24):  # ghost (no GPS): CUS prediction stays 3 min ahead of the clock for 12 min after arrival
            now = self.a(0) + timedelta(seconds=30 * k)
            m = feed()
            add_tu(m, inst.trip, WED, [("CUS", now + timedelta(minutes=3), None, None)])
            st = step(now, m)
        self.assertEqual(st.status, ARRIVED)
        self.assertEqual(evs.count("delay"), 0)
        self.assertIn("arrived", evs)
        self.assertEqual(st.delay_min, 0)  # arrival time = when the slide began

    def test_n3_late_train_crawling_in_is_not_a_ghost(self):
        inst, _ = self.am.current_instance(self.o(-12))
        m = feed()
        add_tu(m, inst.trip, WED, self.stops_from(inst, 0, start_idx=inst.i_from + 1))
        self.run_am(self.d(13), tu=m)
        for k in range(16):  # from 6 min before arrival the prediction stays ~100 s ahead; GPS 3 km out
            now = self.a(-6) + timedelta(seconds=30 * k)
            m, p = feed(), feed()
            add_tu(m, inst.trip, WED, [("CUS", now + timedelta(seconds=100), None, None)])
            add_pos(p, inst.trip, WED, 41.8788889, -87.675, now)  # ~3 km west of CUS
            st, _ = self.run_am(now, tu=m, pos=p)
        self.assertNotEqual(st.status, ARRIVED)

    def test_gps_at_terminal_with_slide_is_arrived(self):
        inst, _ = self.am.current_instance(self.o(-12))
        m = feed()
        add_tu(m, inst.trip, WED, self.stops_from(inst, 0, start_idx=inst.i_from + 1))
        self.run_am(self.d(13), tu=m)
        for k in range(6):
            now = self.a(0) + timedelta(seconds=30 * k)
            m, p = feed(), feed()
            add_tu(m, inst.trip, WED, [("CUS", now + timedelta(minutes=3), None, None)])
            add_pos(p, inst.trip, WED, 41.8780, -87.6395, now)
            st, _ = self.run_am(now, tu=m, pos=p)
        self.assertEqual(st.status, ARRIVED)

    def test_n1_gps_before_station_is_not_departed(self):
        inst, _ = self.am.current_instance(self.o(-12))
        m, p = feed(), feed()
        add_tu(m, inst.trip, WED, self.stops_from(inst, 5))
        lat, lon = self.s.coords(self.am.from_id)
        add_pos(p, inst.trip, WED, lat, lon - 0.011, self.d(5, 20))  # ~0.9 km west of the station, approaching
        st, _ = self.run_am(self.d(5, 20), tu=m, pos=p)
        self.assertFalse(st.departed)

    def test_n2_feed_outage_does_not_extrapolate(self):
        inst, _ = self.pm.current_instance(self.at(WED, 16, 30))
        m = feed()
        add_tu(m, inst.trip, WED, self.stops_from(inst, 0))
        gate, evs = EventGate({}, 5, 3), []
        for minute in range(3, 21):
            st, i = self.run_am(self.p(minute - 5), tu=m, trip=self.pm, fresh=False)
            evs += [e["event_type"] for e in gate.evaluate(self.pm, i, st)]
        self.assertNotIn("delay", evs)
        self.assertLessEqual(st.delay_min or 0, 1)

    def test_n4_planned_future_suspension_is_not_a_halt(self):
        a = feed()
        add_alert(a, "BNSF Weekend Service Change", "BNSF weekend service between Aurora and Route 59 will be suspended "
                  "Saturday, November 14 and Sunday, November 15 for track work.")
        add_alert(a, "Parade", "Expect significant delays on Thursday, November 26 due to the parade.")
        st, _ = self.run_am(self.o(-7), tu=feed(), alerts=a)
        self.assertNotIn("disruption", st.extra)
        self.assertNotEqual(st.status, DISRUPTED)

    def test_h3_in_place_of_cancelled(self):
        a = feed()
        add_alert(a, "Service change", f"Train #{self.T} will make all stops in place of cancelled train #{self.OTHER}.")
        st, _ = self.run_am(self.o(-12), alerts=a)
        self.assertNotEqual(st.status, CANCELLED)

    def test_m8_advisory_and_holiday_train(self):
        a = feed()
        add_alert(a, f"BNSF Train {self.OTHER} Stopped", f"BNSF train #{self.OTHER} is stopped near Hinsdale. Following inbound trains "
                  "may be delayed 15 to 20 minutes.")
        add_alert(a, "Holiday Train", "Holiday Train 2026 visits Naperville on Saturday.")
        st, _ = self.run_am(self.o(-7), alerts=a)
        self.assertEqual(st.extra.get("advisory_delay_min"), 15)
        rels = {x["header"]: x["relevance"] for x in st.alerts}
        self.assertEqual(rels["Holiday Train"], "station")

    def test_h2_nodata_list_ahead_not_departed(self):
        trip = TrackedTrip(TripConfig("Hinsdale", "BNSF", "Clarendon Hills", "Chicago Union Station", "07:20"), self.s)
        inst, _ = trip.current_instance(self.o(-12))
        if inst is None or inst.trip is None:
            self.skipTest("no matching train")
        m = feed()
        add_tu(m, inst.trip, WED, [], nodata=[s.stop_id for s in inst.trip.stops[inst.i_from + 1:]])
        st, _ = self.run_am(inst.sched_dep + timedelta(minutes=1), tu=m, trip=trip)
        self.assertFalse(st.departed)

    def test_h3_board_other_train_is_not_cancel(self):
        a = feed()
        add_alert(a, f"BNSF Train {self.OTHER} Annulled", f"BNSF train #{self.OTHER} has been annulled. Passengers may board train #{self.T}.")
        inst, _ = self.am.current_instance(self.o(-12))
        m = feed()
        add_tu(m, inst.trip, WED, self.stops_from(inst, 0))
        st, _ = self.run_am(self.o(3), tu=m, alerts=a)
        self.assertEqual(st.status, ON_TIME)
        fresh = TrackedTrip(self.am.cfg, self.s)
        st, _ = self.run_am(self.o(-12), alerts=a, trip=fresh)
        self.assertEqual(st.status, SCHEDULED)

    def test_h5_line_halt_flags_problem(self):
        a = feed()
        add_alert(a, "BNSF Service Halted", "BNSF inbound and outbound train movement is halted near Naperville due to "
                  "police activity. Extensive delays are anticipated.")
        st, _ = self.run_am(self.o(-7), tu=feed(), alerts=a)
        self.assertEqual(st.status, DISRUPTED)
        self.assertTrue(st.problem)
        self.assertEqual(st.extra["disruption"]["scope"], "line")
        st, _ = self.run_am(self.at(WED, 5, 0), alerts=a)  # long before the trip: not applied
        self.assertNotIn("disruption", st.extra)

    def test_m2_origin_held_is_late_not_departed(self):
        inst, _ = self.pm.current_instance(self.at(WED, 16, 30))
        m = feed()
        add_tu(m, inst.trip, WED, self.stops_from(inst, 0))  # origin row stays at the scheduled departure
        st, _ = self.run_am(self.p(4), tu=m, trip=self.pm)
        self.assertFalse(st.departed)
        self.assertEqual((st.status, st.delay_min), (LATE, 4))

    def test_m5_restart_after_arrival_no_false_unconfirmed(self):
        runs = {}
        am = TrackedTrip(self.am.cfg, self.s, runs)
        inst, _ = am.current_instance(self.o(-12))
        m = feed()
        add_tu(m, inst.trip, WED, self.stops_from(inst, 0, start_idx=inst.i_from + 1))
        self.run_am(self.d(13), tu=m, trip=am)
        self.run_am(self.a(0, 30), tu=feed(), trip=am)  # left the feed at CUS
        restarted = TrackedTrip(self.am.cfg, self.s, runs)  # same persisted evidence
        st, _ = self.run_am(self.a(2), tu=feed(), trip=restarted)
        self.assertEqual(st.status, ARRIVED)
        self.assertTrue(st.arrived)

    def test_m7_leftover_alert_not_applied_next_day(self):
        a = feed()
        add_alert(a, f"BNSF Train {self.T} Stopped", f"BNSF train #{self.T} is stopped near Route 59 due to police activity.")
        st, inst = self.run_am(self.at(WED, 23, 0), alerts=a)  # shows Thursday's run
        self.assertEqual(inst.service_date, date(2026, 11, 5))
        self.assertEqual(st.status, SCHEDULED)
        self.assertFalse(st.problem)

    def test_m9_after_midnight_train(self):
        trip = TrackedTrip(TripConfig("Late", "BNSF", "Chicago Union Station", "Naperville", "00:33", "daily"),
                           self.s)
        st, inst = trip.compute(self.at(date(2026, 11, 5), 0, 20), {}, {}, [], 5, True)
        self.assertIsNotNone(inst.trip)
        self.assertEqual(inst.sched_dep.strftime("%m-%d %H:%M"), "11-05 00:33")
        self.assertTrue(st.active)

    def test_no_match_config_error(self):
        trip = TrackedTrip(TripConfig("Bad", "BNSF", "Naperville", "Chicago Union Station", "07:25"), self.s)
        st, _ = trip.compute(self.o(-12), {}, {}, [], 5, True)
        self.assertEqual(st.status, NO_MATCH)


@unittest.skipUnless(ZIP and os.path.exists(ZIP), "set METRA_TEST_ZIP")
class EventTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.s = Schedule(ZIP, ROUTES)

    def test_delay_thresholds(self):
        trip = TrackedTrip(TripConfig("Morning", "BNSF", "Naperville", "Chicago Union Station", "07:32"), self.s)
        gate = EventGate({}, threshold=5, change=3)
        inst, _ = trip.current_instance(datetime(2026, 11, 4, 7, 0, tzinfo=self.s.tz))

        def ev(delay, status=LATE):
            st = type("S", (), {})()
            st.status, st.delay_min, st.train, st.active, st.live = status, delay, inst.trip.train, True, True
            st.departed, st.arrived = status == "Departed", False
            st.sched_dep, st.arrival, st.detail, st.extra = inst.sched_dep, inst.sched_arr, "", {}
            return [e["event_type"] for e in gate.evaluate(trip, inst, st)]

        self.assertEqual(ev(4), [])
        self.assertEqual(ev(5), ["delay"])
        self.assertEqual(ev(6), [])
        self.assertEqual(ev(8), ["delay"])
        self.assertEqual(ev(4), [])  # within hysteresis
        self.assertEqual(ev(2, ON_TIME), [])  # recovery must hold for 2 polls
        self.assertEqual(ev(2, ON_TIME), ["recovered"])
        self.assertEqual(ev(6), ["delay"])
        # oscillating around the threshold doesn't spam
        self.assertEqual([x for d in (4, 5, 4, 3, 4, 5, 3, 5, 3) for x in ev(d)], [])


if __name__ == "__main__":
    unittest.main()
