# Time limits for quick commands sent to the UVMC machine, and a log of the ones that ran out.
# Sourced by the other scripts: . "$(dirname "$0")/timeouts.sh"
#
# Quick commands get "timeout" inside the machine ("multipass exec uvmc -- timeout -k 5 20 ..."),
# which stops the command and everything it started, so the exec ends instead of waiting forever.
# Each timeout adds a line to ~/.config/uvmc/timeouts.log (the newest 500 are kept).

UVMC_TIMEOUT_LOG="$HOME/.config/uvmc/timeouts.log"

# log_timeout WHO WHAT HOW: never fails the script that calls it.
log_timeout() {
  { mkdir -p "$(dirname "$UVMC_TIMEOUT_LOG")" &&
    printf '%s  %s  %s  %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$1" "$2" "$3" >> "$UVMC_TIMEOUT_LOG" &&
    tail -n 500 "$UVMC_TIMEOUT_LOG" > "$UVMC_TIMEOUT_LOG.tmp" &&
    mv "$UVMC_TIMEOUT_LOG.tmp" "$UVMC_TIMEOUT_LOG"; } 2>/dev/null || true
}

# check_timeout WHO WHAT LIMIT CODE: when "timeout" stopped the command (exit code 124, or 137
# when it had to kill it), logs it, says so, and returns 0. Otherwise returns 1.
check_timeout() {
  case "$4" in 124|137) ;; *) return 1 ;; esac
  log_timeout "$1" "$2" "stopped in the VM after $3 s"
  echo "The UVMC machine did not answer within $3 seconds ($2); the command was stopped." >&2
  return 0
}
