#!/usr/bin/env bash
# signal-bot installer for Debian stable.
#
#   ./setup.sh               full interactive install (safe to re-run)
#   ./setup.sh --reconfigure re-ask the bot settings (whitelist, tools, ...) and restart
#   ./setup.sh --uninstall   stop and remove the service and container
#
# Run as your normal user (not root); sudo is used where needed.
set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONFIG="$DIR/config.toml"
DATA_DIR="$DIR/signal-cli-config"
UNIT_NAME="signal-bot.service"
UNIT_PATH="/etc/systemd/system/$UNIT_NAME"
PORT="${SIGNAL_CLI_PORT:-8080}"
API="http://127.0.0.1:$PORT"
DEVICE_NAME="${DEVICE_NAME:-signal-bot}"

DOCKER=(docker)
ACCOUNT=""
ACCOUNT_MODE=""      # link | register | existing

# ---------------------------------------------------------------- helpers
if [[ -t 1 ]]; then
  B=$'\e[1m'; G=$'\e[32m'; Y=$'\e[33m'; R=$'\e[31m'; N=$'\e[0m'
else
  B=""; G=""; Y=""; R=""; N=""
fi
info() { printf '%s==>%s %s\n' "$G" "$N" "$*"; }
warn() { printf '%s!!%s  %s\n' "$Y" "$N" "$*" >&2; }
die()  { printf '%sxx%s  %s\n' "$R" "$N" "$*" >&2; exit 1; }
hdr()  { printf '\n%s%s%s\n' "$B" "$*" "$N"; }

# ask VAR "Prompt" [default]
ask() {
  local __var=$1 __prompt=$2 __def=${3-} __ans
  if [[ -n $__def ]]; then
    read -r -p "$__prompt [$__def]: " __ans || true
    __ans=${__ans:-$__def}
  else
    read -r -p "$__prompt: " __ans || true
  fi
  printf -v "$__var" '%s' "$__ans"
}

# confirm "Question" [Y|N default]
confirm() {
  local def=${2:-Y} ans
  read -r -p "$1 [$( [[ $def == Y ]] && echo Y/n || echo y/N )]: " ans || true
  ans=${ans:-$def}
  [[ $ans =~ ^[Yy] ]]
}

