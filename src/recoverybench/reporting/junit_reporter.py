"""JUnit XML report generator for CI/CD test result ingestion."""

import xml.etree.ElementTree as ET
from pathlib import Path
from xml.dom import minidom

from recoverybench.engine.context import RunResult
from recoverybench.security import mask_string_secrets


class JUnitReporter:
    """Serializes test run results and assertion verdicts into standard JUnit XML."""

    @classmethod
    def render(cls, result: RunResult) -> str:
        """Construct JUnit XML document representing scenario assertions and ledger evidence."""
        assertions = result.evaluation.assertions
        total_tests = len(assertions)
        failures = sum(1 for a in assertions if not a.passed)
        duration_sec = result.evaluation.duration_ms / 1000.0

        # Root: <testsuites>
        root = ET.Element(
            "testsuites",
            attrib={
                "name": "RecoveryBench",
                "tests": str(total_tests),
                "failures": str(failures),
                "errors": "0",
                "time": f"{duration_sec:.4f}",
            },
        )

        # Child: <testsuite>
        suite = ET.SubElement(
            root,
            "testsuite",
            attrib={
                "name": result.scenario.scenario_id,
                "tests": str(total_tests),
                "failures": str(failures),
                "errors": "0",
                "time": f"{duration_sec:.4f}",
                "timestamp": result.summary.created_at,
            },
        )

        # <properties>
        props = ET.SubElement(suite, "properties")
        for key, val in [
            ("run_id", result.run_id),
            ("scenario_id", result.scenario.scenario_id),
            ("scenario_name", result.scenario.name),
            ("agent_id", result.agent_id),
            ("verdict", result.evaluation.verdict.value),
            ("expected_side_effects", str(result.evaluation.expected_side_effects)),
            ("actual_side_effects", str(result.evaluation.actual_side_effects)),
            ("duplicate_effects", str(result.evaluation.duplicate_effect_count)),
            ("idempotency_key_stable", str(result.evaluation.idempotency_key_stable)),
            ("duration_ms", f"{result.evaluation.duration_ms:.2f}"),
        ]:
            ET.SubElement(props, "property", attrib={"name": key, "value": val})

        # Testcases
        time_per_test = f"{(duration_sec / max(1, total_tests)):.4f}"
        for a in assertions:
            tc = ET.SubElement(
                suite,
                "testcase",
                attrib={
                    "classname": f"recoverybench.scenarios.{result.scenario.scenario_id}",
                    "name": a.name,
                    "time": time_per_test,
                },
            )

            # System output details
            sysout_lines = [
                f"Rule: {a.name}",
                f"Status: {'PASS' if a.passed else 'FAIL'}",
                f"Expected: {a.expected}",
                f"Actual: {a.actual}",
                f"Message: {a.message}",
            ]
            if a.evidence:
                sysout_lines.append("Supporting Ledger Evidence:")
                for ev in a.evidence:
                    sysout_lines.append(f"  * {ev}")
            if a.event_ids:
                sysout_lines.append(f"Cited Ledger Event IDs: {a.event_ids}")

            if not a.passed:
                fail_elem = ET.SubElement(
                    tc,
                    "failure",
                    attrib={
                        "message": a.message,
                        "type": "AssertionFailure",
                    },
                )
                fail_text_lines = [a.message, "", "Supporting Ledger Evidence:"]
                for ev in a.evidence:
                    fail_text_lines.append(f"  - {ev}")
                fail_elem.text = mask_string_secrets("\n".join(fail_text_lines))

            sysout = ET.SubElement(tc, "system-out")
            sysout.text = mask_string_secrets("\n".join(sysout_lines))

        # Pretty print XML
        raw_xml = ET.tostring(root, encoding="utf-8")
        parsed = minidom.parseString(raw_xml)
        return parsed.toprettyxml(indent="  ", encoding="utf-8").decode("utf-8")

    @classmethod
    def save_report(cls, result: RunResult, output_path: str | Path) -> Path:
        """Write the JUnit XML report to disk."""
        path = Path(output_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        content = cls.render(result)
        path.write_text(content, encoding="utf-8")
        return path
