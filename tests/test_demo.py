import asyncio
import unittest

from travel_tools import TravelTools, estimate_budget


class TravelTests(unittest.IsolatedAsyncioTestCase):
    async def test_weather_respects_configured_timeout(self):
        self.assertEqual((await TravelTools(timeout_seconds=0.5).get_weather("Lisbon"))["status"], "ok")

    async def test_weather_reports_real_timeouts(self):
        tools = TravelTools(timeout_seconds=0.001)
        self.assertEqual((await tools.get_weather("Lisbon"))["status"], "unavailable")

    async def test_itinerary_looks_up_same_city_once(self):
        tools = TravelTools()
        itinerary = await tools.plan_itinerary("Lisbon", 3)
        self.assertEqual(len(itinerary["days"]), 3)
        self.assertEqual(tools.location_calls, 1)

    async def test_lookup_does_not_share_state_between_requests(self):
        first, second = TravelTools(), TravelTools()
        await asyncio.gather(first.plan_itinerary("Lisbon", 1), second.plan_itinerary("Vienna", 1))
        self.assertEqual(first.location_calls, 1)
        self.assertEqual(second.location_calls, 1)

    async def test_itinerary_days_and_requests_have_independent_locations(self):
        tools = TravelTools()
        itinerary = await tools.plan_itinerary("Lisbon", 2)
        itinerary["days"][0]["location"]["city"] = "changed"
        self.assertEqual(itinerary["days"][1]["location"]["city"], "lisbon")
        second = await tools.plan_itinerary("Vienna", 1)
        self.assertEqual(second["days"][0]["location"]["city"], "vienna")

    async def test_itinerary_bounds_work(self):
        with self.assertRaises(ValueError):
            await TravelTools().plan_itinerary("Lisbon", 1000)


class BudgetTests(unittest.TestCase):
    def test_usd_to_eur_uses_quote_direction(self):
        for amount, expected in (("120.00", "100.00"), ("240.00", "200.00"), ("600.00", "500.00"), ("1.01", "0.84"), ("0", "0.00")):
            with self.subTest(amount=amount):
                self.assertEqual(estimate_budget(amount)["converted_amount"], expected)

    def test_budget_rejects_invalid_amounts(self):
        for amount in ("-1", "NaN", "Infinity"):
            with self.subTest(amount=amount), self.assertRaises(ValueError):
                estimate_budget(amount)


if __name__ == "__main__":
    unittest.main()