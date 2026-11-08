#!/usr/bin/env bash
# Signs UVMC in with your Claude plan (Pro or Max), for the panel's "my plan" models.
# A sign-in link appears: open it, approve, and paste the code back if asked.
# The login is kept in the machine where only the supervisor can read it, not the agent.
set -euo pipefail
multipass start uvmc
multipass exec uvmc -- sudo uvmc-claude-signin
