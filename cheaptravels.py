"""Flight alerts using production Amadeus prices and persistent deduplication."""
import argparse
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import time
import urllib.error
import urllib.parse
import urllib.request
from decimal import Decimal


class APIError(RuntimeError):
    pass


def request_json(url, data=None, headers=None):
    req = urllib.request.Request(url, data=data, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=45) as response:
            return json.load(response)
    except urllib.error.HTTPError as exc:
        # Never log URLs or bodies: Telegram URLs contain a secret token.
        raise APIError(f"Remote service returned HTTP {exc.code}") from None
    except urllib.error.URLError:
        raise APIError("Remote service could not be reached") from None


def required(name):
    value = os.environ.get(name)
    if not value:
        raise ValueError(f"Missing environment variable: {name}")
    return value


class Amadeus:
    def __init__(self):
        self.base = "https://api.amadeus.com"
        self.client = required("AMADEUS_CLIENT_ID")
        self.secret = required("AMADEUS_CLIENT_SECRET")
        self.token = None
        self.expires = 0

    def search(self, origin, destination, departure, returning):
        if time.monotonic() >= self.expires:
            body = urllib.parse.urlencode({"grant_type": "client_credentials",
                "client_id": self.client, "client_secret": self.secret}).encode()
            result = request_json(self.base + "/v1/security/oauth2/token", body,
                                  {"Content-Type": "application/x-www-form-urlencoded"})
            self.token = result["access_token"]
            self.expires = time.monotonic() + max(1, int(result["expires_in"]) - 60)
        query = urllib.parse.urlencode({"originLocationCode": origin,
            "destinationLocationCode": destination, "departureDate": departure,
            "returnDate": returning, "adults": 1, "currencyCode": "EUR", "max": 50})
        return request_json(self.base + "/v2/shopping/flight-offers?" + query,
                            headers={"Authorization": "Bearer " + self.token}).get("data", [])


class Telegram:
    def __init__(self):
        self.token = required("TELEGRAM_BOT_TOKEN")
        self.chat = required("TELEGRAM_CHAT_ID")

    def send(self, text):
        body = json.dumps({"chat_id": self.chat, "text": text}).encode()
        result = request_json("https://api.telegram.org/bot" + self.token + "/sendMessage",
                              body, {"Content-Type": "application/json"})
        if not result.get("ok"):
            raise APIError("Telegram did not accept the message")


def load_config(path):
    config = json.loads(Path(path).read_text())
    if not config.get("origins"):
        raise ValueError("Set at least one departure airport in config.json origins")
    for code in config["origins"] + config["destinations"]:
        if len(code) != 3 or not code.isalpha() or code != code.upper():
            raise ValueError("Airport codes must be three uppercase letters")
    if config["nights"] != 4:
        raise ValueError("This workflow requires exactly 4 nights")
    if Decimal(str(config["max_price_eur"])) <= 0:
        raise ValueError("Price limit must be positive")
    if config["requests_per_scan"] < 1 or config["interval_seconds"] < 60:
        raise ValueError("Invalid scan limits")
    return config


def routes(config, today=None):
    today = today or dt.date.today()
    start = max(today + dt.timedelta(days=1), dt.date.fromisoformat(config["start_date"])
                if config.get("start_date") else today + dt.timedelta(days=1))
    end = dt.date.fromisoformat(config["end_date"]) if config.get("end_date") else today + dt.timedelta(days=config["days_ahead"])
    result = []
    for offset in range(max(0, (end - start).days + 1)):
        depart = start + dt.timedelta(days=offset)
        returning = depart + dt.timedelta(days=config["nights"])
        if returning > end:
            continue
        for origin in config["origins"]:
            for destination in config["destinations"]:
                if origin != destination:
                    result.append((origin, destination, depart.isoformat(), returning.isoformat()))
    if not result:
        raise ValueError("No valid four-night trips in the configured date range")
    return result


