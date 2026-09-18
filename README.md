# Toyota Telegram Bot

A Telegram bot that talks to the **Toyota / Lexus Connected Services** API (the same
backend behind the *MyToyota* / *MyLexus* app) and lets you control and query your car
from Telegram: check battery, fuel and range, lock/unlock the doors, start the climate,
see trips and driving scores, and receive app notifications.

> **Vehicle compatibility:** this bot is tested against and tuned for the **Toyota
> C-HR Plug-in Hybrid (PHEV)**. Remote commands, temperature range and the battery
> display formula follow that model — other vehicles may need calibration.

> This code was developed with the help of an AI assistant. It is built on top of the open-source
> [`pytoyoda`](https://github.com/Dadebay/pytoyoda) Python library, which handles all the
> MyToyota API authentication and endpoints.

## What it does

- Reads live vehicle data: fuel level, HV battery, EV/fuel range, odometer, charging status
- Reads door/window/hood lock status and the last parked location
- Reads warning lights, engine-oil status and service history
- Checks for **anomalies** via the `/anomalies` button and lists the precise causes: dashboard warning lights plus unlocked/opened doors, hood and trunk, open windows, lights left on and the rear-seat reminder (wakes the car for fresh state)
- Reads trips, driving scores and consumption summaries
- Sends remote commands: lock/unlock, hazards, trunk, buzzer, climate (with auto-off notice), charging
- Polls the app's notification feed every 5 minutes and forwards new ones to your chat
- Works from a **keyboard** — no need to type commands

## Requirements

- Python 3.11+ on Linux, macOS or Windows
- A Toyota/Lexus account with an active Connected Services subscription
- A Telegram bot token (see below)

## Installation

```bash
git clone <your-repo-url>
cd toyota-bot-project
cp config.example.json config.json
```

Edit `config.json` (see next section), then either:

**Option A — systemd + venv (recommended on Linux):**

```bash
./install.sh
```

This creates a virtualenv, installs dependencies, and installs the bot as a systemd
user service that auto-starts at boot and auto-restarts on crash.

- Status: `systemctl --user status toyota-bot`
- Logs: `journalctl --user -u toyota-bot -f`

**Option B — manual:**

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python bot.py
```

### Uninstall

To remove the bot once installed via `./install.sh`:

```bash
./uninstall.sh                 # stop/disable service, remove service file, keep config/data/venv
./uninstall.sh --purge -y      # full cleanup: also remove .venv, config.json and data/
./uninstall.sh --remove-data   # remove service + data/ only
```

Options: `--purge`, `--keep-venv`, `--remove-config`, `--remove-data`, `--disable-linger`, `-y/--yes` non-interactive, `-h/--help`.

- Handles both the systemd **user** service (`~/.config/systemd/user/toyota-bot.service`) and the **system** service (`/etc/systemd/system/toyota-bot.service`).
- By default keeps `config.json` (Toyota/Telegram secrets) and `data/`; use `--purge` to delete them.

## Configuration (`config.json`)

```json
{
    "toyota": {
        "username": "your@email.com",
        "password": "yourpassword",
        "brand": "T"
    },
    "telegram": {
        "token": "YOUR_BOT_TOKEN_HERE"
    },
    "allowed_user_ids": [123456789],
    "battery": {
        "phev_ev_display": true,
        "soc_buffer": 31.0,
        "soc_factor": 1.3333
    }
}
```

> **🔒 Security & privacy:** The bot **will not start** if `allowed_user_ids` is missing or empty. This is intentional — without an allowlist anyone who discovers your bot username could query your car's location, fuel, lock status and send remote commands. Set at least one Telegram user ID (see step 7 below). The check happens at startup (`bot.py:44`) and the process exits with `Refusing to start: 'allowed_user_ids' is missing or empty` — add your ID to `config.json` and restart.

| Key | Description |
| --- | ----------- |
| `toyota.username` / `toyota.password` | Your Toyota Connected Services account login |
| `toyota.brand` | `"T"` for Toyota, `"L"` for Lexus |
| `telegram.token` | The bot token given by @BotFather (see below) |
| `allowed_user_ids` | **Required.** List of Telegram user IDs allowed to use the bot. The bot **refuses to start** if this is missing or `[]` (security/privacy — prevents open access to your vehicle). Add at least one ID from @userinfobot. |
| `timezone` | *Optional.* IANA timezone override (e.g. `"Europe/Rome"`) for every time the bot shows. Normally not needed — the bot auto-detects the timezone, see "Timezone" below. |
| `battery` | PHEV battery display calibration (see "Battery charge display" below) |

## Telegram bot commands

*Info*

| Command | Description |
| ------- | ----------- |
| `/start`, `/help` | Show the help menu and the command keyboard |
| `/vehicles` | List vehicles on the account |
| `/status` | Fuel, battery, range, odometer, charging |
| `/charge` | Battery and charging detail, schedules |
| `/lock_status` | Doors, windows, hood status |
| `/climate_status` | AC state and target temperature |
| `/location` | Last parked location (Google Maps link) |
| `/health` | Warning lights and engine-oil status |
| `/anomalies` | Check for anomalies and see what caused them: health warning lights + vehicle status details (unlocked/opened doors, hood, trunk, windows, lights, rear-seat reminder). Wakes the car first |
| `/service_history` | Dealer visits |
| `/trips [n]` | Total km driven `n` days ago (`0` = today, `1` = yesterday, ...) |
| `/trip` | Alias for `/trips` |
| `/trips_summary` | Ask for a day or month total (see below) |
| `/last_trip` | Last trip details |
| `/scores [days]` | Driving scores for the last N days (default 7) |
| `/summary <d\|w\|m\|y>` | Daily / weekly / monthly / yearly consumption summary |
| `/notifications` | List app notifications |

*Remote commands (if supported by the vehicle)*

| Command | Description |
| ------- | ----------- |
| `/lock`, `/unlock` | Lock / unlock the doors |
| `/hazards_on`, `/hazards_off` | Turn hazard lights on / off |
| `/trunk_lock`, `/trunk_unlock` | Lock / unlock the trunk |
| `/buzzer` | Buzzer warning |

*Climate*

| Command | Description |
| ------- | ----------- |
| `/ac_on` | Start AC (asks for a temperature between 18 and 29°C, auto-off after 20 min — bot replies with shut-off time) |
| `/ac_off` | Stop AC |
| `/climate <temp>` | Set the target temperature (18–29°C) and start (auto-off after 20 min — bot replies with shut-off time) |
| `/refresh_climate` | Refresh the climate status |

*Battery / charging*

| Command | Description |
| ------- | ----------- |
| `/charge_now` | Start charging immediately |
| `/refresh_battery` | Force a battery status refresh (uses the 12 V battery) |

*System*

| Command | Description |
| ------- | ----------- |
| `/wake` | Wake the vehicle |
| `/alias <name>` | Set a nickname for the vehicle |
| `/timezone` | Set the bot's timezone: share your location or type an IANA name (stored in `data/data.json`, see "Timezone" below) |
| `/notify` | Send app notifications to this chat |
| `/unotify` | Stop sending notifications to this chat |
| `/cancel` | Cancel the current prompt |

### `/trips_summary`

Starts a small conversation. The bot asks whether you want **day** or **month** details:

- **Day**: send a number (`0` = today, `1` = yesterday, ...) or a date in `dd/mm/yyyy`
- **Month**: send a number `1`–`12` (`1` = January, ..., `12` = December; a month number
  higher than the current one refers to the previous year)

The bot replies with the number of trips and total km (and EV-only km) for that period.

## Keyboard

Run `/start` or `/help` to show a persistent keyboard. Every command is available as a
button, so you never have to type `/command`. Buttons that need input (`AC On`,
`Trips summary`) ask a follow-up question.

## Getting a Telegram bot token (step by step, no experience needed)

1. **Install Telegram** on your phone (or open https://web.telegram.org in a browser).
2. **Find the BotFather**: in the Telegram search bar, search for `@BotFather` and open
   the chat (it is the official bot-managing bot, with a blue checkmark).
3. **Create the bot**: send `/newbot` to BotFather.
4. **Pick a name** — anything you like, e.g. `My Toyota Bot`.
5. **Pick a username** — must end in `bot`, e.g. `my_toyota_bot`. If the username is taken,
   try another one.
6. **Copy the token**: BotFather replies with a message like:

   ```
   Done! Congratulations on your new bot.
   Use this token to access the HTTP API:
   123456789:AAHxyz...-token
   ```

   Copy the token string and paste it into `config.json` under `"telegram": { "token": ... }`.

7. **Find your own user ID**: in Telegram, open a chat with `@userinfobot` (or `@RawDataBot`)
   and send any message. It replies with your numeric ID (e.g. `123456789`). Put that number
   in `config.json` under `"allowed_user_ids"` so only you can control the bot.
8. **Start a chat with your bot**: search for the bot username you chose (e.g.
   `@my_toyota_bot`) and press **Start** (or send `/start`). The bot should answer with the
   help menu.
9. **Restart the bot** after any change to `config.json`:

   ```bash
   systemctl --user restart toyota-bot   # if installed with ./install.sh
   ```

## Timezone

The Toyota API returns UTC timestamps, so the bot converts every time it shows to a
single display timezone, resolved in this order:

1. `timezone` in `config.json` — optional override, wins over everything else.
2. The timezone saved in `data/data.json` via `/timezone` (or the *Set timezone* button).
3. The server's system timezone (auto-detected with `tzlocal`).

On a machine with the correct local timezone (e.g. `Europe/Rome`) no setup is needed.

`/timezone` starts a small conversation: share your location with the one-time button
(the coordinates are converted to a timezone **offline** with
[`timezonefinder`](https://github.com/jannikmi/timezonefinder) and never leave the
machine), or type a timezone name like `Europe/Rome`. Location buttons only work in a
private chat — in groups, type the name instead.

If the bot runs where the system timezone is wrong (a UTC server or Docker container),
either set `timezone` in `config.json` or use `/timezone` once — the choice persists in
`data/data.json` across restarts.

## Battery charge display (PHEV) — known issue and fix

This section explains why the bot's battery percentage can differ from the MyToyota app
and how the code handles it.

### The issue

The Telegram bot reported a **higher** battery charge percentage than the one shown in the
MyToyota app.

Measured on the car (a Toyota C-HR Plug-in Hybrid):

| Source | Battery % |
| ------ | --------- |
| Telegram bot (`/status`, `/charge`) | 82% |
| MyToyota app | 68% |

### Root cause

The API (pytoyoda) exposes the raw traction-battery **State of Charge (SOC)**, which is the
physical 0-100% figure and includes the reserves Toyota keeps locked away for hybrid/ICE
operation (engine starting, hybrid battery health protection).

Since app version >= 2.17, the MyToyota app deliberately no longer shows that raw SOC for
plug-in hybrids. Instead it displays the **EV-usable percentage**, i.e. the SOC minus the
reserved buffer, rescaled. So the app's value is *lower* than the raw API value.

This is a known upstream behaviour, tracked in the open issue `pytoyoda/ha_toyota#153`
("Wrong battery information for PHEV"), and the exact formula is not documented by Toyota.
Community measurements (RAV4/Lexus/C-HR PHEVs) consistently fit:

```
app% = (SOC - buffer) * factor
```

- `buffer` is the reserved SOC Toyota keeps for hybrid operation (~29-34% depending on model)
- `factor` rescales the remaining EV portion (typically 4/3)

The bot was displaying the raw `battery_level` verbatim from the
`/v1/global/remote/electric/status` endpoint, so it showed 82% where the app shows 68%.

### The fix

Added a helper `app_battery_pct(soc, car_type)` that converts the raw SOC to the
app-style EV-usable percentage:

- For non-PHEV vehicles it returns the raw value unchanged.
- For plug-in hybrids it applies `min(max((SOC - buffer) * factor, 0), 100)`.

The helper is used for the battery line in both `/status` (HV battery) and
`/charge` (Battery) commands.

The conversion parameters are configurable in `config.json` so the formula can be
recalibrated per vehicle:

```json
"battery": {
    "phev_ev_display": true,
    "soc_buffer": 31.0,
    "soc_factor": 1.3333
}
```

- `phev_ev_display`: set to `false` to show the raw SOC again.
- `soc_buffer`: reserved SOC subtracted (default `31.0`).
- `soc_factor`: scaling applied to the remaining EV portion (default `4/3`).

### Result

With the calibrated defaults, the bot reports the same value the app shows:

```
82%  ->  (82 - 31) * 1.3333  =  68%   (matches the app)
```

### Caveat

The app's exact formula is undocumented and the offset is **not a constant** across the
SOC range. The defaults above are calibrated to one measurement point (82 -> 68). If the
bot and app ever drift apart again, collect two data points (e.g. full charge and an
empty battery) and adjust `soc_buffer`/`soc_factor` in `config.json`.

## Disclaimer

This project is not affiliated with, or endorsed by, Toyota or Telegram. Use the remote
commands responsibly — they control a real vehicle.
