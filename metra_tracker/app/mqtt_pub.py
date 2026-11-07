"""MQTT discovery + state publishing for Home Assistant."""
import json
import logging
import queue

log = logging.getLogger(__name__)

PREFIX = "homeassistant"
BASE = "metra_tracker"
AVAIL = f"{BASE}/status"
EVENT_TYPES = ["delay", "delay_risk", "disruption", "recovered", "cancelled", "reinstated", "not_stopping",
               "unconfirmed", "confirmed", "departed", "arrived",
               "alert_new", "alert_cleared", "brief"]
HUB_DEVICE = {"identifiers": ["metra_tracker"], "name": "Metra Tracker", "manufacturer": "Metra (GTFS)",
              "model": "Metra Tracker add-on"}


def _json(v):
    return json.dumps(v, default=lambda o: o.isoformat() if hasattr(o, "isoformat") else str(o))


class Publisher:
    def __init__(self, host=None, port=1883, username=None, password=None, dry_run=False, version="0"):
        self.dry_run = dry_run
        self.commands = queue.Queue()  # (topic, payload) from the MQTT thread
        self.version = version
        self._last = {}  # topic -> payload (avoid re-publishing unchanged retained states)
        self.client = None
        if dry_run:
            return
        import paho.mqtt.client as mqtt
        c = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="metra_tracker")
        if username:
            c.username_pw_set(username, password)
        c.will_set(AVAIL, "offline", retain=True)
        c.on_connect = self._on_connect
        c.on_message = lambda _c, _u, msg: self.commands.put((msg.topic, msg.payload.decode("utf-8", "replace")))
        c.reconnect_delay_set(1, 60)
        c.connect_async(host, port, keepalive=60)
        c.loop_start()
        self.client = c

    def _on_connect(self, client, _userdata, _flags, reason_code, _props=None):
        log.info("MQTT connected (%s)", reason_code)
        client.subscribe([(f"{BASE}/+/set", 0), (f"{PREFIX}/status", 0)])
        client.publish(AVAIL, "online", retain=True)
        self.commands.put(("__connected__", ""))

    def publish(self, topic, payload, retain=True, force=False):
        data = payload if isinstance(payload, str) else _json(payload)
        if retain and not force and self._last.get(topic) == data:
            return
        self._last[topic] = data
        if self.dry_run:
            log.info("PUB %s %s", topic, data[:300])
            return
        self.client.publish(topic, data, qos=1, retain=retain)

    def forget(self):
        """Force the next publish of every topic (after HA restarts)."""
        self._last.clear()

    def stop(self):
        if self.client:
            self.client.publish(AVAIL, "offline", retain=True).wait_for_publish(3)
            self.client.loop_stop()
            self.client.disconnect()

    # ---------- discovery ----------
    def _disc(self, component, object_id, cfg):
        cfg = dict(cfg, availability_topic=AVAIL, unique_id=f"metra_tracker_{object_id}",
                   default_entity_id=f"{component}.metra_{object_id}")
        cfg.setdefault("device", HUB_DEVICE)
        cfg["device"] = dict(cfg["device"], sw_version=self.version)
        self.publish(f"{PREFIX}/{component}/metra_tracker_{object_id}/config", cfg)

    def remove_trip(self, slug):
        for comp, suffix in (("sensor", "status"), ("sensor", "delay"), ("sensor", "departure"), ("sensor", "arrival"),
                             ("binary_sensor", "problem")):
            self.publish(f"{PREFIX}/{comp}/metra_tracker_{slug}_{suffix}/config", "", force=True)
        for topic in (f"{BASE}/trip/{slug}/state", f"{BASE}/trip/{slug}/attributes"):
            self.publish(topic, "", force=True)

    def discover_hub(self, lines):
        self._disc("switch", "commuting_today", {
            "name": "Commuting today", "icon": "mdi:train-car",
            "command_topic": f"{BASE}/commuting/set", "state_topic": f"{BASE}/commuting/state"})
        self._disc("binary_sensor", "feed", {
            "name": "Live feed", "device_class": "connectivity", "entity_category": "diagnostic",
            "state_topic": f"{BASE}/feed/state", "value_template": "{{ value_json.ok }}",
            "json_attributes_topic": f"{BASE}/feed/state"})
        self._disc("sensor", "schedule_valid_until", {
            "name": "Schedule valid until", "device_class": "date", "icon": "mdi:calendar-clock",
            "entity_category": "diagnostic", "state_topic": f"{BASE}/schedule/state",
            "value_template": "{{ value_json.valid_until }}", "json_attributes_topic": f"{BASE}/schedule/state"})
        self._disc("event", "events", {
            "name": "Events", "icon": "mdi:train-variant", "event_types": EVENT_TYPES,
            "state_topic": f"{BASE}/events"})
        for line in lines:
            lid = line.lower().replace("-", "_")
            self._disc("sensor", f"{lid}_alerts", {
                "name": f"{line} alerts", "icon": "mdi:alert-outline", "state_class": "measurement",
                "state_topic": f"{BASE}/line/{line}/alerts", "value_template": "{{ value_json.count }}",
                "json_attributes_topic": f"{BASE}/line/{line}/alerts",
                "json_attributes_template": "{{ {'alerts': value_json.alerts} | tojson }}"})

    def discover_trip(self, tracked):
        cfg, slug = tracked.cfg, tracked.cfg.slug
        s = tracked.schedule
        device = {"identifiers": [f"metra_tracker_{slug}"], "name": f"Metra {cfg.name}", "manufacturer": "Metra",
                  "model": f"{cfg.line} {s.stop_name(tracked.from_id)} → {s.stop_name(tracked.to_id)} {cfg.departure}",
                  "via_device": "metra_tracker"}
        state = f"{BASE}/trip/{slug}/state"
        attrs = f"{BASE}/trip/{slug}/attributes"
        common = {"device": device, "state_topic": state}
        self._disc("sensor", f"{slug}_status", dict(common, name="Status", icon="mdi:train",
                   value_template="{{ value_json.status }}", json_attributes_topic=attrs))
        self._disc("sensor", f"{slug}_delay", dict(common, name="Delay", icon="mdi:timer-alert-outline",
                   unit_of_measurement="min", state_class="measurement", value_template="{{ value_json.delay_min }}"))
        self._disc("sensor", f"{slug}_departure", dict(common, name="Departure", device_class="timestamp",
                   value_template="{{ value_json.departure }}"))
        self._disc("sensor", f"{slug}_arrival", dict(common, name="Arrival", device_class="timestamp",
                   value_template="{{ value_json.arrival }}"))
        self._disc("binary_sensor", f"{slug}_problem", dict(common, name="Problem", device_class="problem",
                   value_template="{{ value_json.problem }}"))

    # ---------- states ----------
    def trip_state(self, tracked, st, extra):
        slug = tracked.cfg.slug
        self.publish(f"{BASE}/trip/{slug}/state", {
            "status": st.status, "delay_min": st.delay_min,
            "departure": st.departure.isoformat() if st.departure else None,
            "arrival": st.arrival.isoformat() if st.arrival else None,
            "problem": "ON" if st.problem else "OFF"})
        s = tracked.schedule
        self.publish(f"{BASE}/trip/{slug}/attributes", dict({
            "train": st.train, "line": tracked.cfg.line, "service_date": st.service_date,
            "from": s.stop_name(tracked.from_id), "to": s.stop_name(tracked.to_id),
            "scheduled_departure": st.sched_dep, "scheduled_arrival": st.sched_arr,
            "predicted_departure": st.departure, "predicted_arrival": st.arrival,
            "delay_min": st.delay_min, "detail": st.detail, "live": st.live, "data_age_s": st.data_age_s,
            "position": st.position, "alerts": st.alerts, "alternatives": st.alternatives}, **st.extra, **extra))

    def event(self, payload):
        self.publish(f"{BASE}/events", payload, retain=False)
