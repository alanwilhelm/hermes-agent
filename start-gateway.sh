#!/bin/bash
#
# Launch the Hermes messaging gateway from this checkout.
#
# What this does:
# 1. Finds the repo root from the script location.
# 2. Picks a virtualenv in this order:
#    - $HERMES_VENV if you set one explicitly
#    - ./.venv
#    - ./venv
# 3. Activates that virtualenv.
# 4. Runs `python -m hermes_cli.main gateway run` from this branch.
#
# Examples:
#   ./start-gateway.sh
#   ./start-gateway.sh -v
#   ./start-gateway.sh --replace

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Allow an explicit override, but default to the repo-local virtualenv.
if [ -n "${HERMES_VENV:-}" ]; then
    VENV_DIR="$HERMES_VENV"
elif [ -d "$SCRIPT_DIR/.venv" ]; then
    VENV_DIR="$SCRIPT_DIR/.venv"
elif [ -d "$SCRIPT_DIR/venv" ]; then
    VENV_DIR="$SCRIPT_DIR/venv"
else
    echo "No virtualenv found. Expected $SCRIPT_DIR/.venv or $SCRIPT_DIR/venv." >&2
    echo "Run ./setup-hermes.sh first." >&2
    exit 1
fi

# Run from the repo root so gateway startup uses this checkout consistently.
cd "$SCRIPT_DIR"
source "$VENV_DIR/bin/activate"

# Pass through any extra gateway-run args unchanged.
exec python -m hermes_cli.main gateway run "$@"
