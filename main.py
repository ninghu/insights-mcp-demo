import os

from agent_framework import Agent, tool
from agent_framework.foundry import FoundryChatClient
from agent_framework_foundry_hosting import ResponsesHostServer
from azure.identity import DefaultAzureCredential
from dotenv import load_dotenv

from travel_tools import TravelTools, estimate_budget as calculate_budget


@tool(description="Get weather for a city from the fictional travel demo provider.", max_invocations=1)
async def get_weather(city: str) -> dict:
    return await TravelTools(float(os.getenv("WEATHER_TIMEOUT_SECONDS", "0.5"))).get_weather(city)


@tool(description="Build a one-to-seven-day itinerary using the fictional location provider.", max_invocations=1)
async def plan_itinerary(city: str, days: int = 3) -> dict:
    return await TravelTools().plan_itinerary(city, days)


@tool(description="Convert a USD travel budget to EUR using the demo provider's stated FX quote.", max_invocations=1)
def estimate_budget(amount_usd: str) -> dict:
    return calculate_budget(amount_usd)


def build_server() -> ResponsesHostServer:
    load_dotenv()
    client = FoundryChatClient(
        project_endpoint=os.environ["FOUNDRY_PROJECT_ENDPOINT"],
        model=os.environ["FOUNDRY_MODEL_NAME"],
        credential=DefaultAzureCredential(),
    )
    agent = Agent(
        client=client,
        name=os.getenv("FOUNDRY_AGENT_NAME", "travel-insights-demo"),
        instructions=(
            "You are a travel-planning demonstration assistant. All provider data is fictional. "
            "Use the corresponding tool for weather, itinerary or currency requests. "
            "Call only tools needed for the request, once each. If a tool is unavailable, "
            "say so and do not invent its result or retry it. Clearly identify demo data. "
            "For budget requests report the provider's source amount, quote and converted amount. "
            "Keep answers under 120 words. Do not make bookings or purchases."
        ),
        tools=[get_weather, plan_itinerary, estimate_budget],
        default_options={"store": False, "max_output_tokens": 1024},
    )
    return ResponsesHostServer(agent)


if __name__ == "__main__":
    build_server().run()