"""Service alerts: filter to lines and classify what each alert says about a specific train.

Metra alerts only carry route_id (rarely trip_id), no active period, cause/effect UNKNOWN, so meaning comes from
text. Text is split into clauses and every clause "owns" the train numbers it names (clauses with no number inherit
the previous clause's), so "#1101 annulled, passengers may board #1103" cancels 1101, not 1103.
"""
import re
from dataclasses import dataclass, field

from .trainid import parse_trip_id

_NUM = r"\d{3,4}"
# "train 1103", "Train #1103", "train no. 1103", "Trains 1101, 1103 and 1105"
TRAIN_LIST = re.compile(rf"(?<!holiday )\btrains?\s*(?:no\.?\s*|#\s*)?({_NUM}(?:\s*(?:,|and|&|or)\s*#?\s*{_NUM})*)\b", re.I)
# bare "#1103" (Metra writes "train #2010" but also "#1103 will make additional stops")
HASH_NUM = re.compile(rf"(?<!door )(?<!gate )(?<!track )(?<!platform )(?<!exit )(?<!lot )#\s*({_NUM})\b", re.I)
SENTENCES = re.compile(r"(?<=[.;!?])\s+|\n+")
CLAUSES = re.compile(r",\s*(?!\s*(?:and\s+|or\s+)?#?\s*\d)|\s+(?=passengers\b)|\s+(?=instead\b)", re.I)

CANCEL = re.compile(r"\b(cancel(?:l?ed|lation)?|annulled|will not (?:operate|run)|not operating|has been terminated)\b", re.I)
DISRUPT = re.compile(r"\b(stopped|stalled|held|holding|standing|disabled|police|mechanical|signal (?:problem|issue)s?|"
                     r"crossing (?:incident|accident)|freight (?:interference|train)|medical emergency|trespasser|"
                     r"equipment (?:problem|issue)s?|switch (?:problem|issue)s?)\b", re.I)
# Present-tense stoppage wording only ("will be suspended Saturday" is planned work, not a halt now).
HALT = re.compile(r"\b((?:is|are|has been|have been|remains?|currently)\s+(?:halted|suspended|stopped|not moving|"
                  r"not running|not operating)|train movement is (?:halted|stopped)|no (?:inbound |outbound )?trains? "
                  r"(?:are |is )?(?:running|operating|moving)|(?:extensive|significant|major|lengthy) delays "
                  r"(?:are|is) (?:anticipated|expected|occurring|likely)|service (?:is|has been) (?:disrupted|interrupted))\b",
                  re.I)
FUTURE = re.compile(r"\bwill\b(?!.*\b(?:today|this morning|this afternoon|this evening|tonight|now)\b)", re.I)
WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
MONTHS = ["january", "february", "march", "april", "may", "june", "july", "august", "september", "october",
          "november", "december"]
DATE_RX = re.compile(r"\b(" + "|".join(WEEKDAYS) + r")\b|\b(" + "|".join(m[:3] for m in MONTHS) +
                     r")[a-z]*\.?\s+(\d{1,2})\b|\b(\d{1,2})/(\d{1,2})\b", re.I)
GENERAL_TRAINS = re.compile(r"\b(?:following|other|all|some|several|subsequent)?\s*(inbound|outbound)?\s*trains\b", re.I)
NOT_STOP = re.compile(r"\b(?:will not (?:stop|make (?:a |its )?(?:scheduled )?stop) at|(?:will )?(?:skip|bypass)(?:ping)?)"
                      r"\s+([A-Z][\w.' -]{2,40})", re.I)
MINUTES = [
    re.compile(r"(\d{1,3})\s*(?:to|-|–)\s*\d{1,3}\s*min", re.I),
    re.compile(r"(?:approximately|about|around|roughly|up to|nearly|over|at least)\s*(\d{1,3})\s*min", re.I),
    re.compile(r"(\d{1,3})\s*min(?:ute)?s?\s*(?:late|behind|delay)", re.I),
    re.compile(r"delayed\s*(?:by\s*)?(\d{1,3})\s*min", re.I),
]
STATION_ALIASES = {"CUS": ["union station", "cus"]}


@dataclass
class Alert:
    id: str
    routes: set
    trains: set  # (route, train) from informed_entity
    header: str
    description: str
    url: str
    agency_wide: bool = False

    @property
    def text(self):
        return f"{self.header}. {self.description}"


@dataclass
class AlertInfo:
    relevance: str = None  # train | station | line | other_train | None
    cancelled: bool = False
    disrupted: bool = False
    halt: bool = False  # line-wide stoppage
    delay_min: int = None
    not_stopping: list = field(default_factory=list)  # station names this train will skip
    advisory_delay_min: int = None  # "following inbound trains may be delayed 15 to 20 min" (not about one train)


def _translation(ts):
    for t in ts.translation:
        if t.language in ("", "en", "EN"):
            return t.text.strip()
    return ts.translation[0].text.strip() if ts.translation else ""


