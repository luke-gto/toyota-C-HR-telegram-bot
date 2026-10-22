#!/usr/bin/env python3
import asyncio
import calendar
import json
import os
import re
import sys
from datetime import date, datetime, timedelta, timezone, tzinfo
from functools import wraps
from pathlib import Path
from typing import overload
from zoneinfo import ZoneInfo

from loguru import logger

# pytoyoda logs full HTTP request/response bodies (including the login password)
# and parsed data at DEBUG level; keep those out of the logs.
logger.remove()
logger.add(sys.stderr, level="INFO")

from pytoyoda.client import MyT
from pytoyoda.models.endpoints.climate import V2RemoteClimateControlRequestModel
from pytoyoda.models.endpoints.command import CommandType
from pytoyoda.models.endpoints.common import _MessagesModel, UnitValueModel
from pytoyoda.models.endpoints.electric import ChargeCommandType, NextChargeSettings
from pytoyoda.models.summary import SummaryType
from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
    Update,
)
from telegram.constants import ParseMode
from telegram.error import TelegramError
from telegram.ext import (
    Application,
    ApplicationHandlerStop,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    TypeHandler,
    filters,
)
from telegram.helpers import escape_markdown
from timezonefinder import TimezoneFinder
from tzlocal import get_localzone_name

HERE = Path(__file__).parent
CONFIG_FILE = HERE / "config.json"
DATA_FILE = HERE / "data" / "data.json"


def _load_config() -> dict:
    try:
        with open(CONFIG_FILE) as f:
            return json.load(f)
    except FileNotFoundError:
        logger.error(f"Config file not found: {CONFIG_FILE}")
        logger.error("Copy config.example.json to config.json and fill it in.")
        sys.exit(1)
    except json.JSONDecodeError as e:
        logger.error(f"Config file {CONFIG_FILE} is not valid JSON: {e}")
        sys.exit(1)


config = _load_config()

TOYOTA_CFG = config.get("toyota", {})
try:
    TOKEN = config["telegram"]["token"]
except (KeyError, TypeError):
    logger.error("Missing telegram.token in config.json")
    sys.exit(1)
ALLOWED_USERS = config.get("allowed_user_ids")
if not isinstance(ALLOWED_USERS, list) or len(ALLOWED_USERS) == 0:
    logger.error(
        "Refusing to start: 'allowed_user_ids' is missing or empty in config.json "
        "— set at least one allowed Telegram user ID."
    )
    sys.exit(1)

TEMP_WAIT = 1
PERIOD_WAIT = 2
RANGE_WAIT = 3
TZ_WAIT = 5

TEMP_MIN = 18
TEMP_MAX = 29

# Upper bounds for user-supplied ranges so a typo cannot request years of data.
MAX_TRIP_DAYS_AGO = 365
MAX_TRIP_RANGE_DAYS = 365
MAX_SCORE_DAYS = 90

# Keep each Telegram message safely below the 4096-character limit.
MSG_CHUNK = 3500

# Trip-summary inline button callbacks.
TS_DAY = "trips_day"
TS_MONTH = "trips_month"
TS_RANGE = "trips_range"
TS_BACK = "trips_back"

client: MyT | None = None
car = None
vin: str | None = None

# Protects the lazy login/vehicle fetch from concurrent first-command races.
_client_lock = asyncio.Lock()
# Serialises vehicle wakeups so several users cannot trigger simultaneous
# 12 V wakeups (Toyota's gateway rate-limits bursts with 429/APIGW-403).
_wake_lock = asyncio.Lock()
# Protects read-modify-write cycles on data/data.json.
_data_lock = asyncio.Lock()


# ── helpers ──────────────────────────────────────────────────────────


def esc(value) -> str:
    """Escape dynamic text for Telegram's legacy Markdown parse mode."""
    return escape_markdown(str(value), version=1)


class UserError(Exception):
    """An expected failure whose message is safe to show to the user."""


def load_data() -> dict:
    try:
        return json.loads(DATA_FILE.read_text())
    except FileNotFoundError:
        return {"notify_chats": [], "last_notification": None}
    except (json.JSONDecodeError, OSError) as e:
        logger.warning(f"data.json unreadable ({e}); starting from defaults")
        return {"notify_chats": [], "last_notification": None}


def save_data(data):
    """Atomically persist data.json so a crash cannot truncate it."""
    DATA_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = DATA_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, indent=2, default=str))
    os.replace(tmp, DATA_FILE)


def _resolve_timezone() -> tuple[tzinfo, str]:
    """Pick the display timezone: config.json > data.json (/timezone) > system."""
    candidates = [
        ("config.json", config.get("timezone")),
        ("data/data.json, set via /timezone", load_data().get("timezone")),
    ]
    try:
        candidates.append(("system", get_localzone_name()))
    except Exception:
        pass
    for source, name in candidates:
        if not name:
            continue
        try:
            return ZoneInfo(name), source
        except Exception:
            logger.error(f"Ignoring invalid timezone {name!r} from {source}")
    logger.warning("No usable timezone found, using the fixed local UTC offset")
    return datetime.now().astimezone().tzinfo or timezone.utc, "fixed local offset"


USER_TZ, TZ_SOURCE = _resolve_timezone()
logger.info(f"Using timezone {USER_TZ} ({TZ_SOURCE})")

_tz_finder: TimezoneFinder | None = None


def _find_timezone(lat: float, lng: float) -> str | None:
    global _tz_finder
    finder = _tz_finder
    if finder is None:
        finder = TimezoneFinder()
        _tz_finder = finder
    return finder.timezone_at(lat=lat, lng=lng)


