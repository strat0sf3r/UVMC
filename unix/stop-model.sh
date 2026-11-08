#!/usr/bin/env bash
# Stops the model on this computer's graphics chip (unix/start-model.sh starts it again).
exec bash "$(dirname "$0")/local-model.sh" --stop
