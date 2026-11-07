"""Parse Metra trip_ids into (route, train number).

Two formats exist:
  current:   BNSF_BN1230_V2_AA      UP-NW_UNW644_V2_AA     MD-N_MN2128_V1_AA
  announced: BNSF_1230_8_2009_5682524 (trip_ids change weekly)
The suffixes change every service period, so trips are keyed on route + train number + service date.
"""
import re

_TRIP_RE = re.compile(r"^(?P<route>[A-Z0-9-]+?)_(?P<prefix>[A-Z]*)(?P<train>\d+)_")


def parse_trip_id(trip_id):
    """Return (route_id, train_number) or (None, None) if unrecognised."""
    m = _TRIP_RE.match((trip_id or "").strip())
    if not m:
        return None, None
    return m.group("route"), m.group("train")
