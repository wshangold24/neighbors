"""Demo provider: deterministic synthetic NYC listings.

Used automatically when live providers return nothing (blocked, offline),
so the KNN pipeline is always testable. No network requests.
"""

from __future__ import annotations

import random

from ..models import AMENITIES, Listing, NEIGHBORHOOD_COORDS, Preferences
from .base import Provider

# Rough per-neighborhood 1BR median rents used to synthesize plausible prices.
BASE_1BR = {
    "west village": 4900, "soho": 4800, "tribeca": 5200, "chelsea": 4600,
    "greenwich village": 4700, "east village": 3900, "lower east side": 3700,
    "upper east side": 3600, "upper west side": 3800, "midtown": 4100,
    "hell's kitchen": 3900, "murray hill": 3700, "gramercy": 4000,
    "financial district": 4100, "harlem": 2700, "east harlem": 2600,
    "washington heights": 2300, "inwood": 2100,
    "williamsburg": 4200, "greenpoint": 3800, "bushwick": 3000,
    "bed-stuy": 2900, "crown heights": 2800, "prospect heights": 3400,
    "park slope": 3500, "gowanus": 3600, "carroll gardens": 3700,
    "cobble hill": 3800, "boerum hill": 3700, "brooklyn heights": 3900,
    "dumbo": 4400, "fort greene": 3500, "clinton hill": 3300,
    "downtown brooklyn": 3800, "sunset park": 2400, "bay ridge": 2200,
    "flatbush": 2300, "ditmas park": 2400, "ridgewood": 2700,
    "astoria": 2800, "long island city": 3700, "sunnyside": 2500,
    "woodside": 2400, "jackson heights": 2300, "forest hills": 2400,
    "flushing": 2300, "rego park": 2300, "mott haven": 2400,
    "riverdale": 2200, "fordham": 1900, "concourse": 2000,
}

STREETS = ["Bedford Ave", "Graham Ave", "Metropolitan Ave", "5th Ave", "Court St",
           "Amsterdam Ave", "Lexington Ave", "Ludlow St", "Avenue A", "Franklin St",
           "Vernon Blvd", "Steinway St", "Nostrand Ave", "Smith St", "W 4th St"]


class DemoProvider(Provider):
    name = "demo"

    def __init__(self, session=None, n: int = 400, seed: int = 7):
        self.n = n
        self.seed = seed

    def fetch(self, prefs: Preferences, max_pages: int = 2) -> list[Listing]:
        rng = random.Random(self.seed)
        hoods = list(BASE_1BR)
        out: list[Listing] = []
        for i in range(self.n):
            hood = rng.choice(hoods)
            base = BASE_1BR[hood]
            beds = rng.choices([0, 1, 2, 3, 4], weights=[20, 38, 27, 12, 3])[0]
            baths = 1 if beds <= 1 else rng.choices([1, 1.5, 2], weights=[5, 3, 3])[0]
            bed_mult = {0: 0.82, 1: 1.0, 2: 1.45, 3: 1.9, 4: 2.4}[beds]
            price = int(base * bed_mult * rng.uniform(0.82, 1.25) / 25) * 25
            sqft = int((420 + beds * 260) * rng.uniform(0.8, 1.3)) if rng.random() < 0.7 else None
            lat0, lon0 = NEIGHBORHOOD_COORDS[hood]
            n_amen = rng.randint(0, 6)
            amenities = rng.sample(AMENITIES, n_amen)
            out.append(Listing(
                id=f"demo-{i}",
                source=self.name,
                url=f"https://example.com/listing/{i}",
                address=f"{rng.randint(20, 950)} {rng.choice(STREETS)} #{rng.randint(1, 6)}{rng.choice('ABCDEF')}",
                neighborhood=hood.title(),
                price=price,
                beds=beds,
                baths=baths,
                sqft=sqft,
                lat=lat0 + rng.uniform(-0.006, 0.006),
                lon=lon0 + rng.uniform(-0.006, 0.006),
                amenities=amenities,
            ))
        return self._validated(out)
