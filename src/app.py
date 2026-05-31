from flask import Flask, render_template, request, jsonify
from scraper import search_flights, search_nearby
from concurrent.futures import ThreadPoolExecutor

app = Flask(__name__, template_folder="templates", static_folder="static")


@app.route("/debug-raw")
def debug_raw():
    import requests as req
    from datetime import datetime, timedelta
    params = {
        "departureAirportIataCode": "CGN",
        "market": "de-de",
        "outboundDepartureDateFrom": (datetime.now() + timedelta(days=21)).strftime("%Y-%m-%d"),
        "outboundDepartureDateTo":   (datetime.now() + timedelta(days=70)).strftime("%Y-%m-%d"),
        "inboundDepartureDateFrom":  (datetime.now() + timedelta(days=26)).strftime("%Y-%m-%d"),
        "inboundDepartureDateTo":    (datetime.now() + timedelta(days=82)).strftime("%Y-%m-%d"),
        "priceValueTo": 500, "currency": "EUR", "offset": 0, "limit": 2,
    }
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Accept": "application/json", "Referer": "https://www.ryanair.com/",
    }
    r = req.get("https://www.ryanair.com/api/farfnd/3/roundTripFares", params=params, headers=headers, timeout=15)
    return jsonify(r.json())


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/search", methods=["POST"])
def search():
    data = request.json

    origin       = data.get("origin", "CGN")
    budget       = int(data.get("budget", 1500))
    passengers   = int(data.get("passengers", 1))
    duration_min = int(data.get("duration_min", 5))
    duration_max = int(data.get("duration_max", 12))
    dep_from     = data.get("departure_from")
    dep_to       = data.get("departure_to") or dep_from
    nonstop      = bool(data.get("nonstop", False))

    if not dep_from:
        return jsonify({"error": "Bitte ein Abflugdatum angeben."}), 400

    kwargs = dict(dep_date=dep_from, ret_date=dep_to, budget=budget,
                  passengers=passengers, duration_min=duration_min, duration_max=duration_max)
    try:
        with ThreadPoolExecutor(max_workers=2) as ex:
            main_f   = ex.submit(search_flights, origin=origin, nonstop=nonstop, **kwargs)
            nearby_f = ex.submit(search_nearby,  origin=origin, **kwargs)
            results = main_f.result()
            nearby  = nearby_f.result()
    except Exception as e:
        return jsonify({"error": f"API-Fehler: {str(e)}"}), 500

    if not results and not nearby:
        return jsonify({"error": "Keine Ergebnisse — Budget erhöhen oder anderen Flughafen wählen."}), 404

    return jsonify({
        "results": results,
        "nearby":  nearby,
        "passengers": passengers,
        "budget": budget,
        "duration_min": duration_min,
        "duration_max": duration_max,
    })


if __name__ == "__main__":
    app.run(debug=True, port=5050, use_reloader=False)
