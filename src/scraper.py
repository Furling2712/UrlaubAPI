import requests
from datetime import datetime, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed

NEARBY = {
    "CGN": [
        {"iata": "NRN", "name": "Weeze",        "drive": "45 min"},
        {"iata": "DTM", "name": "Dortmund",     "drive": "1 Std."},
        {"iata": "EIN", "name": "Eindhoven",    "drive": "1:20 Std."},
    ],
    "NRN": [
        {"iata": "CGN", "name": "Köln/Bonn",   "drive": "45 min"},
        {"iata": "DTM", "name": "Dortmund",     "drive": "45 min"},
        {"iata": "EIN", "name": "Eindhoven",    "drive": "1 Std."},
    ],
}

RYANAIR_URL = "https://www.ryanair.com/api/farfnd/3/roundTripFares"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "de-DE,de;q=0.9",
    "Referer": "https://www.ryanair.com/",
    "Origin": "https://www.ryanair.com",
}


def search_flights(origin, dep_date, ret_date, budget, passengers, duration_min=5, duration_max=12, nonstop=False):
    dep_from = datetime.strptime(dep_date, "%Y-%m-%d")
    dep_to   = datetime.strptime(ret_date, "%Y-%m-%d")

    inbound_from = dep_from + timedelta(days=duration_min)
    inbound_to   = dep_to  + timedelta(days=duration_max)

    # Ryanair zeigt Preis pro Person (Hin+Rück)
    max_price_per_person = int(budget / max(passengers, 1))

    params = {
        "departureAirportIataCode": origin,
        "market": "de-de",
        "outboundDepartureDateFrom": dep_from.strftime("%Y-%m-%d"),
        "outboundDepartureDateTo":   dep_to.strftime("%Y-%m-%d"),
        "inboundDepartureDateFrom":  inbound_from.strftime("%Y-%m-%d"),
        "inboundDepartureDateTo":    inbound_to.strftime("%Y-%m-%d"),
        "priceValueTo": max_price_per_person,
        "currency": "EUR",
        "offset": 0,
        "limit": 20,
    }

    resp = requests.get(RYANAIR_URL, params=params, headers=HEADERS, timeout=15)
    resp.raise_for_status()
    data = resp.json()

    results = []
    for fare in data.get("fares", []):
        try:
            outbound = fare["outbound"]
            inbound  = fare.get("inbound", {})
            summary  = fare.get("summary", {})

            arrival      = outbound["arrivalAirport"]
            city_obj     = arrival.get("city") or {}
            city         = city_obj.get("name") or arrival.get("name", "")
            country      = arrival.get("countryName", "")
            country_code = city_obj.get("countryCode", "").upper()
            iata         = arrival.get("iataCode", "")

            price_val = (
                (summary.get("price") or {}).get("value")
                or (outbound.get("price") or {}).get("value")
                or 0
            )
            price = float(price_val)
            if price <= 0:
                continue

            dep_dt = outbound.get("departureDate", "")[:10]
            ret_dt = (inbound.get("departureDate") or "")[:10]

            dep_fmt = dep_dt.replace("-", "")[2:]
            ret_fmt = ret_dt.replace("-", "")[2:]

            label = f"{city}, {country}" if country else city

            results.append({
                "destination": label,
                "iata": iata,
                "country_code": country_code,
                "price": round(price, 2),
                "departure_date": dep_dt,
                "return_date": ret_dt,
                "skyscanner_url": (
                    f"https://www.skyscanner.de/transport/fluge/"
                    f"{origin.lower()}/{iata.lower()}/{dep_fmt}/{ret_fmt}/"
                ),
                "booking_url": (
                    f"https://www.booking.com/searchresults.de.html"
                    f"?ss={city}&checkin={dep_dt}&checkout={ret_dt}"
                    f"&group_adults={passengers}&no_rooms=1"
                ),
                "direct_url": (
                    f"https://www.ryanair.com/de/de/trip/flights/select"
                    f"?adults={passengers}&teens=0&children=0&infants=0"
                    f"&dateOut={dep_dt}&dateIn={ret_dt}&isReturn=true"
                    f"&originIata={origin}&destinationIata={iata}"
                ),
            })
        except (KeyError, TypeError, ValueError):
            continue

    return sorted(results, key=lambda x: x["price"])


def search_nearby(origin, dep_date, ret_date, budget, passengers, duration_min=5, duration_max=12):
    airports = NEARBY.get(origin, [])
    if not airports:
        return []

    def _fetch(ap):
        try:
            results = search_flights(
                origin=ap["iata"],
                dep_date=dep_date,
                ret_date=ret_date,
                budget=budget,
                passengers=passengers,
                duration_min=duration_min,
                duration_max=duration_max,
            )
            return {**ap, "results": results[:5]}
        except Exception:
            return {**ap, "results": []}

    with ThreadPoolExecutor(max_workers=4) as ex:
        futures = [ex.submit(_fetch, ap) for ap in airports]
        return [f.result() for f in futures if f.result().get("results")]
