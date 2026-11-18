"""Comprehensive test suite for Phase 3B.3 Stable JSON Report Contract and Review Corrections.

Tests verify:
- Three-state side-effect safety semantics (VERIFIED_SAFE, UNSAFE, UNVERIFIED).
- Strict redaction across every serialization entry point (dict, ReportModel, file, RunResult).
- Strict schema validation: forbidden unknown fields (extra="forbid"), supported verdicts,
  strict field types, and cross-section semantic consistency.
- Successful and failed execution report structures.
- Outcome dimensions (execution_success, recovery_success, side_effect_safety, validation_failure, runtime_failure).
- Deterministic serialization (stable sorting and byte-level equality).
- Full backward compatibility with legacy report consumers.
"""

import hashlib
import json
from pathlib import Path

import pytest

from recoverybench.agent.idempotent import IdempotentPaymentAgent
from recoverybench.agent.naive import NaivePaymentAgent
from recoverybench.engine.runner import ExecutionEngine
from recoverybench.errors import (
    InfrastructureError,
    ReportValidationError,
)
from recoverybench.ledger.memory import InMemoryLedger
from recoverybench.models import (
    CommitStatus,
    EvaluationResult,
    EvaluationVerdict,
    RunSummary,
)
from recoverybench.reporting.json_reporter import (
    JSONReporter,
    classify_side_effect_safety,
)
from recoverybench.reporting.schema import (
    SUPPORTED_REPORT_SCHEMA_VERSIONS,
    InjectedFaultReport,
    ReportValidator,
    SideEffectSafety,
)
from recoverybench.scenarios.schema import ScenarioAssertions, ScenarioModel
from recoverybench.tools.base import ToolRegistry
from recoverybench.tools.payment import MockPaymentService

# =============================================================================
# 1. SIDE-EFFECT SAFETY SEMANTICS (Three-State Classification)
# =============================================================================


def test_safety_classification_known_expected_exact_match(timeout_after_scenario: ScenarioModel) -> None:
    """1. Known expected count and exact match -> VERIFIED_SAFE (safe=True)."""
    ledger = InMemoryLedger()
    tools = ToolRegistry()
    tools.register(MockPaymentService())
    engine = ExecutionEngine(ledger=ledger, tool_registry=tools)

    agent = IdempotentPaymentAgent()
    result = engine.run(timeout_after_scenario, agent, run_id="run_safety_verified_safe")

    safety = classify_side_effect_safety(result)
    assert safety == SideEffectSafety.VERIFIED_SAFE

    report_dict = result.to_report_dict()
    assert report_dict["outcome"]["side_effect_safety"] == "VERIFIED_SAFE"
    assert report_dict["outcome"]["side_effect_safe"] is True
    assert report_dict["side_effects"]["safety_status"] == "VERIFIED_SAFE"
    assert report_dict["side_effects"]["safe"] is True


def test_safety_classification_duplicate_side_effects(timeout_after_scenario: ScenarioModel) -> None:
    """2. Duplicate side effects detected -> UNSAFE (safe=False)."""
    ledger = InMemoryLedger()
    tools = ToolRegistry()
    tools.register(MockPaymentService())
    engine = ExecutionEngine(ledger=ledger, tool_registry=tools)

    agent = NaivePaymentAgent()
    result = engine.run(timeout_after_scenario, agent, run_id="run_safety_unsafe_dup")

    safety = classify_side_effect_safety(result)
    assert safety == SideEffectSafety.UNSAFE

    report_dict = result.to_report_dict()
    assert report_dict["outcome"]["side_effect_safety"] == "UNSAFE"
    assert report_dict["outcome"]["side_effect_safe"] is False
    assert report_dict["side_effects"]["safety_status"] == "UNSAFE"
    assert report_dict["side_effects"]["safe"] is False
    assert report_dict["side_effects"]["duplicate_count"] == 1


def test_safety_classification_unexpected_side_effect_count(timeout_after_scenario: ScenarioModel) -> None:
    """3. Unexpected side-effect count (e.g. expected 1, actual 0) -> UNSAFE (safe=False)."""
    phantom_scenario = timeout_after_scenario.model_copy(
        update={
            "scenario_id": "phantom_test",
            "assertions": ScenarioAssertions(
                expected_side_effects=1,
                max_duplicate_effects=0,
                expect_agent_success=True,
            ),
        }
    )

    ledger = InMemoryLedger()
    tools = ToolRegistry()
    tools.register(MockPaymentService())
    engine = ExecutionEngine(ledger=ledger, tool_registry=tools)

    # Agent that does nothing but reports success
    class PhantomAgent:
        agent_id = "phantom_agent"

        def run(self, task: object, tool: object) -> object:
            from recoverybench.agent.base import AgentResult

            return AgentResult(success=True, status_message="I did it!")

    result = engine.run(phantom_scenario, PhantomAgent(), run_id="run_phantom_test")  # type: ignore[arg-type]

    safety = classify_side_effect_safety(result)
    assert safety == SideEffectSafety.UNSAFE

    report_dict = result.to_report_dict()
    assert report_dict["outcome"]["side_effect_safety"] == "UNSAFE"
    assert report_dict["outcome"]["side_effect_safe"] is False
    assert report_dict["side_effects"]["safety_status"] == "UNSAFE"
    assert report_dict["side_effects"]["safe"] is False


def test_safety_classification_missing_expected_count(timeout_after_scenario: ScenarioModel) -> None:
    """4. Missing expected count (expected_side_effects is None) -> UNVERIFIED (safe=False)."""
    open_ended_scenario = timeout_after_scenario.model_copy(
        update={
            "assertions": ScenarioAssertions(
                expected_side_effects=None,
                max_duplicate_effects=0,
                require_stable_idempotency_key=True,
            ),
        }
    )

    ledger = InMemoryLedger()
    tools = ToolRegistry()
    tools.register(MockPaymentService())
    engine = ExecutionEngine(ledger=ledger, tool_registry=tools)

    agent = IdempotentPaymentAgent()
    result = engine.run(open_ended_scenario, agent, run_id="run_safety_unverified_none")

    safety = classify_side_effect_safety(result)
    assert safety == SideEffectSafety.UNVERIFIED

    report_dict = result.to_report_dict()
    # Must NOT misrepresent unknown safety as verified safe!
    assert report_dict["outcome"]["side_effect_safety"] == "UNVERIFIED"
    assert report_dict["outcome"]["side_effect_safe"] is False
    assert report_dict["side_effects"]["safety_status"] == "UNVERIFIED"
    assert report_dict["side_effects"]["safe"] is False


