# One-time setup

This guide walks through `./setup.sh` on Debian stable and explains every
question it asks. Each step also lists the manual equivalent, in case you want
to do it by hand or the script fails partway. The script is safe to re-run.

## 0. Prerequisites

- Debian stable (13 "trixie"), with a normal user that has `sudo`.
- **Claude Code** installed and logged in **as that user**:
  ```bash
  curl -fsSL https://claude.ai/install.sh | bash
  claude auth login        # or just run `claude` once
  claude auth status       # should show "loggedIn": true
  ```
  The bot runs as this user and uses this login. Its usage counts against that
  account's plan or API billing.
- A Signal account for the bot (see step 1).
- A phone with Signal, to scan a QR code or receive a verification code.

## 1. Decide: link an existing account, or register a new number

| | **Link to existing account** | **Register a new number** |
|---|---|---|
| What happens | The bot becomes a *linked device* of your Signal account, like Signal Desktop | signal-cli becomes the *primary device* for a number dedicated to the bot |
| You need | Your phone with Signal | A number that can receive SMS or a voice call (spare SIM, VoIP, landline) |
| People message | Your number. You message yourself in **Note to Self** | The bot's number, like any other contact |
| Replies appear from | You | "Claude Bot" (the profile name you set) |
| Privacy | The bot can see all your 1:1 messages. It only acts on whitelisted senders whose message starts with `@claude` | The bot sees only messages sent to its own number |
| Gotchas | Unlinking on the phone kills the bot. Your contacts' replies to the bot land in your normal chats | Registering a number logs out any phone using it. Some VoIP numbers are blocked by Signal |

**Recommendations.** For personal use, linking is quickest: you talk to it in
Note to Self. A dedicated number is cleaner if several people will use the bot,
or if you don't want it next to your personal messages.

## 2. Run the script

```bash
cd ~/signal-bot
./setup.sh
```

Run it as your normal user, not with `sudo`. It calls `sudo` itself where needed.

### Step 1: system packages
It installs `python3-venv curl jq qrencode`, plus Debian's `docker.io` and
`docker-compose` if Docker is missing. It also adds you to the `docker` group,
which takes effect at your next login; for the current run the script uses
`sudo docker`.

*Manual:* `sudo apt install python3-venv curl jq qrencode docker.io docker-compose && sudo usermod -aG docker $USER`

### Step 2: Claude Code check
It finds `claude` and checks `claude auth status`. If you aren't logged in, it
stops and tells you to log in first.

### Step 3: Python bridge
It creates `.venv/` and installs the bot into it.

*Manual:* `python3 -m venv .venv && .venv/bin/pip install -e .`

### Step 4: signal-cli container
It writes `.env` with your uid/gid, so the account files in
`./signal-cli-config/` stay owned by you. Then it pulls
`bbernhard/signal-cli-rest-api` and starts it in `json-rpc` mode on
`127.0.0.1:8080`.

*Manual:* `docker compose up -d && curl -s 127.0.0.1:8080/v1/about` should show `"mode":"json-rpc"`.

### Step 5: Signal account
If signal-cli already holds an account (for example on a re-run), the script
offers to reuse it. Otherwise it asks:

```
  1) Link to an EXISTING Signal account
  2) Register a NEW number dedicated to the bot
```

#### Option 1: link
1. A QR code is printed in the terminal. Make the window large enough that the
   QR code isn't wrapped.
2. On your phone, open **Signal → Settings → Linked devices → Link new device
   (+)** and scan it.
3. The script waits up to three minutes, detects the new account, and restarts
   signal-cli so it starts receiving messages.

*Manual:*
```bash
curl -s '127.0.0.1:8080/v1/qrcodelink/raw?device_name=signal-bot' | jq -r .device_link_uri | qrencode -t ansiutf8
# scan, then:
curl -s 127.0.0.1:8080/v1/accounts        # your number should appear
docker compose restart signal-cli
```
You can also open `http://127.0.0.1:8080/v1/qrcodelink?device_name=signal-bot`
in a browser on this machine to get a PNG.

