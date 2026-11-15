"""GitHub Actions Step Summary writer for CI/CD pipelines."""

import os
from pathlib import Path

from recoverybench.engine.context import RunResult
from recoverybench.security import mask_string_secrets


def generate_markdown_summary(results: list[RunResult]) -> str:
    """Generate GitHub-flavored Markdown summary for a list of scenario run results."""
    total = len(results)
    passed_count = sum(1 for r in results if r.passed)
    failed_count = total - passed_count
    total_duplicates = sum(r.evaluation.duplicate_effect_count for r in results)

    lines: list[str] = [
        "## 🛡️ RecoveryBench Recovery Test Summary",
        "",
        f"- **Total Scenarios Evaluated:** {total}",
        f"- **Passed:** {'✅ ' + str(passed_count) if passed_count else '0'}",
        f"- **Failed:** {'❌ ' + str(failed_count) if failed_count else '0'}",
        f"- **Duplicate Side-Effects Detected:** {'🚨 ' + str(total_duplicates) if total_duplicates else '0'}",
        "",
        "### 📊 Execution & Ground-Truth Verification Matrix",
        "",
        "| Status | Scenario | Agent | Verdict | Effects (Exp/Act) | Duplicates | Stable Key? | Duration |",
        "| :---: | :--- | :--- | :---: | :---: | :---: | :---: | ---: |",
    ]

    for r in results:
        status_icon = "✅ PASS" if r.passed else "❌ FAIL"
        scenario_name = r.scenario.name
        agent_id = f"`{r.agent_id}`"
        verdict = f"**{r.verdict}**"
        effects = f"{r.evaluation.expected_side_effects} / {r.evaluation.actual_side_effects}"
        duplicates = f"**{r.evaluation.duplicate_effect_count}**" if r.evaluation.duplicate_effect_count else "0"
        key_stable = "Yes" if r.evaluation.idempotency_key_stable else "No"
        duration = f"{r.evaluation.duration_ms:.1f}ms"

        lines.append(
            f"| {status_icon} | {scenario_name} | {agent_id} | {verdict} | {effects} | {duplicates} | {key_stable} | {duration} |"
        )

    # Add failure diagnostic drawers if any failed
    failures = [r for r in results if not r.passed]
    if failures:
        lines.append("")
        lines.append("### 🔍 Failure Evidence & Diagnostic Ledger Citations")
        lines.append("")
        for f in failures:
            lines.append(f"<details><summary><b>❌ {f.scenario.name} ({f.agent_id}): {f.verdict}</b></summary>")
            lines.append("")
            lines.append(f"- **Scenario ID:** `{f.scenario.scenario_id}`")
            lines.append(f"- **Failure Reasons:** {'; '.join(f.evaluation.failure_reasons)}")
            lines.append("- **Supporting Ledger Evidence:**")
            for reason, ev_list in f.evaluation.evidence_summary.items():
                lines.append(f"  - *{reason}:*")
                for ev in ev_list:
                    lines.append(f"    - {ev}")
            lines.append("</details>")
            lines.append("")

    return mask_string_secrets("\n".join(lines))


def append_github_step_summary(results: list[RunResult], filepath: Path | str | None = None) -> bool:
    """Append the Markdown summary to the GITHUB_STEP_SUMMARY file if in a CI environment."""
    target_path = Path(filepath) if filepath else None
    if target_path is None:
        env_summary = os.environ.get("GITHUB_STEP_SUMMARY")
        if env_summary:
            target_path = Path(env_summary)

    if target_path is None:
        return False

    summary_md = generate_markdown_summary(results)
    target_path.parent.mkdir(parents=True, exist_ok=True)
    with target_path.open("a", encoding="utf-8") as f:
        f.write("\n\n" + summary_md + "\n")

    return True
