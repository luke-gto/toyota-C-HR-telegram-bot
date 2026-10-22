import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "config.json"

if not CONFIG.exists():
    CONFIG.write_text(
        json.dumps(
            {
                "toyota": {"username": "test", "password": "test", "brand": "T"},
                "telegram": {"token": "123:test"},
                "allowed_user_ids": [1],
                "battery": {
                    "phev_ev_display": True,
                    "soc_buffer": 31.0,
                    "soc_factor": 1.3333,
                },
            }
        )
    )
