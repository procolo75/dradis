"""
geocode.py
──────────
Resolving a place name to coordinates through the Open-Meteo geocoding API, shared by
every monitor, by the weather tool and by the Web UI's location hint.

Two things were wrong with the five copies this replaces, both of which sent monitors to
the wrong side of the planet:

  · **The language decides what can be found at all.** The endpoint searches localized
    name tables, so "Napoli" asked in English does not match the Italian city — it is
    indexed there as "Naples" — and the first hit is a village in Gambia, 3000 km away.
    The Italian tables carry the local names *and* the English aliases, which is why
    "Napoli", "Naples", "Rome" and "London" all resolve correctly through them.
  · **`count=1` hands the choice to the API's own ranking**, which is not population.
    Asking for ten and keeping the most populous one is what makes "Napoli" the city of
    909 000 rather than the hamlet named Napoli-Nola.

A trailing country narrows the search when the default is not what you meant:
"Springfield, US" or "Napoli, Gambia" — matched against both the country code and the
country name, and an error is raised rather than quietly falling back if nothing there
matches.
"""

import httpx

_URL      = "https://geocoding-api.open-meteo.com/v1/search"
_LANGUAGE = "it"
_COUNT    = 10


def _split_country(location: str) -> tuple[str, str]:
    """'Napoli, IT' → ('Napoli', 'it'). No comma means no country filter."""
    if "," not in location:
        return location.strip(), ""
    city, _, country = location.rpartition(",")
    return city.strip(), country.strip().lower()


def _matches(result: dict, country: str) -> bool:
    if not country:
        return True
    return country in (str(result.get("country_code", "")).lower(),
                       str(result.get("country", "")).lower())


def _pick(results: list, country: str) -> dict | None:
    """Most populous match wins; an unknown population counts as zero, which leaves the
    API's own order in charge only when nothing has a population at all."""
    candidates = [r for r in results if _matches(r, country)]
    return max(candidates, key=lambda r: r.get("population") or 0, default=None)


async def _fetch(name: str, language: str, timeout: float) -> list:
    async with httpx.AsyncClient(timeout=timeout) as client:
        resp = await client.get(_URL, params={
            "name": name, "count": _COUNT, "language": language, "format": "json",
        })
    return resp.json().get("results", []) or []


async def search(location: str, *, language: str = _LANGUAGE,
                 timeout: float = 30) -> dict:
    """Full geocoding record for `location`, or ValueError if nothing matches."""
    name, country = _split_country(location)
    if not name:
        raise ValueError(f"Location not found: {location!r}")

    best = _pick(await _fetch(name, language, timeout), country)
    if best is None:
        raise ValueError(f"Location not found: {location!r}")

    print(f"[DRADIS] geocode: {location!r} → {best.get('name')}, "
          f"{best.get('country_code')} ({best['latitude']:.4f}, {best['longitude']:.4f})")
    return best


async def geocode(location: str, *, language: str = _LANGUAGE,
                  timeout: float = 30) -> tuple[float, float, str]:
    """(latitude, longitude, resolved name) — the shape the monitors use."""
    best = await search(location, language=language, timeout=timeout)
    return best["latitude"], best["longitude"], best.get("name", location)
