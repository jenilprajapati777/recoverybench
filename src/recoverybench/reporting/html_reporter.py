"""HTML report generator using Jinja2."""

from pathlib import Path

from jinja2 import Template

from recoverybench.engine.context import RunResult

HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>RecoveryBench Report - {{ result.run_id }}</title>
    <style>
        :root {
            --bg: #0f172a;
            --card-bg: #1e293b;
            --text: #f8fafc;
            --text-muted: #94a3b8;
            --border: #334155;
            --green: #22c55e;
            --red: #ef4444;
            --blue: #3b82f6;
            --yellow: #eab308;
        }
        body {
            font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
            background-color: var(--bg);
            color: var(--text);
            margin: 0;
            padding: 2rem;
            line-height: 1.5;
        }
        .container {
            max-width: 1000px;
            margin: 0 auto;
        }
        .card {
            background-color: var(--card-bg);
            border-radius: 8px;
            border: 1px solid var(--border);
            padding: 1.5rem;
            margin-bottom: 2rem;
        }
        h1, h2, h3 {
            margin-top: 0;
        }
        .header {
            display: flex;
            justify-content: space-between;
            align-items: center;
            border-bottom: 1px solid var(--border);
            padding-bottom: 1rem;
            margin-bottom: 1rem;
        }
        .badge {
            display: inline-block;
            padding: 0.25rem 0.75rem;
            border-radius: 9999px;
            font-weight: bold;
            font-size: 0.875rem;
            text-transform: uppercase;
        }
        .badge-pass { background-color: rgba(34, 197, 94, 0.2); color: var(--green); border: 1px solid var(--green); }
        .badge-fail { background-color: rgba(239, 68, 68, 0.2); color: var(--red); border: 1px solid var(--red); }
        .grid {
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
            gap: 1rem;
            margin-bottom: 1.5rem;
        }
        .metric-box {
            background: rgba(0, 0, 0, 0.2);
            padding: 1rem;
            border-radius: 6px;
            border: 1px solid var(--border);
        }
        .metric-title { font-size: 0.8rem; color: var(--text-muted); text-transform: uppercase; }
        .metric-val { font-size: 1.5rem; font-weight: bold; margin-top: 0.25rem; }
        table {
            width: 100%;
            border-collapse: collapse;
            margin-top: 1rem;
        }
        th, td {
            text-align: left;
            padding: 0.75rem;
            border-bottom: 1px solid var(--border);
            font-size: 0.9rem;
        }
        th { color: var(--text-muted); font-weight: 600; }
        tr:hover { background-color: rgba(255, 255, 255, 0.02); }
        .status-pill {
            font-size: 0.75rem;
            padding: 0.15rem 0.5rem;
            border-radius: 4px;
            font-weight: 600;
        }
        .pill-committed { background: rgba(239, 68, 68, 0.2); color: var(--red); }
        .pill-success { background: rgba(34, 197, 94, 0.2); color: var(--green); }
        .pill-fault { background: rgba(234, 179, 8, 0.2); color: var(--yellow); }
    </style>
