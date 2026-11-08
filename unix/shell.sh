#!/usr/bin/env bash
# Opens a plain Linux shell in UVMC, as the agent user.
multipass start uvmc || { echo "UVMC is not installed. Run unix/install.sh first."; exit 1; }
exec multipass exec uvmc -- uvmc-agent shell
