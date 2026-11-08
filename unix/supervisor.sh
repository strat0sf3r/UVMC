#!/usr/bin/env bash
# Starts UVMC and opens your own supervisor (agent loop with your rules and logs).
multipass start uvmc || { echo "UVMC is not installed. Run unix/install.sh first."; exit 1; }
exec multipass exec uvmc -- uvmc "$@"
