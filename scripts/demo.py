import argparse
from datetime import datetime, timezone
import hashlib
import io
import json
import os
from pathlib import Path
import re
import time
from urllib.parse import quote
import uuid
import zipfile

from azure.ai.projects import AIProjectClient
from azure.ai.projects.models import (
    AgentEndpointConfig,
    CodeConfiguration,
    CodeDependencyResolution,
    FixedRatioVersionSelectionRule,
    HostedAgentDefinition,
    ProtocolConfiguration,
    ProtocolVersionRecord,
    ResponsesProtocolConfiguration,
    VersionSelector,
)
from azure.core.exceptions import HttpResponseError, ResourceNotFoundError
from azure.core.rest import HttpRequest
from azure.identity import AzureCliCredential
from azure.monitor.query import LogsQueryClient, LogsQueryStatus
from dotenv import load_dotenv


ROOT = Path(__file__).resolve().parents[1]
ARTIFACTS = ROOT / ".artifacts"
RUNTIME_FILES = ("main.py", "travel_tools.py", "requirements.txt")
API_VERSION = "2025-05-15-preview"


def save_artifact(name: str, value: dict | list) -> None:
    ARTIFACTS.mkdir(exist_ok=True)
    (ARTIFACTS / name).write_text(json.dumps(value, indent=2), encoding="utf-8")


