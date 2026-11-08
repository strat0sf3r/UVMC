#!/usr/bin/env bash
# Takes the model on this computer away: stops it, deletes llama.cpp and the model (the uvmc-local
# folder), starting at login, and the key inside UVMC. It asks first. UVMC itself is not touched.
exec bash "$(dirname "$0")/local-model.sh" --remove