async def access_guard(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Reject any update from a user not on the allowlist.

    Registered as a group=-1 TypeHandler so it runs before every other handler
    (including conversations) and stops further processing.
    """
    user = update.effective_user
    if user is None:
        return
    if user.id not in ALLOWED_USERS:
        logger.warning(f"Denied user {user.id} (@{user.username})")
        if update.effective_message:
            try:
                await update.effective_message.reply_text(
                    f"Access denied. Your ID: {user.id}"
                )
            except TelegramError:
                pass
        raise ApplicationHandlerStop


def handler(func):
    """Wrap a handler so unexpected errors are logged, not leaked to the user."""

    @wraps(func)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        try:
            return await func(update, context)
        except UserError as e:
            await _report_error(update, context, str(e))
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception(f"Handler {func.__name__} failed")
            await _report_error(
                update, context, "Something went wrong. Please try again."
            )
        finally:
            context.user_data.pop("_status", None)

    return wrapper


async def progress(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str):
    """Send a placeholder message and remember it for later edits/error reports."""
    msg = await update.effective_message.reply_text(text)
    context.user_data["_status"] = msg
    return msg


async def set_status(context: ContextTypes.DEFAULT_TYPE, text: str, **kwargs):
    """Edit the placeholder message started by ``progress``."""
    msg = context.user_data.get("_status")
    if msg is not None:
        await msg.edit_text(text, **kwargs)
    elif text:
        raise UserError(text)


async def _report_error(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str):
    msg = context.user_data.get("_status")
    if msg is not None:
        try:
            await msg.edit_text(text)
            return
        except TelegramError:
            pass
    if update.effective_message:
        try:
            await update.effective_message.reply_text(text)
        except TelegramError:
            logger.warning("Could not deliver error message to chat")


@overload
def user_dt(dt: datetime) -> datetime: ...
@overload
def user_dt(dt: None) -> None: ...
def user_dt(dt: datetime | None) -> datetime | None:
    """Convert an API timestamp (UTC, naive or aware) to the user's timezone."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(USER_TZ)


def _aware_utc(dt: datetime | None) -> datetime | None:
    """Normalise any timestamp to timezone-aware UTC (for safe comparisons)."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _today() -> date:
    return datetime.now(USER_TZ).date()


async def get_client() -> MyT:
    """Return the logged-in client, creating it once under a lock."""
    global client, car, vin
    if client is None:
        async with _client_lock:
            if client is None:
                c = MyT(
                    username=TOYOTA_CFG["username"],
                    password=TOYOTA_CFG["password"],
                    use_metric=True,
                    brand=TOYOTA_CFG.get("brand", "T"),
                )
                await c.login()
                vehicles = await c.get_vehicles()
                if not vehicles:
                    raise UserError("No vehicles found on the account.")
                client = c
                car = vehicles[0]
                vin = car.vin
    return client


async def get_car(only: list[str] | None = None):
    """Return the cached vehicle, optionally refreshing just ``only`` endpoints.

    Passing ``only`` avoids the full ``update()`` fan-out (~10 endpoints) that
    otherwise trips Toyota's burst rate limit; read handlers should always pass
    exactly the endpoints they need.
    """
    global car, vin
    await get_client()
    if car is None:
        vehicles = await client.get_vehicles()
        if not vehicles:
            raise UserError("No vehicles found on the account.")
        car = vehicles[0]
        vin = car.vin
    if only is not None:
        await car.update(only=only)
    return car


def _api():
    """Access the pytoyoda Api object.

    pytoyoda exposes no public accessor for a few calls the bot needs (health
    status, climate status/settings, the raw /v1/vehicle/status payload), so we
    reach for it in one place instead of scattering ``client._api`` around.
    """
    return client._api


def _electric_payload(vehicle):
    """Raw electric payload, needed for fuel level/range that the public
    ``Vehicle.dashboard``/``ElectricStatus`` properties do not expose for PHEVs.
    """
    raw = vehicle._endpoint_data.get("electric_status")
    return raw.payload if raw else None


async def refresh_lock_status(vehicle, attempts: int = 3, delay: float = 3.0):
    """Wake the vehicle and fetch fresh door/window status.

    POST refresh-status (the call the Toyota app makes) warms the gateway
    cache that GET /v1/vehicle/status reads; the endpoint otherwise returns a
    cached, stale value (or 429 APIGW-403).
    """
    before = vehicle.lock_status.last_updated if vehicle.lock_status else None
    async with _wake_lock:
        try:
            await vehicle.refresh_status()
        except Exception:
            logger.warning("refresh_status failed while fetching lock status")
        for _ in range(attempts):
            try:
                await vehicle.update(only=["status"])
            except Exception:
                await asyncio.sleep(delay)
                continue
            after = vehicle.lock_status.last_updated if vehicle.lock_status else None
            if after and (not before or after > before):
                break
            await asyncio.sleep(delay)
    return vehicle.lock_status


def _fmt(result):
    if result.message:
        return result.message
    if isinstance(result.status, _MessagesModel) and result.status.messages:
        return result.status.messages[0].description
    payload = getattr(result, "payload", None)
    if payload is not None:
        rc = getattr(payload, "return_code", None) or getattr(
            payload, "returnCode", None
        )
        if rc:
            return "ok" if rc == "000000" else f"return code {rc}"
    return str(result.status)


def app_battery_pct(soc: float | None, car_type: str | None) -> float | None:
    """Convert the API's raw traction-battery SOC to the value the app shows.

    For plug-in hybrids the API ``batteryLevel`` is the physical State of
    Charge (0-100%) and includes the reserves Toyota keeps locked away for
    hybrid/ICE operation. The MyToyota app (>= 2.17) instead displays the
    EV-usable percentage: (SOC - buffer) scaled up. The buffer and scaling
    factor are exposed in config.json under "battery" so the formula can be
    calibrated per vehicle (it varies slightly between models).
    """
    if soc is None:
        return None
    if car_type != "plug_in_hybrid":
        return soc
    cfg = config.get("battery", {})
    if not cfg.get("phev_ev_display", True):
        return soc
    buffer = cfg.get("soc_buffer", 31.0)
    factor = cfg.get("soc_factor", 4 / 3)
    value = (soc - buffer) * factor
    return min(max(value, 0.0), 100.0)


def _fuel_data(vehicle) -> tuple[float | None, float | None]:
    """Fuel percentage and fuel range, preferring the public dashboard."""
    dash = vehicle.dashboard if vehicle else None
    level = dash.fuel_level if dash else None
    rng = dash.fuel_range if dash else None
    if level is None or rng is None:
        payload = _electric_payload(vehicle)
        if payload is not None:
            if level is None:
                level = payload.fuel_level
            if rng is None and payload.fuel_range and payload.fuel_range.value:
                rng = payload.fuel_range.value
    return level, rng


def _simple_cmd(command_name: str, cmd_type: CommandType, beeps: int = 0):
    """Factory for simple one-shot remote commands."""
    @handler
    async def run(update: Update, context: ContextTypes.DEFAULT_TYPE):
        await progress(update, context, f"Executing {command_name}...")
        vehicle = await get_car()
        result = await vehicle.post_command(cmd_type, beeps=beeps)
        await set_status(context, f"{command_name}: {_fmt(result)}")

    return run


def _door_locked(vehicle, door: str = "driver") -> bool | None:
    """Whether a door is locked, None if unknown. door: "driver" or "trunk"."""
    doors = vehicle.lock_status.doors if vehicle.lock_status else None
    if doors is None:
        return None
    d = doors.driver_seat if door == "driver" else doors.trunk
    return d.locked if d else None


async def _verify_lock(vehicle, target: str, before_locked: bool | None,
                       door: str = "driver",
                       attempts: int = 6, delay: float = 5.0):
    """Wait for the car to confirm a lock/unlock command.

    ``return_code == "000000"`` on the command only means the server accepted
    the request; the real outcome shows up in the lock status reads. Polls the
    selected door's lock state until it matches the target.

    Returns ``(outcome, observed_locked)`` with outcome one of ``"ok"`` /
    ``"already"`` (was already in the target state) / ``"timeout"``.
    """
    want = target == "locked"
    for _ in range(attempts):
        try:
            await vehicle.update(only=["status"])
        except Exception:
            await asyncio.sleep(delay)
            continue
        current = _door_locked(vehicle, door)
        if current == want:
            if before_locked is None or before_locked != want:
                return "ok", current
            return "already", current
        await asyncio.sleep(delay)
    return "timeout", _door_locked(vehicle, door)


def _lock_outcome_text(target: str, outcome: str, noun: str = "doors"):
    verb = "locked" if target == "locked" else "unlocked"
    if outcome == "ok":
        return f"{noun.capitalize()} {verb} (confirmed by the car)."
    if outcome == "already":
        return f"The {noun} were already {verb}."
    return (
        f"The car did not confirm that the {noun} were {verb}. "
        "Check the app."
    )


# ── Keyboard ────────────────────────────────────────────────────────────


CONV_LABELS = {"AC On"}

BUTTONS = [
    ("Status", "status"),
    ("Hazards", "hazards_on"),
    ("Hazards off", "hazards_off"),
    ("Lock status", "lock_status"),
    ("Climate status", "climate_status"),
    ("AC On", None),
    ("AC Off", "ac_off"),
    ("Location", "location"),
    ("Lock", "lock"),
    ("Unlock", "unlock"),
    ("Buzzer", "buzzer"),
    ("Service history", "service_history"),
    ("Anomalies", "anomalies"),
    ("Trips today", "trips"),
    ("Trips summary", None),
    ("Last trip", "last_trip"),
    ("Scores", "scores"),
    ("Summary", "summary"),
    ("Notifications", "notifications"),
    ("Vehicles", "vehicles"),
    ("Trunk lock", "trunk_lock"),
    ("Trunk unlock", "trunk_unlock"),
    ("Refresh climate", "refresh_climate"),
    ("Charge now", "charge_now"),
    ("Refresh battery", "refresh_battery"),
    ("Wake", "wake"),
    ("Set timezone", None),
    ("Notify", "notify"),
    ("Stop notify", "unotify"),
    ("Help", "help"),
]

ALL_LABELS = [label for label, _ in BUTTONS]
ROUTER_LABELS = [label for label, cmd in BUTTONS if cmd]


def build_keyboard():
    rows = [ALL_LABELS[i : i + 3] for i in range(0, len(ALL_LABELS), 3)]
    return ReplyKeyboardMarkup(rows, resize_keyboard=True)


async def route_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cb = BUTTON_HANDLERS.get(update.message.text)
    if cb:
        await cb(update, context)


# ── /help ─────────────────────────────────────────────────────────────


HELP_TEXT = (
    "Toyota Bot — all commands\n\n"
    "Info\n"
    "/help  — show this menu\n"
    "/vehicles  — list vehicles\n"
    "/status  — fuel, battery, range\n"
    "/charge  — battery & charging detail\n"
    "/lock_status  — doors, windows, hood\n"
    "/climate_status  — AC state & target\n"
    "/location  — last parked location\n"
    "/health  — warning lights, oil\n"
    "/anomalies  — check for anomalies (warning lights + causes)\n"
    "/service_history  — dealer visits\n"
    "/trips [n]  — km driven n days ago (0 = today)\n"
    "/trip  — alias for /trips\n"
    "/trips_summary  — day/month buttons or a typed date range\n"
    "/last_trip  — last trip details\n"
    "/scores [days]  — driving scores\n"
    "/summary <d|w|m|y>  — consumption summary\n"
    "/notifications  — app notifications\n\n"
    "Remote commands (supported on this vehicle)\n"
    "/lock  /unlock  — doors\n"
    "/hazards_on  — hazard lights on\n"
    "/hazards_off  — hazard lights off\n"
    "/trunk_lock  /trunk_unlock\n"
    "/buzzer  — buzzer warning\n\n"
    "Climate\n"
    "/ac_on  — start AC (asks temp, auto-off in 20 min)\n"
    "/ac_off  — stop AC\n"
    "/climate <temp>  — set target temp (auto-off in 20 min)\n"
    "/refresh_climate  — refresh status\n\n"
    "Battery / Charging\n"
    "/charge_now  — charge immediately\n"
    "/refresh_battery  — refresh SOC\n\n"
    "System\n"
    "/wake  — wake vehicle\n"
    "/alias <name>  — set nickname\n"
    "/timezone  — set the bot's timezone (share location or type a name)\n"
    "/notify  — push notifications here\n"
    "/unotify  — stop notifications\n\n"
    "Tap the buttons below to run commands without typing."
)


@handler
async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(HELP_TEXT, reply_markup=build_keyboard())


# ── /start ────────────────────────────────────────────────────────────


@handler
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(HELP_TEXT, reply_markup=build_keyboard())


# ── Info commands ──────────────────────────────────────────────────────


@handler
async def cmd_vehicles(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await progress(update, context, "Fetching...")
    await get_client()
    vs = await client.get_vehicles()
    lines = [f"• {esc(v.alias or '—')} — {esc(v.type or '?')}" for v in vs]
    await set_status(context, "\n".join(lines) or "No vehicles.", parse_mode=ParseMode.MARKDOWN)


@handler
async def cmd_lock_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await progress(update, context, "Waking and refreshing lock status...")
    vehicle = await get_car()
    ls = await refresh_lock_status(vehicle)
    if not ls:
        return await set_status(context, "Lock status not available.")
    doors = ls.doors
    lines = [f"*{esc(vehicle.alias or 'C-HR')}* lock status"]
    if doors:
        add = [
            ("Driver", doors.driver_seat),
            ("Rear driver", doors.driver_rear_seat),
            ("Passenger", doors.passenger_seat),
            ("Rear pass.", doors.passenger_rear_seat),
            ("Trunk", doors.trunk),
        ]
        for label, door in add:
            if door is None:
                continue
            locked = door.locked
            closed = door.closed
            if locked is not None:
                lines.append(f"• {label}: {'locked' if locked else 'unlocked'}")
            elif closed is not None:
                lines.append(f"• {label}: {'closed' if closed else 'open'}")
    if ls.hood:
        h = ls.hood
        if h.locked is not None:
            lines.append(f"• Hood: {'locked' if h.locked else 'unlocked'}")
        elif h.closed is not None:
            lines.append(f"• Hood: {'closed' if h.closed else 'open'}")
    windows = ls.windows
    if windows:
        wlines = []
        for w in (windows.driver_seat, windows.driver_rear_seat,
                  windows.passenger_seat, windows.passenger_rear_seat):
            if w is not None and w.closed is not None:
                wlines.append("closed" if w.closed else "open")
        if wlines:
            lines.append(f"• Windows: {', '.join(wlines)}")
    if ls.last_updated:
        lines.append(f"_{user_dt(ls.last_updated).strftime('%d/%m %H:%M')}_")
    await set_status(context, "\n".join(lines), parse_mode=ParseMode.MARKDOWN)


@handler
async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await progress(update, context, "Waking vehicle and fetching fresh status...")
    vehicle = await get_car()
    await vehicle.update(only=["electric_status", "telemetry"])

    def fuel_seen() -> bool:
        level, rng = _fuel_data(vehicle)
        return bool((level or 0) > 0 or rng)

    es = vehicle.electric_status
    before = es.last_update_timestamp if es else None
    woke = False

    if before or not fuel_seen():
        async with _wake_lock:
            try:
                r = await vehicle.refresh_status()
                rc = getattr(r.payload, "return_code", None)
                woke = rc == "000000"
            except Exception:
                woke = False
            for _ in range(4):
                await asyncio.sleep(6)
                try:
                    await vehicle.update(only=["electric_status", "telemetry"])
                except Exception:
                    continue
                es = vehicle.electric_status
                after = es.last_update_timestamp if es else None
                if fuel_seen():
                    break
                if woke and after and (not before or after > before):
                    break
    logger.info(f"Status woke={woke} fresh={fuel_seen()}")

    dash = vehicle.dashboard
    es = vehicle.electric_status
    lines = [f"*{esc(vehicle.alias or 'C-HR')}*"]
    if dash and dash.odometer:
        lines.append(f"Odometer: {dash.odometer:,.0f} km")
    fuel_pct, fuel_range = _fuel_data(vehicle)
    if fuel_range:
        lines.append(f"Fuel range: {fuel_range:,.0f} km")
    if fuel_pct is not None:
        lines.append(f"Fuel: {fuel_pct}%")
    if es and es.battery_level is not None:
        pct = app_battery_pct(es.battery_level, vehicle.type)
        lines.append(f"HV battery: {pct:.0f}%")
    if es and es.ev_range:
        lines.append(f"EV range: {es.ev_range:,.0f} km")
    if es and es.ev_range_with_ac:
        lines.append(f"EV range (AC): {es.ev_range_with_ac:,.0f} km")
    if es and es.charging_status:
        lines.append(f"Charging: {esc(es.charging_status)}")
    if not fuel_seen():
        lines.append("_(data may be stale — wake failed)_")
    settings = vehicle.climate_settings
    if settings and settings.temperature:
        t = settings.temperature
        lines.append(f"Target: {t.value}°{esc(t.unit)}")
    await set_status(context, "\n".join(lines), parse_mode=ParseMode.MARKDOWN)


@handler
async def cmd_charge(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await progress(update, context, "Fetching battery & charging info...")
    vehicle = await get_car(only=["electric_status"])
    elec = vehicle.electric_status
    if not elec:
        return await set_status(context, "No electric status available.")
    lines = [f"*{esc(vehicle.alias or 'C-HR')}* — battery/charging"]
    if elec.battery_level is not None:
        pct = app_battery_pct(elec.battery_level, vehicle.type)
        lines.append(f"Battery: {pct:.0f}%")
    if elec.ev_range is not None:
        lines.append(f"EV range: {elec.ev_range:,.0f} km")
    if elec.charging_status:
        lines.append(f"Charging: {esc(elec.charging_status)}")
    if elec.remaining_charge_time is not None:
        lines.append(f"Charge done in: {elec.remaining_charge_time:.0f} min")
    if elec.ev_range_with_ac is not None:
        lines.append(f"EV range (AC on): {elec.ev_range_with_ac:,.0f} km")
    next_ev = elec.next_charging_event
    if next_ev and next_ev.timestamp:
        lines.append(
            f"Next charge: {esc(next_ev.event_type)} "
            f"{user_dt(next_ev.timestamp).strftime('%d/%m %H:%M')}"
        )
    schedules = elec.charging_schedules
    if schedules:
        lines.append(f"Schedules: {len(schedules)}")
        for s in schedules[:5]:
            days_on = [
                d for d in ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
                if getattr(s.days, d, False)
            ]
            days_str = ", ".join(d[:3] for d in days_on) if days_on else "?"
            start = f"{s.start_time.hour:02d}:{s.start_time.minute:02d}"
            end = None
            if s.end_time:
                end = f"{s.end_time.hour:02d}:{s.end_time.minute:02d}"
            range_str = f"{start}–{end}" if end else f"{start}"
            lines.append(f"• [{days_str}] {range_str}" + ("" if s.enabled else " (off)"))
    await set_status(context, "\n".join(lines), parse_mode=ParseMode.MARKDOWN)


@handler
async def cmd_climate_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await progress(update, context, "Fetching climate status...")
    await get_client()
    api = _api()
    status = await api.get_climate_status(vin)
    settings = await api.get_climate_settings(vin)

    lines = ["*Climate*"]
    p = status.payload
    if p:
        state = p.status or "unknown"
        lines.append(f"Active: {'yes' if state == 'running' else 'no'} ({esc(state)})")
        if p.duration:
            lines.append(f"Duration: {p.duration} min")
        if p.started_at:
            lines.append(f"Started: {user_dt(p.started_at):%d/%m %H:%M}")
        if p.current_temperature:
            cur = p.current_temperature
            lines.append(f"Cabin: {cur.value}°{esc(cur.unit)}")
        if p.target_temperature:
            tgt = p.target_temperature
            lines.append(f"Target: {tgt.value}°{esc(tgt.unit)}")
    s = settings.payload
    if s:
        if s.temperature:
            t = s.temperature
            lines.append(f"Temp: {t.value}°{esc(t.unit)}")
        if s.duration:
            lines.append(f"Settings duration: {s.duration} min")
    await set_status(context, "\n".join(lines), parse_mode=ParseMode.MARKDOWN)


@handler
async def cmd_location(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await progress(update, context, "Fetching location...")
    vehicle = await get_car(only=["location"])
    loc = vehicle.location
    if not loc or loc.latitude is None:
        return await set_status(context, "Location not available.")
    lat, lon = loc.latitude, loc.longitude
    maps = f"https://www.google.com/maps?q={lat},{lon}"
    text = f"📍 [{lat:.5f}, {lon:.5f}]({maps})"
    if loc.state:
        text += f"\n{esc(loc.state)}"
    if loc.timestamp:
        text += f"\n_{user_dt(loc.timestamp).strftime('%d/%m %H:%M')}_"
    await set_status(context, text, parse_mode=ParseMode.MARKDOWN,
                     disable_web_page_preview=True)


def _health_item_text(item) -> str:
    if isinstance(item, dict):
        for key in ("description", "code", "name", "message"):
            if item.get(key):
                return str(item[key])
        return " ".join(f"{k}={v}" for k, v in item.items() if v not in (None, ""))
    return str(item)


_STATUS_DOOR_LABELS = [
    ("driver", "Driver"),
    ("passenger", "Passenger"),
    ("rearLeft", "Rear left"),
    ("rearRight", "Rear right"),
    ("rearBack", "Trunk"),
]

_STATUS_WINDOW_LABELS = [
    ("driver", "Driver"),
    ("passenger", "Passenger"),
    ("rearLeft", "Rear left"),
    ("rearRight", "Rear right"),
]

_STATUS_LIGHT_LABELS = {
    "head": "Headlights",
    "tail": "Tail lights",
    "hazard": "Hazards",
}


def _api_ts(value: datetime | str | None) -> datetime | None:
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    return None


def _status_anomaly_lines(payload: dict) -> list[str]:
    """Precise causes of the warning count from a /v1/vehicle/status payload."""
    lines: list[str] = []
    doors = payload.get("doors") or {}
    unlocked: list[str] = []
    opened: list[str] = []
    for key, label in _STATUS_DOOR_LABELS:
        door = doors.get(key) or {}
        if (door.get("lockStatus") or {}).get("status") == "unlocked":
            unlocked.append(label)
        if (door.get("openStatus") or {}).get("status") == "open":
            opened.append(label)
    if ((doors.get("hood") or {}).get("openStatus") or {}).get("status") == "open":
        opened.append("Hood")
    if unlocked:
        lines.append(f"🔓 Unlocked: {', '.join(unlocked)}")
    if opened:
        lines.append(f"🚪 Open: {', '.join(opened)}")

    windows = payload.get("windows") or {}
    w_open = [
        label
        for key, label in _STATUS_WINDOW_LABELS
        if (windows.get(key) or {}).get("status") == "open"
    ]
    w_vent = [
        label
        for key, label in _STATUS_WINDOW_LABELS
        if (windows.get(key) or {}).get("status") == "venting"
    ]
    if w_open:
        lines.append(f"🪟 Windows open: {', '.join(w_open)}")
    if w_vent:
        lines.append(f"🪟 Windows venting: {', '.join(w_vent)}")

    lights = payload.get("lights") or {}
    on = [
        str(_STATUS_LIGHT_LABELS.get(key, str(key).title()))
        for key, val in lights.items()
        if (val or {}).get("status") == "on"
    ]
    if on:
        lines.append(f"💡 Lights on: {', '.join(on)}")

    rear = payload.get("rearSeatReminder") or {}
    if rear.get("warning") is True or rear.get("reason") not in (
        None,
        "",
        "notDetected",
    ):
        lines.append("🪑 Rear seat reminder")
    return lines


def _anomalies_text(title: str, health, status: dict) -> str:
    """Build the /anomalies report from health and raw status payloads."""
    lines = [f"*{esc(title)}* — anomalies"]
    details: list[str] = []
    has_anomaly = False

    if health:
        warnings = health.warning or []
        if warnings:
            has_anomaly = True
            details.append(f"⚠️ Dashboard warnings: {len(warnings)}")
            for w in warnings[:10]:
                details.append(f"  • {esc(_health_item_text(w))}")
        oil = health.quantity_of_eng_oil_icon or []
        if oil:
            has_anomaly = True
            details.append(
                "⚠️ Engine oil: " + ", ".join(esc(_health_item_text(o)) for o in oil)
            )

    component = _status_anomaly_lines(status)
    if component:
        has_anomaly = True
        details.extend(component)

    count = status.get("overallWarningCounts")
    grouped = status.get("groupedWarningCount")
    overall = status.get("overallStatus")
    if count is not None and count > 0:
        has_anomaly = True
        grouped_txt = f" in {grouped} group(s)" if grouped else ""
        details.append(
            f"⚠️ {count} vehicle status warning(s){grouped_txt} "
            f"(overall: {esc(overall or '?')})"
        )
    elif overall and overall.lower() not in ("ok", "normal", "no_warning", ""):
        has_anomaly = True
        details.append(f"⚠️ Overall status: {esc(overall)}")

    if has_anomaly:
        lines.append("Anomalies found:")
        lines.extend(details)
    else:
        lines.append("✅ No anomalies detected — all systems normal.")
        lines.extend(details)

    if health and health.wng_last_upd_time:
        health_ts = user_dt(health.wng_last_upd_time).strftime("%d/%m %H:%M")
        lines.append(f"_Health updated: {health_ts}_")
    status_ts = _api_ts(status.get("lastUpdateTimestamp"))
    if status_ts:
        lines.append(f"_Status updated: {user_dt(status_ts).strftime('%d/%m %H:%M')}_")
    return "\n".join(lines)


@handler
async def cmd_health(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await progress(update, context, "Fetching health...")
    await get_client()
    api = _api()
    h = await api.get_vehicle_health_status(vin)
    if not h.payload:
        return await set_status(context, "No health data.")
    p = h.payload
    lines = ["*Vehicle health*"]
    warnings = p.warning or []
    if warnings:
        lines.append("Warnings:")
        for w in warnings:
            lines.append(f"⚠ {esc(_health_item_text(w))}")
    else:
        lines.append("Warnings: none")
    oil = p.quantity_of_eng_oil_icon or []
    if oil:
        lines.append("Engine oil: " + ", ".join(esc(_health_item_text(o)) for o in oil))
    else:
        lines.append("Engine oil: OK")
    if p.wng_last_upd_time:
        lines.append(f"_{user_dt(p.wng_last_upd_time).strftime('%d/%m %H:%M')}_")
    await set_status(context, "\n".join(lines), parse_mode=ParseMode.MARKDOWN)


@handler
async def cmd_anomalies(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await progress(update, context, "Checking for anomalies...")
    vehicle = await get_car()
    title = vehicle.alias or "C-HR"
    await set_status(context, "Waking the car, this can take up to 30 seconds...")
    await refresh_lock_status(vehicle)
    await get_client()
    api = _api()
    health, remote_status = await asyncio.gather(
        api.get_vehicle_health_status(vin),
        api.controller.request_json("GET", "/v1/vehicle/status", vin=vin),
    )
    payload = (remote_status or {}).get("payload") or {}
    text = _anomalies_text(title, health.payload if health else None, payload)
    await set_status(context, text, parse_mode=ParseMode.MARKDOWN)


@handler
async def cmd_service_history(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await progress(update, context, "Fetching service history...")
    vehicle = await get_car(only=["service_history"])
    history = vehicle.service_history
    if not history:
        return await set_status(context, "No service history.")
    lines = []
    for s in history[:10]:
        date_str = s.service_date.strftime("%d/%m/%Y") if s.service_date else "?"
        dealer = s.servicing_dealer or ""
        cat = s.service_category or ""
        lines.append(f"• {date_str} — {esc(cat)} {esc(dealer)}")
    await set_status(context, "\n".join(lines))


@handler
async def cmd_last_trip(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await progress(update, context, "Fetching last trip...")
    vehicle = await get_car()
    trip = await vehicle.get_last_trip()
    if not trip:
        return await set_status(context, "No trip data.")
    dur = trip.duration
    dur_str = f"{dur.seconds // 60} min" if dur else "?"
    lines = [
        "*Last trip*",
        f"Distance: {trip.distance or '?'} km",
        f"Duration: {dur_str}",
    ]
    avg_fuel = trip.average_fuel_consumed
    if avg_fuel:
        lines.append(f"Avg fuel: {avg_fuel} l/100km")
    if trip.ev_distance and trip.ev_distance > 0:
        lines.append(f"EV distance: {trip.ev_distance:.1f} km")
    if trip.score:
        lines.append(f"Score: {trip.score:.0f}/100")
    if trip.start_time:
        lines.append(f"Start: {user_dt(trip.start_time):%d/%m/%Y %H:%M}")
    if trip.end_time:
        lines.append(f"End: {user_dt(trip.end_time):%d/%m/%Y %H:%M}")
    loc = trip.locations
    if loc and loc.start:
        start_gm = f"https://www.google.com/maps?q={loc.start.lat},{loc.start.lon}"
        lines.append(f"[Start map]({start_gm})")
    await set_status(context, "\n".join(lines), parse_mode=ParseMode.MARKDOWN)


def _day_key(st: datetime) -> date:
    return user_dt(st).date()


async def _summarize_day(vehicle, target: date) -> str | None:
    trips = await vehicle.get_trips(target - timedelta(days=1), target + timedelta(days=1))
    if not trips:
        return None
    total = ev_total = 0.0
    count = 0
    for t in trips:
        st = t.start_time
        if st is None or t.distance is None:
            continue
        if _day_key(st) != target:
            continue
        total += t.distance
        ev = getattr(t, "ev_distance", None)
        if ev:
            ev_total += ev
        count += 1
    if count == 0:
        return None
    lines = [f"*{target:%A %d/%m}* — {count} trip{'s' if count != 1 else ''}"]
    lines.append(f"Total: {total:.1f} km")
    if ev_total > 0:
        lines.append(f"EV: {ev_total:.1f} km")
    return "\n".join(lines)


async def _summarize_month(vehicle, target: date) -> str | None:
    first = date(target.year, target.month, 1)
    last = date(
        target.year, target.month,
        calendar.monthrange(target.year, target.month)[1],
    )
    trips = await vehicle.get_trips(first - timedelta(days=1), last + timedelta(days=1))
    if not trips:
        return None
    total = ev_total = 0.0
    count = 0
    for t in trips:
        st = t.start_time
        if st is None or t.distance is None:
            continue
        day = _day_key(st)
        if (day.year, day.month) != (target.year, target.month):
            continue
        total += t.distance
        ev = getattr(t, "ev_distance", None)
        if ev:
            ev_total += ev
        count += 1
    if count == 0:
        return None
    lines = [f"*{target:%B %Y}* — {count} trip{'s' if count != 1 else ''}"]
    lines.append(f"Total: {total:.1f} km")
    if ev_total > 0:
        lines.append(f"EV: {ev_total:.1f} km")
    return "\n".join(lines)


@handler
async def cmd_trips(update: Update, context: ContextTypes.DEFAULT_TYPE):
    days_ago = 0
    if context.args:
        try:
            days_ago = int(context.args[0])
        except ValueError:
            return await update.message.reply_text(
                "Usage: /trips <days ago>  (e.g. 0 = today, 1 = yesterday)"
            )
        days_ago = max(0, min(days_ago, MAX_TRIP_DAYS_AGO))
    target = _today() - timedelta(days=days_ago)
    await progress(update, context, f"Fetching trips for {target:%d/%m/%Y}...")
    vehicle = await get_car()
    text = await _summarize_day(vehicle, target)
    await set_status(context, text or f"No trips on {target:%d/%m/%Y}.")


# ── /trips_summary conversation ────────────────────────────────────────


RANGE_PROMPT = (
    "Send the date range as dd/mm/yyyy - dd/mm/yyyy\n"
    "Example: 01/10/2026 - 15/10/2026\n"
    f"Maximum {MAX_TRIP_RANGE_DAYS} days."
)

_RANGE_RE = re.compile(
    r"^\s*(\d{1,2}/\d{1,2}/\d{4})\s*(?:-|–|to|,)\s*(\d{1,2}/\d{1,2}/\d{4})\s*$",
    re.IGNORECASE,
)


def _back_button() -> InlineKeyboardButton:
    return InlineKeyboardButton("Back", callback_data=TS_BACK)


def _trips_summary_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[
            InlineKeyboardButton("Day", callback_data=TS_DAY),
            InlineKeyboardButton("Month", callback_data=TS_MONTH),
            InlineKeyboardButton("Date range", callback_data=TS_RANGE),
        ]]
    )


def _day_picker_keyboard(today: date | None = None) -> InlineKeyboardMarkup:
    today = today or _today()
    buttons = []
    for days_ago in range(7):
        target = today - timedelta(days=days_ago)
        if days_ago == 0:
            label = "Today"
        elif days_ago == 1:
            label = "Yesterday"
        else:
            label = f"{target:%a %d/%m}"
        buttons.append(
            InlineKeyboardButton(
                label, callback_data=f"{TS_DAY}:{target.isoformat()}"
            )
        )
    rows = [buttons[i : i + 3] for i in range(0, len(buttons), 3)]
    rows.append([_back_button()])
    return InlineKeyboardMarkup(rows)


def _add_months(first: date, months: int) -> date:
    total = first.month - 1 + months
    year = first.year + total // 12
    month = total % 12 + 1
    return date(year, month, 1)


def _month_picker_keyboard(today: date | None = None) -> InlineKeyboardMarkup:
    today = today or _today()
    first = today.replace(day=1)
    buttons = []
    for months_ago in range(12):
        target = _add_months(first, -months_ago)
        buttons.append(
            InlineKeyboardButton(
                f"{target:%B %Y}",
                callback_data=f"{TS_MONTH}:{target:%Y-%m}",
            )
        )
    rows = [buttons[i : i + 3] for i in range(0, len(buttons), 3)]
    rows.append([_back_button()])
    return InlineKeyboardMarkup(rows)


def _parse_range_input(text: str) -> tuple[date | None, date | None, str | None]:
    match = _RANGE_RE.match(text)
    if not match:
        return None, None, "Send the range as dd/mm/yyyy - dd/mm/yyyy."
    try:
        start = datetime.strptime(match.group(1), "%d/%m/%Y").date()
        end = datetime.strptime(match.group(2), "%d/%m/%Y").date()
    except ValueError:
        return None, None, "Those dates are not valid. Use dd/mm/yyyy - dd/mm/yyyy."
    if start > end:
        return None, None, "The start date must be before or equal to the end date."
    if end > _today():
        return None, None, "The end date cannot be in the future."
    if (end - start).days + 1 > MAX_TRIP_RANGE_DAYS:
        return None, None, f"Ranges are limited to {MAX_TRIP_RANGE_DAYS} days."
    return start, end, None


def _build_range_summary(trips, start: date, end: date) -> str:
    by_day: dict[date, list[float]] = {}
    for trip in trips or []:
        started = trip.start_time
        if started is None or trip.distance is None:
            continue
        day = _day_key(started)
        if day < start or day > end:
            continue
        totals = by_day.setdefault(day, [0.0, 0.0, 0])
        totals[0] += trip.distance
        ev = getattr(trip, "ev_distance", None)
        if ev:
            totals[1] += ev
        totals[2] += 1

    lines = [f"*Trips {start:%d/%m/%Y} – {end:%d/%m/%Y}*"]
    total = ev_total = 0.0
    current = start
    while current <= end:
        distance, ev, count = by_day.get(current, (0.0, 0.0, 0))
        total += distance
        ev_total += ev
        suffix = f" ({count} trip{'s' if count != 1 else ''})" if count else ""
        lines.append(f"• {current:%a %d/%m} — {distance:.1f} km{suffix}")
        current += timedelta(days=1)
    lines.append(f"Total: {total:.1f} km")
    if ev_total > 0:
        lines.append(f"EV: {ev_total:.1f} km")
    return "\n".join(lines)


async def _summarize_range(vehicle, start: date, end: date) -> str:
    trips = await vehicle.get_trips(start - timedelta(days=1), end + timedelta(days=1))
    return _build_range_summary(trips, start, end)


def _split_message(text: str, limit: int = MSG_CHUNK) -> list[str]:
    """Split text on line boundaries so each chunk fits a Telegram message."""
    if not text:
        return [""]
    if len(text) <= limit:
        return [text]
    chunks = []
    current = ""
    for line in text.splitlines():
        candidate = f"{current}\n{line}" if current else line
        if len(candidate) <= limit:
            current = candidate
            continue
        if current:
            chunks.append(current)
        while len(line) > limit:
            chunks.append(line[:limit])
            line = line[limit:]
        current = line
    if current:
        chunks.append(current)
    return chunks


async def _send_long(update, context, text: str, **kwargs):
    chunks = _split_message(text)
    await set_status(context, chunks[0], **kwargs)
    for chunk in chunks[1:]:
        await update.effective_message.reply_text(chunk, **kwargs)


@handler
async def cmd_trips_summary(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Trip summary — pick a period:",
        reply_markup=_trips_summary_keyboard(),
    )
    return PERIOD_WAIT


async def _show_callback_day(query, target: date):
    await query.edit_message_text(f"Fetching trips for {target:%d/%m/%Y}...")
    vehicle = await get_car()
    text = await _summarize_day(vehicle, target) or f"No trips on {target:%d/%m/%Y}."
    await query.edit_message_text(text, parse_mode=ParseMode.MARKDOWN)


async def _show_callback_month(query, target: date):
    await query.edit_message_text(f"Fetching trips for {target:%B %Y}...")
    vehicle = await get_car()
    text = await _summarize_month(vehicle, target) or f"No trips in {target:%B %Y}."
    await query.edit_message_text(text, parse_mode=ParseMode.MARKDOWN)


@handler
async def receive_trips_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data or ""

    if data == TS_DAY:
        await query.edit_message_text(
            "Pick a day:", reply_markup=_day_picker_keyboard()
        )
        return PERIOD_WAIT
    if data == TS_MONTH:
        await query.edit_message_text(
            "Pick a month:", reply_markup=_month_picker_keyboard()
        )
        return PERIOD_WAIT
    if data == TS_RANGE:
        await query.edit_message_text(
            RANGE_PROMPT,
            reply_markup=InlineKeyboardMarkup([[_back_button()]]),
        )
        return RANGE_WAIT
    if data == TS_BACK:
        await query.edit_message_text(
            "Trip summary — pick a period:",
            reply_markup=_trips_summary_keyboard(),
        )
        return PERIOD_WAIT

    if data.startswith(f"{TS_DAY}:"):
        try:
            target = date.fromisoformat(data.split(":", 1)[1])
        except ValueError:
            await query.edit_message_text("Invalid day selection.")
            return ConversationHandler.END
        await _show_callback_day(query, target)
        return ConversationHandler.END

    if data.startswith(f"{TS_MONTH}:"):
        try:
            target = date.fromisoformat(f"{data.split(':', 1)[1]}-01")
        except ValueError:
            await query.edit_message_text("Invalid month selection.")
            return ConversationHandler.END
        await _show_callback_month(query, target)
        return ConversationHandler.END

    return ConversationHandler.END


@handler
async def receive_range(update: Update, context: ContextTypes.DEFAULT_TYPE):
    start, end, err = _parse_range_input(update.message.text or "")
    if err or start is None or end is None:
        await update.message.reply_text(f"{err}\n\n{RANGE_PROMPT}")
        return RANGE_WAIT
    await progress(
        update, context, f"Fetching trips {start:%d/%m/%Y} – {end:%d/%m/%Y}..."
    )
    vehicle = await get_car()
    text = await _summarize_range(vehicle, start, end)
    await _send_long(update, context, text, parse_mode=ParseMode.MARKDOWN)
    return ConversationHandler.END


@handler
async def cmd_scores(update: Update, context: ContextTypes.DEFAULT_TYPE):
    days = 7
    if context.args:
        try:
            days = int(context.args[0])
        except ValueError:
            pass
    days = max(1, min(days, MAX_SCORE_DAYS))
    await progress(update, context, f"Fetching driving scores ({days}d)...")
    vehicle = await get_car()
    from_date = _today() - timedelta(days=days)
    trips = await vehicle.get_trips(from_date, _today())
    if not trips:
        return await set_status(context, "No trips in period.")
    lines = [f"*Driving scores ({len(trips)} trips)*"]
    for t in trips[:15]:
        start = user_dt(t.start_time).strftime("%d/%m") if t.start_time else "?"
        score = t.score
        if score is not None:
            lines.append(f"• {start} — overall {score:.0f}/100")
        else:
            lines.append(f"• {start} — no score")
    await set_status(context, "\n".join(lines), parse_mode=ParseMode.MARKDOWN)


@handler
async def cmd_summary(update: Update, context: ContextTypes.DEFAULT_TYPE):
    typ_map = {"d": SummaryType.DAILY, "w": SummaryType.WEEKLY,
               "m": SummaryType.MONTHLY, "y": SummaryType.YEARLY}
    arg = context.args[0] if context.args else None
    st = typ_map.get(arg, SummaryType.MONTHLY)
    label = {"d": "Daily", "w": "Weekly", "m": "Monthly", "y": "Yearly"}.get(
        arg, "Monthly"
    )
    await progress(update, context, f"Fetching {label} summary...")
    vehicle = await get_car()
    days = 365 if st == SummaryType.YEARLY else 90
    s = await vehicle.get_summary(
        _today() - timedelta(days=days), _today(), summary_type=st
    )
    if not s:
        return await set_status(context, "No summary data.")
    lines = [f"*{label} summary*"]
    for item in s[:12]:
        start = item.from_date.strftime("%d/%m") if item.from_date else "?"
        end = item.to_date.strftime("%d/%m") if item.to_date else "?"
        lines.append(f"• {start}-{end}: {item.distance or '?'} km")
    await set_status(context, "\n".join(lines), parse_mode=ParseMode.MARKDOWN)


# ── Remote command handlers ──────────────────────────────────────────


def _lock_cmd(command_name: str, cmd_type: CommandType, target: str,
              door: str = "driver", noun: str = "doors"):
    """Handler for lock/unlock that waits for vehicle confirmation."""
    @handler
    async def run(update: Update, context: ContextTypes.DEFAULT_TYPE):
        await progress(update, context, f"{command_name}...")
        vehicle = await get_car()
        try:
            await vehicle.update(only=["status"])
        except Exception:
            pass
        before = _door_locked(vehicle, door)
        result = await vehicle.post_command(cmd_type)
        payload = getattr(result, "payload", None)
        rc = getattr(payload, "return_code", None) or getattr(
            payload, "returnCode", None
        )
        if rc and rc != "000000":
            return await set_status(
                context, f"{command_name} failed (return code {rc})."
            )
        await set_status(
            context,
            f"{command_name} — waiting for the car to confirm (up to ~30 s)...",
        )
        outcome, _ = await _verify_lock(vehicle, target, before, door)
        await set_status(
            context, f"{command_name}: {_lock_outcome_text(target, outcome, noun)}"
        )

    return run


cmd_lock = _lock_cmd("Lock doors", CommandType.DOOR_LOCK, "locked")
cmd_unlock = _lock_cmd("Unlock doors", CommandType.DOOR_UNLOCK, "unlocked")
cmd_hazards_on = _simple_cmd("Hazards on", CommandType.HAZARD_ON)
cmd_hazards_off = _simple_cmd("Hazards off", CommandType.HAZARD_OFF)
cmd_trunk_lock = _lock_cmd("Trunk lock", CommandType.TRUNK_LOCK, "locked",
                           door="trunk", noun="trunk")
cmd_trunk_unlock = _lock_cmd("Trunk unlock", CommandType.TRUNK_UNLOCK, "unlocked",
                             door="trunk", noun="trunk")
cmd_buzzer = _simple_cmd("Buzzer warning", CommandType.BUZZER_WARNING)


# ── Climate ─────────────────────────────────────────────────────────────


def _climate_request(temp: float):
    return V2RemoteClimateControlRequestModel(
        command="start",
        temperature=UnitValueModel(value=temp, unit="C"),
        save_settings=True,
    )


async def _start_climate(update, context, temp: float):
    """Shared climate-start flow used by /ac_on and /climate."""
    await progress(update, context, f"Setting AC to {temp}°C and starting...")
    await get_client()
    api = _api()
    vehicle = await get_car()
    try:
        before = await api.get_climate_status(vin)
    except Exception:
        before = None
    before_started = getattr(getattr(before, "payload", None), "started_at", None)
    result = await vehicle.set_climate(_climate_request(temp))
    payload = getattr(result, "payload", None)
    rc = getattr(payload, "return_code", None)
    if rc and rc != "000000":
        return await set_status(context, f"AC command failed (return code {rc}).")
    await set_status(context, "Waiting for the car to confirm (up to ~60 s)...")
    outcome, state, started = await _verify_climate(api, "start", before_started)
    await set_status(context, _climate_outcome_text("start", temp, outcome, state, started))


@handler
async def cmd_ac_on(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "What temperature? (e.g. 22)\nReply a number or /cancel"
    )
    return TEMP_WAIT


@handler
async def receive_temp(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        temp = float(update.message.text)
    except ValueError:
        await update.message.reply_text("Send a valid number (e.g. 22)")
        return TEMP_WAIT
    if not TEMP_MIN <= temp <= TEMP_MAX:
        await update.message.reply_text(
            f"Temperature must be between {TEMP_MIN} and {TEMP_MAX}°C for this vehicle. "
            "Send another value or /cancel"
        )
        return TEMP_WAIT
    await _start_climate(update, context, temp)
    return ConversationHandler.END


@handler
async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Cancelled.")
    return ConversationHandler.END


async def _verify_climate(api, action: str, before_started=None,
                          attempts: int = 12, delay: float = 5.0):
    """Wait for the car to confirm a climate command.

    ``return_code == "000000"`` on the control command only means the server
    accepted the request; the real outcome shows up in the climate status reads:

    - start: confirmed only when status becomes ``running`` with a fresh
      ``started_at``. A rejected activation briefly shows ``starting`` and then
      reverts to ``stopped``; if it was already running, ``started_at`` stays
      unchanged (the new activation was not confirmed).
    - stop: confirmed when status becomes ``stopped``.

    Returns ``(outcome, state, started_at)`` with outcome one of
    ``"ok"`` / ``"already"`` (start while running, unchanged) /
    ``"reverted"`` (attempted then rejected) / ``"timeout"``.
    """
    saw_attempt = False
    state = None
    started_at = before_started
    for _ in range(attempts):
        status = await api.get_climate_status(vin)
        state = getattr(status.payload, "status", None)
        started_at = getattr(status.payload, "started_at", None) or started_at
        if action == "start":
            if state == "running":
                if before_started is None or started_at != before_started:
                    return "ok", state, started_at
                return "already", state, started_at
            if state == "starting":
                saw_attempt = True
            if state == "stopped" and saw_attempt:
                return "reverted", state, started_at
        else:  # stop
            if state == "stopped":
                return "ok", state, started_at
            if state == "stopping":
                saw_attempt = True
        await asyncio.sleep(delay)
    return "timeout", state, started_at


def _climate_outcome_text(action, temp, outcome, state, started):
    if action == "start":
        if outcome == "ok":
            # AC auto-offs after 20 min — tell the user when
            base = user_dt(started) or datetime.now(USER_TZ)
            off = base + timedelta(minutes=20)
            return (
                f"AC set to {temp}°C and started (confirmed by the car). "
                f"Will auto-off at {off.strftime('%H:%M %d/%m')} (~20 min)."
            )
        if outcome == "already":
            when = user_dt(started).strftime("%d/%m %H:%M") if started else "earlier"
            return (
                f"The AC was already running (since {when}) and the new activation was "
                "not confirmed — the engine may need to be on before a new AC activation. "
                "Check the app."
            )
        if outcome == "reverted":
            return (
                "The AC did not start — the car rejected the activation. The engine may "
                "need to be on before a new AC activation. Check the app."
            )
        return (
            f"The car did not confirm that the AC started (last state: {state}). "
            "The engine may need to be on before a new AC activation. Check the app."
        )
    if outcome == "ok":
        return "AC stopped (confirmed by the car)."
    return (
        f"The car did not confirm that the AC stopped (last state: {state}). "
        "Check the app."
    )


@handler
async def cmd_ac_off(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await progress(update, context, "Stopping AC...")
    await get_client()
    api = _api()
    vehicle = await get_car()
    result = await vehicle.set_climate(V2RemoteClimateControlRequestModel(command="stop"))
    payload = getattr(result, "payload", None)
    rc = getattr(payload, "return_code", None)
    if rc and rc != "000000":
        return await set_status(context, f"AC stop command failed (return code {rc}).")
    await set_status(context, "Waiting for the car to confirm (up to ~60 s)...")
    outcome, state, started = await _verify_climate(api, "stop")
    await set_status(context, _climate_outcome_text("stop", None, outcome, state, started))


@handler
async def cmd_climate(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        return await update.message.reply_text("Usage: /climate <temp>")
    try:
        temp = float(context.args[0])
    except ValueError:
        return await update.message.reply_text("Temperature must be a number")
    if not TEMP_MIN <= temp <= TEMP_MAX:
        return await update.message.reply_text(
            f"Temperature must be between {TEMP_MIN} and {TEMP_MAX}°C for this vehicle"
        )
    await _start_climate(update, context, temp)


@handler
async def cmd_refresh_climate(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await progress(update, context, "Refreshing climate status...")
    vehicle = await get_car()
    result = await vehicle.refresh_climate_status()
    await set_status(context, f"Climate refresh: {_fmt(result)}")


# ── Battery / Charging ──────────────────────────────────────────────────


@handler
async def cmd_charge_now(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await progress(update, context, "Setting charge now...")
    vehicle = await get_car()
    cmd = NextChargeSettings(command=ChargeCommandType.CHARGE_NOW)
    result = await vehicle.send_next_charging_command(cmd)
    await set_status(context, f"Charge now: {_fmt(result)}")


@handler
async def cmd_refresh_battery(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await progress(update, context, "Refreshing battery status (may drain 12V)...")
    vehicle = await get_car()
    result = await vehicle.refresh_electric_realtime_status()
    await set_status(context, f"Battery refresh: {_fmt(result)}")


# ── System ──────────────────────────────────────────────────────────────


@handler
async def cmd_wake(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await progress(update, context, "Waking vehicle...")
    vehicle = await get_car()
    result = await vehicle.refresh_status()
    await set_status(context, f"Wake: {_fmt(result)}")


@handler
async def cmd_alias(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        return await update.message.reply_text("Usage: /alias <name>")
    name = " ".join(context.args)
    await progress(update, context, f"Setting alias to {name}...")
    vehicle = await get_car()
    result = await vehicle.set_alias(name)
    await set_status(context, f"Alias set: {result}")


@handler
async def cmd_timezone(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if config.get("timezone"):
        await update.message.reply_text(
            f"Note: config.json sets 'timezone' to '{config['timezone']}' and "
            "takes priority over the choice made here."
        )
    location_kb = ReplyKeyboardMarkup(
        [[KeyboardButton("📍 Share my location", request_location=True)]],
        resize_keyboard=True,
        one_time_keyboard=True,
    )
    await update.message.reply_text(
        f"Current timezone: {USER_TZ} ({TZ_SOURCE}).\n"
        "Share your location with the button below, or type a timezone name "
        "like Europe/Rome (or /cancel).",
        reply_markup=location_kb,
    )
    return TZ_WAIT


async def _apply_timezone(update: Update, name: str) -> int:
    global USER_TZ, TZ_SOURCE
    USER_TZ = ZoneInfo(name)
    TZ_SOURCE = "data/data.json, set via /timezone"
    async with _data_lock:
        data = load_data()
        data["timezone"] = name
        save_data(data)
    logger.info(f"Timezone set to {name} via /timezone")
    await update.message.reply_text(
        f"Timezone set to {name} — current time: {datetime.now(USER_TZ):%H:%M %d/%m/%Y}",
        reply_markup=ReplyKeyboardRemove(),
    )
    return ConversationHandler.END


@handler
async def receive_tz_location(update: Update, context: ContextTypes.DEFAULT_TYPE):
    loc = update.message.location
    name = _find_timezone(loc.latitude, loc.longitude) if loc else None
    if not name:
        await update.message.reply_text(
            "Could not determine a timezone from that location. "
            "Type one instead, e.g. Europe/Rome (or /cancel)."
        )
        return TZ_WAIT
    return await _apply_timezone(update, name)


@handler
async def receive_tz_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    name = update.message.text.strip()
    try:
        ZoneInfo(name)
    except Exception:
        await update.message.reply_text(
            f"'{name}' is not a valid timezone name. Try e.g. Europe/Rome (or /cancel)."
        )
        return TZ_WAIT
    return await _apply_timezone(update, name)


# ── Notifications ────────────────────────────────────────────────────────


@handler
async def cmd_notify(update: Update, context: ContextTypes.DEFAULT_TYPE):
    data = load_data()
    chat_id = update.effective_chat.id
    if chat_id in data["notify_chats"]:
        await update.message.reply_text("Already registered.")
    else:
        data["notify_chats"].append(chat_id)
        save_data(data)
        await update.message.reply_text("You'll receive app notifications here.")


@handler
async def cmd_unotify(update: Update, context: ContextTypes.DEFAULT_TYPE):
    data = load_data()
    chat_id = update.effective_chat.id
    if chat_id not in data["notify_chats"]:
        await update.message.reply_text("Notifications are not active for this chat.")
    else:
        data["notify_chats"].remove(chat_id)
        save_data(data)
        await update.message.reply_text("Notifications deactivated for this chat.")


@handler
async def cmd_notifications(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await progress(update, context, "Fetching notifications...")
    vehicle = await get_car(only=["notifications"])
    notifs = vehicle.notifications
    if not notifs:
        return await set_status(context, "No notifications.")
    lines = []
    for n in notifs[:10]:
        ds = user_dt(n.date).strftime("%d/%m %H:%M") if n.date else "?"
        lines.append(f"• [{ds}] {esc(n.message or n.category or n.type)}")
    await set_status(context, "\n".join(lines))


async def poll_notifications(context: ContextTypes.DEFAULT_TYPE):
    try:
        vehicle = await get_car(only=["notifications"])
        notifs = vehicle.notifications
        if not notifs:
            return

        async with _data_lock:
            data = load_data()
            last_str = data.get("last_notification")
            last_dt = (
                _aware_utc(datetime.fromisoformat(last_str))
                if last_str
                else None
            ) or datetime.min.replace(tzinfo=timezone.utc)

            new = [n for n in notifs if n.date and _aware_utc(n.date) > last_dt]
            if not new:
                return

            data["last_notification"] = max(_aware_utc(n.date) for n in new).isoformat()
            save_data(data)

        for n in new:
            ds = user_dt(n.date).strftime("%d/%m %H:%M") if n.date else "?"
            text = f"🚗 *{esc(n.category or 'Notification')}*\n{ds}\n{esc(n.message or '—')}"
            for cid in data.get("notify_chats", []):
                try:
                    await context.bot.send_message(
                        chat_id=cid, text=text, parse_mode=ParseMode.MARKDOWN
                    )
                except TelegramError as e:
                    logger.warning(f"Notify send fail to {cid}: {e}")
    except UserError as e:
        logger.warning(f"Notification poll skipped: {e}")
    except Exception:
        logger.exception("Notification poll failed")


async def post_init(app: Application):
    app.job_queue.run_repeating(poll_notifications, interval=300, first=30)
    logger.info("Notification polling started (every 5 min)")
    try:
        await get_client()
        logger.info(f"Logged in; controlling VIN {vin}")
    except UserError as e:
        logger.error(f"Initial login failed: {e}")
    except Exception:
        logger.exception("Initial login failed; will retry on demand")


async def post_shutdown(app: Application):
    if client is not None:
        try:
            await client.aclose()
        except Exception:
            logger.warning("Error closing Toyota client")


# ── main ────────────────────────────────────────────────────────────────


HANDLERS = {
    "start": cmd_start, "help": cmd_help,
    "vehicles": cmd_vehicles, "status": cmd_status, "charge": cmd_charge,
    "lock_status": cmd_lock_status, "climate_status": cmd_climate_status,
    "location": cmd_location, "health": cmd_health, "anomalies": cmd_anomalies,
    "service_history": cmd_service_history, "trips": cmd_trips,
    "last_trip": cmd_last_trip, "scores": cmd_scores, "summary": cmd_summary,
    "notifications": cmd_notifications, "lock": cmd_lock, "unlock": cmd_unlock,
    "hazards_on": cmd_hazards_on, "hazards_off": cmd_hazards_off,
    "trunk_lock": cmd_trunk_lock,
    "trunk_unlock": cmd_trunk_unlock, "buzzer": cmd_buzzer,
    "ac_off": cmd_ac_off, "climate": cmd_climate,
    "refresh_climate": cmd_refresh_climate, "charge_now": cmd_charge_now,
    "refresh_battery": cmd_refresh_battery, "wake": cmd_wake, "alias": cmd_alias,
    "notify": cmd_notify, "unotify": cmd_unotify,
}

BUTTON_HANDLERS = {label: HANDLERS[cmd] for label, cmd in BUTTONS if cmd}


def main():
    app = (
        Application.builder()
        .token(TOKEN)
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .build()
    )

    # Access control runs first for every update.
    app.add_handler(TypeHandler(Update, access_guard), group=-1)

    ac_conv = ConversationHandler(
        entry_points=[
            CommandHandler("ac_on", cmd_ac_on),
            MessageHandler(filters.Text(sorted(CONV_LABELS)), cmd_ac_on),
        ],
        states={
            TEMP_WAIT: [
                MessageHandler(
                    filters.TEXT & ~filters.COMMAND & filters.Text(sorted(CONV_LABELS)),
                    cmd_ac_on,
                ),
                MessageHandler(
                    filters.TEXT & ~filters.COMMAND & ~filters.Text(ALL_LABELS),
                    receive_temp,
                ),
            ],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        conversation_timeout=600,
    )

    trips_conv = ConversationHandler(
        entry_points=[
            CommandHandler("trips_summary", cmd_trips_summary),
            MessageHandler(filters.Text("Trips summary"), cmd_trips_summary),
            CallbackQueryHandler(receive_trips_callback, pattern=r"^trips_"),
        ],
        states={
            PERIOD_WAIT: [
                CallbackQueryHandler(receive_trips_callback, pattern=r"^trips_")
            ],
            RANGE_WAIT: [
                CallbackQueryHandler(receive_trips_callback, pattern=r"^trips_"),
                MessageHandler(
                    filters.TEXT & ~filters.COMMAND & ~filters.Text(ALL_LABELS),
                    receive_range,
                ),
            ],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        conversation_timeout=600,
    )

    tz_conv = ConversationHandler(
        entry_points=[
            CommandHandler("timezone", cmd_timezone),
            MessageHandler(filters.Text("Set timezone"), cmd_timezone),
        ],
        states={
            TZ_WAIT: [
                MessageHandler(filters.LOCATION, receive_tz_location),
                MessageHandler(
                    filters.TEXT & ~filters.COMMAND & ~filters.Text(ALL_LABELS),
                    receive_tz_text,
                ),
            ],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        conversation_timeout=600,
    )

    app.add_handler(ac_conv)
    app.add_handler(trips_conv)
    app.add_handler(tz_conv)
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))

    # Info
    app.add_handler(CommandHandler("vehicles", cmd_vehicles))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("charge", cmd_charge))
    app.add_handler(CommandHandler("lock_status", cmd_lock_status))
    app.add_handler(CommandHandler("climate_status", cmd_climate_status))
    app.add_handler(CommandHandler("location", cmd_location))
    app.add_handler(CommandHandler("health", cmd_health))
    app.add_handler(CommandHandler("anomalies", cmd_anomalies))
    app.add_handler(CommandHandler("service_history", cmd_service_history))
    app.add_handler(CommandHandler("trips", cmd_trips))
    app.add_handler(CommandHandler("trip", cmd_trips))
    app.add_handler(CommandHandler("last_trip", cmd_last_trip))
    app.add_handler(CommandHandler("scores", cmd_scores))
    app.add_handler(CommandHandler("summary", cmd_summary))
    app.add_handler(CommandHandler("notifications", cmd_notifications))

    # Remote commands
    app.add_handler(CommandHandler("lock", cmd_lock))
    app.add_handler(CommandHandler("unlock", cmd_unlock))
    app.add_handler(CommandHandler("hazards_on", cmd_hazards_on))
    app.add_handler(CommandHandler("hazards_off", cmd_hazards_off))
    app.add_handler(CommandHandler("trunk_lock", cmd_trunk_lock))
    app.add_handler(CommandHandler("trunk_unlock", cmd_trunk_unlock))
    app.add_handler(CommandHandler("buzzer", cmd_buzzer))

    # Climate
    app.add_handler(CommandHandler("ac_off", cmd_ac_off))
    app.add_handler(CommandHandler("climate", cmd_climate))
    app.add_handler(CommandHandler("refresh_climate", cmd_refresh_climate))

    # Battery / Charging
    app.add_handler(CommandHandler("charge_now", cmd_charge_now))
    app.add_handler(CommandHandler("refresh_battery", cmd_refresh_battery))

    # System
    app.add_handler(CommandHandler("wake", cmd_wake))
    app.add_handler(CommandHandler("alias", cmd_alias))
    app.add_handler(CommandHandler("notify", cmd_notify))
    app.add_handler(CommandHandler("unotify", cmd_unotify))

    # Keyboard buttons (exact text match; conversation labels handled above)
    app.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND & filters.Text(ROUTER_LABELS),
            route_button,
        )
    )

    logger.info("Bot started, polling...")
    app.run_polling()


if __name__ == "__main__":
    main()
