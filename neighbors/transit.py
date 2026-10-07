"""Offline NYC-area transit commute estimates (pure — no network, no I/O).

Given an apartment and an "important place" (work, a friend's apartment),
answer: roughly how long by public transit, and exactly what the trip looks
like — an ordered list of STEPS (walk / ride / transfer), each with minutes.

Method: nearest station to each end, then
  walk in + wait + ride (great-circle distance at an effective speed that
  already includes stops) + walk out, plus a transfer penalty when the two
  stations share no line.

These are ESTIMATES — good for comparing apartments against each other, not
for catching a train. The UI links every commute to live Apple/Google Maps
transit directions for the exact answer.

The station table is a curated subset (~105 stations) covering the
neighborhoods this app knows about, INCLUDING the PATH system — so Jersey
City / Hoboken destinations route correctly instead of falling back to
"no nearby subway". Coordinates are approximate (±100 m), fine at
walking-time resolution. Lines are '/'-separated names ("J/M/Z", "PATH").
"""

from __future__ import annotations

from dataclasses import dataclass

from .knn import haversine_km

WALK_KMH = 4.8            # brisk city walking
SUBWAY_KMH = 28.0         # effective speed incl. stops
WAIT_MIN = 5.0            # platform wait, folded into the first ride step
TRANSFER_MIN = 8.0        # penalty when no shared line (wait + walk inside)
MAX_STATION_WALK_KM = 2.0 # beyond this there's effectively no nearby station
WALK_ONLY_KM = 1.2        # closer than this: just walk

