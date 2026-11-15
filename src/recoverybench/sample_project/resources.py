"""Sample project resources and scenario definitions."""

SAMPLE_CONFIG_YAML = """# RecoveryBench Project Configuration
version: "0.1.0"
scenarios_dir: "scenarios"
db_path: ".recoverybench/ledger.db"
reports_dir: ".recoverybench/reports"
max_attempts: 5
default_timeout_seconds: 10.0
"""

SAMPLE_TIMEOUT_AFTER_COMMIT_YAML = """# Scenario: Payment Gateway Timeout After Commit
# The critical distributed failure: server commits transaction,
# but network acknowledgement packet is dropped.
scenario_id: payment_timeout_after_commit
name: "Payment Timeout After Commit (Double Charge Hazard)"
description: "Simulates server committing a charge, but client network connection dropping before receiving 200 OK."
target_tool: payment_gateway
target_action: charge

faults:
  - type: timeout_after_commit
    target_tool: payment_gateway
    target_action: charge
    failure_count: 1
    error_message: "Network dropped connection after server committed charge"

task:
  amount: 5000
  currency: USD
  customer_id: "cust_demo_alice_99"

assertions:
  expected_side_effects: 1
  max_duplicate_effects: 0
  require_stable_idempotency_key: true
  max_allowed_retries: 3
  expect_agent_success: true
"""

SAMPLE_TIMEOUT_BEFORE_COMMIT_YAML = """# Scenario: Payment Gateway Timeout Before Commit
# Network fails before the server receives or processes the charge.
scenario_id: payment_timeout_before_commit
name: "Payment Timeout Before Commit"
description: "Simulates socket timeout before any transaction commits on the payment gateway."
target_tool: payment_gateway
target_action: charge

faults:
  - type: timeout_before_commit
    target_tool: payment_gateway
    target_action: charge
    failure_count: 1
    error_message: "Connection timed out before request reached server"

task:
  amount: 3200
  currency: USD
  customer_id: "cust_demo_bob_42"

assertions:
  expected_side_effects: 1
  max_duplicate_effects: 0
  require_stable_idempotency_key: true
  max_allowed_retries: 3
  expect_agent_success: true
"""