def parse_alerts(msg):
    out = []
    if msg is None:
        return out
    for ent in msg.entity:
        if not ent.HasField("alert"):
            continue
        a = ent.alert
        routes, trains, agency_wide = set(), set(), False
        for ie in a.informed_entity:
            if ie.route_id:
                routes.add(ie.route_id)
            if ie.trip.trip_id:
                r, t = parse_trip_id(ie.trip.trip_id)
                if t:
                    trains.add((ie.trip.route_id or r, t))
                    routes.add(ie.trip.route_id or r)
            if not ie.route_id and not ie.trip.trip_id and not ie.stop_id:
                agency_wide = True
        out.append(Alert(ent.id, routes, trains, _translation(a.header_text), _translation(a.description_text),
                         _translation(a.url), agency_wide))
    return out


def _numbers(text):
    nums = []
    for m in TRAIN_LIST.finditer(text):
        nums += re.findall(_NUM, m.group(1))
    nums += HASH_NUM.findall(text)
    return list(dict.fromkeys(nums))


def clauses(alert):
    """[(clause_text, {train numbers it refers to})] with number inheritance between clauses."""
    out = []
    for sentence in SENTENCES.split(alert.text):
        owner = set()
        for clause in CLAUSES.split(sentence):
            if not clause or not clause.strip():
                continue
            nums = set(_numbers(clause))
            if nums:
                owner = nums
            out.append((clause, set(owner)))
    return out


def _about(clause, rx, train):
    """Does wording matched by rx in this clause apply to `train`? With several numbers in one clause the word
    applies to the nearest number ("#1103 will make all stops in place of cancelled train #1101" -> 1101)."""
    m = rx.search(clause)
    if not m:
        return False
    spots = [(n.start(1), n.group(1)) for n in re.finditer(rf"(?<!\d)({_NUM})(?!\d)", clause)
             if n.group(1) in set(_numbers(clause))]
    if len({n for _, n in spots}) <= 1:
        return True
    nearest = min(spots, key=lambda s: abs(s[0] - m.start()))[1]
    return nearest == train


def is_current(clause, now):
    """False if the clause names a weekday/date that isn't today (alerts carry no active period)."""
    if now is None:
        return True
    for m in DATE_RX.finditer(clause):
        if m.group(1) and m.group(1).lower() != WEEKDAYS[now.weekday()]:
            return False
        if m.group(2):
            mon = [x[:3] for x in MONTHS].index(m.group(2).lower()[:3]) + 1
            if (mon, int(m.group(3))) != (now.month, now.day):
                return False
        if m.group(4) and (int(m.group(4)), int(m.group(5))) != (now.month, now.day):
            return False
    return True


def mentioned_trains(alert):
    return {t for _, t in alert.trains} | set(_numbers(alert.text))


def mentions_station(alert, stop_id, stop_name):
    names = [stop_name.lower()] + STATION_ALIASES.get(stop_id, [])
    if stop_name.lower().startswith("chicago "):
        names.append(stop_name.lower()[len("chicago "):])
    text = alert.text.lower()
    return any(re.search(rf"\b{re.escape(n)}\b", text) for n in names if n)


def minutes(text):
    for rx in MINUTES:
        m = rx.search(text)
        if m:
            return int(m.group(1))
    return None


def classify(alert, route, train, stations, now=None, direction=None):
    """What does this alert mean for `train` on `route`? stations = [(stop_id, name), ...].
    now: local datetime, to ignore wording about other dates. direction: 'inbound'/'outbound' of our train."""
    info = AlertInfo()
    on_line = route in alert.routes or alert.agency_wide
    ours_informed = (route, train) in alert.trains
    parts = [(c, o) for c, o in clauses(alert) if is_current(c, now)]
    ours = [c for c, owners in parts if train in owners]
    if ours_informed and not any(owners for _, owners in parts):
        ours = [c for c, _ in parts]  # trip-tagged alert that never names a number: whole text is about us
    if not (on_line or ours):
        return info
    if ours:
        info.relevance = "train"
        text = " ".join(ours)
        info.cancelled = any(_about(c, CANCEL, train) for c in ours)
        info.disrupted = any(_about(c, DISRUPT, train) for c in ours)
        info.delay_min = minutes(text)
        for m in NOT_STOP.finditer(text):
            target = m.group(1).lower()
            for sid, name in stations:
                n = name.lower()
                if n in target or target.startswith(n.replace("chicago ", "")):
                    info.not_stopping.append(name)
    elif any(not owners and GENERAL_TRAINS.search(c) and minutes(c) for c, owners in parts):
        info.relevance = "line"  # e.g. "Following inbound trains may be delayed 15 to 20 minutes"
    elif mentioned_trains(alert):
        info.relevance = "other_train"
    elif any(mentions_station(alert, sid, name) for sid, name in stations):
        info.relevance = "station"
    else:
        info.relevance = "line"
    # A line-wide stoppage: halt wording in a clause that isn't about some other specific train.
    for c, owners in parts:
        g = GENERAL_TRAINS.search(c)
        if not owners and g and minutes(c) and (not g.group(1) or not direction or g.group(1).lower() == direction):
            info.advisory_delay_min = minutes(c)
    info.halt = on_line and any(HALT.search(c) and not FUTURE.search(c) and (not owners or train in owners)
                                for c, owners in parts)
    return info


def for_line(alerts, route):
    return [a for a in alerts if route in a.routes or a.agency_wide]


def dedupe(alerts):
    """Station-wide alerts are copied once per line; keep one per header+description."""
    seen, out = set(), []
    for a in alerts:
        key = (a.header.lower(), a.description.lower())
        if key not in seen:
            seen.add(key)
            out.append(a)
    return out
