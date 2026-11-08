#!/usr/bin/env bash
# Stops UVMC. Everything inside is kept; run logs are also copied to the repo's logs folder.
"$(dirname "$0")/save-logs.sh"
exec multipass stop uvmc
