# Metra Tracker

Tracks the Metra trains you ride and publishes Home Assistant entities and events over MQTT, plus a live map panel.

## Requirements
- A free Metra GTFS API token: https://metra.com/metra-gtfs-api
- The **Mosquitto broker** add-on and the **MQTT** integration (entities are created by MQTT discovery).

## Configuration
```yaml
api_token: "<your Metra API token>"
trips:
  - name: Morning                 # becomes sensor.metra_morning_status etc.
    line: BNSF                    # Metra route_id: BNSF, ME, HC, MD-N, MD-W, NCS, RI, SWS, UP-N, UP-NW, UP-W
    from_station: Naperville      # where you board (Metra's station name)
    to_station: Chicago Union Station
    departure: "07:32"            # your train's departure from from_station (24h); matched within 5 minutes
    days: weekdays                # weekdays | weekends | daily
  - name: Evening
    line: BNSF
    from_station: Chicago Union Station
    to_station: Naperville
    departure: "17:20"
    days: weekdays
commute_days: weekdays            # days "Commuting today" switches itself on (03:00)
delay_threshold_min: 5            # first delay event at this many minutes late
delay_change_min: 3               # further delay events when it changes by this much
log_level: info
```
Station names are matched loosely ("Union Station", "LaGrange Rd", "Stone Avenue" work). The log shows what each trip
resolved to; a typo or a time with no train shows the status **No matching train**.

## Entities
Per trip (e.g. `morning`):
- `sensor.metra_morning_status`, with attributes for train, times, alerts, alternatives and the details below
- `sensor.metra_morning_delay`, `sensor.metra_morning_departure`, `sensor.metra_morning_arrival`
- `binary_sensor.metra_morning_problem`

Shared:
- `switch.metra_commuting_today`: turn off on days you don't commute; no events are sent while it's off.
- `event.metra_events`: subscribe to this in automations.
- `sensor.metra_<line>_alerts`: active alerts for the line.
- `binary_sensor.metra_feed`: whether Metra's live feed is reachable.
- `sensor.metra_schedule_valid_until`.

## Statuses
- **Scheduled**: no live data yet.
- **Unconfirmed**: Metra isn't reporting the train after it should be running.
- **No prediction**: Metra lists the train but gives no times.
- **On time** / **Late**
- **Delayed**: a Metra alert says the train or the line is held, stopped or halted.
- **Departed** / **Arrived**: only declared on evidence (the stop leaves Metra's list, a prediction passes, or GPS).
- **Cancelled** / **Not stopping**
- **Not running today**: for example a holiday schedule; the status names the closest train that day.
- **Schedule unavailable** / **No matching train**

## Events (`event.metra_events`, attribute `event_type`)
- `delay` / `recovered`
- `delay_risk`: the equipment for a train that starts at your station is arriving late.
- `disruption`
- `cancelled` / `reinstated` / `not_stopping`
- `unconfirmed` / `confirmed`
- `departed` / `arrived`
- `alert_new` / `alert_cleared`

Every event carries `trip`, `train`, `message`, `delay_min` and `status`.

## Live map
Open **Metra Map** in the sidebar (enable "Show in sidebar" on the add-on's Info tab). Map data comes from
© OpenStreetMap contributors. The page loads Leaflet from unpkg.com.

## Limits of Metra's data
- Trains appear in the live feed only about 4 minutes before leaving their first stop.
- There is no platform or crowding data.
- Cancellations are rarely flagged in the feed, so the add-on also reads alert text.
- Metra's server often fails briefly. The add-on keeps the last good data and shows how old it is.