# (name, lines, lat, lon)
STATIONS: list[tuple[str, str, float, float]] = [
    # L
    ("Bedford Av", "L", 40.7172, -73.9567),
    ("Lorimer St / Metropolitan Av", "L/G", 40.7140, -73.9520),
    ("Graham Av", "L", 40.7146, -73.9441),
    ("Grand St (Bklyn)", "L", 40.7118, -73.9403),
    ("Montrose Av", "L", 40.7076, -73.9399),
    ("Morgan Av", "L", 40.7061, -73.9331),
    ("Jefferson St", "L", 40.7068, -73.9229),
    ("DeKalb Av (Bushwick)", "L", 40.7036, -73.9184),
    ("Myrtle-Wyckoff Avs", "L/M", 40.6995, -73.9109),
    ("1 Av", "L", 40.7308, -73.9817),
    ("3 Av", "L", 40.7327, -73.9861),
    # G
    ("Greenpoint Av", "G", 40.7313, -73.9542),
    ("Nassau Av", "G", 40.7245, -73.9514),
    ("Broadway (Bklyn G)", "G", 40.7061, -73.9503),
    ("Flushing Av (G)", "G", 40.7003, -73.9503),
    ("Myrtle-Willoughby Avs", "G", 40.6945, -73.9490),
    ("Bedford-Nostrand Avs", "G", 40.6896, -73.9535),
    ("Classon Av", "G", 40.6889, -73.9600),
    ("Clinton-Washington Avs (G)", "G", 40.6883, -73.9668),
    ("Fulton St (G)", "G", 40.6873, -73.9754),
    ("Hoyt-Schermerhorn", "G/A/C", 40.6884, -73.9851),
    # J M Z
    ("Marcy Av", "J/M/Z", 40.7084, -73.9578),
    ("Hewes St", "J/M", 40.7069, -73.9532),
    ("Lorimer St (JM)", "J/M", 40.7038, -73.9474),
    ("Flushing Av (JM)", "J/M", 40.7003, -73.9412),
    ("Myrtle Av-Broadway", "J/M/Z", 40.6972, -73.9354),
    ("Gates Av", "J/Z", 40.6899, -73.9221),
    ("Essex-Delancey", "J/M/Z/F", 40.7185, -73.9880),
    # A C E
    ("High St", "A/C", 40.6994, -73.9905),
    ("Jay St-MetroTech", "A/C/F/R", 40.6922, -73.9856),
    ("Nostrand Av (AC)", "A/C", 40.6805, -73.9505),
    ("Utica Av (AC)", "A/C", 40.6795, -73.9308),
    ("14 St / 8 Av", "A/C/E/L", 40.7402, -74.0021),
    ("W 4 St", "A/C/E/B/D/F/M", 40.7322, -74.0003),
    ("Fulton St (Manhattan)", "A/C/2/3/4/5/J/Z", 40.7102, -74.0080),
    ("168 St", "A/C/1", 40.8404, -73.9397),
    ("181 St (A)", "A", 40.8518, -73.9380),
    ("Inwood-207 St", "A", 40.8680, -73.9198),
    ("125 St (ACBD)", "A/C/B/D", 40.8111, -73.9524),
    # 1 2 3
    ("96 St (123)", "1/2/3", 40.7939, -73.9722),
    ("72 St (123)", "1/2/3", 40.7787, -73.9820),
    ("Times Sq-42 St", "1/2/3/7/N/Q/R/W/S", 40.7558, -73.9871),
    ("14 St (123)", "1/2/3/F/M/L", 40.7376, -74.0003),
    ("Chambers St (123)", "1/2/3", 40.7153, -74.0093),
    ("125 St (23)", "2/3", 40.8076, -73.9454),
    ("231 St", "1", 40.8788, -73.9048),
    ("Marble Hill-225 St", "1", 40.8743, -73.9098),
    # 4 5 6
    ("Grand Central-42 St", "4/5/6/7/S", 40.7527, -73.9772),
    ("Union Sq-14 St", "4/5/6/N/Q/R/W/L", 40.7349, -73.9906),
    ("86 St (456)", "4/5/6", 40.7794, -73.9559),
    ("125 St (456)", "4/5/6", 40.8043, -73.9375),
    ("33 St (6)", "6", 40.7461, -73.9820),
    ("Astor Pl", "6", 40.7300, -73.9915),
    ("Borough Hall", "2/3/4/5/R", 40.6933, -73.9898),
    ("Wall St (45)", "4/5", 40.7069, -74.0091),
    ("Bowling Green", "4/5", 40.7048, -74.0140),
    ("149 St-Grand Concourse", "2/4/5", 40.8183, -73.9273),
    ("161 St-Yankee Stadium", "4/B/D", 40.8279, -73.9257),
    ("Fordham Rd (4)", "4", 40.8624, -73.9012),
    ("Fordham Rd (BD)", "B/D", 40.8613, -73.8977),
    # N Q R W
    ("Canal St", "N/Q/R/W/6/J/Z", 40.7191, -74.0018),
    ("Atlantic Av-Barclays", "B/D/N/Q/R/2/3/4/5", 40.6841, -73.9787),
    ("DeKalb Av (Bklyn)", "B/Q/R", 40.6905, -73.9818),
    ("36 St (Sunset Park)", "D/N/R", 40.6550, -74.0035),
    ("45 St (R)", "R", 40.6489, -74.0100),
    ("59 St (NR)", "N/R", 40.6414, -74.0177),
    ("86 St (R Bay Ridge)", "R", 40.6226, -74.0284),
    ("Bay Ridge Av", "R", 40.6350, -74.0234),
    # Astoria / LIC / Queens
    ("Astoria-Ditmars Blvd", "N/W", 40.7750, -73.9121),
    ("Astoria Blvd", "N/W", 40.7700, -73.9179),
    ("30 Av", "N/W", 40.7666, -73.9214),
    ("Broadway (Astoria)", "N/W", 40.7617, -73.9252),
    ("36 Av", "N/W", 40.7566, -73.9299),
    ("Queensboro Plaza", "7/N/W", 40.7508, -73.9401),
    ("Court Sq", "7/G/E/M", 40.7470, -73.9454),
    ("Vernon Blvd-Jackson Av", "7", 40.7426, -73.9538),
    ("Hunters Point Av", "7", 40.7423, -73.9489),
    ("46 St (MR)", "M/R", 40.7566, -73.9134),
    ("Steinway St", "M/R", 40.7570, -73.9204),
    ("Jackson Hts-Roosevelt Av", "E/F/M/R/7", 40.7466, -73.8914),
    ("Woodside-61 St", "7", 40.7454, -73.9029),
    ("52 St (7)", "7", 40.7444, -73.9126),
    ("40 St-Lowery St", "7", 40.7434, -73.9240),
    ("Forest Hills-71 Av", "E/F/M/R", 40.7215, -73.8444),
    ("Rego Park-63 Dr", "M/R", 40.7297, -73.8617),
    ("Flushing-Main St", "7", 40.7596, -73.8301),
    ("21 St-Queensbridge", "F", 40.7544, -73.9425),
    # F / B D and south Brooklyn
    ("2 Av", "F", 40.7231, -73.9899),
    ("Grand St (Manhattan)", "B/D", 40.7182, -73.9938),
    ("Bergen St (FG)", "F/G", 40.6862, -73.9908),
    ("Carroll St", "F/G", 40.6795, -73.9950),
    ("Smith-9 Sts", "F/G", 40.6737, -73.9959),
    ("7 Av (Park Slope)", "F/G", 40.6663, -73.9800),
    ("Church Av (BQ)", "B/Q", 40.6501, -73.9628),
    ("Cortelyou Rd", "Q", 40.6409, -73.9639),
    ("Church Av (25)", "2/5", 40.6509, -73.9493),
    # Ridgewood / Bushwick M
    ("Forest Av", "M", 40.7044, -73.9036),
    ("Fresh Pond Rd", "M", 40.7062, -73.8957),
    ("Seneca Av", "M", 40.7027, -73.9077),
    # Harlem / uptown east
    ("116 St (23)", "2/3", 40.8020, -73.9497),
    ("116 St (6)", "6", 40.7986, -73.9418),
    # PATH — connects Manhattan with Jersey City / Hoboken
    ("World Trade Center (PATH)", "PATH", 40.7126, -74.0099),
    ("Christopher St (PATH)", "PATH", 40.7331, -74.0070),
    ("9 St (PATH)", "PATH", 40.7341, -73.9989),
    ("14 St (PATH)", "PATH", 40.7373, -73.9968),
    ("23 St (PATH)", "PATH", 40.7429, -73.9928),
    ("33 St (PATH)", "PATH", 40.7486, -73.9884),
    ("Hoboken (PATH)", "PATH", 40.7349, -74.0273),
    ("Newport (PATH)", "PATH", 40.7267, -74.0339),
    ("Exchange Place (PATH)", "PATH", 40.7162, -74.0329),
    ("Grove St (PATH)", "PATH", 40.7195, -74.0431),
    ("Journal Square (PATH)", "PATH", 40.7328, -74.0632),
]


