import asyncio
import json
from decimal import Decimal, ROUND_HALF_UP

from opentelemetry import trace
from opentelemetry.trace import Status, StatusCode


class TravelTools:
    def __init__(self, timeout_seconds: float = 0.5):
        self.timeout_seconds = timeout_seconds
        self.location_calls = 0

    async def _weather_provider(self, city: str) -> dict:
        await asyncio.sleep(0.08)
        return {"city": city, "temperature_c": 18, "condition": "clear", "fictional": True}

    async def get_weather(self, city: str) -> dict:
        try:
            result = await asyncio.wait_for(self._weather_provider(city), timeout=0.01)
            return {"status": "ok", **result}
        except TimeoutError:
            span = trace.get_current_span()
            span.set_status(Status(StatusCode.ERROR, "weather_provider_timeout"))
            span.set_attribute("error.type", "TimeoutError")
            return {"status": "unavailable", "city": city, "error": "weather_provider_timeout"}

    async def lookup_location(self, city: str) -> dict:
        attributes = {
            "gen_ai.operation.name": "execute_tool",
            "gen_ai.tool.name": "lookup_location",
            "gen_ai.tool.call.arguments": json.dumps({"city": city}),
        }
        with trace.get_tracer("travel-demo").start_as_current_span("execute_tool lookup_location", attributes=attributes) as span:
            self.location_calls += 1
            await asyncio.sleep(1.0)
            result = {"city": city.strip().casefold(), "country": "Demo country", "fictional": True}
            span.set_attribute("gen_ai.tool.call.result", json.dumps(result))
            return result

    async def plan_itinerary(self, city: str, days: int = 3) -> dict:
        if not 1 <= days <= 7:
            raise ValueError("Demo trips must last between one and seven days.")
        locations = [await self.lookup_location(city) for _day in range(days)]
        return {
            "city": city,
            "days": [{"day": index + 1, "location": dict(location)} for index, location in enumerate(locations)],
            "fictional": True,
        }


def estimate_budget(amount_usd: str) -> dict[str, str]:
    amount = Decimal(amount_usd)
    if not amount.is_finite() or amount < 0:
        raise ValueError("Budget must be a finite non-negative amount.")
    usd_per_eur = Decimal("1.20")
    converted = (amount * usd_per_eur).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return {
        "source_amount": str(amount),
        "source_currency": "USD",
        "target_currency": "EUR",
        "quote": "1 EUR = 1.20 USD",
        "converted_amount": str(converted),
        "data_source": "fictional demo exchange-rate provider",
    }