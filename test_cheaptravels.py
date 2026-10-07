import copy
import datetime as dt
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import cheaptravels as app


def offer(price="99.99"):
    return {"price": {"currency": "EUR", "grandTotal": price}, "itineraries": [
        {"segments": [{"departure": {"iataCode": "ATH", "at": "2026-11-01T08:00:00"},
                        "arrival": {"iataCode": "VIE", "at": "2026-11-01T10:00:00"},
                        "carrierCode": "A3", "number": "880"}]},
        {"segments": [{"departure": {"iataCode": "VIE", "at": "2026-11-05T12:00:00"},
                        "arrival": {"iataCode": "ATH", "at": "2026-11-05T14:00:00"},
                        "carrierCode": "A3", "number": "881"}]}]}


ROUTE = ("ATH", "VIE", "2026-11-01", "2026-11-05")
CONFIG = {"origins": ["ATH"], "destinations": ["VIE"], "start_date": "2026-11-01",
          "end_date": "2026-11-05", "days_ahead": 180, "nights": 4,
          "max_price_eur": 100, "requests_per_scan": 40}


class FakeAPI:
    def search(self, *args):
        return [offer(), offer("100.00")]


class FakeTelegram:
    def __init__(self, fail=False):
        self.messages = []
        self.fail = fail

    def send(self, text):
        if self.fail:
            raise app.APIError("Rejected")
        self.messages.append(text)


class Tests(unittest.TestCase):
    def duffel_response(self, live=True, currency="EUR"):
        return {"data": {"live_mode": live, "offers": [{
            "live_mode": live, "total_amount": "99.99", "total_currency": currency,
            "expires_at": "2099-01-01T00:00:00Z",
            "slices": [{"segments": [{
                "origin": {"iata_code": s["departure"]["iataCode"]},
                "destination": {"iata_code": s["arrival"]["iataCode"]},
                "departing_at": s["departure"]["at"], "arriving_at": s["arrival"]["at"],
                "marketing_carrier": {"iata_code": s["carrierCode"]},
                "marketing_carrier_flight_number": s["number"]
            } for s in i["segments"]]} for i in offer()["itineraries"]]
        }]}}

    @patch.dict("os.environ", {"DUFFEL_ACCESS_TOKEN": "unit-test-only"})
    def test_duffel_request_and_normalization(self):
        with patch.object(app, "request_json", return_value=self.duffel_response()) as request:
            results = app.Duffel().search(*ROUTE)
        self.assertEqual(results, [offer()])
        url, body, headers = request.call_args.args
        self.assertIn("return_offers=true", url)
        data = app.json.loads(body)["data"]
        self.assertEqual(data["passengers"], [{"type": "adult"}])
        self.assertEqual(data["slices"][1], {"origin": "VIE", "destination": "ATH", "departure_date": "2026-11-05"})
        self.assertEqual(headers["Duffel-Version"], "v2")

    @patch.dict("os.environ", {"DUFFEL_ACCESS_TOKEN": "unit-test-only"})
    def test_duffel_rejects_sandbox_and_wrong_currency(self):
        for result in [self.duffel_response(live=False), self.duffel_response(currency="GBP")]:
            with patch.object(app, "request_json", return_value=result):
                with self.assertRaises(app.APIError):
                    app.Duffel().search(*ROUTE)

    @patch.dict("os.environ", {"DUFFEL_ACCESS_TOKEN": "unit-test-only"})
    def test_duffel_skips_expired_offers(self):
        result = self.duffel_response()
        result["data"]["offers"][0]["expires_at"] = "2020-01-01T00:00:00Z"
        with patch.object(app, "request_json", return_value=result):
            self.assertEqual(app.Duffel().search(*ROUTE), [])

    def test_strict_price_and_currency(self):
        self.assertIsNotNone(app.offer_details(offer(), ROUTE, 100))
        self.assertIsNone(app.offer_details(offer("100"), ROUTE, 100))
        self.assertIsNone(app.offer_details(offer("101"), ROUTE, 100))
        item = offer(); item["price"]["currency"] = "USD"
        self.assertIsNone(app.offer_details(item, ROUTE, 100))

    def test_four_nights_and_date_bounds(self):
        self.assertEqual(app.routes(CONFIG, dt.date(2026, 10, 7)), [ROUTE])
        overnight = offer()
        overnight["itineraries"][0]["segments"][0]["arrival"]["at"] = "2026-11-02T00:01:00"
        self.assertIsNone(app.offer_details(overnight, ROUTE, 100))

    def test_persistent_dedup_and_price_change(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(app, "routes", return_value=[ROUTE]):
            path = str(Path(folder) / "state.sqlite3")
            bot = FakeTelegram()
            with app.connect(path) as db:
                app.scan(CONFIG, FakeAPI(), bot, db)
            with app.connect(path) as db:
                app.scan(CONFIG, FakeAPI(), bot, db)
                changed = copy.deepcopy(offer("80"))
                self.assertEqual(app.offer_details(changed, ROUTE, 100)[0], app.offer_details(offer(), ROUTE, 100)[0])
            self.assertEqual(len(bot.messages), 1)

    def test_failed_delivery_is_retried(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(app, "routes", return_value=[ROUTE]):
            with app.connect(str(Path(folder) / "db")) as db:
                with self.assertRaises(app.APIError):
                    app.scan(CONFIG, FakeAPI(), FakeTelegram(True), db)
                self.assertEqual(db.execute("SELECT count(*) FROM sent").fetchone()[0], 0)
                bot = FakeTelegram()
                app.scan(CONFIG, FakeAPI(), bot, db)
                self.assertEqual(len(bot.messages), 1)

    def test_dry_run_does_not_mark_sent(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(app, "routes", return_value=[ROUTE]):
            with app.connect(str(Path(folder) / "db")) as db:
                app.scan(CONFIG, FakeAPI(), None, db, dry_run=True)
                self.assertEqual(db.execute("SELECT count(*) FROM sent").fetchone()[0], 0)
                self.assertEqual(db.execute("SELECT count(*) FROM metadata").fetchone()[0], 0)


if __name__ == "__main__":
    unittest.main()
