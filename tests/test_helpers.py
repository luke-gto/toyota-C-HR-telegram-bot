from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

import bot


def test_esc_escapes_markdown():
    assert bot.esc("a_b*c`d[e") == r"a\_b\*c\`d\[e"


def test_app_battery_pct_non_phev_passthrough():
    assert bot.app_battery_pct(55.0, "ev") == 55.0
    assert bot.app_battery_pct(None, "ev") is None


def test_app_battery_pct_phev_conversion(monkeypatch):
    monkeypatch.setitem(
        bot.config,
        "battery",
        {"phev_ev_display": True, "soc_buffer": 31.0, "soc_factor": 1.3333},
    )
    assert bot.app_battery_pct(82, "plug_in_hybrid") == pytest.approx(68.0, abs=0.2)


def test_app_battery_pct_phev_clamped(monkeypatch):
    monkeypatch.setitem(
        bot.config,
        "battery",
        {"phev_ev_display": True, "soc_buffer": 31.0, "soc_factor": 1.3333},
    )
    assert bot.app_battery_pct(10, "plug_in_hybrid") == 0.0
    assert bot.app_battery_pct(120, "plug_in_hybrid") == 100.0


def test_app_battery_pct_disabled(monkeypatch):
    monkeypatch.setitem(bot.config, "battery", {"phev_ev_display": False})
    assert bot.app_battery_pct(82, "plug_in_hybrid") == 82


def test_parse_range_input_valid():
    start, end, err = bot._parse_range_input("01/10/2026 - 15/10/2026")
    assert err is None
    assert start == date(2026, 10, 1)
    assert end == date(2026, 10, 15)


def test_parse_range_input_accepts_to_and_single_digit_days():
    start, end, err = bot._parse_range_input("1/10/2026 to 5/10/2026")
    assert err is None
    assert start == date(2026, 10, 1)
    assert end == date(2026, 10, 5)


def test_parse_range_input_reversed():
    start, end, err = bot._parse_range_input("15/10/2026 - 01/10/2026")
    assert start is None
    assert end is None
    assert err


def test_parse_range_input_future_is_rejected():
    today = bot._today()
    start, end, err = bot._parse_range_input(
        f"{today:%d/%m/%Y} - {today + timedelta(days=1):%d/%m/%Y}"
    )
    assert start is None
    assert end is None
    assert err


def test_parse_range_input_too_long():
    today = bot._today()
    start, end, err = bot._parse_range_input(
        f"{today - timedelta(days=400):%d/%m/%Y} - {today:%d/%m/%Y}"
    )
    assert start is None
    assert end is None
    assert err


def test_parse_range_input_invalid():
    start, end, err = bot._parse_range_input("not-a-range")
    assert start is None
    assert end is None
    assert err


def test_build_range_summary_lists_every_day(monkeypatch):
    monkeypatch.setattr(bot, "USER_TZ", timezone.utc)
    start = date(2026, 10, 1)
    end = date(2026, 10, 3)
    trips = [
        SimpleNamespace(
            start_time=datetime(2026, 10, 1, 10, 0, tzinfo=timezone.utc),
            distance=10.5,
            ev_distance=2.0,
        ),
        SimpleNamespace(
            start_time=datetime(2026, 10, 1, 18, 0, tzinfo=timezone.utc),
            distance=4.5,
            ev_distance=None,
        ),
    ]

    text = bot._build_range_summary(trips, start, end)

    assert "Thu 01/10 — 15.0 km (2 trips)" in text
    assert "Fri 02/10 — 0.0 km" in text
    assert "Sat 03/10 — 0.0 km" in text
    assert "Total: 15.0 km" in text
    assert "EV: 2.0 km" in text


def test_split_message_respects_limit():
    text = "\n".join(f"line {i}" for i in range(1000))
    chunks = bot._split_message(text, limit=100)
    assert len(chunks) > 1
    assert all(len(chunk) <= 100 for chunk in chunks)
    assert "\n".join(chunks) == text


def test_trip_summary_keyboards_use_short_callbacks():
    keyboards = [
        bot._trips_summary_keyboard(),
        bot._day_picker_keyboard(date(2026, 10, 22)),
        bot._month_picker_keyboard(date(2026, 10, 22)),
    ]
    for keyboard in keyboards:
        callbacks = [
            button.callback_data
            for row in keyboard.inline_keyboard
            for button in row
        ]
        assert all(len(callback.encode()) <= 64 for callback in callbacks)
        assert len(set(callbacks)) == len(callbacks)


def test_aware_utc_naive_and_aware():
    naive = datetime(2026, 1, 1, 12, 0, 0)
    assert bot._aware_utc(naive) == datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
    aware = datetime(2026, 1, 1, 13, 0, tzinfo=timezone(timedelta(hours=1)))
    assert bot._aware_utc(aware) == datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
    assert bot._aware_utc(None) is None


def test_user_dt_none():
    assert bot.user_dt(None) is None


def test_status_anomaly_lines_detects_causes():
    payload = {
        "doors": {
            "driver": {"lockStatus": {"status": "unlocked"}},
            "rearBack": {"openStatus": {"status": "open"}},
            "hood": {"openStatus": {"status": "open"}},
        },
        "windows": {"driver": {"status": "open"}, "passenger": {"status": "venting"}},
        "lights": {"head": {"status": "on"}, "tail": {"status": "off"}},
        "rearSeatReminder": {"reason": "detected"},
    }
    lines = bot._status_anomaly_lines(payload)
    joined = "\n".join(lines)
    assert "Unlocked: Driver" in joined
    assert "Open: Trunk, Hood" in joined
    assert "Windows open: Driver" in joined
    assert "Windows venting: Passenger" in joined
    assert "Lights on: Headlights" in joined
    assert "Rear seat reminder" in joined


def test_status_anomaly_lines_clean():
    assert bot._status_anomaly_lines({}) == []


def test_anomalies_text_reports_no_anomalies():
    text = bot._anomalies_text("C-HR", None, {"overallStatus": "ok"})
    assert "No anomalies detected" in text


def test_anomalies_text_reports_warnings():
    class Health:
        warning = [{"description": "Tyre pressure low"}]
        quantity_of_eng_oil_icon = ["low"]
        wng_last_upd_time = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)

    text = bot._anomalies_text("C-HR", Health(), {"overallWarningCounts": 2})
    assert "Anomalies found" in text
    assert "Tyre pressure low" in text
    assert "Engine oil" in text
    assert "2 vehicle status warning(s)" in text


def test_lock_outcome_text():
    assert "confirmed by the car" in bot._lock_outcome_text("locked", "ok")
    assert "already" in bot._lock_outcome_text("unlocked", "already")
    assert "did not confirm" in bot._lock_outcome_text("locked", "timeout")


def test_data_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setattr(bot, "DATA_FILE", tmp_path / "data" / "data.json")
    bot.save_data({"notify_chats": [7], "last_notification": None})
    assert bot.load_data()["notify_chats"] == [7]


def test_load_data_missing_returns_defaults(tmp_path, monkeypatch):
    monkeypatch.setattr(bot, "DATA_FILE", tmp_path / "missing.json")
    assert bot.load_data() == {"notify_chats": [], "last_notification": None}
