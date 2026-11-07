# Changelog

## 0.4.2
- A trip that is late by the delay threshold or more (before departure or en route) now sets its Problem sensor.
- After arrival, the departure sensor keeps the actual departure time instead of reverting to the schedule.

## 0.4.1
- Clear message when no token/trips are configured; generic defaults and docs.

## 0.4.0
- Live map panel (Home Assistant ingress): line, stations, live trains coloured by delay, your trips, service alerts.

## 0.3.x
- Evidence-based Departed/Arrived (stop leaves Metra's list, prediction passes, GPS); "assumed" arrivals never notify.
- Terminal "ghost" detection, origin holds shown as Late, GPS dropout handling ("No prediction").
- Alert parsing by clause: train-specific cancellations/delays/disruptions, present-tense line halts, planned work ignored.
- Delay/recovered hysteresis; quiet catch-up when "Commuting today" is switched on mid-trip.
- Crash safety: new schedules are validated before replacing the last good one; the main loop never dies.
- Trains after midnight, station-name normalisation, "No matching train" for config mistakes.

## 0.2.0
- First version: schedule, realtime matching, MQTT entities and events.