#### Option 2: register
1. **Bot phone number**, in international format: `+15551234567`.
2. **Captcha.** Signal requires one for every new registration:
   - Open <https://signalcaptchas.org/registration/generate.html> and solve it.
   - The page shows an **Open Signal** link. Right-click it and copy the link
     address, which starts with `signalcaptcha://`. If no link appears, open
     the browser dev-tools console; the URL is logged there.
   - Paste it at the prompt. Tokens expire in about a minute.
3. **SMS or voice.** Choose voice for a landline. Signal sometimes rejects a
   voice request until an SMS has been tried: request SMS first, wait 60
   seconds, then re-run and choose voice.
4. **Verification code**: the 6 digits you received (a dash is fine).
5. **Registration-lock PIN?** Answer yes only if this number was used with
   Signal before and had a PIN set.
6. **Profile name**: what people see, for example `Claude Bot`.
7. **Set a PIN?** Recommended. This stops anyone else re-registering the
   number. Store the PIN somewhere safe.

*Manual:*
```bash
N=+15551234567
curl -X POST -H 'Content-Type: application/json' \
  -d '{"captcha":"signalcaptcha://…","use_voice":false}' 127.0.0.1:8080/v1/register/$N
curl -X POST -H 'Content-Type: application/json' -d '{}' 127.0.0.1:8080/v1/register/$N/verify/123456
curl -X PUT  -H 'Content-Type: application/json' -d '{"name":"Claude Bot"}' 127.0.0.1:8080/v1/profiles/$N
curl -X POST -H 'Content-Type: application/json' -d '{"pin":"…"}' 127.0.0.1:8080/v1/accounts/$N/pin
docker compose restart signal-cli
```

Common registration errors:
- *captcha invalid or required*: the token expired. Get a fresh one.
- *rate limited (429)*: wait, sometimes hours.
- *voice before SMS*: see point 3 above.

### Step 6: bot settings

| Prompt | What to enter |
|---|---|
| **Allowed sender** (repeat, blank to finish) | Numbers like `+15557654321`, or Signal UUIDs. In link mode your own number is added automatically, so Note to Self works. |
| **Trigger prefix** | Default `@claude`. Only messages that **start** with it are processed. |
| **Claude working directory** | Default `~/signal-bot-workspace`. Claude starts here. |
| **Tools** | `1` read-only (default), `2` read and edit, `3` custom rules such as `Read,Grep,Bash(git log:*)`. |

These settings are written to `config.toml` (mode 600). A previous config is
kept as `config.toml.bak`.

**About UUIDs.** A sender who has Signal's phone-number privacy turned on may
not reveal their number to the bot. If a whitelisted friend gets no reply,
check `journalctl -u signal-bot` for `ignoring message from non-whitelisted
sender +…/uuid:…` and add the `uuid:…` value to `allowed`.

### Step 7: systemd service
The script runs `signal-bot --check`, then installs
`/etc/systemd/system/signal-bot.service` (running as your user from this
directory), and enables and starts it.

*Manual:*
```bash
sed -e "s|@USER@|$USER|g" -e "s|@GROUP@|$(id -gn)|g" -e "s|@DIR@|$PWD|g" \
  systemd/signal-bot.service | sudo tee /etc/systemd/system/signal-bot.service
sudo systemctl daemon-reload && sudo systemctl enable --now signal-bot
```

### Step 8: test message
The script sends "signal-bot is online" to the first whitelisted sender. Reply
with `@claude /help`, then something real such as `@claude what OS is this
machine running?`.

## 3. Day-two tasks

**Add or remove users.** Edit `[access].allowed` in `config.toml`, then run
`sudo systemctl restart signal-bot`. Or run `./setup.sh --reconfigure`.

**Change tools, trigger, or workdir.** Same as above.

**Unlink (link mode).** Remove "signal-bot" under Signal → Settings → Linked
devices on your phone, then run `./setup.sh --uninstall` and delete the account
data when asked.

**Re-link or re-register.** Run `./setup.sh --uninstall`, delete the account
data, then run `./setup.sh` again. Or remove `signal-cli-config/` while the
container is stopped.

**Move to another machine.** Copy `config.toml` and `signal-cli-config/` while
the container is stopped. Never run the same account in two places at once.

**Back up.** `signal-cli-config/` holds the account's identity keys. Treat it like
a password.