# Normalise a phone number to E.164, or a UUID to uuid:<lower>. Prints nothing on failure.
norm_id() {
  local s=${1//[[:space:]()\-.]/}
  s=${s#uuid:}
  if [[ ${s,,} =~ ^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$ ]]; then
    printf 'uuid:%s' "${s,,}"; return 0
  fi
  [[ $s == 00* ]] && s="+${s#00}"
  if [[ $s =~ ^\+[1-9][0-9]{6,14}$ ]]; then printf '%s' "$s"; return 0; fi
  return 1
}

ask_number() {
  local __var=$1 __prompt=$2 __def=${3-} __raw __n
  while true; do
    ask __raw "$__prompt" "$__def"
    if __n=$(norm_id "$__raw") && [[ $__n == +* ]]; then
      printf -v "$__var" '%s' "$__n"; return
    fi
    warn "Enter the number in international format, e.g. +15551234567"
  done
}

# TOML basic-string escaping
toml_str() {
  local s=${1//\\/\\\\}
  s=${s//\"/\\\"}
  printf '"%s"' "$s"
}

compose() { "${DOCKER[@]}" compose -f "$DIR/docker-compose.yml" --project-directory "$DIR" "$@"; }

# api METHOD PATH [JSON]  -> prints body, returns non-zero on HTTP >= 300
api() {
  local method=$1 path=$2 body=${3-} out code
  out=$(mktemp)
  if [[ -n $body ]]; then
    code=$(curl -sS -o "$out" -w '%{http_code}' -X "$method" -H 'Content-Type: application/json' \
      --data-binary "$body" "$API$path") || { rm -f "$out"; return 1; }
  else
    code=$(curl -sS -o "$out" -w '%{http_code}' -X "$method" "$API$path") || { rm -f "$out"; return 1; }
  fi
  cat "$out"; rm -f "$out"
  [[ $code -lt 300 ]]
}

wait_api() {
  info "Waiting for the signal-cli REST API on $API (first start can take a minute)..."
  local i
  for i in $(seq 1 90); do
    if curl -sf "$API/v1/about" >/dev/null 2>&1; then
      info "API is up: $(curl -s "$API/v1/about" | jq -c '{version, mode}')"
      return 0
    fi
    sleep 2
  done
  compose logs --tail 40 signal-cli || true
  die "signal-cli API did not come up. See the logs above."
}

list_accounts() { curl -sf "$API/v1/accounts" | jq -r '.[]?' 2>/dev/null || true; }

# ---------------------------------------------------------------- steps
preflight() {
  [[ $EUID -ne 0 ]] || die "Run as your normal user, not root (the bot runs as you and uses your Claude login)."
  command -v sudo >/dev/null || die "sudo is required."
  if [[ -r /etc/os-release ]]; then
    # shellcheck disable=SC1091
    . /etc/os-release
    if [[ ${ID:-} != debian ]]; then
      warn "This script targets Debian stable; detected ${PRETTY_NAME:-unknown}."
      confirm "Continue anyway?" N || exit 1
    fi
  fi
}

install_packages() {
  hdr "1. System packages"
  local pkgs=(python3-venv curl jq qrencode ca-certificates)
  if ! command -v docker >/dev/null; then
    pkgs+=(docker.io docker-compose)
  elif ! docker compose version >/dev/null 2>&1; then
    if dpkg -s docker-ce >/dev/null 2>&1; then pkgs+=(docker-compose-plugin); else pkgs+=(docker-compose); fi
  fi
  local missing=()
  local p
  for p in "${pkgs[@]}"; do dpkg -s "$p" >/dev/null 2>&1 || missing+=("$p"); done
  if ((${#missing[@]})); then
    info "Installing: ${missing[*]}"
    sudo apt-get update -qq
    sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq "${missing[@]}"
  else
    info "All packages present."
  fi
  sudo systemctl enable --now docker >/dev/null 2>&1 || true

  if ! docker info >/dev/null 2>&1; then
    if ! id -nG "$USER" | tr ' ' '\n' | grep -qx docker; then
      info "Adding $USER to the docker group (takes effect at next login)."
      sudo usermod -aG docker "$USER"
    fi
    warn "Using sudo for docker during this run."
    DOCKER=(sudo docker)
  fi
}

check_claude() {
  hdr "2. Claude Code"
  CLAUDE_BIN=$(command -v claude || true)
  [[ -z $CLAUDE_BIN && -x $HOME/.local/bin/claude ]] && CLAUDE_BIN=$HOME/.local/bin/claude
  if [[ -z $CLAUDE_BIN ]]; then
    die "The 'claude' CLI is not installed. Install it (curl -fsSL https://claude.ai/install.sh | bash), run 'claude' once to log in, then re-run this script."
  fi
  CLAUDE_BIN=$(readlink -f "$CLAUDE_BIN")
  info "Found $("$CLAUDE_BIN" --version 2>/dev/null | head -1) at $CLAUDE_BIN"
  if ! "$CLAUDE_BIN" auth status 2>/dev/null | jq -e '.loggedIn == true' >/dev/null 2>&1; then
    die "Claude Code is not logged in. Run 'claude auth login' (or 'claude' once), then re-run this script."
  fi
  info "Claude Code is logged in."
}

install_python() {
  hdr "3. Python bridge"
  if [[ ! -x $DIR/.venv/bin/python ]]; then
    python3 -m venv "$DIR/.venv"
  fi
  "$DIR/.venv/bin/pip" install -q --upgrade pip
  "$DIR/.venv/bin/pip" install -q -e "$DIR"
  info "Installed into $DIR/.venv"
}

start_signal_cli() {
  hdr "4. signal-cli container"
  mkdir -p "$DATA_DIR"
  chmod 700 "$DATA_DIR"
  # compose reads .env from the project directory
  printf 'SIGNAL_CLI_UID=%s\nSIGNAL_CLI_GID=%s\nSIGNAL_CLI_PORT=%s\n' "$(id -u)" "$(id -g)" "$PORT" >"$DIR/.env"
  compose pull --quiet
  compose up -d
  wait_api
}

setup_account() {
  hdr "5. Signal account"
  local existing
  existing=$(list_accounts)
  if [[ -n $existing ]]; then
    info "signal-cli already has account(s): $(echo "$existing" | tr '\n' ' ')"
    if confirm "Use $(echo "$existing" | head -1) for the bot?" Y; then
      ACCOUNT=$(echo "$existing" | head -1)
      ACCOUNT_MODE=existing
      return
    fi
  fi

  cat <<EOF

How should the bot connect to Signal?

  1) Link to an EXISTING Signal account (your phone stays primary)
     - The bot becomes a "linked device", like Signal Desktop.
     - Replies come from YOUR number; the bot can see all your 1:1 messages
       (it only acts on whitelisted senders whose message starts with the trigger).
     - Good for personal use: message yourself via "Note to Self".

  2) Register a NEW number dedicated to the bot
     - Needs a number that can receive an SMS or voice call (spare SIM,
       VoIP number, or landline with voice verification).
     - Registering a number with signal-cli logs out any phone using it.
     - People message the bot's number like any other contact.

EOF
  local choice
  while true; do
    ask choice "Choose 1 or 2" 1
    case $choice in
      1) link_account; return ;;
      2) register_account; return ;;
      *) warn "Please enter 1 or 2." ;;
    esac
  done
}

link_account() {
  ACCOUNT_MODE="link"
  local before uri
  before=$(list_accounts)
  uri=$(api GET "/v1/qrcodelink/raw?device_name=$DEVICE_NAME" | jq -r '.device_link_uri // empty') \
    || die "Could not start linking (is the API healthy?)."
  [[ -n $uri ]] || die "API returned no link URI."
  cat <<EOF

On your phone open Signal -> Settings -> Linked devices -> Link new device (+),
then scan this QR code:

EOF
  qrencode -t ansiutf8 -m 2 "$uri"
  printf '\nLink URI (if you need to make your own QR code):\n%s\n\n' "$uri"
  info "Waiting for you to scan (up to 3 minutes)..."
  local i acct
  for i in $(seq 1 90); do
    acct=$(comm -13 <(echo "$before" | sort) <(list_accounts | sort) | head -1)
    if [[ -n $acct ]]; then
      ACCOUNT=$acct
      info "Linked as $ACCOUNT. Restarting signal-cli so it starts receiving for the new account..."
      compose restart signal-cli >/dev/null
      wait_api
      return
    fi
    sleep 2
  done
  die "Timed out waiting for the link. Re-run ./setup.sh to get a fresh QR code."
}

register_account() {
  ACCOUNT_MODE=register
  ask_number ACCOUNT "Bot phone number (international format, e.g. +15551234567)"
  cat <<EOF

Signal requires a captcha for new registrations:
  1. Open https://signalcaptchas.org/registration/generate.html in a browser.
  2. Solve the captcha. The page then tries to open a 'signalcaptcha://...' link.
  3. Right-click "Open Signal" and copy the link (or copy it from the browser's
     console/address bar). It starts with signalcaptcha://
  The token expires after about a minute, so paste it promptly.

EOF
  local captcha voice=false body resp code pin=""
  while true; do
    ask captcha "Paste captcha token"
    captcha=${captcha#"${captcha%%[![:space:]]*}"}; captcha=${captcha%"${captcha##*[![:space:]]}"}
    [[ -n $captcha ]] || { warn "Captcha is required."; continue; }
    if confirm "Verify by voice call instead of SMS? (landlines; Signal may require an SMS attempt first)" N; then
      voice=true
    else
      voice=false
    fi
    body=$(jq -nc --arg c "$captcha" --argjson v "$voice" '{captcha: $c, use_voice: $v}')
    if resp=$(api POST "/v1/register/$ACCOUNT" "$body"); then
      info "Verification code requested by $( $voice && echo voice call || echo SMS )."
      break
    fi
    warn "Registration request failed: $resp"
    warn "Common causes: expired captcha (get a new one), rate limiting (wait), or voice before SMS (try SMS first, wait 60s, then voice)."
    confirm "Try again?" Y || die "Registration aborted."
  done

  while true; do
    ask code "Enter the 6-digit verification code"
    code=${code//[^0-9]/}
    if confirm "Does this number have a Signal registration-lock PIN set?" N; then
      read -r -s -p "PIN: " pin; echo
    fi
    body=$( [[ -n $pin ]] && jq -nc --arg p "$pin" '{pin: $p}' || echo '{}' )
    if resp=$(api POST "/v1/register/$ACCOUNT/verify/$code" "$body"); then
      info "Registered $ACCOUNT."
      break
    fi
    warn "Verification failed: $resp"
    confirm "Try again?" Y || die "Verification aborted."
  done

  local name
  ask name "Profile name shown to people who message the bot" "Claude Bot"
  api PUT "/v1/profiles/$ACCOUNT" "$(jq -nc --arg n "$name" '{name: $n}')" >/dev/null \
    || warn "Could not set profile name (you can retry later)."

  if confirm "Set a Signal PIN (registration lock) to protect this number from re-registration?" Y; then
    local p1 p2
    while true; do
      read -r -s -p "New PIN (at least 4 digits): " p1; echo
      read -r -s -p "Repeat PIN: " p2; echo
      [[ $p1 == "$p2" && ${#p1} -ge 4 ]] && break
      warn "PINs did not match or too short."
    done
    if api POST "/v1/accounts/$ACCOUNT/pin" "$(jq -nc --arg p "$p1" '{pin: $p}')" >/dev/null; then
      info "PIN set. Store it in your password manager."
    else
      warn "Could not set PIN."
    fi
  fi
  compose restart signal-cli >/dev/null
  wait_api
}

configure_bot() {
  hdr "6. Bot settings"
  if [[ -z $ACCOUNT ]]; then
    ACCOUNT=$(sed -n 's/^account *= *"\(.*\)"/\1/p' "$CONFIG" 2>/dev/null | head -1)
    [[ -n $ACCOUNT ]] || ACCOUNT=$(list_accounts | head -1)
    [[ -n $ACCOUNT ]] || die "No Signal account found; run ./setup.sh without --reconfigure."
  fi
  info "Bot account: $ACCOUNT"

  cat <<EOF

Whitelist: only these senders can use the bot. Enter phone numbers in
international format (+15551234567) or Signal UUIDs, one per line.
Finish with an empty line.
EOF
  ALLOWED=()
  if [[ $ACCOUNT_MODE == link ]] || { [[ $ACCOUNT_MODE != register ]] && confirm "Allow the account owner ($ACCOUNT) via Note to Self?" Y; }; then
    [[ $ACCOUNT_MODE == link ]] && info "Adding $ACCOUNT (you, via Note to Self)."
    ALLOWED+=("$ACCOUNT")
  fi
  local raw n
  while true; do
    read -r -p "Allowed sender (blank to finish): " raw || true
    [[ -z $raw ]] && { ((${#ALLOWED[@]})) && break; warn "Add at least one."; continue; }
    if n=$(norm_id "$raw"); then
      ALLOWED+=("$n"); info "Added $n"
    else
      warn "Not a valid number/UUID: $raw"
    fi
  done

  ask TRIGGER "Trigger prefix (messages must start with this)" "@claude"
  ask WORKDIR "Claude working directory" "$HOME/signal-bot-workspace"
  WORKDIR=${WORKDIR/#\~/$HOME}
  mkdir -p "$WORKDIR"

  cat <<EOF

Which tools may Claude use without asking? Anything not allowed is denied.
  1) Read-only:   Read, Glob, Grep, WebSearch, WebFetch   (recommended)
  2) Read + edit: the above plus Edit, Write (files under the workdir)
  3) Custom:      enter Claude Code permission rules, comma-separated,
                  e.g. Read,Grep,Bash(git status:*),Bash(df:*)
Note: allowing unrestricted "Bash" lets every whitelisted user run any
command as $USER on this machine.
EOF
  local t
  ask t "Choose 1, 2 or 3" 1
  case $t in
    2) TOOLS=(Read Glob Grep WebSearch WebFetch Edit Write) ;;
    3)
      local custom
      ask custom "Rules"
      IFS=',' read -r -a TOOLS <<<"$custom"
      local i
      for i in "${!TOOLS[@]}"; do TOOLS[i]=$(echo "${TOOLS[i]}" | xargs); done
      ;;
    *) TOOLS=(Read Glob Grep WebSearch WebFetch) ;;
  esac

  write_config
}

write_config() {
  if [[ -f $CONFIG ]]; then
    cp -p "$CONFIG" "$CONFIG.bak"
    info "Backed up previous config to config.toml.bak"
  fi
  local allowed_toml tools_toml x
  allowed_toml=""; for x in "${ALLOWED[@]}"; do allowed_toml+="$(toml_str "$x"), "; done
  tools_toml="";   for x in "${TOOLS[@]}";   do [[ -n $x ]] && tools_toml+="$(toml_str "$x"), "; done
  (
    umask 077
    cat >"$CONFIG" <<EOF
# Written by setup.sh on $(date -Iseconds). See config.example.toml for all options.

[signal]
api_url = $(toml_str "$API")
account = $(toml_str "$ACCOUNT")

[access]
allowed = [${allowed_toml%, }]
reply_to_unknown = false

[bot]
trigger = $(toml_str "$TRIGGER")
state_dir = "state"
max_concurrent = 2
chunk_size = 1800

[claude]
binary = $(toml_str "$CLAUDE_BIN")
workdir = $(toml_str "$WORKDIR")
allowed_tools = [${tools_toml%, }]
permission_mode = "dontAsk"
model = ""
timeout_seconds = 600
max_budget_usd = 0
append_system_prompt = "You are answering over Signal on a phone. Keep replies concise and use plain text, not Markdown tables."
extra_args = []
EOF
  )
  chmod 600 "$CONFIG"
  info "Wrote $CONFIG"
}

install_service() {
  hdr "7. systemd service"
  "$DIR/.venv/bin/signal-bot" --config "$CONFIG" --check || die "Config check failed (see above)."
  local tmp
  tmp=$(mktemp)
  sed -e "s|@USER@|$USER|g" -e "s|@GROUP@|$(id -gn)|g" -e "s|@DIR@|$DIR|g" \
    "$DIR/systemd/signal-bot.service" >"$tmp"
  sudo install -m 644 "$tmp" "$UNIT_PATH"
  rm -f "$tmp"
  sudo systemctl daemon-reload
  sudo systemctl enable "$UNIT_NAME" >/dev/null 2>&1
  sudo systemctl restart "$UNIT_NAME"
  sleep 3
  if systemctl is-active --quiet "$UNIT_NAME"; then
    info "signal-bot is running."
  else
    sudo journalctl -u "$UNIT_NAME" -n 30 --no-pager || true
    die "signal-bot failed to start (logs above)."
  fi
}

send_test() {
  hdr "8. Test message"
  local to=${ALLOWED[0]}
  [[ $to == uuid:* ]] && to=${to#uuid:}
  local msg="signal-bot is online. Send: ${TRIGGER} /help"
  if api POST /v2/send "$(jq -nc --arg m "$msg" --arg a "$ACCOUNT" --arg r "$to" \
        '{message: $m, number: $a, recipients: [$r]}')" >/dev/null; then
    info "Sent a test message to ${ALLOWED[0]}."
  else
    warn "Could not send the test message; check 'docker compose logs signal-cli'."
  fi
}

finish() {
  cat <<EOF

${B}Done.${N}

  Try it:   send "${TRIGGER} hello" to $ACCOUNT from a whitelisted phone
  $( [[ $ACCOUNT_MODE == link ]] && echo "          (or in Note to Self on your own phone)" )
  Logs:     journalctl -u signal-bot -f
            docker compose -f $DIR/docker-compose.yml logs -f signal-cli
  Change:   edit $CONFIG, then: sudo systemctl restart signal-bot
            or re-run: ./setup.sh --reconfigure
EOF
}

uninstall() {
  hdr "Uninstall signal-bot"
  docker info >/dev/null 2>&1 || DOCKER=(sudo docker)
  sudo systemctl disable --now "$UNIT_NAME" 2>/dev/null || true
  sudo rm -f "$UNIT_PATH"
  sudo systemctl daemon-reload
  info "Service removed."
  compose down 2>/dev/null || true
  info "Container removed."
  if confirm "Delete Signal account data in $DATA_DIR? (keys; you would need to re-link/re-register)" N; then
    sudo rm -rf "$DATA_DIR"
    info "Deleted. If this was a linked device, also remove it in Signal -> Settings -> Linked devices."
  fi
  if confirm "Delete config.toml and state/?" N; then
    rm -rf "$CONFIG" "$CONFIG.bak" "$DIR/state"
  fi
}

usage() { sed -n '2,8p' "$0" | sed 's/^# \{0,1\}//'; }

main() {
  case ${1-} in
    -h|--help) usage; exit 0 ;;
    --uninstall) preflight; uninstall; exit 0 ;;
    --reconfigure)
      preflight
      docker info >/dev/null 2>&1 || DOCKER=(sudo docker)
      check_claude
      curl -sf "$API/v1/about" >/dev/null || start_signal_cli
      configure_bot; install_service; finish
      exit 0 ;;
    "") ;;
    *) usage; exit 1 ;;
  esac

  preflight
  install_packages
  check_claude
  install_python
  start_signal_cli
  setup_account
  if [[ -f $CONFIG && $ACCOUNT_MODE == existing ]] && confirm "Keep the existing bot settings in config.toml?" Y; then
    "$DIR/.venv/bin/signal-bot" --config "$CONFIG" --check || die "Existing config is invalid; run ./setup.sh --reconfigure"
    mapfile -t ALLOWED < <("$DIR/.venv/bin/python" -c 'import sys,tomllib; [print(x) for x in tomllib.load(open(sys.argv[1],"rb"))["access"]["allowed"]]' "$CONFIG")
    TRIGGER=$("$DIR/.venv/bin/python" -c 'import sys,tomllib; print(tomllib.load(open(sys.argv[1],"rb")).get("bot",{}).get("trigger","@claude"))' "$CONFIG")
  else
    configure_bot
  fi
  install_service
  send_test
  finish
}

main "$@"
