#!/bin/bash
# Stand-in for tools.python_executable in scenario 07: same CLI contract, simulated vendor.
exec /home/yu/.venv/mydev/bin/python /tmp/mctx_verify/sim_cli.py "$@"
