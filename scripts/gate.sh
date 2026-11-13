#!/usr/bin/env bash
# The repo's full gate: what every pipeline build must pass. Used by the "fleet-gate" runner profile.
set -euo pipefail
cd "$(dirname "$0")/.."
python3 -m unittest tests/test_grounding.py tests/test_ask_grounded.py tests/test_pipeline.py tests/test_policy.py tests/test_runner.py tests/test_runner_security.py \
  tests/test_runner_models.py tests/test_runner_policy.py tests/test_runner_power.py tests/test_evals.py \
  tests/test_runner_budget.py tests/test_runner_handoff.py tests/test_runner_live.py tests/test_runner_engines.py \
  tests/test_runner_stall.py tests/test_real_engine.py tests/test_onboard.py
uv run -q --with fastapi --with uvicorn --with httpx --with 'mcp<2' python -m unittest tests/test_controller.py tests/test_tasks_lineage.py tests/test_console.py tests/test_ask_grounded.py \
  tests/test_budget.py tests/test_limit_handoff.py tests/test_live.py tests/test_mcp_live.py tests/test_stall.py tests/test_dashboard_limits.py
uv run -q --with fastapi --with uvicorn --with httpx --with 'mcp<2' python -m unittest tests/test_leader.py tests/test_orchestrator_hygiene.py
python3 nodes/build.py --check
bash -n nodes/onboard.sh master/setup.sh
echo "gate passed"