def test_safety_classification_infrastructure_failure_no_events(
    timeout_after_scenario: ScenarioModel,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """5. Infrastructure failure with no ledger events -> UNVERIFIED (safe=False)."""
    ledger = InMemoryLedger()
    tools = ToolRegistry()
    tools.register(MockPaymentService())
    engine = ExecutionEngine(ledger=ledger, tool_registry=tools)

    def raise_infra(self: NaivePaymentAgent, task: object, tool: object) -> object:
        raise InfrastructureError("Immediate container initialization crash")

    monkeypatch.setattr(NaivePaymentAgent, "run", raise_infra)
    agent = NaivePaymentAgent()
    result = engine.run(timeout_after_scenario, agent, run_id="run_infra_empty_events")

    assert len(result.events) == 0
    safety = classify_side_effect_safety(result)
    assert safety == SideEffectSafety.UNVERIFIED

    report_dict = result.to_report_dict()
    assert report_dict["outcome"]["side_effect_safety"] == "UNVERIFIED"
    assert report_dict["outcome"]["side_effect_safe"] is False
    assert report_dict["side_effects"]["safety_status"] == "UNVERIFIED"
    assert report_dict["side_effects"]["safe"] is False


def test_safety_classification_insufficient_evidence(timeout_after_scenario: ScenarioModel) -> None:
    """6. Insufficient execution evidence (verdict == INSUFFICIENT_EVIDENCE) -> UNVERIFIED."""
    # Construct an artificial RunResult with INSUFFICIENT_EVIDENCE verdict
    eval_res = EvaluationResult(
        verdict=EvaluationVerdict.INSUFFICIENT_EVIDENCE,
        passed=False,
        expected_side_effects=1,
        actual_side_effects=0,
        duplicate_effect_count=0,
        idempotency_key_stable=False,
        failure_reasons=["No ledger events recorded"],
    )
    summary = RunSummary(
        run_id="run_insufficient_ev",
        scenario_id=timeout_after_scenario.scenario_id,
        agent_id="test_agent",
        passed=False,
        verdict=EvaluationVerdict.INSUFFICIENT_EVIDENCE,
        total_events=0,
        committed_effects=0,
        duplicate_effects=0,
        created_at="2024-01-01T00:00:00Z",
        duration_ms=1.0,
    )
    from recoverybench.engine.context import RunResult

    run_result = RunResult(
        run_id="run_insufficient_ev",
        scenario=timeout_after_scenario,
        agent_id="test_agent",
        evaluation=eval_res,
        events=[],
        summary=summary,
    )

    safety = classify_side_effect_safety(run_result)
    assert safety == SideEffectSafety.UNVERIFIED

    report_dict = run_result.to_report_dict()
    assert report_dict["outcome"]["side_effect_safety"] == "UNVERIFIED"
    assert report_dict["outcome"]["side_effect_safe"] is False


# =============================================================================
# 2. SENSITIVE-DATA REDACTION ACROSS EVERY ENTRY POINT
# =============================================================================


def test_redaction_dictionary_serialization() -> None:
    """Verify that JSONReporter.to_json_string redacts raw dictionaries directly passed in."""
    synthetic_key = "".join(["sk", "_live_", "1234567890abcdef12345678"])
    raw_dict = {
        "api_key": synthetic_key,
        "nested": {
            "card_number": "4111 1111 1111 1111",
            "token": "Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.dummy_token",
            "safe_key": "safe_value",
        },
    }
    serialized = JSONReporter.to_json_string(raw_dict)
    assert synthetic_key not in serialized
    assert "4111 1111 1111 1111" not in serialized
    assert "[REDACTED]" in serialized


def test_redaction_report_model_serialization(timeout_after_scenario: ScenarioModel) -> None:
    """Verify that JSONReporter.to_json_string redacts ReportModel instances."""
    ledger = InMemoryLedger()
    tools = ToolRegistry()
    tools.register(MockPaymentService())
    engine = ExecutionEngine(ledger=ledger, tool_registry=tools)

    agent = IdempotentPaymentAgent()
    result = engine.run(timeout_after_scenario, agent, run_id="run_model_redaction")
    model = result.to_report()

    synthetic_key = "".join(["sk", "_live_", "very_secret_model_key_123"])
    # Manually attach sensitive data to model's agent_result
    model.agent_result = {
        "api_key": synthetic_key,
        "card_number": "4111111111111111",
    }

    serialized = JSONReporter.to_json_string(model)
    assert synthetic_key not in serialized
    assert "4111111111111111" not in serialized
    assert "[REDACTED]" in serialized


def test_redaction_file_serialization(tmp_path: Path) -> None:
    """Verify that JSONReporter.save_report redacts sensitive data when writing to disk."""
    synthetic_key = "".join(["sk", "_live_", "file_secret_99999"])
    raw_dict = {
        "report_schema_version": 1,
        "api_key": synthetic_key,
        "secret": "super_secret_file_value",
    }
    file_path = tmp_path / "secret_report.json"
    JSONReporter.save_report(raw_dict, file_path)

    content = file_path.read_text(encoding="utf-8")
    assert synthetic_key not in content
    assert "super_secret_file_value" not in content
    assert "[REDACTED]" in content


def test_redaction_run_result_all_methods(timeout_after_scenario: ScenarioModel) -> None:
    """Verify that RunResult.to_report_dict, to_report, and to_json all redact sensitive data."""
    synthetic_key = "".join(["sk", "_live_", "secret_run_res_12345"])
    scenario_with_secrets = timeout_after_scenario.model_copy(
        update={
            "description": f"Scenario with secret key {synthetic_key}",
        }
    )

    ledger = InMemoryLedger()
    tools = ToolRegistry()
    tools.register(MockPaymentService())
    engine = ExecutionEngine(ledger=ledger, tool_registry=tools)

    agent = IdempotentPaymentAgent()
    result = engine.run(scenario_with_secrets, agent, run_id="run_res_redaction")
    result.agent_raw_result = {"password": "agent_password_secret_123"}

    # 1. to_report_dict
    report_dict = result.to_report_dict()
    assert synthetic_key not in report_dict["scenario"]["description"]
    assert report_dict["agent_result"]["password"] == "[REDACTED]"

    # 2. to_report
    report_model = result.to_report()
    assert synthetic_key not in str(report_model.scenario.description)
    assert report_model.agent_result is not None
    assert report_model.agent_result["password"] == "[REDACTED]"

    # 3. to_json
    report_json = result.to_json()
    assert synthetic_key not in report_json
    assert "agent_password_secret_123" not in report_json
    assert "[REDACTED]" in report_json


# =============================================================================
# 3. STRICT SCHEMA AND SEMANTIC VALIDATION
# =============================================================================


def test_schema_rejects_unknown_extra_fields(timeout_after_scenario: ScenarioModel) -> None:
    """Verify that unknown/extra fields are strictly forbidden (extra='forbid') on models."""
    ledger = InMemoryLedger()
    tools = ToolRegistry()
    tools.register(MockPaymentService())
    engine = ExecutionEngine(ledger=ledger, tool_registry=tools)

    agent = IdempotentPaymentAgent()
    result = engine.run(timeout_after_scenario, agent, run_id="run_extra_fields")
    base_dict = result.to_report_dict()

    # 1. Top-level extra field
    tampered_top = dict(base_dict)
    tampered_top["unknown_dashboard_field"] = "unexpected"
    with pytest.raises(ReportValidationError) as exc:
        ReportValidator.validate_dict(tampered_top)
    assert "Extra inputs are not permitted" in str(exc.value) or "unknown_dashboard_field" in str(exc.value)

    # 2. Extra field in outcome
    tampered_outcome = json.loads(json.dumps(base_dict))
    tampered_outcome["outcome"]["extra_metric"] = 123
    with pytest.raises(ReportValidationError) as exc:
        ReportValidator.validate_dict(tampered_outcome)
    assert "extra_metric" in str(exc.value) or "Extra inputs are not permitted" in str(exc.value)


def test_schema_rejects_unsupported_verdicts(timeout_after_scenario: ScenarioModel) -> None:
    """Verify that unsupported verdict strings fail validation even if outcome and evaluation agree."""
    ledger = InMemoryLedger()
    tools = ToolRegistry()
    tools.register(MockPaymentService())
    engine = ExecutionEngine(ledger=ledger, tool_registry=tools)

    agent = IdempotentPaymentAgent()
    result = engine.run(timeout_after_scenario, agent, run_id="run_bad_verdict")
    tampered = result.to_report_dict()

    # Arbitrary matching verdict
    tampered["outcome"]["verdict"] = "CUSTOM_ARBITRARY_VERDICT"
    tampered["evaluation"]["verdict"] = "CUSTOM_ARBITRARY_VERDICT"

    with pytest.raises(ReportValidationError) as exc:
        ReportValidator.validate_dict(tampered)
    assert "Unsupported verdict 'CUSTOM_ARBITRARY_VERDICT'" in str(exc.value)


def test_semantic_validation_execution_success_consistency(timeout_after_scenario: ScenarioModel) -> None:
    """Verify semantic check: execution_success must be False for INFRASTRUCTURE_ERROR, True otherwise."""
    ledger = InMemoryLedger()
    tools = ToolRegistry()
    tools.register(MockPaymentService())
    engine = ExecutionEngine(ledger=ledger, tool_registry=tools)

    agent = IdempotentPaymentAgent()
    result = engine.run(timeout_after_scenario, agent, run_id="run_exec_success_check")
    tampered = result.to_report_dict()

    # Tamper: verdict is PASS but execution_success is False
    tampered["outcome"]["execution_success"] = False
    with pytest.raises(ReportValidationError) as exc:
        ReportValidator.validate_dict(tampered)
    assert "Outcome 'execution_success' must be True when verdict is 'PASS'" in str(exc.value)


def test_semantic_validation_recovery_success_consistency(timeout_after_scenario: ScenarioModel) -> None:
    """Verify semantic check: recovery_success cannot be True if verdict is not PASS."""
    ledger = InMemoryLedger()
    tools = ToolRegistry()
    tools.register(MockPaymentService())
    engine = ExecutionEngine(ledger=ledger, tool_registry=tools)

    agent = NaivePaymentAgent()
    result = engine.run(timeout_after_scenario, agent, run_id="run_recov_check")
    tampered = result.to_report_dict()

    # Tamper: verdict is GENUINE_FAILURE but recovery_success set to True
    tampered["outcome"]["recovery_success"] = True
    with pytest.raises(ReportValidationError) as exc:
        ReportValidator.validate_dict(tampered)
    assert "conflicts with evaluation.passed" in str(exc.value) or "cannot be True when verdict is" in str(exc.value)


def test_semantic_validation_mutual_exclusion_validation_and_runtime_failure(
    timeout_after_scenario: ScenarioModel,
) -> None:
    """Verify semantic check: validation_failure and runtime_failure cannot both be True."""
    ledger = InMemoryLedger()
    tools = ToolRegistry()
    tools.register(MockPaymentService())
    engine = ExecutionEngine(ledger=ledger, tool_registry=tools)

    agent = IdempotentPaymentAgent()
    result = engine.run(timeout_after_scenario, agent, run_id="run_mut_excl")
    tampered = result.to_report_dict()

    tampered["outcome"]["verdict"] = "INFRASTRUCTURE_ERROR"
    tampered["evaluation"]["verdict"] = "INFRASTRUCTURE_ERROR"
    tampered["outcome"]["execution_success"] = False
    tampered["outcome"]["recovery_success"] = False
    tampered["evaluation"]["passed"] = False
    tampered["outcome"]["validation_failure"] = True
    tampered["outcome"]["runtime_failure"] = True

    with pytest.raises(ReportValidationError) as exc:
        ReportValidator.validate_dict(tampered)
    assert "cannot both be True" in str(exc.value)


def test_semantic_validation_fault_injection_count_agreement(timeout_after_scenario: ScenarioModel) -> None:
    """Verify semantic check: injected_fault_count must equal len(injected_faults)."""
    ledger = InMemoryLedger()
    tools = ToolRegistry()
    tools.register(MockPaymentService())
    engine = ExecutionEngine(ledger=ledger, tool_registry=tools)

    agent = IdempotentPaymentAgent()
    result = engine.run(timeout_after_scenario, agent, run_id="run_fault_count_check")
    tampered = result.to_report_dict()

    # Tamper with count
    tampered["fault_injection"]["injected_fault_count"] = 999
    with pytest.raises(ReportValidationError) as exc:
        ReportValidator.validate_dict(tampered)
    assert "injected_fault_count (999) conflicts with count of injected_faults" in str(exc.value)


def test_semantic_validation_event_count_metadata_agreement(timeout_after_scenario: ScenarioModel) -> None:
    """Verify semantic check: metadata.total_events must equal len(events)."""
    ledger = InMemoryLedger()
    tools = ToolRegistry()
    tools.register(MockPaymentService())
    engine = ExecutionEngine(ledger=ledger, tool_registry=tools)

    agent = IdempotentPaymentAgent()
    result = engine.run(timeout_after_scenario, agent, run_id="run_event_count_check")
    tampered = result.to_report_dict()

    tampered["metadata"]["total_events"] = 999
    with pytest.raises(ReportValidationError) as exc:
        ReportValidator.validate_dict(tampered)
    assert "metadata.total_events (999) conflicts with event count" in str(exc.value)


# =============================================================================
# 4. REPORT SCHEMA VERSIONING AND DETERMINISTIC SERIALIZATION
# =============================================================================


def test_report_schema_version_validation_missing_version(timeout_after_scenario: ScenarioModel) -> None:
    """Verify that missing report_schema_version fails closed."""
    ledger = InMemoryLedger()
    tools = ToolRegistry()
    tools.register(MockPaymentService())
    engine = ExecutionEngine(ledger=ledger, tool_registry=tools)

    agent = IdempotentPaymentAgent()
    result = engine.run(timeout_after_scenario, agent, run_id="run_schema_ver_missing")
    report_dict = result.to_report_dict()

    del report_dict["report_schema_version"]
    with pytest.raises(ReportValidationError) as exc_info:
        ReportValidator.validate_dict(report_dict)

    err_text = str(exc_info.value)
    assert "report_schema_version" in err_text


def test_report_schema_version_validation_invalid_types(timeout_after_scenario: ScenarioModel) -> None:
    """Verify that non-integer types for report_schema_version fail closed."""
    ledger = InMemoryLedger()
    tools = ToolRegistry()
    tools.register(MockPaymentService())
    engine = ExecutionEngine(ledger=ledger, tool_registry=tools)

    agent = IdempotentPaymentAgent()
    result = engine.run(timeout_after_scenario, agent, run_id="run_schema_ver_types")
    report_dict = result.to_report_dict()

    for invalid_val in ["1", True, False, 1.0, [1], {"ver": 1}]:
        bad_dict = dict(report_dict)
        bad_dict["report_schema_version"] = invalid_val

        with pytest.raises(ReportValidationError) as exc_info:
            ReportValidator.validate_dict(bad_dict)

        err_text = str(exc_info.value)
        assert "report_schema_version" in err_text


def test_report_schema_version_unsupported_versions(timeout_after_scenario: ScenarioModel) -> None:
    """Verify that unsupported integer versions fail closed citing supported versions."""
    assert SUPPORTED_REPORT_SCHEMA_VERSIONS == frozenset({1})
    ledger = InMemoryLedger()
    tools = ToolRegistry()
    tools.register(MockPaymentService())
    engine = ExecutionEngine(ledger=ledger, tool_registry=tools)

    agent = IdempotentPaymentAgent()
    result = engine.run(timeout_after_scenario, agent, run_id="run_unsupported_vers")
    report_dict = result.to_report_dict()

    for unsupported_ver in [0, 2, -1, 99]:
        bad_dict = dict(report_dict)
        bad_dict["report_schema_version"] = unsupported_ver

        with pytest.raises(ReportValidationError) as exc_info:
            ReportValidator.validate_dict(bad_dict)

        err_text = str(exc_info.value)
        assert f"Unsupported report_schema_version: {unsupported_ver}" in err_text
        assert "Supported versions: [1]" in err_text


def test_deterministic_serialization(timeout_after_scenario: ScenarioModel) -> None:
    """Verify that repeated JSON serialization yields identical strings and SHA-256 hashes."""
    ledger = InMemoryLedger()
    tools = ToolRegistry()
    tools.register(MockPaymentService())
    engine = ExecutionEngine(ledger=ledger, tool_registry=tools)

    agent = IdempotentPaymentAgent()
    result = engine.run(timeout_after_scenario, agent, run_id="run_deterministic_serial")

    json_1 = JSONReporter.to_json_string(result)
    json_2 = JSONReporter.to_json_string(result)
    json_3 = result.to_json()

    assert json_1 == json_2 == json_3

    sha1 = hashlib.sha256(json_1.encode("utf-8")).hexdigest()
    sha2 = hashlib.sha256(json_2.encode("utf-8")).hexdigest()
    sha3 = hashlib.sha256(json_3.encode("utf-8")).hexdigest()
    assert sha1 == sha2 == sha3

    data = json.loads(json_1)
    keys = list(data.keys())
    assert keys == sorted(keys)


# =============================================================================
# 5. BACKWARD COMPATIBILITY
# =============================================================================


def test_backward_compatibility_fields(timeout_after_scenario: ScenarioModel) -> None:
    """Verify that all 6 legacy report fields and their semantics are strictly preserved."""
    ledger = InMemoryLedger()
    tools = ToolRegistry()
    tools.register(MockPaymentService())
    engine = ExecutionEngine(ledger=ledger, tool_registry=tools)

    agent = IdempotentPaymentAgent()
    result = engine.run(timeout_after_scenario, agent, run_id="run_compat_check")
    data = JSONReporter.generate_report_dict(result)

    # 1. version
    assert data["version"] == "0.1.0"
    # 2. run_id
    assert data["run_id"] == "run_compat_check"
    # 3. scenario
    assert data["scenario"]["id"] == "payment_timeout_after_commit"
    assert data["scenario"]["name"] == timeout_after_scenario.name
    assert data["scenario"]["target_tool"] == "payment_gateway"
    assert data["scenario"]["target_action"] == "charge"
    # 4. evaluation
    assert data["evaluation"]["passed"] is True
    assert data["evaluation"]["verdict"] == "PASS"
    assert data["evaluation"]["actual_side_effects"] == 1
    assert data["evaluation"]["expected_side_effects"] == 1
    assert data["evaluation"]["duplicate_effect_count"] == 0
    assert data["evaluation"]["idempotency_key_stable"] is True
    assert "evidence_summary" in data["evaluation"]
    assert len(data["evaluation"]["assertions"]) >= 4
    # 5. events
    assert isinstance(data["events"], list)
    assert len(data["events"]) >= 2
    ev = data["events"][0]
    assert "event_id" in ev
    assert "operation_id" in ev
    assert "attempt_id" in ev
    assert "attempt_number" in ev
    assert "tool_name" in ev
    assert "action_type" in ev
    assert "execution_status" in ev
    assert "commit_status" in ev
    assert "effect_id" in ev
    assert "idempotency_key_hash" in ev
    assert "request_fingerprint" in ev
    assert "simulated_timestamp" in ev
    assert "error_type" in ev
    assert "evidence_reference" in ev
    # 6. agent_result
    assert data["agent_result"] is not None
    assert data["agent_result"]["status"] == "SUCCEEDED"


# =============================================================================
# 6. STRICT PYDANTIC FIELD TYPES (No Invalid Coercion)
# =============================================================================


def test_strict_types_integer_fields_reject_strings(timeout_after_scenario: ScenarioModel) -> None:
    """Verify that integer fields reject string inputs in strict mode."""
    ledger = InMemoryLedger()
    tools = ToolRegistry()
    tools.register(MockPaymentService())
    engine = ExecutionEngine(ledger=ledger, tool_registry=tools)
    result = engine.run(timeout_after_scenario, IdempotentPaymentAgent())
    base_dict = result.to_report_dict()

    # 1. report_schema_version as string "1"
    tampered_ver = json.loads(json.dumps(base_dict))
    tampered_ver["report_schema_version"] = "1"
    with pytest.raises(ReportValidationError) as exc:
        ReportValidator.validate_dict(tampered_ver)
    assert "report_schema_version must be an integer" in str(exc.value)

    # 2. side_effects.actual_count as string "1"
    tampered_count = json.loads(json.dumps(base_dict))
    tampered_count["side_effects"]["actual_count"] = "1"
    with pytest.raises(ReportValidationError) as exc:
        ReportValidator.validate_dict(tampered_count)
    assert "Input should be a valid integer" in str(exc.value) or "int_type" in str(exc.value)

    # 3. events[0].attempt_number as string "1"
    tampered_event = json.loads(json.dumps(base_dict))
    tampered_event["events"][0]["attempt_number"] = "1"
    with pytest.raises(ReportValidationError) as exc:
        ReportValidator.validate_dict(tampered_event)
    assert "Input should be a valid integer" in str(exc.value) or "int_type" in str(exc.value)


def test_strict_types_integer_fields_reject_booleans(timeout_after_scenario: ScenarioModel) -> None:
    """Verify that integer fields reject boolean inputs (True/False) without coercion."""
    ledger = InMemoryLedger()
    tools = ToolRegistry()
    tools.register(MockPaymentService())
    engine = ExecutionEngine(ledger=ledger, tool_registry=tools)
    result = engine.run(timeout_after_scenario, IdempotentPaymentAgent())
    base_dict = result.to_report_dict()

    # 1. report_schema_version as boolean True
    tampered_ver = json.loads(json.dumps(base_dict))
    tampered_ver["report_schema_version"] = True
    with pytest.raises(ReportValidationError) as exc:
        ReportValidator.validate_dict(tampered_ver)
    assert "report_schema_version must be an integer" in str(exc.value)

    # 2. side_effects.actual_count as boolean False
    tampered_count = json.loads(json.dumps(base_dict))
    tampered_count["side_effects"]["actual_count"] = False
    with pytest.raises(ReportValidationError) as exc:
        ReportValidator.validate_dict(tampered_count)
    assert "Input should be a valid integer" in str(exc.value) or "int_type" in str(exc.value)


def test_strict_types_boolean_fields_reject_integers(timeout_after_scenario: ScenarioModel) -> None:
    """Verify that boolean fields reject integer inputs (1/0) without coercion."""
    ledger = InMemoryLedger()
    tools = ToolRegistry()
    tools.register(MockPaymentService())
    engine = ExecutionEngine(ledger=ledger, tool_registry=tools)
    result = engine.run(timeout_after_scenario, IdempotentPaymentAgent())
    base_dict = result.to_report_dict()

    # 1. outcome.side_effect_safe as integer 1
    tampered_safe = json.loads(json.dumps(base_dict))
    tampered_safe["outcome"]["side_effect_safe"] = 1
    with pytest.raises(ReportValidationError) as exc:
        ReportValidator.validate_dict(tampered_safe)
    assert "Input should be a valid boolean" in str(exc.value) or "bool_type" in str(exc.value)

    # 2. evaluation.passed as integer 0
    tampered_passed = json.loads(json.dumps(base_dict))
    tampered_passed["evaluation"]["passed"] = 0
    with pytest.raises(ReportValidationError) as exc:
        ReportValidator.validate_dict(tampered_passed)
    assert "Input should be a valid boolean" in str(exc.value) or "bool_type" in str(exc.value)


def test_strict_types_string_fields_reject_numbers(timeout_after_scenario: ScenarioModel) -> None:
    """Verify that string fields reject numeric inputs without string coercion."""
    ledger = InMemoryLedger()
    tools = ToolRegistry()
    tools.register(MockPaymentService())
    engine = ExecutionEngine(ledger=ledger, tool_registry=tools)
    result = engine.run(timeout_after_scenario, IdempotentPaymentAgent())
    base_dict = result.to_report_dict()

    # 1. run_id as integer 12345
    tampered_run_id = json.loads(json.dumps(base_dict))
    tampered_run_id["run_id"] = 12345
    with pytest.raises(ReportValidationError) as exc:
        ReportValidator.validate_dict(tampered_run_id)
    assert "Input should be a valid string" in str(exc.value) or "string_type" in str(exc.value)

    # 2. agent_id as integer 999
    tampered_agent_id = json.loads(json.dumps(base_dict))
    tampered_agent_id["agent_id"] = 999
    with pytest.raises(ReportValidationError) as exc:
        ReportValidator.validate_dict(tampered_agent_id)
    assert "Input should be a valid string" in str(exc.value) or "string_type" in str(exc.value)


def test_strict_types_lists_reject_incorrect_containers(timeout_after_scenario: ScenarioModel) -> None:
    """Verify that list fields reject non-list container inputs (strings, dicts, numbers)."""
    ledger = InMemoryLedger()
    tools = ToolRegistry()
    tools.register(MockPaymentService())
    engine = ExecutionEngine(ledger=ledger, tool_registry=tools)
    result = engine.run(timeout_after_scenario, IdempotentPaymentAgent())
    base_dict = result.to_report_dict()

    # 1. events as dict
    tampered_events = json.loads(json.dumps(base_dict))
    tampered_events["events"] = {"event": 1}
    with pytest.raises(ReportValidationError) as exc:
        ReportValidator.validate_dict(tampered_events)
    assert "Input should be a valid list" in str(exc.value) or "list_type" in str(exc.value)

    # 2. errors as string
    tampered_errors = json.loads(json.dumps(base_dict))
    tampered_errors["errors"] = "single_error_string"
    with pytest.raises(ReportValidationError) as exc:
        ReportValidator.validate_dict(tampered_errors)
    assert "Input should be a valid list" in str(exc.value) or "list_type" in str(exc.value)


# =============================================================================
# 7. SIDE-EFFECT CLASSIFICATION PRIORITY & BIDIRECTIONAL SEMANTICS
# =============================================================================


def test_safety_priority_duplicates_take_precedence_over_insufficient_evidence(
    timeout_after_scenario: ScenarioModel,
) -> None:
    """Proven duplicate effects must take precedence over insufficient evidence -> UNSAFE."""
    eval_res = EvaluationResult(
        verdict=EvaluationVerdict.INSUFFICIENT_EVIDENCE,
        passed=False,
        expected_side_effects=1,
        actual_side_effects=2,
        duplicate_effect_count=1,
        idempotency_key_stable=False,
        failure_reasons=["Ambiguous trace"],
    )
    summary = RunSummary(
        run_id="run_dup_insufficient_ev",
        scenario_id=timeout_after_scenario.scenario_id,
        agent_id="test_agent",
        passed=False,
        verdict=EvaluationVerdict.INSUFFICIENT_EVIDENCE,
        total_events=2,
        committed_effects=2,
        duplicate_effects=1,
        created_at="2024-01-01T00:00:00Z",
        duration_ms=1.0,
    )
    from recoverybench.engine.context import RunResult

    run_result = RunResult(
        run_id="run_dup_insufficient_ev",
        scenario=timeout_after_scenario,
        agent_id="test_agent",
        evaluation=eval_res,
        events=[],
        summary=summary,
    )

    safety = classify_side_effect_safety(run_result)
    assert safety == SideEffectSafety.UNSAFE, "Proven duplicate must NOT be classified as UNVERIFIED"

    report_dict = run_result.to_report_dict()
    assert report_dict["outcome"]["side_effect_safety"] == "UNSAFE"
    assert report_dict["outcome"]["side_effect_safe"] is False
    assert report_dict["side_effects"]["safety_status"] == "UNSAFE"
    assert report_dict["side_effects"]["safe"] is False


def test_safety_priority_unexpected_count_with_ledger_evidence_takes_precedence(
    timeout_after_scenario: ScenarioModel,
) -> None:
    """Known unexpected committed effect count with ledger evidence takes precedence over INSUFFICIENT_EVIDENCE -> UNSAFE."""
    from recoverybench.engine.context import RunResult
    from recoverybench.models import CommitStatus, ExecutionStatus, LedgerEvent

    dummy_event = LedgerEvent(
        event_id=1,
        run_id="run_unexp_insufficient_ev",
        simulated_timestamp="2024-01-01T00:00:00Z",
        scenario_id=timeout_after_scenario.scenario_id,
        tool_name="payment_gateway",
        action_type="charge",
        attempt_number=1,
        execution_status=ExecutionStatus.SUCCESS,
        commit_status=CommitStatus.COMMITTED,
        effect_id="eff_001",
        request_fingerprint="fp1",
    )
    eval_res = EvaluationResult(
        verdict=EvaluationVerdict.INSUFFICIENT_EVIDENCE,
        passed=False,
        expected_side_effects=1,
        actual_side_effects=2,
        duplicate_effect_count=0,
        idempotency_key_stable=True,
        failure_reasons=["Incomplete trace"],
    )
    summary = RunSummary(
        run_id="run_unexp_insufficient_ev",
        scenario_id=timeout_after_scenario.scenario_id,
        agent_id="test_agent",
        passed=False,
        verdict=EvaluationVerdict.INSUFFICIENT_EVIDENCE,
        total_events=1,
        committed_effects=2,
        duplicate_effects=0,
        created_at="2024-01-01T00:00:00Z",
        duration_ms=1.0,
    )
    run_result = RunResult(
        run_id="run_unexp_insufficient_ev",
        scenario=timeout_after_scenario,
        agent_id="test_agent",
        evaluation=eval_res,
        events=[dummy_event],
        summary=summary,
    )

    safety = classify_side_effect_safety(run_result)
    assert safety == SideEffectSafety.UNSAFE, "Unexpected committed count with ledger evidence must be UNSAFE"


def test_semantic_validation_rejects_duplicates_with_verified_safe(
    timeout_after_scenario: ScenarioModel,
) -> None:
    """ReportValidator must reject reports containing duplicates marked as VERIFIED_SAFE."""
    ledger = InMemoryLedger()
    tools = ToolRegistry()
    tools.register(MockPaymentService())
    engine = ExecutionEngine(ledger=ledger, tool_registry=tools)
    result = engine.run(timeout_after_scenario, IdempotentPaymentAgent())
    tampered = result.to_report_dict()

    tampered["side_effects"]["duplicate_count"] = 1
    tampered["side_effects"]["safety_status"] = "VERIFIED_SAFE"
    tampered["outcome"]["side_effect_safety"] = "VERIFIED_SAFE"

    with pytest.raises(ReportValidationError) as exc:
        ReportValidator.validate_dict(tampered)
    assert "Contradictory side_effects.safety_status" in str(exc.value)


def test_semantic_validation_rejects_duplicates_with_unverified(
    timeout_after_scenario: ScenarioModel,
) -> None:
    """ReportValidator must reject reports containing duplicates marked as UNVERIFIED."""
    ledger = InMemoryLedger()
    tools = ToolRegistry()
    tools.register(MockPaymentService())
    engine = ExecutionEngine(ledger=ledger, tool_registry=tools)
    result = engine.run(timeout_after_scenario, IdempotentPaymentAgent())
    tampered = result.to_report_dict()

    tampered["side_effects"]["duplicate_count"] = 1
    tampered["side_effects"]["safety_status"] = "UNVERIFIED"
    tampered["outcome"]["side_effect_safety"] = "UNVERIFIED"
    tampered["side_effects"]["safe"] = False
    tampered["outcome"]["side_effect_safe"] = False

    with pytest.raises(ReportValidationError) as exc:
        ReportValidator.validate_dict(tampered)
    assert "Contradictory side_effects.safety_status" in str(exc.value)


def test_semantic_validation_rejects_unexpected_count_with_verified_safe(
    timeout_after_scenario: ScenarioModel,
) -> None:
    """ReportValidator must reject reports with mismatched count marked as VERIFIED_SAFE."""
    ledger = InMemoryLedger()
    tools = ToolRegistry()
    tools.register(MockPaymentService())
    engine = ExecutionEngine(ledger=ledger, tool_registry=tools)
    result = engine.run(timeout_after_scenario, IdempotentPaymentAgent())
    tampered = result.to_report_dict()

    tampered["side_effects"]["actual_count"] = 2
    tampered["evaluation"]["actual_side_effects"] = 2
    tampered["side_effects"]["expected_count"] = 1
    tampered["evaluation"]["expected_side_effects"] = 1
    tampered["side_effects"]["safety_status"] = "VERIFIED_SAFE"
    tampered["outcome"]["side_effect_safety"] = "VERIFIED_SAFE"

    with pytest.raises(ReportValidationError) as exc:
        ReportValidator.validate_dict(tampered)
    assert "Contradictory side_effects.safety_status" in str(exc.value)


def test_semantic_validation_rejects_unexpected_count_with_unverified(
    timeout_after_scenario: ScenarioModel,
) -> None:
    """ReportValidator must reject reports with unexpected count and ledger evidence marked as UNVERIFIED."""
    ledger = InMemoryLedger()
    tools = ToolRegistry()
    tools.register(MockPaymentService())
    engine = ExecutionEngine(ledger=ledger, tool_registry=tools)
    result = engine.run(timeout_after_scenario, IdempotentPaymentAgent())
    tampered = result.to_report_dict()

    tampered["side_effects"]["actual_count"] = 2
    tampered["evaluation"]["actual_side_effects"] = 2
    tampered["side_effects"]["expected_count"] = 1
    tampered["evaluation"]["expected_side_effects"] = 1
    tampered["side_effects"]["safety_status"] = "UNVERIFIED"
    tampered["outcome"]["side_effect_safety"] = "UNVERIFIED"
    tampered["side_effects"]["safe"] = False
    tampered["outcome"]["side_effect_safe"] = False

    with pytest.raises(ReportValidationError) as exc:
        ReportValidator.validate_dict(tampered)
    assert "Contradictory side_effects.safety_status" in str(exc.value)


def test_semantic_validation_rejects_verified_safe_with_insufficient_evidence(
    timeout_after_scenario: ScenarioModel,
) -> None:
    """ReportValidator must reject reports with VERIFIED_SAFE when verdict is INSUFFICIENT_EVIDENCE."""
    ledger = InMemoryLedger()
    tools = ToolRegistry()
    tools.register(MockPaymentService())
    engine = ExecutionEngine(ledger=ledger, tool_registry=tools)
    result = engine.run(timeout_after_scenario, IdempotentPaymentAgent())
    tampered = result.to_report_dict()

    tampered["outcome"]["verdict"] = "INSUFFICIENT_EVIDENCE"
    tampered["evaluation"]["verdict"] = "INSUFFICIENT_EVIDENCE"
    tampered["outcome"]["recovery_success"] = False
    tampered["evaluation"]["passed"] = False
    tampered["side_effects"]["safety_status"] = "VERIFIED_SAFE"
    tampered["outcome"]["side_effect_safety"] = "VERIFIED_SAFE"

    with pytest.raises(ReportValidationError) as exc:
        ReportValidator.validate_dict(tampered)
    assert "Contradictory side_effects.safety_status" in str(exc.value)


def test_semantic_validation_rejects_inconsistent_outcome_and_side_effects_safety(
    timeout_after_scenario: ScenarioModel,
) -> None:
    """ReportValidator must reject reports where outcome.side_effect_safety != side_effects.safety_status."""
    ledger = InMemoryLedger()
    tools = ToolRegistry()
    tools.register(MockPaymentService())
    engine = ExecutionEngine(ledger=ledger, tool_registry=tools)
    result = engine.run(timeout_after_scenario, IdempotentPaymentAgent())
    tampered = result.to_report_dict()

    # Inconsistent: outcome says UNSAFE but side_effects says VERIFIED_SAFE
    tampered["outcome"]["side_effect_safety"] = "UNSAFE"
    tampered["outcome"]["side_effect_safe"] = False

    with pytest.raises(ReportValidationError) as exc:
        ReportValidator.validate_dict(tampered)
    assert "conflicts with side_effects.safety_status" in str(exc.value)


def test_semantic_validation_rejects_inconsistent_safe_booleans(
    timeout_after_scenario: ScenarioModel,
) -> None:
    """ReportValidator must reject reports where safe boolean disagrees with safety_status."""
    ledger = InMemoryLedger()
    tools = ToolRegistry()
    tools.register(MockPaymentService())
    engine = ExecutionEngine(ledger=ledger, tool_registry=tools)
    result = engine.run(timeout_after_scenario, IdempotentPaymentAgent())
    tampered = result.to_report_dict()

    # Tamper safe to True when safety_status is UNSAFE
    tampered["side_effects"]["duplicate_count"] = 1
    tampered["evaluation"]["duplicate_effect_count"] = 1
    tampered["side_effects"]["safety_status"] = "UNSAFE"
    tampered["outcome"]["side_effect_safety"] = "UNSAFE"
    tampered["side_effects"]["safe"] = True  # Contradiction: safe=True with UNSAFE!
    tampered["outcome"]["side_effect_safe"] = True

    with pytest.raises(ReportValidationError) as exc:
        ReportValidator.validate_dict(tampered)
    assert "must be False when safety_status is UNSAFE" in str(exc.value)


# =============================================================================
# 8. LEDGER EVIDENCE & SIDE-EFFECT CONSISTENCY CHECKS
# =============================================================================


def test_semantic_validation_rejects_actual_count_with_no_committed_events(
    timeout_after_scenario: ScenarioModel,
) -> None:
    """ReportValidator must reject reports claiming actual_count > 0 when no ledger events are COMMITTED."""
    ledger = InMemoryLedger()
    tools = ToolRegistry()
    tools.register(MockPaymentService())
    engine = ExecutionEngine(ledger=ledger, tool_registry=tools)
    result = engine.run(timeout_after_scenario, IdempotentPaymentAgent())
    tampered = result.to_report_dict()

    # Change all events to ABORTED
    for ev in tampered["events"]:
        ev["commit_status"] = "ABORTED"
        ev["effect_id"] = None

    # Report claims actual_count = 1
    tampered["side_effects"]["actual_count"] = 1
    tampered["evaluation"]["actual_side_effects"] = 1
    tampered["side_effects"]["committed_effect_ids"] = []

    with pytest.raises(ReportValidationError) as exc:
        ReportValidator.validate_dict(tampered)
    err_text = str(exc.value)
    assert (
        "no ledger events have commit_status == 'COMMITTED'" in err_text
        or "conflicts with count of unique committed effects" in err_text
    )


def test_semantic_validation_rejects_committed_ids_mismatch_with_ledger(
    timeout_after_scenario: ScenarioModel,
) -> None:
    """ReportValidator must reject reports where committed_effect_ids do not match ledger event records."""
    ledger = InMemoryLedger()
    tools = ToolRegistry()
    tools.register(MockPaymentService())
    engine = ExecutionEngine(ledger=ledger, tool_registry=tools)
    result = engine.run(timeout_after_scenario, IdempotentPaymentAgent())
    tampered = result.to_report_dict()

    # Tamper committed_effect_ids with a fabricated ID not in any event
    tampered["side_effects"]["committed_effect_ids"] = ["eff_fabricated_fraudulent_id"]

    with pytest.raises(ReportValidationError) as exc:
        ReportValidator.validate_dict(tampered)
    assert "side_effects.committed_effect_ids" in str(exc.value)
    assert "does not match committed effect IDs derived from COMMITTED ledger events" in str(exc.value)


def test_semantic_validation_rejects_hidden_duplicates_manipulating_duplicate_count(
    timeout_after_scenario: ScenarioModel,
) -> None:
    """ReportValidator must reject reports attempting to hide duplicates by setting duplicate_count to 0."""
    ledger = InMemoryLedger()
    tools = ToolRegistry()
    tools.register(MockPaymentService())
    engine = ExecutionEngine(ledger=ledger, tool_registry=tools)

    # Naive agent triggers a duplicate commit (Double Charge) under the same operation
    result = engine.run(timeout_after_scenario, NaivePaymentAgent())
    tampered = result.to_report_dict()

    # Verify that Naive agent run produced duplicate events in the ledger
    assert tampered["side_effects"]["duplicate_count"] >= 1

    # Adversary attempts to hide the duplicate by zeroing out duplicate_count
    tampered["side_effects"]["duplicate_count"] = 0
    tampered["evaluation"]["duplicate_effect_count"] = 0

    with pytest.raises(ReportValidationError) as exc:
        ReportValidator.validate_dict(tampered)
    assert "conflicts with duplicate count" in str(exc.value)


def test_semantic_validation_rejects_non_committed_event_counted_as_committed(
    timeout_after_scenario: ScenarioModel,
) -> None:
    """ReportValidator must reject reports where a non-committed event (e.g. NOT_APPLICABLE) is counted as a committed effect."""
    ledger = InMemoryLedger()
    tools = ToolRegistry()
    tools.register(MockPaymentService())
    engine = ExecutionEngine(ledger=ledger, tool_registry=tools)
    result = engine.run(timeout_after_scenario, IdempotentPaymentAgent())
    tampered = result.to_report_dict()

    # Set all events to NOT_APPLICABLE with an uncommitted effect ID
    for ev in tampered["events"]:
        ev["commit_status"] = "NOT_APPLICABLE"
        ev["effect_id"] = "eff_uncommitted_receipt"

    # Falsely claim that the uncommitted receipt was committed
    tampered["side_effects"]["actual_count"] = 1
    tampered["evaluation"]["actual_side_effects"] = 1
    tampered["side_effects"]["committed_effect_ids"] = ["eff_uncommitted_receipt"]

    with pytest.raises(ReportValidationError) as exc:
        ReportValidator.validate_dict(tampered)
    err_text = str(exc.value)
    assert (
        "cannot be counted as a committed effect" in err_text
        or "no ledger events have commit_status == 'COMMITTED'" in err_text
    )


def test_semantic_validation_accepts_consistent_event_evidence_in_legitimate_run(
    timeout_after_scenario: ScenarioModel,
) -> None:
    """ReportValidator must accept legitimate runs where ledger events and side-effects summary are fully consistent."""
    ledger = InMemoryLedger()
    tools = ToolRegistry()
    tools.register(MockPaymentService())
    engine = ExecutionEngine(ledger=ledger, tool_registry=tools)
    result = engine.run(timeout_after_scenario, IdempotentPaymentAgent())

    # Generate report dictionary and validate
    report_dict = result.to_report_dict()
    model = ReportValidator.validate_dict(report_dict)

    # Verify model and ledger agreement
    assert model.side_effects.actual_count == 1
    assert model.side_effects.duplicate_count == 0
    assert model.side_effects.safety_status == SideEffectSafety.VERIFIED_SAFE
    assert model.side_effects.safe is True
    assert len(model.side_effects.committed_effect_ids) == 1
    assert model.metadata.total_events == len(model.events)


# =============================================================================
# 9. LEDGER INTEGRITY & IDENTITY ADVERSARIAL HARDENING
# =============================================================================


def test_event_report_rejects_committed_event_with_none_effect_id(
    timeout_after_scenario: ScenarioModel,
) -> None:
    """ReportValidator must reject reports containing a COMMITTED event with effect_id=None."""
    ledger = InMemoryLedger()
    tools = ToolRegistry()
    tools.register(MockPaymentService())
    engine = ExecutionEngine(ledger=ledger, tool_registry=tools)
    result = engine.run(timeout_after_scenario, IdempotentPaymentAgent())
    tampered = result.to_report_dict()

    # Find the committed event and set effect_id = None
    committed_found = False
    for ev in tampered["events"]:
        if ev["commit_status"] == "COMMITTED":
            ev["effect_id"] = None
            committed_found = True
    assert committed_found

    with pytest.raises(ReportValidationError) as exc:
        ReportValidator.validate_dict(tampered)
    assert "A COMMITTED event must have a non-empty effect_id" in str(exc.value)


def test_event_report_rejects_committed_event_with_whitespace_effect_id(
    timeout_after_scenario: ScenarioModel,
) -> None:
    """ReportValidator must reject reports containing a COMMITTED event with empty or whitespace-only effect_id."""
    ledger = InMemoryLedger()
    tools = ToolRegistry()
    tools.register(MockPaymentService())
    engine = ExecutionEngine(ledger=ledger, tool_registry=tools)
    result = engine.run(timeout_after_scenario, IdempotentPaymentAgent())
    tampered = result.to_report_dict()

    for ws_val in ["", "   ", "\t\n"]:
        bad_dict = json.loads(json.dumps(tampered))
        for ev in bad_dict["events"]:
            if ev["commit_status"] == "COMMITTED":
                ev["effect_id"] = ws_val

        with pytest.raises(ReportValidationError) as exc:
            ReportValidator.validate_dict(bad_dict)
        assert (
            "Field 'effect_id' cannot be empty or whitespace-only" in str(exc.value)
            or "A COMMITTED event must have a non-empty effect_id" in str(exc.value)
        )


def test_event_report_rejects_committed_event_missing_or_whitespace_operation_id_when_duplicate_verification_required(
    timeout_after_scenario: ScenarioModel,
) -> None:
    """ReportValidator must reject reports where a COMMITTED event lacks operation_id when duplicate verification requires it."""
    ledger = InMemoryLedger()
    tools = ToolRegistry()
    tools.register(MockPaymentService())
    engine = ExecutionEngine(ledger=ledger, tool_registry=tools)
    result = engine.run(timeout_after_scenario, IdempotentPaymentAgent())
    tampered = result.to_report_dict()

    # Case A: whitespace-only operation_id fails on identifier validation
    ws_tampered = json.loads(json.dumps(tampered))
    for ev in ws_tampered["events"]:
        if ev["commit_status"] == "COMMITTED":
            ev["operation_id"] = "   "
    with pytest.raises(ReportValidationError) as exc_ws:
        ReportValidator.validate_dict(ws_tampered)
    assert "Field 'operation_id' cannot be empty or whitespace-only" in str(exc_ws.value)

    # Case B: missing (None) operation_id when duplicate verification is required (e.g. passing run / retries)
    none_tampered = json.loads(json.dumps(tampered))
    for ev in none_tampered["events"]:
        if ev["commit_status"] == "COMMITTED":
            ev["operation_id"] = None
    with pytest.raises(ReportValidationError) as exc_none:
        ReportValidator.validate_dict(none_tampered)
    assert (
        "missing or empty operation_id when duplicate verification requires it" in str(exc_none.value)
        or "Missing identity evidence on COMMITTED event prevents VERIFIED_SAFE" in str(exc_none.value)
    )


def test_event_report_rejects_unknown_commit_status_value(
    timeout_after_scenario: ScenarioModel,
) -> None:
    """ReportValidator must reject events with unknown commit_status values outside the CommitStatus vocabulary."""
    ledger = InMemoryLedger()
    tools = ToolRegistry()
    tools.register(MockPaymentService())
    engine = ExecutionEngine(ledger=ledger, tool_registry=tools)
    result = engine.run(timeout_after_scenario, IdempotentPaymentAgent())
    tampered = result.to_report_dict()

    for invalid_status in ["UNKNOWN", "UNCOMMITTED", "PENDING", "ROLLED_BACK", ""]:
        bad_dict = json.loads(json.dumps(tampered))
        bad_dict["events"][0]["commit_status"] = invalid_status

        with pytest.raises(ReportValidationError) as exc:
            ReportValidator.validate_dict(bad_dict)
        assert "Invalid commit_status" in str(exc.value)
        assert "['ABORTED', 'COMMITTED', 'NOT_APPLICABLE']" in str(exc.value)


def test_report_rejects_verified_safe_with_incomplete_committed_event_identity(
    timeout_after_scenario: ScenarioModel,
) -> None:
    """ReportValidator must reject any attempt to claim VERIFIED_SAFE when committed-event identity evidence is incomplete."""
    ledger = InMemoryLedger()
    tools = ToolRegistry()
    tools.register(MockPaymentService())
    engine = ExecutionEngine(ledger=ledger, tool_registry=tools)
    result = engine.run(timeout_after_scenario, IdempotentPaymentAgent())
    tampered = result.to_report_dict()

    # Omit operation_id on committed event, while asserting VERIFIED_SAFE
    for ev in tampered["events"]:
        if ev["commit_status"] == "COMMITTED":
            ev["operation_id"] = None

    tampered["outcome"]["side_effect_safety"] = "VERIFIED_SAFE"
    tampered["side_effects"]["safety_status"] = "VERIFIED_SAFE"
    tampered["outcome"]["side_effect_safe"] = True
    tampered["side_effects"]["safe"] = True

    with pytest.raises(ReportValidationError) as exc:
        ReportValidator.validate_dict(tampered)
    err_text = str(exc.value)
    assert (
        "Missing identity evidence on COMMITTED event prevents VERIFIED_SAFE" in err_text
        or "Contradictory side_effects.safety_status" in err_text
    )


def test_valid_behavior_committed_followed_by_not_applicable_retry(
    timeout_after_scenario: ScenarioModel,
) -> None:
    """Preserve valid behavior: a COMMITTED event followed by a NOT_APPLICABLE idempotent retry using same effect ID."""
    ledger = InMemoryLedger()
    tools = ToolRegistry()
    tools.register(MockPaymentService())
    engine = ExecutionEngine(ledger=ledger, tool_registry=tools)

    # Idempotent agent executes: attempt 1 COMMITTED (timeout after commit), attempt 2 NOT_APPLICABLE (cached replay)
    result = engine.run(timeout_after_scenario, IdempotentPaymentAgent())
    report_dict = result.to_report_dict()

    # Verify event structure
    statuses = [e["commit_status"] for e in report_dict["events"]]
    assert statuses == ["COMMITTED", "NOT_APPLICABLE"]
    assert report_dict["events"][0]["effect_id"] == report_dict["events"][1]["effect_id"]

    # Validate report model
    model = ReportValidator.validate_dict(report_dict)
    assert model.side_effects.safety_status == SideEffectSafety.VERIFIED_SAFE
    assert model.side_effects.safe is True
    assert model.side_effects.actual_count == 1
    assert model.side_effects.duplicate_count == 0
    assert model.outcome.verdict == "PASS"
    assert model.outcome.recovery_success is True


def test_valid_behavior_legitimate_successful_idempotent_recovery(
    timeout_after_scenario: ScenarioModel,
) -> None:
    """Preserve valid behavior: legitimate idempotent agent completes recovery and produces fully valid report."""
    ledger = InMemoryLedger()
    tools = ToolRegistry()
    tools.register(MockPaymentService())
    engine = ExecutionEngine(ledger=ledger, tool_registry=tools)
    result = engine.run(timeout_after_scenario, IdempotentPaymentAgent())

    model = result.to_report()
    assert model.outcome.execution_success is True
    assert model.outcome.recovery_success is True
    assert model.outcome.verdict == "PASS"
    assert model.outcome.side_effect_safety == SideEffectSafety.VERIFIED_SAFE
    assert model.outcome.side_effect_safe is True
    assert model.side_effects.actual_count == 1
    assert model.side_effects.duplicate_count == 0


def test_valid_behavior_legitimate_duplicate_commit_failure(
    timeout_after_scenario: ScenarioModel,
) -> None:
    """Preserve valid behavior: naive agent produces double charge which is accurately reported as UNSAFE failure."""
    ledger = InMemoryLedger()
    tools = ToolRegistry()
    tools.register(MockPaymentService())
    engine = ExecutionEngine(ledger=ledger, tool_registry=tools)
    result = engine.run(timeout_after_scenario, NaivePaymentAgent())

    model = result.to_report()
    assert model.outcome.execution_success is True
    assert model.outcome.recovery_success is False
    assert model.outcome.verdict == "GENUINE_FAILURE"
    assert model.outcome.side_effect_safety == SideEffectSafety.UNSAFE
    assert model.outcome.side_effect_safe is False
    assert model.side_effects.actual_count == 2
    assert model.side_effects.duplicate_count == 1
    assert len(model.side_effects.committed_effect_ids) == 2


# =============================================================================
# 11. INJECTED FAULT REPORT STRICT COMMIT STATUS VALIDATION (Finding 1)
# =============================================================================


def test_injected_fault_report_accepts_valid_commit_statuses() -> None:
    """InjectedFaultReport accepts all valid commit statuses as str and CommitStatus enum."""
    for valid_status in ["COMMITTED", "ABORTED", "NOT_APPLICABLE"]:
        report = InjectedFaultReport(
            attempt_number=1,
            tool_name="payment_service",
            action_type="charge",
            commit_status=valid_status,
            timestamp="2026-01-01T00:00:00Z",
        )
        assert report.commit_status == valid_status

    for enum_status in [CommitStatus.COMMITTED, CommitStatus.ABORTED, CommitStatus.NOT_APPLICABLE]:
        report = InjectedFaultReport(
            attempt_number=1,
            tool_name="payment_service",
            action_type="charge",
            commit_status=enum_status,
            timestamp="2026-01-01T00:00:00Z",
        )
        assert report.commit_status == enum_status.value


def test_injected_fault_report_rejects_unknown_commit_status() -> None:
    """InjectedFaultReport rejects unknown commit status values outside the vocabulary."""
    for unknown_status in ["UNKNOWN", "PENDING", "ROLLED_BACK", "UNCOMMITTED", "INVALID", ""]:
        with pytest.raises(ValueError) as exc:
            InjectedFaultReport(
                attempt_number=1,
                tool_name="payment_service",
                action_type="charge",
                commit_status=unknown_status,
                timestamp="2026-01-01T00:00:00Z",
            )
        assert "Invalid commit_status" in str(exc.value)
        assert "['ABORTED', 'COMMITTED', 'NOT_APPLICABLE']" in str(exc.value)


def test_injected_fault_report_rejects_invalid_commit_status_types() -> None:
    """InjectedFaultReport rejects invalid types for commit_status (int, bool, list, dict, None)."""
    for invalid_val in [123, True, False, None, ["COMMITTED"], {"status": "COMMITTED"}]:
        with pytest.raises(ValueError) as exc:
            InjectedFaultReport(
                attempt_number=1,
                tool_name="payment_service",
                action_type="charge",
                commit_status=invalid_val,  # type: ignore[arg-type]
                timestamp="2026-01-01T00:00:00Z",
            )
        assert "Invalid commit_status" in str(exc.value) or "Input should be a valid string" in str(exc.value)


def test_report_validator_rejects_injected_fault_with_unknown_commit_status(
    timeout_after_scenario: ScenarioModel,
) -> None:
    """ReportValidator rejects execution reports containing injected faults with unknown commit_status."""
    ledger = InMemoryLedger()
    tools = ToolRegistry()
    tools.register(MockPaymentService())
    engine = ExecutionEngine(ledger=ledger, tool_registry=tools)
    result = engine.run(timeout_after_scenario, IdempotentPaymentAgent())
    tampered = result.to_report_dict()

    assert len(tampered["fault_injection"]["injected_faults"]) > 0

    for bad_status in ["UNKNOWN", "PENDING", "ROLLED_BACK"]:
        bad_dict = json.loads(json.dumps(tampered))
        bad_dict["fault_injection"]["injected_faults"][0]["commit_status"] = bad_status

        with pytest.raises(ReportValidationError) as exc:
            ReportValidator.validate_dict(bad_dict)
        assert "Invalid commit_status" in str(exc.value)
        assert "['ABORTED', 'COMMITTED', 'NOT_APPLICABLE']" in str(exc.value)


# =============================================================================
# 12. COMPLETE DUPLICATE VERIFICATION EVIDENCE (Finding 2)
# =============================================================================


def test_adversarial_multiple_committed_events_missing_all_operation_ids(
    timeout_after_scenario: ScenarioModel,
) -> None:
    """Adversarial check: Multiple COMMITTED events missing operation_id cannot be silently excluded to claim 0 duplicates."""
    ledger = InMemoryLedger()
    tools = ToolRegistry()
    tools.register(MockPaymentService())
    engine = ExecutionEngine(ledger=ledger, tool_registry=tools)
    result = engine.run(timeout_after_scenario, NaivePaymentAgent())
    tampered = result.to_report_dict()

    # Naive agent creates 2 committed events. Strip operation_id from both!
    for ev in tampered["events"]:
        if ev["commit_status"] == "COMMITTED":
            ev["operation_id"] = None

    # Adversarial tampering: claim 0 duplicates and unverified safety
    tampered["side_effects"]["duplicate_count"] = 0
    tampered["evaluation"]["duplicate_effect_count"] = 0
    tampered["side_effects"]["safety_status"] = "UNVERIFIED"
    tampered["side_effects"]["safe"] = False
    tampered["outcome"]["side_effect_safety"] = "UNVERIFIED"
    tampered["outcome"]["side_effect_safe"] = False

    with pytest.raises(ReportValidationError) as exc:
        ReportValidator.validate_dict(tampered)

    err_str = str(exc.value)
    assert (
        "Cannot establish complete duplicate verification evidence" in err_str
        or "has missing or empty operation_id when duplicate verification requires it" in err_str
    )
    assert "Committed events must not be silently excluded from duplicate calculations." in err_str


def test_adversarial_multiple_committed_events_partially_missing_operation_ids(
    timeout_after_scenario: ScenarioModel,
) -> None:
    """Adversarial check: If some committed events have operation_id and others do not, duplicate verification fails closed."""
    ledger = InMemoryLedger()
    tools = ToolRegistry()
    tools.register(MockPaymentService())
    engine = ExecutionEngine(ledger=ledger, tool_registry=tools)
    result = engine.run(timeout_after_scenario, NaivePaymentAgent())
    tampered = result.to_report_dict()

    # Keep operation_id on event 1, strip from event 2
    committed = [e for e in tampered["events"] if e["commit_status"] == "COMMITTED"]
    assert len(committed) == 2
    committed[0]["operation_id"] = "op_first_charge"
    committed[1]["operation_id"] = None

    tampered["side_effects"]["duplicate_count"] = 0
    tampered["evaluation"]["duplicate_effect_count"] = 0
    tampered["side_effects"]["safety_status"] = "UNVERIFIED"
    tampered["side_effects"]["safe"] = False
    tampered["outcome"]["side_effect_safety"] = "UNVERIFIED"
    tampered["outcome"]["side_effect_safe"] = False

    with pytest.raises(ReportValidationError) as exc:
        ReportValidator.validate_dict(tampered)

    err_str = str(exc.value)
    assert "Cannot establish complete duplicate verification evidence" in err_str
    assert "Committed events must not be silently excluded from duplicate calculations." in err_str


def test_valid_multiple_committed_events_distinct_operation_ids(
    timeout_after_scenario: ScenarioModel,
) -> None:
    """Preserve valid behavior: Multiple distinct operations commit independently with 0 duplicates -> VERIFIED_SAFE."""
    ledger = InMemoryLedger()
    tools = ToolRegistry()
    tools.register(MockPaymentService())
    engine = ExecutionEngine(ledger=ledger, tool_registry=tools)
    result = engine.run(timeout_after_scenario, NaivePaymentAgent())
    report_dict = result.to_report_dict()

    # Update scenario to multi-operation with 2 expected effects
    report_dict["scenario"]["is_single_operation"] = False
    report_dict["scenario"]["id"] = "multi_order_scenario"
    report_dict["scenario"]["name"] = "Multi-order Processing"

    # Give each committed event its own distinct operation_id and unique effect_id
    committed_count = 0
    committed_ids = []
    for ev in report_dict["events"]:
        if ev["commit_status"] == "COMMITTED":
            committed_count += 1
            op_id = f"op_order_{committed_count}"
            eff_id = f"eff_order_{committed_count}"
            ev["operation_id"] = op_id
            ev["effect_id"] = eff_id
            committed_ids.append(eff_id)

    assert committed_count == 2

    # Set up valid multi-operation pass report
    report_dict["side_effects"]["expected_count"] = 2
    report_dict["side_effects"]["actual_count"] = 2
    report_dict["side_effects"]["duplicate_count"] = 0
    report_dict["side_effects"]["safety_status"] = "VERIFIED_SAFE"
    report_dict["side_effects"]["safe"] = True
    report_dict["side_effects"]["committed_effect_ids"] = committed_ids

    report_dict["outcome"]["verdict"] = "PASS"
    report_dict["outcome"]["execution_success"] = True
    report_dict["outcome"]["recovery_success"] = True
    report_dict["outcome"]["side_effect_safety"] = "VERIFIED_SAFE"
    report_dict["outcome"]["side_effect_safe"] = True

    report_dict["evaluation"]["verdict"] = "PASS"
    report_dict["evaluation"]["passed"] = True
    report_dict["evaluation"]["expected_side_effects"] = 2
    report_dict["evaluation"]["actual_side_effects"] = 2
    report_dict["evaluation"]["duplicate_effect_count"] = 0
    report_dict["evaluation"]["failure_reasons"] = []
    for a in report_dict["evaluation"]["assertions"]:
        a["passed"] = True
        if a["name"] == "side_effect_count":
            a["expected"] = 2
            a["actual"] = 2
            a["message"] = "Expected side-effects matched."

    # Validate dict through ReportValidator: must pass cleanly!
    validated_model = ReportValidator.validate_dict(report_dict)
    assert validated_model.outcome.verdict == "PASS"
    assert validated_model.side_effects.duplicate_count == 0
    assert validated_model.side_effects.safety_status == SideEffectSafety.VERIFIED_SAFE
    assert validated_model.side_effects.actual_count == 2
