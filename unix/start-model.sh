#!/usr/bin/env bash
# Starts the model on this computer's graphics chip (set up once with unix/local-model.sh).
exec bash "$(dirname "$0")/local-model.sh" --start
