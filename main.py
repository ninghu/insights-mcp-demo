import os

from azure.identity import DefaultAzureCredential
from dotenv import load_dotenv
from langchain.agents import create_agent
from langchain.agents.middleware import ToolCallLimitMiddleware
from langchain_core.language_models import BaseChatModel
from langchain_core.tools import tool
from langchain_azure_ai.agents.hosting import ResponsesHostServer
from langchain_azure_ai.callbacks.tracers import enable_auto_tracing
from langchain_azure_ai.chat_models import AzureAIOpenAIApiChatModel
from langgraph.graph.state import CompiledStateGraph

from travel_tools import TravelTools, estimate_budget as calculate_budget


@tool(description="Get weather for a city from the fictional travel demo provider.")
async def get_weather(city: str) -> dict:
    return await TravelTools(float(os.getenv("WEATHER_TIMEOUT_SECONDS", "0.5"))).get_weather(city)


@tool(description="Build a one-to-seven-day itinerary using the fictional location provider.")
async def plan_itinerary(city: str, days: int = 3) -> dict:
    return await TravelTools().plan_itinerary(city, days)


@tool(description="Convert a USD travel budget to EUR using the demo provider's stated FX quote.")
def estimate_budget(amount_usd: str) -> dict:
    return calculate_budget(amount_usd)


def build_graph(model: BaseChatModel | None = None) -> CompiledStateGraph:
    if model is None:
        model = AzureAIOpenAIApiChatModel(
            project_endpoint=os.environ["FOUNDRY_PROJECT_ENDPOINT"],
            model=os.environ["FOUNDRY_MODEL_NAME"],
            credential=DefaultAzureCredential(),
            use_responses_api=True,
            output_version="responses/v1",
            store=False,
            max_tokens=1024,
            max_retries=2,
            timeout=120,
        )
    tools = [get_weather, plan_itinerary, estimate_budget]
    return create_agent(
        model=model,
        name=os.getenv("FOUNDRY_AGENT_NAME", "travel-insights-demo"),
        system_prompt=(
            "You are a travel-planning demonstration assistant. All provider data is fictional. "
            "Use the corresponding tool for weather, itinerary or currency requests. "
            "Call only tools needed for the request, once each. If a tool is unavailable, "
            "say so and do not invent its result or retry it. Clearly identify demo data. "
            "For budget requests report the provider's source amount, quote and converted amount. "
            "Keep answers under 120 words. Do not make bookings or purchases."
        ),
        tools=tools,
        middleware=[ToolCallLimitMiddleware(tool_name=registered_tool.name, run_limit=1, exit_behavior="end")
                    for registered_tool in tools],
    ).with_config({"recursion_limit": 12})


def build_server() -> ResponsesHostServer:
    load_dotenv()
    server = ResponsesHostServer(build_graph())
    enable_auto_tracing(
        auto_configure_azure_monitor=False,
        enable_content_recording=os.getenv("OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT", "true").lower() not in {"false", "0"},
    )
    return server


if __name__ == "__main__":
    build_server().run()