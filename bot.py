#!/usr/bin/env python3
import asyncio
import calendar
import json
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from loguru import logger

# pytoyoda logs full HTTP request/response bodies (including the login password)
# and parsed data at DEBUG level; keep those out of the logs.
logger.remove()
logger.add(sys.stderr, level="INFO")

from pytoyoda.client import MyT
from pytoyoda.models.endpoints.climate import V2RemoteClimateControlRequestModel
from pytoyoda.models.endpoints.command import CommandType
from pytoyoda.models.endpoints.common import _MessagesModel, UnitValueModel
from pytoyoda.models.endpoints.electric import (
    ChargeCommandType,
    NextChargeSettings,
)
from pytoyoda.models.summary import SummaryType
from telegram import ReplyKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CommandHandler,
    ConversationHandler,
    MessageHandler,
    filters,
    ContextTypes,
)

HERE = Path(__file__).parent
CONFIG_FILE = HERE / "config.json"
DATA_FILE = HERE / "data" / "data.json"

with open(CONFIG_FILE) as f:
    config = json.load(f)

TOYOTA_CFG = config["toyota"]
TOKEN = config["telegram"]["token"]
ALLOWED_USERS = config.get("allowed_user_ids", [])

TEMP_WAIT = 1
PERIOD_WAIT = 2
DAY_WAIT = 3
MONTH_WAIT = 4

client: MyT | None = None
vin: str | None = None


# ── helpers ──────────────────────────────────────────────────────────


def load_data():
    if DATA_FILE.exists():
        return json.loads(DATA_FILE.read_text())
    return {"notify_chats": [], "last_notification": None}


def save_data(data):
    DATA_FILE.parent.mkdir(parents=True, exist_ok=True)
    DATA_FILE.write_text(json.dumps(data, indent=2, default=str))


def check_allowed(update: Update) -> int | None:
    uid = update.effective_user.id
    if ALLOWED_USERS and uid not in ALLOWED_USERS:
        return uid
    return None


async def ensure_client():
    global client, vin
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
            raise RuntimeError("No vehicles found on account")
        client = c
        vin = vehicles[0].vin


async def get_car():
    await ensure_client()
    vehicles = await client.get_vehicles()
    car = vehicles[0]
    await car.update(skip=["status"])
    return car


async def get_lock_status(car):
    """Fetch fresh door/window status following the pytoyoda README.

    GET /v1/global/remote/status (used by Vehicle.lock_status) frequently
    returns 429 APIGW-403. Wake the vehicle with POST refresh-status first
    (the same call the Toyota app makes), then poll the status endpoint
    until lock_status.last_updated advances past the cached value.
    """
    before = car.lock_status.last_updated if car.lock_status else None
    if before:
        await car.refresh_status()
    for _ in range(3):
        try:
            await car.update(only=["status"])
        except Exception:
            await asyncio.sleep(2)
            continue
        after = car.lock_status.last_updated if car.lock_status else None
        if after and (not before or after > before):
            return car.lock_status
        await asyncio.sleep(3)
    return car.lock_status


def _fmt(result):
    if result.message:
        return result.message
    if isinstance(result.status, _MessagesModel) and result.status.messages:
        return result.status.messages[0].description
    payload = getattr(result, "payload", None)
    if payload is not None:
        rc = getattr(payload, "return_code", None) or getattr(payload, "returnCode", None)
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


def _simple_cmd(command_name: str, cmd_type: CommandType, beeps: int = 0):
    """Factory for simple one-shot remote commands."""
    async def handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
        uid = check_allowed(update)
        if uid:
            return await update.message.reply_text(f"Access denied. Your ID: {uid}")
        msg = await update.message.reply_text(f"Executing {command_name}...")
        try:
            await ensure_client()
            car = (await client.get_vehicles())[0]
            result = await car.post_command(cmd_type, beeps=beeps)
            await msg.edit_text(f"{command_name}: {_fmt(result)}")
        except Exception as e:
            await msg.edit_text(f"Error: {e}")
    return handler


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
    handler = BUTTON_HANDLERS.get(update.message.text)
    if handler:
        await handler(update, context)


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
    "/service_history  — dealer visits\n"
    "/trips [n]  — km driven n days ago (0 = today)\n"
    "/trip  — alias for /trips\n"
    "/trips_summary  — day or month totals (asks)\n"
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
    "/ac_on  — start AC (asks temp)\n"
    "/ac_off  — stop AC\n"
    "/climate <temp>  — set target temp\n"
    "/refresh_climate  — refresh status\n\n"
    "Battery / Charging\n"
    "/charge_now  — charge immediately\n"
    "/refresh_battery  — refresh SOC\n\n"
    "System\n"
    "/wake  — wake vehicle\n"
    "/alias <name>  — set nickname\n"
    "/notify  — push notifications here\n"
    "/unotify  — stop notifications\n\n"
    "Tap the buttons below to run commands without typing."
)


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = check_allowed(update)
    if uid:
        return await update.message.reply_text(f"Access denied. Your ID: {uid}")
    await update.message.reply_text(HELP_TEXT,
                                    reply_markup=build_keyboard())


