"""Unit tests for JSON, HTML, and JUnit XML report generators."""

import json
import xml.etree.ElementTree as ET
from pathlib import Path

from recoverybench.agent.idempotent import IdempotentPaymentAgent
from recoverybench.agent.naive import NaivePaymentAgent
from recoverybench.engine.runner import ExecutionEngine
from recoverybench.ledger.memory import InMemoryLedger
from recoverybench.reporting.html_reporter import HTMLReporter
from recoverybench.reporting.json_reporter import JSONReporter
from recoverybench.reporting.junit_reporter import JUnitReporter
from recoverybench.scenarios.schema import ScenarioModel
from recoverybench.tools.base import ToolRegistry
from recoverybench.tools.payment import MockPaymentService


def test_json_and_html_reporting_with_evidence(tmp_path: Path, timeout_after_scenario: ScenarioModel) -> None:
    ledger = InMemoryLedger()
    tools = ToolRegistry()
    tools.register(MockPaymentService())
    engine = ExecutionEngine(ledger=ledger, tool_registry=tools)

    agent = IdempotentPaymentAgent()
    result = engine.run(timeout_after_scenario, agent, run_id="run_report_test")

    # 1. JSON report with evidence
    json_path = tmp_path / "report.json"
    JSONReporter.save_report(result, json_path)
    assert json_path.exists()

    data = json.loads(json_path.read_text(encoding="utf-8"))
    assert data["run_id"] == "run_report_test"
    assert data["evaluation"]["passed"] is True
    assert data["evaluation"]["actual_side_effects"] == 1
    assert "evidence_summary" in data["evaluation"]
    assert "side_effect_count" in data["evaluation"]["evidence_summary"]

    assertions = data["evaluation"]["assertions"]
    assert len(assertions) >= 4
    for a in assertions:
        assert "evidence" in a
        assert isinstance(a["evidence"], list)

    # 2. HTML report with evidence drawers
    html_path = tmp_path / "report.html"
    HTMLReporter.save_report(result, html_path)
    assert html_path.exists()

    html_content = html_path.read_text(encoding="utf-8")
    assert "RecoveryBench Report" in html_content
    assert "payment_timeout_after_commit" in html_content
    assert "PASSED" in html_content
    assert "View Ledger Evidence" in html_content


def test_junit_xml_reporting_passing_run(tmp_path: Path, timeout_after_scenario: ScenarioModel) -> None:
    ledger = InMemoryLedger()
    tools = ToolRegistry()
    tools.register(MockPaymentService())
    engine = ExecutionEngine(ledger=ledger, tool_registry=tools)

    agent = IdempotentPaymentAgent()
    result = engine.run(timeout_after_scenario, agent, run_id="run_junit_pass")

    xml_path = tmp_path / "report.xml"
    JUnitReporter.save_report(result, xml_path)
    assert xml_path.exists()

    xml_text = xml_path.read_text(encoding="utf-8")
    root = ET.fromstring(xml_text)

    assert root.tag == "testsuites"
    assert root.attrib["name"] == "RecoveryBench"
    assert int(root.attrib["failures"]) == 0
    assert int(root.attrib["tests"]) >= 4

    suite = root.find("testsuite")
    assert suite is not None
    assert suite.attrib["name"] == timeout_after_scenario.scenario_id

    # Verify properties
    props = suite.find("properties")
    assert props is not None
    prop_map = {p.attrib["name"]: p.attrib["value"] for p in props.findall("property")}
    assert prop_map["verdict"] == "PASS"
    assert prop_map["run_id"] == "run_junit_pass"

    # Verify testcases have system-out containing evidence
    testcases = suite.findall("testcase")
    assert len(testcases) >= 4
    for tc in testcases:
        sysout = tc.find("system-out")
        assert sysout is not None
        assert "Supporting Ledger Evidence" in (sysout.text or "")


def test_junit_xml_reporting_failing_run(tmp_path: Path, timeout_after_scenario: ScenarioModel) -> None:
    ledger = InMemoryLedger()
    tools = ToolRegistry()
    tools.register(MockPaymentService())
    engine = ExecutionEngine(ledger=ledger, tool_registry=tools)

    # Naive agent will cause double-charge failure
    agent = NaivePaymentAgent()
    result = engine.run(timeout_after_scenario, agent, run_id="run_junit_fail")

    xml_path = tmp_path / "report_fail.xml"
    JUnitReporter.save_report(result, xml_path)
    assert xml_path.exists()

    root = ET.fromstring(xml_path.read_text(encoding="utf-8"))
    assert int(root.attrib["failures"]) > 0

    suite = root.find("testsuite")
    assert suite is not None
    testcases = suite.findall("testcase")

    # Find the failing testcase (e.g. side_effect_count or idempotency_key_stability)
    failing_cases = [tc for tc in testcases if tc.find("failure") is not None]
    assert len(failing_cases) >= 1

    failure_elem = failing_cases[0].find("failure")
    assert failure_elem is not None
    assert "Supporting Ledger Evidence" in (failure_elem.text or "")
