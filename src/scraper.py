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


WEG_BASE    = "https://www.weg.de"
WEG_SEARCH  = "https://api.weg.de/comvel-productsearch-service/rest/productsearch2"
WEG_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "de-DE,de;q=0.9",
    "Referer": "https://www.weg.de/",
    "Origin": "https://www.weg.de",
}

# Nearby airports for CGN/NRN so we find more flights
NEARBY_AIRPORTS = {
    "CGN": ["CGN", "DUS", "NRN", "FRA"],
    "NRN": ["NRN", "CGN", "DUS", "FRA"],
}

WEGDE_COUNTRIES = ["GR", "TR", "EG", "ES", "TN", "HR", "PT", "CY", "BG", "MA"]

MEAL_LABEL = {
    "AI": "All Inclusive", "UAI": "Ultra All Inclusive",
    "HP": "Halbpension",   "VP":  "Vollpension",
    "FR": "Frühstück",     "F":   "Frühstück",
    "OV": "Nur Übernachtung",
}

# Hierarchy for minimum-meal filtering
MEAL_RANK = {
    "Ü": 0, "OV": 0,
    "F": 1, "FR": 1, "ÜF": 1,
    "HP": 2,
    "VP": 3,
    "AI": 4, "UAI": 5,
}


def _weg_hotel_booking_url(hotel_name, hotel_id, dep_date, country, dur, passengers):
    from urllib.parse import urlencode
    import re
    slug = re.sub(r'[^a-z0-9]+', '-', (hotel_name or "hotel").lower()).strip('-')
    # Use exact departure date as 1-day window so weg.de shows offers for that date
    params = urlencode({
        "country": country,
        "duration": dur,
        "from": dep_date,
        "to": dep_date,
        "sort": "price",
        "travellerRoomAllocations": passengers,
        "travellers": ",".join(["30"] * passengers),
        "rooms":      ",".join(["30"] * passengers),
    })
    return f"{WEG_BASE}/hotel/{slug}-cid_{hotel_id}?{params}"


def _fetch_weg_country_dur(country, dep_from, dep_to, dur, passengers, budget, origin):
    from urllib.parse import urlencode
    travellers_str = ",".join(["30"] * passengers)
    qs = urlencode({
        "country": country,
        "duration": dur,
        "from": dep_from,
        "to": dep_to,
        "sort": "price",
        "travellerRoomAllocations": passengers,
        "travellers": travellers_str,
        "rooms":      travellers_str,
        "channel": "PACKAGE",
        "departureFlightTimeUntil": 1440,
        "returnFlightTimeUntil": 1440,
        "count": 100,
    })
    try:
        resp = requests.get(WEG_SEARCH + "?" + qs, headers=WEG_HEADERS, timeout=15)
        if resp.status_code != 200:
            return []

        result = []
        for o in resp.json().get("offerList", []):
            dep_ap = (o.get("departureAirport") or {}).get("iataCode", "")

            price = o.get("price") or o.get("totalPrice")
            if not price:
                continue
            price = float(price)
            total = round(price * passengers, 2)

            hotel    = o.get("comvelHotel") or {}
            hotel_id = o.get("comvelHotelId") or hotel.get("id", "")
            h_name   = hotel.get("hotelName", "")
            city_obj = hotel.get("city") or {}
            city     = city_obj.get("name", "") if isinstance(city_obj, dict) else str(city_obj)
            region   = (hotel.get("region") or {}).get("name", "") if isinstance(hotel.get("region"), dict) else ""
            country_name = (hotel.get("country") or {}).get("name", "") if isinstance(hotel.get("country"), dict) else ""
            stars    = hotel.get("stars") or hotel.get("hotelCategory") or o.get("hotelCategory")
            meal     = (o.get("mealType") or {})
            meal_abbr  = meal.get("abbr", "")
            meal_name  = meal.get("name", "")
            meal_label = MEAL_LABEL.get(meal_abbr, "") or meal_name

            dep_date = o.get("from", dep_from)
            ret_date = o.get("to",   "")

            result.append({
                "hotel_name":       h_name,
                "city":             city,
                "region":           region,
                "country":          country_name,
                "country_code":     country,
                "duration":         o.get("duration", dur),
                "dep_date":         dep_date,
                "ret_date":         ret_date,
                "stars":            stars,
                "meal_type":        meal_abbr,
                "meal_label":       meal_label,
                "room_type":        (o.get("roomType") or {}).get("abbr", ""),
                "price_per_person": round(price, 2),
                "price_total":      total,
                "dep_airport":      dep_ap,
                "dst_airport":      (o.get("destinationAirport") or {}).get("iataCode", ""),
                "tour_operator":    (o.get("tourOperator") or {}).get("name", ""),
                "rating":           hotel.get("ratingAverage"),
                "reviews":          hotel.get("reviewsCount"),
                "image":            "",
                "booking_url":      _weg_hotel_booking_url(
                    h_name, hotel_id, dep_date,
                    country, o.get("duration", dur), passengers
                ),
                "source": "weg.de",
            })
        return result
    except Exception:
        return []


def search_package_deals(origin, dep_from, dep_to, dur_min, dur_max, passengers, budget=None, min_meal=0):
    dur_mid = (dur_min + dur_max) // 2
    durations = sorted({dur_min, dur_mid, dur_max})
    tasks = [(c, d) for c in WEGDE_COUNTRIES for d in durations]

    def fetch(args):
        return _fetch_weg_country_dur(args[0], dep_from, dep_to, args[1], passengers, budget, origin)

    all_offers = []
    with ThreadPoolExecutor(max_workers=12) as ex:
        for result in as_completed([ex.submit(fetch, t) for t in tasks]):
            try:
                all_offers.extend(result.result())
            except Exception:
                pass

    # Apply minimum meal filter
    if min_meal > 0:
        all_offers = [o for o in all_offers if MEAL_RANK.get(o["meal_type"], 0) >= min_meal]

    # Deduplicate: keep cheapest offer per hotel
    best: dict = {}
    for o in all_offers:
        key = (o["hotel_name"].lower().strip(), o["country_code"])
        if key not in best or o["price_per_person"] < best[key]["price_per_person"]:
            best[key] = o

    # Within-budget first, then over-budget — each group sorted by price
    in_budget  = sorted([o for o in best.values() if not budget or o["price_total"] <= budget],
                        key=lambda x: x["price_per_person"])
    over_budget = sorted([o for o in best.values() if budget and o["price_total"] > budget],
                         key=lambda x: x["price_per_person"])
    sorted_all = in_budget + over_budget

    # Max 4 results per country, then take top 40 overall
    country_count: dict = {}
    results = []
    for o in sorted_all:
        cc = o["country_code"]
        if country_count.get(cc, 0) < 4:
            results.append(o)
            country_count[cc] = country_count.get(cc, 0) + 1
        if len(results) >= 40:
            break

    return {"offers": results, "source": "weg.de", "search_url": WEG_BASE + "/urlaub/"}


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