# ── /start ────────────────────────────────────────────────────────────


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = check_allowed(update)
    if uid:
        return await update.message.reply_text(
            f"Access denied. Your ID is {uid} — add it to allowed_user_ids in config."
        )
    await update.message.reply_text(HELP_TEXT,
                                    reply_markup=build_keyboard())


# ── Info commands ──────────────────────────────────────────────────────


async def cmd_vehicles(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = check_allowed(update)
    if uid:
        return await update.message.reply_text(f"Access denied. Your ID: {uid}")
    msg = await update.message.reply_text("Fetching...")
    try:
        await ensure_client()
        vs = await client.get_vehicles()
        lines = []
        for v in vs:
            typ = v.type or "?"
            alias = v.alias or "—"
            lines.append(f"• {alias} — {typ}")
        await msg.edit_text("\n".join(lines), parse_mode="Markdown")
    except Exception as e:
        await msg.edit_text(f"Error: {e}")


async def cmd_lock_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = check_allowed(update)
    if uid:
        return await update.message.reply_text(f"Access denied. Your ID: {uid}")
    msg = await update.message.reply_text("Waking and refreshing lock status...")
    try:
        car = await get_car()
        ls = await get_lock_status(car)
        if not ls:
            return await msg.edit_text("Lock status not available.")
        doors = ls.doors
        lines = [f"*{car.alias or 'C-HR'}* lock status"]
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
            lines.append(f"_{ls.last_updated.strftime('%d/%m %H:%M')}_")
        await msg.edit_text("\n".join(lines), parse_mode="Markdown")
    except Exception as e:
        await msg.edit_text(f"Error: {e}")


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = check_allowed(update)
    if uid:
        return await update.message.reply_text(f"Access denied. Your ID: {uid}")
    msg = await update.message.reply_text("Waking vehicle and fetching fresh status...")
    try:
        car = await get_car()

        def electric_ts():
            raw = car._endpoint_data.get("electric_status")
            if raw and raw.payload and raw.payload.last_update_timestamp:
                return raw.payload.last_update_timestamp
            return None

        def fuel_seen():
            raw = car._endpoint_data.get("electric_status")
            if raw and raw.payload:
                if (raw.payload.fuel_level or 0) > 0:
                    return True
                if raw.payload.fuel_range and raw.payload.fuel_range.value:
                    return True
            return False

        before = electric_ts()
        woke = False
        if before or not fuel_seen():
            try:
                r = await car.refresh_status()
                rc = getattr(r.payload, "return_code", None)
                woke = rc == "000000"
            except Exception:
                woke = False
            for _ in range(4):
                await asyncio.sleep(6)
                try:
                    await car.update(only=["electric_status", "telemetry"])
                except Exception:
                    continue
                after = electric_ts()
                if woke and after and (not before or after > before):
                    break
                if fuel_seen():
                    break
        logger.info(f"Status woke={woke} fresh={fuel_seen()}")

        raw = car._endpoint_data.get("electric_status")
        ep = raw.payload if raw else None
        tele = car._endpoint_data.get("telemetry")
        tp = tele.payload if tele else None
        lines = [f"*{car.alias or 'C-HR'}*"]
        if tp and tp.odometer and tp.odometer.value:
            lines.append(f"Odometer: {tp.odometer.value:,.0f} km")
        fuel_pct = ep.fuel_level if ep and ep.fuel_level is not None else None
        fuel_range = None
        if ep and ep.fuel_range and ep.fuel_range.value:
            fuel_range = ep.fuel_range.value
        if fuel_range:
            lines.append(f"Fuel range: {fuel_range:,.0f} km")
        if fuel_pct is not None:
            lines.append(f"Fuel: {fuel_pct}%")
        if ep and ep.battery_level is not None:
            pct = app_battery_pct(ep.battery_level, car.type)
            lines.append(f"HV battery: {pct:.0f}%")
        if ep and ep.ev_range and ep.ev_range.value:
            lines.append(f"EV range: {ep.ev_range.value:,.0f} km")
        if ep and ep.ev_range_with_ac and ep.ev_range_with_ac.value:
            lines.append(f"EV range (AC): {ep.ev_range_with_ac.value:,.0f} km")
        if ep and ep.charging_status:
            lines.append(f"Charging: {ep.charging_status}")
        if not fuel_seen():
            lines.append("_(data may be stale — wake failed)_")
        settings = car.climate_settings
        if settings and settings.temperature:
            t = settings.temperature
            lines.append(f"Target: {t.value}°{t.unit}")
        await msg.edit_text("\n".join(lines), parse_mode="Markdown")
    except Exception as e:
        await msg.edit_text(f"Error: {e}")


async def cmd_charge(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = check_allowed(update)
    if uid:
        return await update.message.reply_text(f"Access denied. Your ID: {uid}")
    msg = await update.message.reply_text("Fetching battery & charging info...")
    try:
        car = await get_car()
        elec = car.electric_status
        if not elec:
            return await msg.edit_text("No electric status available.")
        lines = [f"*{car.alias or 'C-HR'}* — battery/charging"]
        if elec.battery_level is not None:
            pct = app_battery_pct(elec.battery_level, car.type)
            lines.append(f"Battery: {pct:.0f}%")
        if elec.ev_range is not None:
            lines.append(f"EV range: {elec.ev_range:,.0f} km")
        if elec.charging_status:
            lines.append(f"Charging: {elec.charging_status}")
        if elec.remaining_charge_time is not None:
            lines.append(f"Charge done in: {elec.remaining_charge_time:.0f} min")
        if elec.ev_range_with_ac is not None:
            lines.append(f"EV range (AC on): {elec.ev_range_with_ac:,.0f} km")
        raw = car._endpoint_data.get("electric_status")
        payload = raw.payload if raw else None
        next_ev = getattr(payload, "next_charging_event", None) if payload else None
        if next_ev and next_ev.timestamp:
            lines.append(
                f"Next charge: {next_ev.event_type} {next_ev.timestamp.strftime('%d/%m %H:%M')}"
            )
        schedules = getattr(payload, "charging_schedules", None) if payload else None
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
        await msg.edit_text("\n".join(lines), parse_mode="Markdown")
    except Exception as e:
        await msg.edit_text(f"Error: {e}")


async def cmd_climate_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = check_allowed(update)
    if uid:
        return await update.message.reply_text(f"Access denied. Your ID: {uid}")
    msg = await update.message.reply_text("Fetching climate status...")
    try:
        await ensure_client()
        api = client._api
        status = await api.get_climate_status(vin)
        settings = await api.get_climate_settings(vin)

        lines = ["*Climate*"]
        p = status.payload
        if p:
            state = p.status or "unknown"
            lines.append(f"Active: {'yes' if state == 'running' else 'no'} ({state})")
            if p.duration:
                lines.append(f"Duration: {p.duration} min")
            if p.started_at:
                lines.append(f"Started: {p.started_at:%d/%m %H:%M}")
            if p.current_temperature:
                cur = p.current_temperature
                lines.append(f"Cabin: {cur.value}°{cur.unit}")
            if p.target_temperature:
                tgt = p.target_temperature
                lines.append(f"Target: {tgt.value}°{tgt.unit}")
        s = settings.payload
        if s:
            if s.temperature:
                t = s.temperature
                lines.append(f"Temp: {t.value}°{t.unit}")
            if s.duration:
                lines.append(f"Settings duration: {s.duration} min")
        await msg.edit_text("\n".join(lines), parse_mode="Markdown")
    except Exception as e:
        await msg.edit_text(f"Error: {e}")


async def cmd_location(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = check_allowed(update)
    if uid:
        return await update.message.reply_text(f"Access denied. Your ID: {uid}")
    msg = await update.message.reply_text("Fetching location...")
    try:
        car = await get_car()
        loc = car.location
        if not loc or loc.latitude is None:
            await msg.edit_text("Location not available.")
            return
        lat, lon = loc.latitude, loc.longitude
        maps = f"https://www.google.com/maps?q={lat},{lon}"
        text = f"📍 [{lat:.5f}, {lon:.5f}]({maps})"
        if loc.state:
            text += f"\n{loc.state}"
        if loc.timestamp:
            text += f"\n_{loc.timestamp.strftime('%d/%m %H:%M')}_"
        await msg.edit_text(text, parse_mode="Markdown", disable_web_page_preview=True)
    except Exception as e:
        await msg.edit_text(f"Error: {e}")


def _health_item_text(item):
    if isinstance(item, dict):
        for key in ("description", "code", "name", "message"):
            if item.get(key):
                return str(item[key])
        return " ".join(f"{k}={v}" for k, v in item.items() if v not in (None, ""))
    return str(item)


async def cmd_health(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = check_allowed(update)
    if uid:
        return await update.message.reply_text(f"Access denied. Your ID: {uid}")
    msg = await update.message.reply_text("Fetching health...")
    try:
        await ensure_client()
        api = client._api
        h = await api.get_vehicle_health_status(vin)
        if not h.payload:
            await msg.edit_text("No health data.")
            return
        p = h.payload
        lines = ["*Vehicle health*"]
        warnings = p.warning or []
        if warnings:
            lines.append("Warnings:")
            for w in warnings:
                lines.append(f"⚠ {_health_item_text(w)}")
        else:
            lines.append("Warnings: none")
        oil = p.quantity_of_eng_oil_icon or []
        if oil:
            lines.append("Engine oil: " + ", ".join(map(_health_item_text, oil)))
        else:
            lines.append("Engine oil: OK")
        if p.wng_last_upd_time:
            ts = p.wng_last_upd_time
            if ts.tzinfo is not None:
                ts = ts.astimezone()
            lines.append(f"_{ts.strftime('%d/%m %H:%M')}_")
        await msg.edit_text("\n".join(lines), parse_mode="Markdown")
    except Exception as e:
        await msg.edit_text(f"Error: {e}")


async def cmd_service_history(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = check_allowed(update)
    if uid:
        return await update.message.reply_text(f"Access denied. Your ID: {uid}")
    msg = await update.message.reply_text("Fetching service history...")
    try:
        car = await get_car()
        history = car.service_history
        if not history:
            await msg.edit_text("No service history.")
            return
        lines = []
        for s in history[:10]:
            date_str = s.service_date.strftime("%d/%m/%Y") if s.service_date else "?"
            dealer = s.servicing_dealer or ""
            cat = s.service_category or ""
            lines.append(f"• {date_str} — {cat} {dealer}")
        await msg.edit_text("\n".join(lines))
    except Exception as e:
        await msg.edit_text(f"Error: {e}")


async def cmd_last_trip(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = check_allowed(update)
    if uid:
        return await update.message.reply_text(f"Access denied. Your ID: {uid}")
    msg = await update.message.reply_text("Fetching last trip...")
    try:
        car = await get_car()
        trip = await car.get_last_trip()
        if not trip:
            await msg.edit_text("No trip data.")
            return
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
            lines.append(f"Start: {trip.start_time}")
        if trip.end_time:
            lines.append(f"End: {trip.end_time}")
        loc = trip.locations
        if loc and loc.start:
            start_gm = f"https://www.google.com/maps?q={loc.start.lat},{loc.start.lon}"
            lines.append(f"[Start map]({start_gm})")
        await msg.edit_text("\n".join(lines), parse_mode="Markdown")
    except Exception as e:
        await msg.edit_text(f"Error: {e}")


def _day_key(st: datetime) -> date:
    return st.date() if st.tzinfo is None else st.astimezone().date()


async def _summarize_day(car, target: date) -> str | None:
    trips = await car.get_trips(target - timedelta(days=1), target + timedelta(days=1))
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


async def _summarize_month(car, target: date) -> str | None:
    first = date(target.year, target.month, 1)
    last = date(
        target.year, target.month,
        calendar.monthrange(target.year, target.month)[1],
    )
    trips = await car.get_trips(first - timedelta(days=1), last + timedelta(days=1))
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


async def cmd_trips(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = check_allowed(update)
    if uid:
        return await update.message.reply_text(f"Access denied. Your ID: {uid}")
    days_ago = 0
    if context.args:
        try:
            days_ago = max(int(context.args[0]), 0)
        except ValueError:
            return await update.message.reply_text(
                "Usage: /trips <days ago>  (e.g. 0 = today, 1 = yesterday)"
            )
    target = date.today() - timedelta(days=days_ago)
    msg = await update.message.reply_text(f"Fetching trips for {target:%d/%m/%Y}...")
    try:
        car = await get_car()
        text = await _summarize_day(car, target)
        await msg.edit_text(text or f"No trips on {target:%d/%m/%Y}.")
    except Exception as e:
        await msg.edit_text(f"Error: {e}")


async def cmd_trips_summary(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = check_allowed(update)
    if uid:
        await update.message.reply_text(f"Access denied. Your ID: {uid}")
        return ConversationHandler.END
    await update.message.reply_text(
        "Trip summary — send *day* or *month* (or /cancel)."
    )
    return PERIOD_WAIT


async def receive_period(update: Update, context: ContextTypes.DEFAULT_TYPE):
    choice = update.message.text.strip().lower()
    if choice in ("day", "d"):
        await update.message.reply_text(
            "Which day?\n"
            "• number: 0 = today, 1 = yesterday, ...\n"
            "• or date: dd/mm/yyyy"
        )
        return DAY_WAIT
    if choice in ("month", "m"):
        await update.message.reply_text(
            "Which month? Send 1–12 (1 = January, ..., 12 = December)."
        )
        return MONTH_WAIT
    await update.message.reply_text("Please send 'day' or 'month'.")
    return PERIOD_WAIT


def _parse_day_input(text: str) -> tuple[date | None, str | None]:
    text = text.strip()
    try:
        days_ago = max(int(text), 0)
        return date.today() - timedelta(days=days_ago), None
    except ValueError:
        pass
    try:
        return datetime.strptime(text, "%d/%m/%Y").date(), None
    except ValueError:
        return None, "Use a number (0 = today) or a date like 12/08/2026."


async def receive_day(update: Update, context: ContextTypes.DEFAULT_TYPE):
    target, err = _parse_day_input(update.message.text)
    if err:
        await update.message.reply_text(err)
        return DAY_WAIT
    await update.message.reply_text(f"Fetching trips for {target:%d/%m/%Y}...")
    try:
        car = await get_car()
        text = await _summarize_day(car, target)
        await update.message.reply_text(text or f"No trips on {target:%d/%m/%Y}.")
    except Exception as e:
        await update.message.reply_text(f"Error: {e}")
    return ConversationHandler.END


async def receive_month(update: Update, context: ContextTypes.DEFAULT_TYPE):
    raw = update.message.text.strip()
    try:
        month = int(raw)
    except ValueError:
        await update.message.reply_text("Please send a number 1–12.")
        return MONTH_WAIT
    if not 1 <= month <= 12:
        await update.message.reply_text("Month must be between 1 and 12.")
        return MONTH_WAIT
    now = date.today()
    year = now.year if month <= now.month else now.year - 1
    target = date(year, month, 1)
    await update.message.reply_text(f"Fetching trips for {target:%B %Y}...")
    try:
        car = await get_car()
        text = await _summarize_month(car, target)
        await update.message.reply_text(text or f"No trips in {target:%B %Y}.")
    except Exception as e:
        await update.message.reply_text(f"Error: {e}")
    return ConversationHandler.END


async def cmd_scores(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = check_allowed(update)
    if uid:
        return await update.message.reply_text(f"Access denied. Your ID: {uid}")
    days = 7
    if context.args:
        try:
            days = int(context.args[0])
        except ValueError:
            pass
    msg = await update.message.reply_text(f"Fetching driving scores ({days}d)...")
    try:
        car = await get_car()
        from_date = date.today() - timedelta(days=days)
        trips = await car.get_trips(from_date, date.today())
        if not trips:
            await msg.edit_text("No trips in period.")
            return
        lines = [f"*Driving scores ({len(trips)} trips)*"]
        for t in trips[:15]:
            start = t.start_time.strftime("%d/%m") if t.start_time else "?"
            score = t.score
            if score is not None:
                lines.append(f"• {start} — overall {score:.0f}/100")
            else:
                lines.append(f"• {start} — no score")
        await msg.edit_text("\n".join(lines), parse_mode="Markdown")
    except Exception as e:
        await msg.edit_text(f"Error: {e}")


async def cmd_summary(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = check_allowed(update)
    if uid:
        return await update.message.reply_text(f"Access denied. Your ID: {uid}")
    typ_map = {"d": SummaryType.DAILY, "w": SummaryType.WEEKLY,
               "m": SummaryType.MONTHLY, "y": SummaryType.YEARLY}
    st = typ_map.get(context.args[0] if context.args else None, SummaryType.MONTHLY)
    label = {"d": "Daily", "w": "Weekly", "m": "Monthly", "y": "Yearly"}.get(
        context.args[0] if context.args else None, "Monthly"
    )
    msg = await update.message.reply_text(f"Fetching {label} summary...")
    try:
        car = await get_car()
        days = 365 if st == SummaryType.YEARLY else 90
        s = await car.get_summary(
            date.today() - timedelta(days=days), date.today(), summary_type=st
        )
        if not s:
            await msg.edit_text("No summary data.")
            return
        lines = [f"*{label} summary*"]
        for item in s[:12]:
            start = item.from_date.strftime("%d/%m") if item.from_date else "?"
            end = item.to_date.strftime("%d/%m") if item.to_date else "?"
            lines.append(f"• {start}-{end}: {item.distance or '?'} km")
        await msg.edit_text("\n".join(lines), parse_mode="Markdown")
    except Exception as e:
        await msg.edit_text(f"Error: {e}")


# ── Remote command handlers ──────────────────────────────────────────


cmd_lock = _simple_cmd("Lock doors", CommandType.DOOR_LOCK)
cmd_unlock = _simple_cmd("Unlock doors", CommandType.DOOR_UNLOCK)
cmd_hazards_on = _simple_cmd("Hazards on", CommandType.HAZARD_ON)
cmd_hazards_off = _simple_cmd("Hazards off", CommandType.HAZARD_OFF)
cmd_trunk_lock = _simple_cmd("Trunk lock", CommandType.TRUNK_LOCK)
cmd_trunk_unlock = _simple_cmd("Trunk unlock", CommandType.TRUNK_UNLOCK)
cmd_buzzer = _simple_cmd("Buzzer warning", CommandType.BUZZER_WARNING)


# ── Climate ─────────────────────────────────────────────────────────────


async def cmd_ac_on(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = check_allowed(update)
    if uid:
        await update.message.reply_text(f"Access denied. Your ID: {uid}")
        return ConversationHandler.END
    await update.message.reply_text(
        "What temperature? (e.g. 22)\nReply a number or /cancel"
    )
    return TEMP_WAIT


async def receive_temp(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = check_allowed(update)
    if uid:
        await update.message.reply_text(f"Access denied. Your ID: {uid}")
        return ConversationHandler.END
    try:
        temp = float(update.message.text)
    except ValueError:
        await update.message.reply_text("Send a valid number (e.g. 22)")
        return TEMP_WAIT
    if not 14 <= temp <= 29:
        await update.message.reply_text(
            "Temperature must be between 14 and 29°C for this vehicle. Send another value or /cancel"
        )
        return TEMP_WAIT

    msg = await update.message.reply_text(f"Setting AC to {temp}°C and starting...")
    try:
        await ensure_client()
        api = client._api

        try:
            before = await api.get_climate_status(vin)
        except Exception:
            before = None
        before_started = getattr(getattr(before, "payload", None), "started_at", None)

        req = V2RemoteClimateControlRequestModel(
            command="start",
            temperature=UnitValueModel(value=temp, unit="C"),
            save_settings=True,
        )
        result = await api.send_climate_control_command(vin, req)
        payload = getattr(result, "payload", None)
        rc = getattr(payload, "return_code", None)
        if rc and rc != "000000":
            await msg.edit_text(f"AC command failed (return code {rc}).")
            return ConversationHandler.END
        await msg.edit_text(
            "Waiting for the car to confirm (up to ~60 s)..."
        )
        outcome, state, started = await _verify_climate(api, "start", before_started)
        await msg.edit_text(_climate_outcome_text("start", temp, outcome, state, started))
    except Exception as e:
        await msg.edit_text(f"Error: {e}")
    return ConversationHandler.END


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
            return f"AC set to {temp}°C and started (confirmed by the car)."
        if outcome == "already":
            when = started.strftime("%d/%m %H:%M") if started else "earlier"
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
    return f"The car did not confirm that the AC stopped (last state: {state}). Check the app."


async def cmd_ac_off(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = check_allowed(update)
    if uid:
        return await update.message.reply_text(f"Access denied. Your ID: {uid}")
    msg = await update.message.reply_text("Stopping AC...")
    try:
        await ensure_client()
        api = client._api
        req = V2RemoteClimateControlRequestModel(command="stop")
        result = await api.send_climate_control_command(vin, req)
        payload = getattr(result, "payload", None)
        rc = getattr(payload, "return_code", None)
        if rc and rc != "000000":
            await msg.edit_text(f"AC stop command failed (return code {rc}).")
            return
        await msg.edit_text("Waiting for the car to confirm (up to ~60 s)...")
        outcome, state, started = await _verify_climate(api, "stop")
        await msg.edit_text(_climate_outcome_text("stop", None, outcome, state, started))
    except Exception as e:
        await msg.edit_text(f"Error: {e}")


async def cmd_climate(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = check_allowed(update)
    if uid:
        return await update.message.reply_text(f"Access denied. Your ID: {uid}")
    if not context.args:
        return await update.message.reply_text("Usage: /climate <temp>")
    try:
        temp = float(context.args[0])
    except ValueError:
        return await update.message.reply_text("Temperature must be a number")
    if not 14 <= temp <= 29:
        return await update.message.reply_text(
            "Temperature must be between 14 and 29°C for this vehicle"
        )

    msg = await update.message.reply_text(f"Setting AC to {temp}°C...")
    try:
        await ensure_client()
        api = client._api
        try:
            before = await api.get_climate_status(vin)
        except Exception:
            before = None
        before_started = getattr(getattr(before, "payload", None), "started_at", None)
        req = V2RemoteClimateControlRequestModel(
            command="start",
            temperature=UnitValueModel(value=temp, unit="C"),
            save_settings=True,
        )
        result = await api.send_climate_control_command(vin, req)
        payload = getattr(result, "payload", None)
        rc = getattr(payload, "return_code", None)
        if rc and rc != "000000":
            await msg.edit_text(f"Climate command failed (return code {rc}).")
            return
        await msg.edit_text("Waiting for the car to confirm (up to ~60 s)...")
        outcome, state, started = await _verify_climate(api, "start", before_started)
        await msg.edit_text(_climate_outcome_text("start", temp, outcome, state, started))
    except Exception as e:
        await msg.edit_text(f"Error: {e}")


async def cmd_refresh_climate(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = check_allowed(update)
    if uid:
        return await update.message.reply_text(f"Access denied. Your ID: {uid}")
    msg = await update.message.reply_text("Refreshing climate status...")
    try:
        await ensure_client()
        api = client._api
        result = await api.refresh_climate_status(vin)
        await msg.edit_text(f"Climate refresh: {_fmt(result)}")
    except Exception as e:
        await msg.edit_text(f"Error: {e}")


# ── Battery / Charging ──────────────────────────────────────────────────


async def cmd_charge_now(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = check_allowed(update)
    if uid:
        return await update.message.reply_text(f"Access denied. Your ID: {uid}")
    msg = await update.message.reply_text("Setting charge now...")
    try:
        await ensure_client()
        api = client._api
        cmd = NextChargeSettings(command=ChargeCommandType.CHARGE_NOW)
        result = await api.send_next_charging_command(vin, cmd)
        await msg.edit_text(f"Charge now: {_fmt(result)}")
    except Exception as e:
        await msg.edit_text(f"Error: {e}")


async def cmd_refresh_battery(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = check_allowed(update)
    if uid:
        return await update.message.reply_text(f"Access denied. Your ID: {uid}")
    msg = await update.message.reply_text("Refreshing battery status (may drain 12V)...")
    try:
        await ensure_client()
        api = client._api
        result = await api.refresh_electric_realtime_status(vin)
        await msg.edit_text(f"Battery refresh: {_fmt(result)}")
    except Exception as e:
        await msg.edit_text(f"Error: {e}")


# ── System ──────────────────────────────────────────────────────────────


async def cmd_wake(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = check_allowed(update)
    if uid:
        return await update.message.reply_text(f"Access denied. Your ID: {uid}")
    msg = await update.message.reply_text("Waking vehicle...")
    try:
        await ensure_client()
        api = client._api
        result = await api.refresh_vehicle_status(vin)
        await msg.edit_text(f"Wake: {_fmt(result)}")
    except Exception as e:
        await msg.edit_text(f"Error: {e}")


async def cmd_alias(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = check_allowed(update)
    if uid:
        return await update.message.reply_text(f"Access denied. Your ID: {uid}")
    if not context.args:
        return await update.message.reply_text("Usage: /alias <name>")
    name = " ".join(context.args)
    msg = await update.message.reply_text(f"Setting alias to {name}...")
    try:
        await ensure_client()
        car = (await client.get_vehicles())[0]
        result = await car.set_alias(name)
        await msg.edit_text(f"Alias set: {result}")
    except Exception as e:
        await msg.edit_text(f"Error: {e}")


# ── Notifications ────────────────────────────────────────────────────────


async def cmd_notify(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = check_allowed(update)
    if uid:
        return await update.message.reply_text(f"Access denied. Your ID: {uid}")
    data = load_data()
    chat_id = update.effective_chat.id
    if chat_id in data["notify_chats"]:
        await update.message.reply_text("Already registered.")
    else:
        data["notify_chats"].append(chat_id)
        save_data(data)
        await update.message.reply_text("You'll receive app notifications here.")


async def cmd_unotify(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = check_allowed(update)
    if uid:
        return await update.message.reply_text(f"Access denied. Your ID: {uid}")
    data = load_data()
    chat_id = update.effective_chat.id
    if chat_id not in data["notify_chats"]:
        await update.message.reply_text("Notifications are not active for this chat.")
    else:
        data["notify_chats"].remove(chat_id)
        save_data(data)
        await update.message.reply_text("Notifications deactivated for this chat.")


async def cmd_notifications(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = check_allowed(update)
    if uid:
        return await update.message.reply_text(f"Access denied. Your ID: {uid}")
    msg = await update.message.reply_text("Fetching notifications...")
    try:
        car = await get_car()
        notifs = car.notifications
        if not notifs:
            return await msg.edit_text("No notifications.")
        lines = []
        for n in notifs[:10]:
            ds = n.date.strftime("%d/%m %H:%M") if n.date else "?"
            lines.append(f"• [{ds}] {n.message or n.category or n.type}")
        await msg.edit_text("\n".join(lines))
    except Exception as e:
        await msg.edit_text(f"Error: {e}")


async def poll_notifications(context: ContextTypes.DEFAULT_TYPE):
    try:
        await ensure_client()
        vehicles = await client.get_vehicles()
        car = vehicles[0]
        await car.update(skip=["status"])
        notifs = car.notifications
        if not notifs:
            return

        data = load_data()
        last_str = data.get("last_notification")
        if last_str:
            last_dt = datetime.fromisoformat(last_str)
            if last_dt.tzinfo is None:
                last_dt = last_dt.replace(tzinfo=timezone.utc)
        else:
            last_dt = datetime.min.replace(tzinfo=timezone.utc)

        new = []
        for n in notifs:
            if not n.date:
                continue
            nd = n.date
            if nd.tzinfo is None:
                nd = nd.replace(tzinfo=timezone.utc)
            if nd > last_dt:
                new.append(n)
        if not new:
            return

        data["last_notification"] = max(n.date for n in new).isoformat()
        save_data(data)

        for n in new:
            ds = n.date.strftime("%d/%m %H:%M") if n.date else "?"
            text = f"🚗 *{n.category or 'Notification'}*\n{ds}\n{n.message or '—'}"
            for cid in data.get("notify_chats", []):
                try:
                    await context.bot.send_message(
                        chat_id=cid, text=text, parse_mode="Markdown"
                    )
                except Exception as e:
                    logger.warning(f"Notify send fail to {cid}: {e}")
    except Exception as e:
        logger.warning(f"Notification poll fail: {e}")


async def post_init(app: Application):
    app.job_queue.run_repeating(poll_notifications, interval=300, first=30)
    logger.info("Notification polling started (every 5 min)")


# ── main ────────────────────────────────────────────────────────────────


HANDLERS = {
    "start": cmd_start, "help": cmd_help,
    "vehicles": cmd_vehicles, "status": cmd_status, "charge": cmd_charge,
    "lock_status": cmd_lock_status, "climate_status": cmd_climate_status,
    "location": cmd_location, "health": cmd_health,
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
        Application.builder().token(TOKEN).post_init(post_init).build()
    )

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
        ],
        states={
            PERIOD_WAIT: [
                MessageHandler(
                    filters.TEXT & ~filters.COMMAND & ~filters.Text(ALL_LABELS),
                    receive_period,
                )
            ],
            DAY_WAIT: [
                MessageHandler(
                    filters.TEXT & ~filters.COMMAND & ~filters.Text(ALL_LABELS),
                    receive_day,
                )
            ],
            MONTH_WAIT: [
                MessageHandler(
                    filters.TEXT & ~filters.COMMAND & ~filters.Text(ALL_LABELS),
                    receive_month,
                )
            ],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        conversation_timeout=600,
    )

    app.add_handler(ac_conv)
    app.add_handler(CommandHandler("ac_on", cmd_ac_on))
    app.add_handler(trips_conv)
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