</head>
<body>
    <div class="container">
        <div class="header">
            <div>
                <h1>RecoveryBench Test Report</h1>
                <p style="color: var(--text-muted); margin: 0;">Tagline: Test the failure before production does.</p>
            </div>
            <div style="display: flex; gap: 0.5rem; align-items: center;">
                {% if result.isolation_mode == 'isolated' %}
                <span class="badge" style="background: rgba(56, 189, 248, 0.15); color: #38bdf8; border: 1px solid #38bdf8;">ISOLATED (Docker)</span>
                {% else %}
                <span class="badge" style="background: rgba(234, 179, 8, 0.15); color: #eab308; border: 1px solid #eab308;">TRUSTED (NOT ISOLATED)</span>
                {% endif %}
                {% if result.evaluation.passed %}
                <span class="badge badge-pass">PASSED ({{ result.evaluation.verdict.value }})</span>
                {% else %}
                <span class="badge badge-fail">FAILED ({{ result.evaluation.verdict.value }})</span>
                {% endif %}
            </div>
        </div>

        <div class="card">
            <h2>Scenario & Agent Overview</h2>
            <div class="grid">
                <div class="metric-box">
                    <div class="metric-title">Scenario ID</div>
                    <div class="metric-val" style="font-size: 1rem;">{{ result.scenario.scenario_id }}</div>
                </div>
                <div class="metric-box">
                    <div class="metric-title">Agent ID</div>
                    <div class="metric-val" style="font-size: 1rem;">{{ result.agent_id }}</div>
                </div>
                <div class="metric-box">
                    <div class="metric-title">Committed Effects</div>
                    <div class="metric-val" style="color: {% if result.evaluation.duplicate_effect_count > 0 %}var(--red){% else %}var(--green){% endif %};">
                        {{ result.evaluation.actual_side_effects }} ({{ result.evaluation.duplicate_effect_count }} dup)
                    </div>
                </div>
                <div class="metric-box">
                    <div class="metric-title">Execution Time</div>
                    <div class="metric-val">{{ "%.1f"|format(result.evaluation.duration_ms) }} ms</div>
                </div>
            </div>
            <p><strong>Description:</strong> {{ result.scenario.description }}</p>
        </div>

        <div class="card">
            <h2>Deterministic Recovery Assertions</h2>
            <table>
                <thead>
                    <tr>
                        <th>Assertion Name</th>
                        <th>Expected</th>
                        <th>Actual</th>
                        <th>Status</th>
                        <th>Details</th>
                    </tr>
                </thead>
                <tbody>
                    {% for a in result.evaluation.assertions %}
                    <tr>
                        <td><strong>{{ a.name }}</strong></td>
                        <td>{{ a.expected }}</td>
                        <td>{{ a.actual }}</td>
                        <td>
                            {% if a.passed %}
                            <span class="status-pill pill-success">PASS</span>
                            {% else %}
                            <span class="status-pill pill-committed">FAIL</span>
                            {% endif %}
                        </td>
                        <td>
                            <div>{{ a.message }}</div>
                            {% if a.evidence %}
                            <details style="margin-top: 0.5rem; font-size: 0.8rem; color: var(--text-muted);">
                                <summary style="cursor: pointer; color: var(--blue);">View Ledger Evidence ({{ a.evidence|length }})</summary>
                                <ul style="margin: 0.25rem 0 0 1rem; padding: 0;">
                                    {% for ev in a.evidence %}
                                    <li>{{ ev }}</li>
                                    {% endfor %}
                                </ul>
                            </details>
                            {% endif %}
                        </td>
                    </tr>
                    {% endfor %}
                </tbody>
            </table>
        </div>

        <div class="card">
            <h2>Ground-Truth Effect Ledger (Event Sequence)</h2>
            <table>
                <thead>
                    <tr>
                        <th>#</th>
                        <th>Attempt</th>
                        <th>Tool</th>
                        <th>Action</th>
                        <th>Exec Status</th>
                        <th>Commit Status</th>
                        <th>Idempotency Hash</th>
                        <th>Injected Fault / Error</th>
                    </tr>
                </thead>
                <tbody>
                    {% for e in result.events %}
                    <tr>
                        <td>{{ e.event_id }}</td>
                        <td>{{ e.attempt_number }}</td>
                        <td>{{ e.tool_name }}</td>
                        <td>{{ e.action_type }}</td>
                        <td>
                            {% if e.execution_status.value == 'SUCCESS' %}
                            <span class="status-pill pill-success">SUCCESS</span>
                            {% else %}
                            <span class="status-pill pill-fault">{{ e.execution_status.value }}</span>
                            {% endif %}
                        </td>
                        <td>
                            {% if e.commit_status.value == 'COMMITTED' %}
                            <span class="status-pill pill-committed">COMMITTED</span>
                            {% else %}
                            <span style="color: var(--text-muted);">{{ e.commit_status.value }}</span>
                            {% endif %}
                        </td>
                        <td><code>{{ e.idempotency_key_hash or '-' }}</code></td>
                        <td>{{ e.error_type or '-' }}</td>
                    </tr>
                    {% endfor %}
                </tbody>
            </table>
        </div>
    </div>
</body>
</html>
"""


class HTMLReporter:
    """Generates standalone HTML reports for scenario runs."""

    @classmethod
    def render(cls, result: RunResult) -> str:
        """Render the HTML template with run result context."""
        template = Template(HTML_TEMPLATE)
        return str(template.render(result=result))

    @classmethod
    def save_report(cls, result: RunResult, output_path: str | Path) -> Path:
        """Write the HTML report to disk."""
        path = Path(output_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        content = cls.render(result)
        path.write_text(content, encoding="utf-8")
        return path
