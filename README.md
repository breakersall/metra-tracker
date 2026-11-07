# Metra Tracker for Home Assistant

A Home Assistant add-on that follows the Metra trains you ride. It turns Metra's live data into entities, events and a
live map, so you can get a push when your train is late, hear a morning brief on a speaker, or change a light's colour.

- **Your trips**: pick a line, a boarding station, a destination and your usual departure time. Each day the add-on
  finds that day's train (holidays and schedule changes included).
- **Live status**:
  - States: Scheduled, On time, Late, Departed, Arrived, Delayed, Cancelled, Not stopping, Unconfirmed, Not running today.
  - Delay figures plus predicted departure and arrival times.
- **Smart about Metra's data**: Metra's feed has quirks: trains that keep reporting after arriving, GPS dropouts, and
  cancellations that only appear in alert text. Departure and arrival are only declared on evidence.
- **Alerts**: line alerts, plus alerts about *your* train or stations. Each alert is read clause by clause, so
  "#1101 annulled, passengers may board #1103" doesn't cancel 1103. Line-wide halts flag your trip.
- **Late equipment**: for trains that start at your station, it warns when the inbound train bringing your equipment is
  running late.
- **Events for automations**: delay, delay_risk, disruption, cancelled, departed, arrived, alert_new and more.
- **Commuting today switch**: turn it off when you work from home and the add-on stays quiet.
- **Live map**: a sidebar panel showing your line, stations, every live train coloured by delay, and your trips.

Everything runs locally. The add-on talks only to Metra's API. The map page loads OpenStreetMap tiles and Leaflet
from unpkg.com in your browser.

## Install

1. Get a free API token at <https://metra.com/metra-gtfs-api>.
2. Make sure the **Mosquitto broker** add-on and the **MQTT** integration are set up.
3. Add the add-on, either way:
   - **Add-on repository (recommended):** in Home Assistant go to Settings → Add-ons → Add-on store → ⋮ →
     Repositories, and add `https://github.com/breakersall/metra-tracker`. Updates then show up in Settings.
   - **Local add-on:** copy the `metra_tracker` folder into the `/addons` share (with the Samba or SSH add-on). Then go to
     Add-on store → ⋮ → Check for updates; it appears under **Local add-ons**.
4. Install **Metra Tracker**, open its **Configuration** tab, add your token and trips, and start it. See the add-on's
   Documentation tab for the full option reference.
5. To get the **Metra Map** panel, turn on **Show in sidebar** on the add-on's Info tab.

Example configuration:
```yaml
api_token: "<your token>"
trips:
  - name: Morning
    line: BNSF
    from_station: Naperville
    to_station: Chicago Union Station
    departure: "07:32"
    days: weekdays
  - name: Evening
    line: BNSF
    from_station: Chicago Union Station
    to_station: Naperville
    departure: "17:20"
    days: weekdays
```

## Dashboards and automations

The `examples/` folder has:
- `dashboard.yaml`: a sections dashboard (needs Mushroom from HACS) with status cards, other trains, alerts and a map link.
- `commute_summary.yaml`: a one-line "next train" template sensor.
- `automations.yaml`: a push for problems, a morning brief (push and TTS), and a problem light.

## Development

```bash
# tests need the Metra schedule zip and the GTFS-RT bindings
curl -o /tmp/schedule.zip https://schedules.metrarail.com/gtfs/schedule.zip
pip install gtfs-realtime-bindings protobuf
METRA_TEST_ZIP=/tmp/schedule.zip PYTHONPATH=metra_tracker python -m unittest discover -s tests -v

# run locally without MQTT (prints what it would publish), map at http://localhost:8099
mkdir -p /tmp/metra && cp /tmp/schedule.zip /tmp/metra/   # options.json goes in /tmp/metra too
METRA_DATA=/tmp/metra DRY_RUN=1 METRA_WEB_OPEN=1 PYTHONPATH=metra_tracker python -m app
```

The tests use a fictional commute on the BNSF line and work out their times from the current schedule, so they keep
working when Metra republishes it.

## Notes and limits
- Trains show up in Metra's live feed only a few minutes before leaving their first stop. Earlier than that, status
  comes from the schedule and alerts.
- Metra provides no platform, track or crowding data.
- Metra's servers often fail for a few seconds at a time. The add-on keeps the last good data and polls on (every
  30 s around your trips, every few minutes otherwise).
- Metra has announced a new trip_id format. The add-on keys trains on line, train number and service date, and parses
  both formats.

Not affiliated with or endorsed by Metra. Schedule and real-time data © Metra.

## License
MIT