def source_archive() -> tuple[bytes, str]:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name in RUNTIME_FILES:
            info = zipfile.ZipInfo(name, date_time=(2026, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(info, (ROOT / name).read_bytes())
    content = buffer.getvalue()
    return content, hashlib.sha256(content).hexdigest()


def cloud_request(client: AIProjectClient, endpoint: str, method: str, path: str, body: dict | None = None) -> dict:
    separator = "&" if "?" in path else "?"
    headers = {"Accept": "application/json"}
    if method == "POST" and path.endswith("/runs"):
        headers["Operation-Id"] = str(uuid.uuid4())
    request = HttpRequest(method, f"{endpoint}{path}{separator}api-version={API_VERSION}", headers=headers, json=body)
    response = client.send_request(request)
    if response.status_code >= 400:
        error = response.json().get("error", {})
        raise RuntimeError(f"Cloud API {response.status_code}: {error.get('code', 'Unknown')}: {error.get('message', 'Request failed')}")
    return response.json()


def deploy(client: AIProjectClient, endpoint: str, agent_name: str, model: str, new_version: bool) -> None:
    try:
        client.agents.get(agent_name=agent_name)
    except ResourceNotFoundError:
        if new_version:
            raise RuntimeError("Cannot update an agent that does not exist.")
    else:
        if not new_version:
            raise RuntimeError("Agent already exists. Use --new-version only for a demo-owned agent.")
        manifest_path = ARTIFACTS / "deployment.json"
        if not manifest_path.exists():
            raise RuntimeError("A local deployment manifest is required before changing an existing agent.")
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if manifest["agent_name"] != agent_name or manifest["project_endpoint"] != endpoint:
            raise RuntimeError("Existing deployment manifest belongs to another agent or project.")
    content, digest = source_archive()
    environment = {
        "FOUNDRY_PROJECT_ENDPOINT": endpoint,
        "FOUNDRY_MODEL_NAME": model,
        "WEATHER_TIMEOUT_SECONDS": "0.5",
        "OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT": "true",
        "OTEL_TRACES_SAMPLER": "always_on",
        "APPLICATIONINSIGHTS_CONNECTION_STRING": client.telemetry.get_application_insights_connection_string(),
    }
    if not environment["APPLICATIONINSIGHTS_CONNECTION_STRING"]:
        raise RuntimeError("The project requires a connected Application Insights resource.")
    code_stream = io.BytesIO(content)
    code_stream.name = "travel-agent.zip"
    created = client.agents.create_version_from_code(
        agent_name=agent_name,
        description="Fictional travel demo for trace-grounded issue remediation.",
        metadata={"demo": "insights-mcp-demo"},
        definition=HostedAgentDefinition(
            cpu="0.5",
            memory="1Gi",
            code_configuration=CodeConfiguration(
                runtime="python_3_13",
                entry_point=["python", "main.py"],
                dependency_resolution=CodeDependencyResolution.REMOTE_BUILD,
            ),
            protocol_versions=[ProtocolVersionRecord(protocol="responses", version="2.0.0")],
            environment_variables=environment,
        ),
        code=code_stream,
        code_zip_sha256=digest,
    )
    manifest = {"agent_name": agent_name, "version": created.version, "source_sha256": digest, "project_endpoint": endpoint}
    save_artifact("deployment.json", manifest)
    save_artifact(f"deployment-v{created.version}.json", manifest)
    print(f"Created hosted version {created.version}; source SHA256 {digest}", flush=True)
    print("Use activate after Foundry reports this version active.", flush=True)


def activate(client: AIProjectClient, endpoint: str, agent_name: str) -> None:
    manifest = json.loads((ARTIFACTS / "deployment.json").read_text(encoding="utf-8"))
    if manifest["agent_name"] != agent_name or manifest["project_endpoint"] != endpoint:
        raise RuntimeError("Deployment manifest belongs to another agent or project.")
    version = manifest["version"]
    details = client.agents.get_version(agent_name=agent_name, agent_version=version)
    status = getattr(details.status, "value", details.status)
    if status != "active":
        raise RuntimeError(f"Hosted version {version} is {status}; it is not ready to route.")
    client.agents.update_details(
        agent_name=agent_name,
        agent_endpoint=AgentEndpointConfig(
            version_selector=VersionSelector(version_selection_rules=[
                FixedRatioVersionSelectionRule(agent_version=version, traffic_percentage=100)
            ]),
            protocol_configuration=ProtocolConfiguration(responses=ResponsesProtocolConfiguration()),
        ),
    )
    print(f"Routed demo agent to version {version}", flush=True)


def traffic(client: AIProjectClient, agent_name: str, rounds: int, label: str) -> None:
    scenarios = json.loads((ROOT / "data" / "scenarios.json").read_text(encoding="utf-8"))
    started = datetime.now(timezone.utc).isoformat()
    records = []
    with client.get_openai_client(agent_name=agent_name) as openai:
        for iteration in range(rounds):
            for scenario in scenarios:
                trace_id = uuid.uuid4().hex
                span_id = uuid.uuid4().hex[:16]
                start = time.monotonic()
                response = openai.responses.create(
                    input=scenario["prompt"],
                    extra_headers={"traceparent": f"00-{trace_id}-{span_id}-01"},
                    timeout=180,
                )
                record = {
                    "scenario": scenario["id"], "family": scenario["family"], "iteration": iteration,
                    "trace_id": trace_id, "response_id": response.id,
                    "duration_seconds": round(time.monotonic() - start, 3), "output": response.output_text,
                }
                records.append(record)
                save_artifact(f"traffic-{label}.json", {"agent_name": agent_name, "started_at": started, "records": records})
                print(f"{scenario['id']}: response={response.id} seconds={record['duration_seconds']}", flush=True)
    save_artifact(f"traffic-{label}.json", {
        "agent_name": agent_name, "started_at": started,
        "ended_at": datetime.now(timezone.utc).isoformat(), "records": records,
    })
    print(f"Completed {len(records)} real hosted invocations", flush=True)


def analyze(client: AIProjectClient, endpoint: str, agent_name: str, model: str, lookback: float) -> None:
    monitors = cloud_request(client, endpoint, "GET", f"/agent_insight_monitors?agent_name={quote(agent_name)}")
    if monitors["data"]:
        monitor = monitors["data"][0]
    else:
        monitor = cloud_request(client, endpoint, "POST", "/agent_insight_monitors", {
            "agent_name": agent_name, "enabled": False, "run_interval_hours": 6, "model_deployment_name": model,
        })
    result = cloud_request(client, endpoint, "POST", f"/agent_insight_monitors/{monitor['id']}/runs", {"lookback_hours": lookback})
    save_artifact("analysis.json", {"monitor_id": monitor["id"], "run_id": result["id"]})
    print(json.dumps({key: result.get(key) for key in ("id", "status", "window_start", "window_end")}, indent=2))


def evidence(client: AIProjectClient, credential: AzureCliCredential, agent_name: str, label: str) -> tuple[dict, list, list]:
    manifest = json.loads((ARTIFACTS / f"traffic-{label}.json").read_text(encoding="utf-8"))
    if manifest["agent_name"] != agent_name:
        raise RuntimeError("Traffic manifest belongs to a different agent.")
    connections = [connection for connection in client.connections.list()
                   if getattr(connection.type, "value", connection.type) == "AppInsights"]
    if len(connections) != 1:
        raise RuntimeError("This demo requires exactly one Application Insights connection.")
    started = datetime.fromisoformat(manifest["started_at"])
    ended = datetime.fromisoformat(manifest["ended_at"])
    trace_ids = [record["trace_id"] for record in manifest["records"]]
    query = (
        "union requests, dependencies "
        f"| where operation_Id in (dynamic({json.dumps(trace_ids)})) "
        f"| where tostring(customDimensions['gen_ai.agent.name']) == {json.dumps(agent_name)} "
        "| project operation_Id, id, name, success, duration, customDimensions"
    )
    result = LogsQueryClient(credential).query_resource(connections[0].target, query, timespan=(started, ended))
    if result.status != LogsQueryStatus.SUCCESS:
        raise RuntimeError("Telemetry query did not complete successfully.")
    spans = [dict(zip(table.columns, row)) for table in result.tables for row in table.rows]
    for span in spans:
        if isinstance(span["customDimensions"], str):
            span["customDimensions"] = json.loads(span["customDimensions"])
    save_artifact(f"evidence-{label}.json", spans)
    root_ids = {span["operation_Id"] for span in spans if span["name"].startswith("invoke_agent")}
    tool_counts = {}
    versions = set()
    for span in spans:
        if span["name"].startswith("execute_tool"):
            tool_counts[span["name"]] = tool_counts.get(span["name"], 0) + 1
        versions.add(span["customDimensions"].get("gen_ai.agent.version"))
    print(json.dumps({"expected_requests": len(trace_ids), "requests_with_agent_span": len(root_ids),
                      "tool_spans": tool_counts, "versions": list(versions)}, indent=2))
    if root_ids != set(trace_ids):
        raise RuntimeError("Not all requested traces are queryable yet. Check ingestion or sampling before analysis.")
    content_query = (
        f"genAIContent | where operation_Id in (dynamic({json.dumps(trace_ids)})) "
        "| where isnotempty(toolCallResult) | project operation_Id, id, toolCallArguments, toolCallResult"
    )
    content_result = LogsQueryClient(credential).query_resource(connections[0].target, content_query, timespan=(started, ended))
    if content_result.status != LogsQueryStatus.SUCCESS:
        raise RuntimeError("Tool content query did not complete successfully.")
    content = [dict(zip(table.columns, row)) for table in content_result.tables for row in table.rows]
    save_artifact(f"tool-content-{label}.json", content)
    return manifest, spans, content


def verify_replay(manifest: dict, spans: list, content: list, version: str, label: str) -> dict:
    expected_budgets = {"budget-120": "100.00", "budget-240": "200.00", "budget-600": "500.00"}
    baseline_budgets = {"budget-120": "144.00", "budget-240": "288.00", "budget-600": "720.00"}
    expected_days = {"itinerary-lisbon": 5, "itinerary-vienna": 6, "itinerary-stockholm": 7}
    content_by_span = {(row["operation_Id"], row["id"]): row for row in content}
    scenarios = json.loads((ROOT / "data" / "scenarios.json").read_text(encoding="utf-8"))
    if {row["scenario"] for row in manifest["records"]} != {row["id"] for row in scenarios}:
        raise RuntimeError("Replay does not cover the complete scenario set.")
    if any(str(span["customDimensions"].get("gen_ai.agent.version")) != version for span in spans):
        raise RuntimeError("Replay contains evidence from an unexpected agent version.")
    checks = []
    for record in manifest["records"]:
        if not record["output"].strip():
            raise RuntimeError(f"Empty agent response for {record['scenario']}.")
        tools = [span for span in spans if span["operation_Id"] == record["trace_id"] and span["name"].startswith("execute_tool")]
        if record["family"] == "control":
            if tools:
                raise RuntimeError("A healthy control unexpectedly called a tool.")
            checks.append({"scenario": record["scenario"], "tool_calls": 0})
            continue
        tool_name = {"weather": "get_weather", "budget": "estimate_budget", "itinerary": "plan_itinerary"}[record["family"]]
        selected = [span for span in tools if span["name"] == f"execute_tool {tool_name}"]
        if len(selected) != 1:
            raise RuntimeError(f"Expected exactly one {tool_name} call for {record['scenario']}.")
        tool_span = selected[0]
        result = json.loads(content_by_span[(tool_span["operation_Id"], tool_span["id"])]["toolCallResult"])
        check = {"scenario": record["scenario"]}
        if record["family"] == "weather":
            expected_status = "ok" if label == "fixed" else "unavailable"
            if result.get("status") != expected_status:
                raise RuntimeError("Weather result does not match the expected replay state.")
            if label == "fixed":
                if result.get("error") or str(tool_span["success"]).lower() != "true":
                    raise RuntimeError("Fixed weather request still reports an error.")
            elif result.get("error") != "weather_provider_timeout":
                raise RuntimeError("Baseline weather result is missing the expected provider timeout.")
            check["weather_status"] = result["status"]
            check["span_success"] = str(tool_span["success"]).lower() == "true"
            check["provider_error"] = result.get("error")
        elif record["family"] == "budget":
            expected = (expected_budgets if label == "fixed" else baseline_budgets)[record["scenario"]]
            if result.get("converted_amount") != expected:
                raise RuntimeError(f"Unexpected FX result for {record['scenario']}.")
            check["converted_amount"] = result["converted_amount"]
        else:
            days = expected_days[record["scenario"]]
            lookups = sum(span["name"] == "execute_tool lookup_location" for span in tools)
            if len(result["days"]) != days or lookups != (1 if label == "fixed" else days):
                raise RuntimeError(f"Itinerary shape or lookup count is incorrect for {record['scenario']}.")
            check["location_lookups"] = lookups
        checks.append(check)
    return {"version": version, "label": label, "passed": True, "requests": len(checks), "checks": checks}


def main() -> None:
    load_dotenv(ROOT / ".env")
    parser = argparse.ArgumentParser(description="Prepare a source-hosted demo; read insights using remote Foundry MCP.")
    parser.add_argument("action", choices=["preflight", "deploy", "activate", "traffic", "evidence", "verify", "analyze", "status"])
    parser.add_argument("--agent-name", default=os.getenv("FOUNDRY_AGENT_NAME", "travel-insights-demo"))
    parser.add_argument("--new-version", action="store_true")
    parser.add_argument("--rounds", type=int, choices=range(1, 3), default=2)
    parser.add_argument("--label", choices=["baseline", "fixed", "rehearsal"], default="baseline")
    parser.add_argument("--lookback-hours", type=float, default=1)
    parser.add_argument("--expected-version")
    args = parser.parse_args()
    if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_-]{0,62}", args.agent_name):
        parser.error("Invalid demo agent name.")
    endpoint = os.environ["FOUNDRY_PROJECT_ENDPOINT"].rstrip("/")
    with AzureCliCredential() as credential, AIProjectClient(endpoint, credential) as client:
        model = os.environ["FOUNDRY_MODEL_NAME"]
        if args.action == "preflight":
            result = cloud_request(client, endpoint, "GET", "/agent_insight_monitors?limit=1")
            print(json.dumps({"cloud_insights_read": isinstance(result.get("data"), list),
                              "telemetry_connected": bool(client.telemetry.get_application_insights_connection_string())}))
        elif args.action == "deploy":
            deploy(client, endpoint, args.agent_name, model, args.new_version)
        elif args.action == "activate":
            activate(client, endpoint, args.agent_name)
        elif args.action == "traffic":
            traffic(client, args.agent_name, args.rounds, args.label)
        elif args.action == "analyze":
            analyze(client, endpoint, args.agent_name, model, args.lookback_hours)
        elif args.action == "evidence":
            evidence(client, credential, args.agent_name, args.label)
        elif args.action == "verify":
            if not args.expected_version or args.label == "rehearsal":
                parser.error("verify requires --expected-version and a baseline or fixed label.")
            manifest, spans, content = evidence(client, credential, args.agent_name, args.label)
            result = verify_replay(manifest, spans, content, args.expected_version, args.label)
            save_artifact(f"verification-{args.label}.json", result)
            print(json.dumps(result, indent=2))
        else:
            manifest = json.loads((ARTIFACTS / "analysis.json").read_text(encoding="utf-8"))
            result = cloud_request(client, endpoint, "GET", f"/agent_insight_monitors/{manifest['monitor_id']}/runs/{manifest['run_id']}")
            print(json.dumps(result, indent=2))


if __name__ == "__main__":
    try:
        main()
    except HttpResponseError as error:
        detail = error.error
        code = getattr(detail, "code", "Unknown")
        message = getattr(detail, "message", "Check project permissions and service availability.")
        raise SystemExit(f"Azure request failed (HTTP {error.status_code}, {code}): {message}") from None