@dataclass
class Station:
    name: str
    lines: set[str]
    lat: float
    lon: float
    walk_km: float

    @property
    def walk_min(self) -> int:
        return max(1, round(self.walk_km / WALK_KMH * 60))

    @property
    def lines_str(self) -> str:
        return "/".join(sorted(self.lines))


@dataclass
class Step:
    kind: str      # "walk" | "ride" | "transfer"
    minutes: int
    label: str


@dataclass
class CommuteEstimate:
    minutes: int
    steps: list[Step]  # ordered door-to-door itinerary
    route: str         # one-line summary (CLI / logs)
    kind: str          # "subway" | "walk" | "no_subway"


def stations_within(lat: float, lon: float, max_walk_min: int = 10,
                    limit: int = 3) -> list[Station]:
    """Stations reachable within `max_walk_min` on foot, nearest first —
    the "what trains does this apartment actually have?" answer."""
    max_km = max_walk_min / 60 * WALK_KMH
    found = []
    for name, lines, slat, slon in STATIONS:
        km = haversine_km(lat, lon, slat, slon)
        if km <= max_km:
            found.append(Station(name, set(lines.split("/")), slat, slon, km))
    return sorted(found, key=lambda s: s.walk_km)[:limit]


def nearest_station(lat: float, lon: float) -> Station | None:
    """Closest station within a walkable radius, or None."""
    best, best_km = None, MAX_STATION_WALK_KM
    for name, lines, slat, slon in STATIONS:
        km = haversine_km(lat, lon, slat, slon)
        if km < best_km:
            best, best_km = (name, lines, slat, slon), km
    if best is None:
        return None
    name, lines, slat, slon = best
    return Station(name, set(lines.split("/")), slat, slon, best_km)


def _ride_min(km: float) -> int:
    return max(2, round(km / SUBWAY_KMH * 60))


def estimate_commute(from_lat: float, from_lon: float,
                     to_lat: float, to_lon: float) -> CommuteEstimate:
    """Door-to-door public-transit estimate with an ordered step list."""
    direct_km = haversine_km(from_lat, from_lon, to_lat, to_lon)
    if direct_km <= WALK_ONLY_KM:
        m = max(1, round(direct_km / WALK_KMH * 60))
        return CommuteEstimate(m, [Step("walk", m, f"walk ({direct_km:.1f} km)")],
                               f"walk ({direct_km:.1f} km)", "walk")

    a, b = nearest_station(from_lat, from_lon), nearest_station(to_lat, to_lon)
    if a is None or b is None:
        # crude surface-transit fallback so far-flung places still compare
        m = round(direct_km / 18.0 * 60 + 15)
        return CommuteEstimate(m, [Step("ride", m, "no nearby station — bus/other")],
                               "no nearby station — bus/other", "no_subway")

    ride_km = haversine_km(a.lat, a.lon, b.lat, b.lon)
    shared = a.lines & b.lines
    steps = [Step("walk", a.walk_min, f"walk to {a.name} ({a.lines_str})")]
    if shared:
        line = "/".join(sorted(shared))
        steps.append(Step("ride", round(WAIT_MIN + _ride_min(ride_km)),
                          f"{line} to {b.name} (incl. ~{WAIT_MIN:.0f} min wait)"))
        route = f"{line} from {a.name} to {b.name}"
    else:
        # split the ride around a transfer; which lines carry each half is an
        # estimate — the linked live directions give the exact routing
        half = _ride_min(ride_km) / 2
        steps.append(Step("ride", round(WAIT_MIN + half),
                          f"{a.lines_str} from {a.name} (incl. ~{WAIT_MIN:.0f} min wait)"))
        steps.append(Step("transfer", round(TRANSFER_MIN),
                          f"transfer to {b.lines_str}"))
        steps.append(Step("ride", round(half), f"{b.lines_str} to {b.name}"))
        route = f"{a.lines_str} at {a.name} → transfer → {b.lines_str} at {b.name}"
    steps.append(Step("walk", b.walk_min, "walk to destination"))
    total = sum(s.minutes for s in steps)
    return CommuteEstimate(total, steps, route, "subway")
