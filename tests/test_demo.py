import asyncio
import json
import unittest
from unittest.mock import patch

from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage, ToolMessage
from langgraph.graph.state import CompiledStateGraph
from opentelemetry import trace
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from starlette.testclient import TestClient

from main import build_graph, build_server
from scripts.demo import verify_replay
from travel_tools import TravelTools, estimate_budget


class ScriptedChatModel(FakeMessagesListChatModel):
    def bind_tools(self, tools, **kwargs):
        return self


class GraphTests(unittest.IsolatedAsyncioTestCase):
    def budget_call(self, call_id: str) -> AIMessage:
        return AIMessage(content="", tool_calls=[{
            "name": "estimate_budget", "args": {"amount_usd": "120.00"}, "id": call_id, "type": "tool_call",
        }])

    async def test_graph_executes_model_tool_model_cycle(self):
        model = ScriptedChatModel(responses=[self.budget_call("budget-1"), AIMessage(content="EUR 100.00")])
        graph = build_graph(model)
        self.assertIsInstance(graph, CompiledStateGraph)
        self.assertIn("tools", graph.get_graph().nodes)
        with patch("main.calculate_budget", return_value={"converted_amount": "100.00"}) as calculate:
            result = await graph.ainvoke({"messages": [("user", "Convert USD 120 to EUR.")]})
        calculate.assert_called_once_with("120.00")
        outputs = [message for message in result["messages"] if isinstance(message, ToolMessage)]
        self.assertEqual(len(outputs), 1)
        self.assertEqual(json.loads(outputs[0].content)["converted_amount"], "100.00")
        self.assertEqual(result["messages"][-1].content, "EUR 100.00")

    async def test_graph_blocks_repeated_tool_execution(self):
        model = ScriptedChatModel(responses=[self.budget_call("budget-1"), self.budget_call("budget-2")])
        graph = build_graph(model)
        with patch("main.calculate_budget", wraps=estimate_budget) as calculate:
            result = await graph.ainvoke({"messages": [("user", "Convert USD 120 to EUR.")]})
        self.assertEqual(calculate.call_count, 1)
        self.assertIsInstance(result["messages"][-1], AIMessage)

    async def test_graph_tool_limit_is_per_request(self):
        model = ScriptedChatModel(responses=[self.budget_call("budget-1"), AIMessage(content="EUR 100.00")])
        graph = build_graph(model)
        with patch("main.calculate_budget", wraps=estimate_budget) as calculate:
            for _request in range(2):
                result = await graph.ainvoke({"messages": [("user", "Convert USD 120 to EUR.")]})
                self.assertEqual(result["messages"][-1].content, "EUR 100.00")
        self.assertEqual(calculate.call_count, 2)

    async def test_graph_executes_async_travel_tools(self):
        calls = AIMessage(content="", tool_calls=[
            {"name": "get_weather", "args": {"city": "Lisbon"}, "id": "weather-1", "type": "tool_call"},
            {"name": "plan_itinerary", "args": {"city": "Lisbon", "days": 2}, "id": "itinerary-1", "type": "tool_call"},
        ])
        graph = build_graph(ScriptedChatModel(responses=[calls, AIMessage(content="Trip prepared.")]))
        with patch("main.TravelTools", autospec=True) as tools_factory:
            tools = tools_factory.return_value
            tools.get_weather.return_value = {"status": "ok", "city": "Lisbon"}
            tools.plan_itinerary.return_value = {"days": [{"day": 1}, {"day": 2}]}
            result = await graph.ainvoke({"messages": [("user", "Check weather and plan two days in Lisbon.")]})
        tools.get_weather.assert_awaited_once_with("Lisbon")
        tools.plan_itinerary.assert_awaited_once_with("Lisbon", 2)
        outputs = {message.name: json.loads(message.content) for message in result["messages"] if isinstance(message, ToolMessage)}
        self.assertEqual(outputs["get_weather"]["status"], "ok")
        self.assertEqual(len(outputs["plan_itinerary"]["days"]), 2)
        self.assertEqual(result["messages"][-1].content, "Trip prepared.")


class HostingTests(unittest.TestCase):
    def test_responses_host_records_each_tool_once(self):
        tool_call = AIMessage(content="", tool_calls=[{
            "name": "estimate_budget", "args": {"amount_usd": "120.00"}, "id": "budget-host-1", "type": "tool_call",
        }])
        graph = build_graph(ScriptedChatModel(responses=[tool_call, AIMessage(content="EUR 100.00")]))
        with patch("main.build_graph", return_value=graph), patch("main.load_dotenv"):
            server = build_server()
        exporter = InMemorySpanExporter()
        trace.get_tracer_provider().add_span_processor(SimpleSpanProcessor(exporter))
        with TestClient(server.app) as client:
            response = client.post("/responses", json={"input": "Convert USD 120 to EUR.", "stream": False, "store": False})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "completed")
        tool_spans = [span for span in exporter.get_finished_spans()
                      if span.attributes.get("gen_ai.operation.name") == "execute_tool"
                      and span.attributes.get("gen_ai.tool.name") == "estimate_budget"]
        self.assertEqual(len(tool_spans), 1)
        self.assertEqual(tool_spans[0].attributes["gen_ai.tool.call.id"], "budget-host-1")


class ReplayVerificationTests(unittest.TestCase):
    def verify_weather(self, result, span_success, label):
        manifest = {"records": [{"scenario": "weather-test", "family": "weather", "trace_id": "test-trace", "output": "Weather response."}]}
        spans = [{"operation_Id": "test-trace", "id": "test-span", "name": "execute_tool get_weather",
                  "success": span_success, "customDimensions": {"gen_ai.agent.version": "7"}}]
        content = [{"operation_Id": "test-trace", "id": "test-span", "toolCallResult": json.dumps(result)}]
        with patch("pathlib.Path.read_text", return_value=json.dumps([{"id": "weather-test"}])):
            return verify_replay(manifest, spans, content, "7", label)

    def test_baseline_requires_explicit_provider_timeout(self):
        for span_success in ("True", "False"):
            with self.subTest(span_success=span_success):
                result = self.verify_weather({"status": "unavailable", "error": "weather_provider_timeout"}, span_success, "baseline")
                self.assertTrue(result["passed"])
                self.assertEqual(result["checks"][0]["span_success"], span_success == "True")
        with self.assertRaises(RuntimeError):
            self.verify_weather({"status": "unavailable"}, "True", "baseline")

    def test_fixed_weather_rejects_handled_provider_failure(self):
        with self.assertRaises(RuntimeError):
            self.verify_weather({"status": "unavailable", "error": "weather_provider_timeout"}, "True", "fixed")

    def test_fixed_weather_requires_successful_span_and_result(self):
        with self.assertRaises(RuntimeError):
            self.verify_weather({"status": "ok"}, "False", "fixed")
        self.assertTrue(self.verify_weather({"status": "ok"}, "True", "fixed")["passed"])


class TravelTests(unittest.IsolatedAsyncioTestCase):
    @unittest.expectedFailure
    async def test_weather_respects_configured_timeout(self):
        self.assertEqual((await TravelTools(timeout_seconds=0.5).get_weather("Lisbon"))["status"], "ok")

    async def test_weather_reports_real_timeouts(self):
        tools = TravelTools(timeout_seconds=0.001)
        self.assertEqual((await tools.get_weather("Lisbon"))["status"], "unavailable")

    @unittest.expectedFailure
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
    @unittest.expectedFailure
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