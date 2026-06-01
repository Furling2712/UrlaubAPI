import requests
import re
import asyncio
from datetime import datetime, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed
from playwright.async_api import async_playwright

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

            if dep_dt and ret_dt:
                try:
                    actual_nights = (datetime.strptime(ret_dt, "%Y-%m-%d") - datetime.strptime(dep_dt, "%Y-%m-%d")).days
                    if not (duration_min <= actual_nights <= duration_max):
                        continue
                except ValueError:
                    pass

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


async def _fetch_hotel_price(context, city, checkin, checkout, adults, nights):
    page = await context.new_page()
    try:
        url = (
            f"https://www.booking.com/searchresults.de.html"
            f"?ss={requests.utils.quote(city)}"
            f"&checkin={checkin}&checkout={checkout}"
            f"&group_adults={adults}&no_rooms=1&order=price&currency=EUR"
        )
        await page.goto(url, wait_until="domcontentloaded", timeout=25000)

        # Cookie-Banner
        for sel in ['[data-testid="accept-btn"]', 'button:has-text("Akzeptieren")', '#onetrust-accept-btn-handler']:
            try:
                await page.click(sel, timeout=2000)
                break
            except Exception:
                continue

        await page.wait_for_timeout(3000)

        # Gesamtpreis der ersten (günstigsten) Property holen, dann durch Nächte teilen
        price_total = await page.evaluate("""() => {
            // Erste Property-Karte = günstigstes Ergebnis
            const card = document.querySelector('[data-testid="property-card"]');
            if (!card) return null;

            // Preis-Element innerhalb der Karte
            const priceEl = card.querySelector('[data-testid="price-and-discounted-price"]');
            if (priceEl) {
                const digits = priceEl.textContent.replace(/[^\\d]/g, '');
                const p = parseInt(digits);
                if (p > 20 && p < 100000) return p;
            }

            // Fallback: alle Zahlen im ersten Card-Block, Minimum nehmen das plausibel ist
            const nums = [];
            card.querySelectorAll('*').forEach(el => {
                if (el.children.length > 0) return;
                const m = (el.textContent || '').trim().match(/^[€\\s]*(\\d{2,6})[€\\s]*$/);
                if (m) {
                    const p = parseInt(m[1]);
                    if (p >= 20 && p <= 50000) nums.push(p);
                }
            });
            return nums.length ? Math.max(...nums) : null;
        }""")

        if price_total and nights > 0:
            return round(price_total / nights)
        return None
    except Exception:
        return None
    finally:
        await page.close()


async def _batch_hotel_prices(items, passengers):
    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-blink-features=AutomationControlled"]
        )
        context = await browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            locale="de-DE",
            viewport={"width": 1280, "height": 800},
        )
        await context.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")

        tasks = []
        for item in items:
            city   = item.get("search_term") or item["destination"].split(",")[0].strip()
            nights = max(1, (datetime.strptime(item["return_date"], "%Y-%m-%d") -
                             datetime.strptime(item["departure_date"], "%Y-%m-%d")).days)
            tasks.append(_fetch_hotel_price(context, city, item["departure_date"],
                                            item["return_date"], passengers, nights))

        prices = await asyncio.gather(*tasks)
        await browser.close()
        return prices


def enrich_with_hotel_prices(items, passengers):
    """Fügt hotel_price_per_night zu jedem Item hinzu (in-place)."""
    if not items:
        return
    prices = asyncio.run(_batch_hotel_prices(items, passengers))
    for item, price in zip(items, prices):
        item["hotel_price_per_night"] = price


