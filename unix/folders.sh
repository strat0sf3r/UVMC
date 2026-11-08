#!/usr/bin/env bash
# The "Start UVMC" folder choice: which folders the agent can see this time.
# Run by unix/panel.sh before the panel opens. It only edits shares.txt; sharing is
# done by unix/share.sh afterwards.
#   --force   ask even when "don't ask again" is set
# macOS: native dialogs. Linux: zenity if there's a desktop, else questions in the terminal.
# Exit code 0 = start, 1 = cancelled.
set -uo pipefail
cd "$(dirname "$0")/.." || exit 1
REPO="$PWD"
LIST="$REPO/shares.txt"
[ -f "$LIST" ] || cp "$REPO/shares.example.txt" "$LIST" 2>/dev/null || true
SKIP="$REPO/.uvmc-skip-folder-window"
ADD="+ Add a folder..."
NOASK="(don't ask again next time)"

[ "${1:-}" = "--force" ] || { [ -f "$SKIP" ] && exit 0; }

# --- read shares.txt ---------------------------------------------------------------------
header=() names=() paths=() ons=()
in_header=1
if [ -f "$LIST" ]; then
  while IFS= read -r raw || [ -n "$raw" ]; do
    raw="${raw%$'\r'}"
    line="$(echo "$raw" | sed 's/^[[:space:]]*//;s/[[:space:]]*$//')"
    on=1
    if [[ "$line" =~ ^#off[[:space:]]+(.*)$ ]]; then line="${BASH_REMATCH[1]}"; on=0
    elif [[ -z "$line" || "$line" == \#* ]]; then
      [ "$in_header" = 1 ] && header+=("$raw")
      continue
    fi
    [[ "$line" == *=* ]] || continue
    in_header=0
    names+=("$(echo "${line%%=*}" | xargs)")
    paths+=("$(echo "${line#*=}" | xargs)")
    ons+=("$on")
  done < "$LIST"
fi

clean_name() {
  local n
  n="$(echo "$1" | tr '[:upper:]' '[:lower:]' | sed 's/[^a-z0-9_-]/-/g; s/^-*//; s/-*$//' | cut -c1-40)"
  [[ "$n" =~ ^[a-z0-9] ]] || n="folder${n:+-$n}"
  local base="$n" i=2
  while printf '%s\n' "${names[@]+"${names[@]}"}" | grep -qx "$n"; do n="$base-$i"; i=$((i + 1)); done
  echo "$n"
}
shown_path() { case "$1" in /*) echo "$1" ;; *) echo "$REPO/$1" ;; esac; }
add_folder() {  # $1 = absolute folder, $2 = wanted name
  local full="${1%/}" name
  if [ -z "$full" ] || [ "$full" = "$HOME" ] || [ "$full" = "/" ]; then
    echo "Sharing your whole home folder or disk is not allowed."; return 1
  fi
  [ -d "$full" ] || { echo "Not a folder: $full"; return 1; }
  name="$(clean_name "${2:-$(basename "$full")}")"
  case "$full" in "$REPO"/*) full="${full#"$REPO"/}" ;; esac
  names+=("$name"); paths+=("$full"); ons+=(1)
}
noask=0

# --- ask -------------------------------------------------------------------------------------
if [ "$(uname -s)" = "Darwin" ]; then
  while :; do
    items=() defaults=""
    for i in "${!names[@]}"; do
      items+=("${names[$i]}: $(shown_path "${paths[$i]}")")
      [ "${ons[$i]}" = 1 ] && defaults+="${names[$i]}: $(shown_path "${paths[$i]}")"$'\n'
    done
    choice="$(UVMC_DEFAULTS="$defaults" osascript - "${items[@]+"${items[@]}"}" "$ADD" "$NOASK" <<'AS'
on run argv
  set AppleScript's text item delimiters to linefeed
  set defs to text items of (system attribute "UVMC_DEFAULTS")
  set picked to choose from list argv with title "Start UVMC" with prompt "Folders the agent can see this time (read-only for it). Select several with Cmd-click." default items defs OK button name "Start" cancel button name "Cancel" with multiple selections allowed and empty selection allowed
  if picked is false then return "CANCEL"
  return picked as text
end run
AS
)" || exit 1
    [ "$choice" = "CANCEL" ] && exit 1
    if printf '%s\n' "$choice" | grep -qxF "$ADD"; then
      for i in "${!names[@]}"; do
        printf '%s\n' "$choice" | grep -qxF "${names[$i]}: $(shown_path "${paths[$i]}")" && ons[$i]=1 || ons[$i]=0
      done
      folder="$(osascript -e 'POSIX path of (choose folder with prompt "Choose a folder for the agent to see (read-only)")' 2>/dev/null)" || continue
      suggested="$(basename "${folder%/}")"
      name="$(osascript - "$suggested" <<'AS' 2>/dev/null
on run argv
  text returned of (display dialog "Short name for this folder (the agent sees /mnt/shared/<name>):" default answer (item 1 of argv) with title "Folder name")
end run
AS
)" || continue
      add_folder "$folder" "$name" || osascript -e 'display alert "Sharing your whole home folder or disk is not allowed."' >/dev/null
      continue
    fi
    for i in "${!names[@]}"; do
      printf '%s\n' "$choice" | grep -qxF "${names[$i]}: $(shown_path "${paths[$i]}")" && ons[$i]=1 || ons[$i]=0
    done
    printf '%s\n' "$choice" | grep -qxF "$NOASK" && noask=1
    break
  done
elif command -v zenity >/dev/null 2>&1 && { [ -n "${DISPLAY:-}" ] || [ -n "${WAYLAND_DISPLAY:-}" ]; }; then
  while :; do
    rows=()
    for i in "${!names[@]}"; do
      [ "${ons[$i]}" = 1 ] && rows+=(TRUE) || rows+=(FALSE)
      rows+=("${names[$i]}" "$(shown_path "${paths[$i]}")")
    done
    rows+=(FALSE "$ADD" "" FALSE "$NOASK" "")
    choice="$(zenity --list --checklist --title "Start UVMC" \
      --text "Folders the agent can see this time (read-only for it)." \
      --column "Share" --column "Name" --column "Folder" "${rows[@]}" \
      --print-column=2 --separator=$'\n' --ok-label "Start" --width 680 --height 420)" || exit 1
    for i in "${!names[@]}"; do
      printf '%s\n' "$choice" | grep -qxF "${names[$i]}" && ons[$i]=1 || ons[$i]=0
    done
    if printf '%s\n' "$choice" | grep -qxF "$ADD"; then
      folder="$(zenity --file-selection --directory --title "Choose a folder for the agent to see (read-only)")" || continue
      name="$(zenity --entry --title "Folder name" --text "Short name (the agent sees /mnt/shared/<name>):" --entry-text "$(basename "$folder")")" || continue
      add_folder "$folder" "$name" || zenity --error --text "Sharing your whole home folder or disk is not allowed."
      continue
    fi
    printf '%s\n' "$choice" | grep -qxF "$NOASK" && noask=1
    break
  done
else
  while :; do
    echo
    echo "Folders the agent can see this time (read-only for it):"
    for i in "${!names[@]}"; do
      [ "${ons[$i]}" = 1 ] && mark="x" || mark=" "
      printf '  %d. [%s] %s = %s\n' "$((i + 1))" "$mark" "${names[$i]}" "$(shown_path "${paths[$i]}")"
    done
    read -rp "Numbers to switch on/off, a = add a folder, n = don't ask again, Enter = start, q = cancel: " answer
    case "$answer" in
      "") break ;;
      q|Q) exit 1 ;;
      n|N) noask=1; echo "Won't ask next time."; continue ;;
      a|A)
        read -rp "Folder path: " folder
        folder="${folder/#\~/$HOME}"
        read -rp "Short name [$(basename "$folder")]: " name
        add_folder "$(cd "$folder" 2>/dev/null && pwd)" "${name:-$(basename "$folder")}"
        continue ;;
    esac
    for n in $answer; do
      if [[ "$n" =~ ^[0-9]+$ ]] && [ "$n" -ge 1 ] && [ "$n" -le "${#names[@]}" ]; then
        i=$((n - 1)); [ "${ons[$i]}" = 1 ] && ons[$i]=0 || ons[$i]=1
      fi
    done
  done
fi

# --- save -----------------------------------------------------------------------------------
{
  for h in "${header[@]+"${header[@]}"}"; do printf '%s\n' "$h"; done
  [ "${#header[@]}" -gt 0 ] && [ -n "${header[${#header[@]}-1]// /}" ] && echo
  for i in "${!names[@]}"; do
    if [ "${ons[$i]}" = 1 ]; then printf '%s = %s\n' "${names[$i]}" "${paths[$i]}"
    else printf '#off %s = %s\n' "${names[$i]}" "${paths[$i]}"; fi
  done
} > "$LIST.new" && mv "$LIST.new" "$LIST"
if [ "$noask" = 1 ]; then
  echo "unix/panel.sh skips the folder choice. Delete this file (or run unix/panel.sh --folders) to see it again." > "$SKIP"
fi
exit 0