def offer_details(offer, route, limit):
    if offer["price"].get("currency") != "EUR":
        return None
    price = Decimal(offer["price"]["grandTotal"])
    if price >= Decimal(str(limit)):
        return None
    itineraries = offer.get("itineraries", [])
    if len(itineraries) != 2 or not all(i.get("segments") for i in itineraries):
        return None
    outbound, inbound = itineraries
    origin, destination, departure, returning = route
    if (outbound["segments"][0]["departure"]["iataCode"] != origin
        or outbound["segments"][-1]["arrival"]["iataCode"] != destination
        or inbound["segments"][0]["departure"]["iataCode"] != destination
        or inbound["segments"][-1]["arrival"]["iataCode"] != origin
        or outbound["segments"][0]["departure"]["at"][:10] != departure
        or outbound["segments"][-1]["arrival"]["at"][:10] != departure
        or inbound["segments"][0]["departure"]["at"][:10] != returning):
        return None
    identity = [[{k: s.get(k) for k in ("departure", "arrival", "carrierCode", "number")}
                 for s in i["segments"]] for i in itineraries]
    key = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()
    flights = "\n".join(" → ".join(s["departure"]["iataCode"] + " " + s["departure"]["at"] +
                         " (" + s["carrierCode"] + s["number"] + ")" for s in i["segments"]) +
                         " → " + i["segments"][-1]["arrival"]["iataCode"] for i in itineraries)
    text = (f"✈️ Νέα προσφορά: {origin} ↔ {destination}\n{price:.2f} € / άτομο, με επιστροφή\n"
            f"{departure} – {returning} · 4 διανυκτερεύσεις\n{flights}\n"
            "Μόνο πτήσεις, χωρίς διαμονή. Έλεγξε αποσκευές και τελική τιμή πριν την κράτηση.\n"
            "Πηγή: Amadeus · Η τιμή μπορεί να αλλάξει.")
    return key, text


def connect(path):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path)
    db.execute("CREATE TABLE IF NOT EXISTS sent (id TEXT PRIMARY KEY, sent_at TEXT NOT NULL)")
    db.execute("CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
    db.commit()
    return db


def scan(config, api, telegram, db, dry_run=False):
    candidates = routes(config)
    row = db.execute("SELECT value FROM metadata WHERE key='cursor'").fetchone()
    cursor = int(row[0]) % len(candidates) if row else 0
    notified = 0
    for n in range(min(config["requests_per_scan"], len(candidates))):
        index = (cursor + n) % len(candidates)
        route = candidates[index]
        for offer in api.search(*route):
            detail = offer_details(offer, route, config["max_price_eur"])
            if not detail:
                continue
            key, text = detail
            if db.execute("SELECT 1 FROM sent WHERE id=?", (key,)).fetchone():
                continue
            if dry_run:
                print(text)
                continue
            telegram.send(text)
            db.execute("INSERT INTO sent VALUES (?, ?)", (key, dt.datetime.now(dt.timezone.utc).isoformat()))
            db.commit()
            notified += 1
        if not dry_run:
            db.execute("INSERT OR REPLACE INTO metadata VALUES ('cursor', ?)", (str((index + 1) % len(candidates)),))
            db.commit()
    print(f"Scan complete: {min(config['requests_per_scan'], len(candidates))} searches, {notified} new alerts")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="config.json")
    parser.add_argument("--watch", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--check-config", action="store_true")
    args = parser.parse_args()
    try:
        config = load_config(args.config)
        if args.check_config:
            print(f"Configuration valid: {len(routes(config))} routes/dates")
            return
        api = Amadeus()
        telegram = None if args.dry_run else Telegram()
        with connect(config["state_db"]) as db:
            while True:
                try:
                    scan(config, api, telegram, db, args.dry_run)
                except (APIError, ValueError, KeyError) as exc:
                    if not args.watch:
                        raise
                    print(f"Scan failed ({type(exc).__name__}); retrying next interval", flush=True)
                if not args.watch:
                    break
                time.sleep(config["interval_seconds"])
    except (ValueError, APIError) as exc:
        parser.exit(1, str(exc) + "\n")


if __name__ == "__main__":
    main()