GERMANY_DESTINATIONS = [
    {"name": "Sylt",             "region": "Schleswig-Holstein",     "lat": 54.91, "lon":  8.33},
    {"name": "St. Peter-Ording", "region": "Schleswig-Holstein",     "lat": 54.31, "lon":  8.65},
    {"name": "Rügen",            "region": "Mecklenburg-Vorpommern", "lat": 54.37, "lon": 13.38},
    {"name": "Usedom",           "region": "Mecklenburg-Vorpommern", "lat": 53.90, "lon": 14.02},
    {"name": "Allgäu",           "region": "Bayern",                 "lat": 47.56, "lon": 10.31},
    {"name": "Schwarzwald",      "region": "Baden-Württemberg",      "lat": 47.93, "lon":  8.18},
    {"name": "Bayerischer Wald", "region": "Bayern",                 "lat": 48.93, "lon": 13.37},
    {"name": "Harz",             "region": "Sachsen-Anhalt",         "lat": 51.83, "lon": 10.78},
    {"name": "Mosel",            "region": "Rheinland-Pfalz",        "lat": 50.15, "lon":  7.17},
    {"name": "Sauerland",        "region": "Nordrhein-Westfalen",    "lat": 51.19, "lon":  8.14},
    {"name": "Lübecker Bucht",   "region": "Schleswig-Holstein",     "lat": 54.00, "lon": 10.74},
    {"name": "Berchtesgaden",    "region": "Bayern",                 "lat": 47.63, "lon": 13.00},
]


WELLNESS_DESTINATIONS = [
    {"name": "Valkenburg",       "region": "Niederlande",         "country": "NL", "lat": 50.86, "lon":  5.83},
    {"name": "Spa",              "region": "Belgien",             "country": "BE", "lat": 50.49, "lon":  5.86},
    {"name": "Ardennes",         "region": "Belgien",             "country": "BE", "lat": 50.23, "lon":  5.68},
    {"name": "Bad Münstereifel", "region": "Nordrhein-Westfalen", "country": "DE", "lat": 50.56, "lon":  6.76},
    {"name": "Eifel",            "region": "Rheinland-Pfalz",     "country": "DE", "lat": 50.35, "lon":  6.85},
    {"name": "Bad Neuenahr",     "region": "Rheinland-Pfalz",     "country": "DE", "lat": 50.55, "lon":  7.12},
    {"name": "Bergisches Land",  "region": "Nordrhein-Westfalen", "country": "DE", "lat": 51.10, "lon":  7.42},
    {"name": "Mosel",            "region": "Rheinland-Pfalz",     "country": "DE", "lat": 50.15, "lon":  7.17},
    {"name": "Hunsrück",         "region": "Rheinland-Pfalz",     "country": "DE", "lat": 49.90, "lon":  7.30},
    {"name": "Westerwald",       "region": "Rheinland-Pfalz",     "country": "DE", "lat": 50.63, "lon":  7.95},
]


def get_wellness_hotel_options(checkin, checkout, passengers):
    nights = max(1, (datetime.strptime(checkout, "%Y-%m-%d") - datetime.strptime(checkin, "%Y-%m-%d")).days)
    return [
        {
            "destination":   dest["name"],
            "search_term":   f"wellness hotel {dest['name']}",
            "region":        dest["region"],
            "country_code":  dest["country"],
            "lat":           dest["lat"],
            "lon":           dest["lon"],
            "price":         0,
            "departure_date": checkin,
            "return_date":    checkout,
            "nights":         nights,
            "booking_url": (
                f"https://www.booking.com/searchresults.de.html"
                f"?ss={requests.utils.quote('wellness hotel ' + dest['name'])}"
                f"&checkin={checkin}&checkout={checkout}"
                f"&group_adults={passengers}&no_rooms=1&order=price"
            ),
        }
        for dest in WELLNESS_DESTINATIONS
    ]


def get_germany_hotel_options(checkin, checkout, passengers):
    nights = max(1, (datetime.strptime(checkout, "%Y-%m-%d") - datetime.strptime(checkin, "%Y-%m-%d")).days)
    return [
        {
            "destination": dest["name"],
            "region":      dest["region"],
            "lat":         dest["lat"],
            "lon":         dest["lon"],
            "country_code": "DE",
            "price":        0,
            "departure_date": checkin,
            "return_date":    checkout,
            "nights":         nights,
            "booking_url": (
                f"https://www.booking.com/searchresults.de.html"
                f"?ss={requests.utils.quote(dest['name'])}&checkin={checkin}&checkout={checkout}"
                f"&group_adults={passengers}&no_rooms=1&order=price"
            ),
        }
        for dest in GERMANY_DESTINATIONS
    ]


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
