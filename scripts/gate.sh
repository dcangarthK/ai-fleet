#!/usr/bin/env bash
# The repo's full gate: what every pipeline build must pass. Used by the "fleet-gate" runner profile.
set -euo pipefail
cd "$(dirname "$0")/.."
python3 -m unittest tests/test_pipeline.py tests/test_policy.py tests/test_runner.py tests/test_runner_security.py \
  tests/test_runner_models.py tests/test_runner_policy.py tests/test_runner_power.py tests/test_evals.py
uv run -q --with fastapi --with uvicorn --with httpx python -m unittest tests/test_controller.py tests/test_console.py
python3 nodes/build.py --check
bash -n nodes/onboard.sh master/setup.sh
echo "gate passed"
