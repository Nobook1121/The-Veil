"""房间 AI 用量与花费统计。

覆盖：按每百万 token 单价计费、多币种、未配置单价只统计 token、
重复调用累加，以及损坏文件回退零值。
"""

from __future__ import annotations

from pathlib import Path

import time

from flask import Flask

from trpg_server.agents.telemetry import record_ai_usage
from trpg_server.json_store import write_json_atomic
from trpg_server.room_usage import _peak_multiplier, _platform_pricing, _request_cost, load_room_usage, record_room_usage


def _write_platform(config_dir: Path, *, currency: str = "CNY", input_price: float = 0, output_price: float = 0) -> None:
    platform_dir = config_dir / "aiplatform"
    platform_dir.mkdir(parents=True, exist_ok=True)
    write_json_atomic(
        platform_dir / "deepseek.json",
        {
            "config": {"currency": currency},
            "models": [
                {"id": "deepseek-chat", "input_price": input_price, "output_price": output_price},
            ],
        },
    )


class TestRoomUsage:
    def test_prices_use_per_million_token_units(self, tmp_path: Path):
        room_dir = tmp_path / "rooms" / "r1"
        room_dir.mkdir(parents=True)
        config_dir = tmp_path / "config"
        _write_platform(config_dir, currency="USD", input_price=2, output_price=8)

        record_room_usage(
            room_dir,
            {
                "provider": "deepseek",
                "model": "deepseek-chat",
                "prompt_tokens": 1_000_000,
                "completion_tokens": 500_000,
            },
            config_dir=config_dir,
        )

        data = load_room_usage(room_dir, "r1")
        assert data["room_id"] == "r1"
        assert data["requests"] == 1
        assert data["prompt_tokens"] == 1_000_000
        assert data["completion_tokens"] == 500_000
        assert data["total_tokens"] == 1_500_000
        # 输入：1M / 1M * 2 = 2 USD；输出：0.5M / 1M * 8 = 4 USD。
        assert data["costs"] == {"USD": 6.0}
        assert data["models"]["deepseek/deepseek-chat"]["costs"] == {"USD": 6.0}

    def test_missing_price_counts_tokens_only(self, tmp_path: Path):
        room_dir = tmp_path / "rooms" / "r2"
        room_dir.mkdir(parents=True)
        config_dir = tmp_path / "config"
        _write_platform(config_dir)

        record_room_usage(
            room_dir,
            {"provider": "deepseek", "model": "deepseek-chat", "prompt_tokens": 1200, "completion_tokens": 300},
            config_dir=config_dir,
        )

        data = load_room_usage(room_dir)
        assert data["total_tokens"] == 1500
        assert data["costs"] == {}
        assert data["models"]["deepseek/deepseek-chat"]["total_tokens"] == 1500

    def test_successive_calls_accumulate(self, tmp_path: Path):
        room_dir = tmp_path / "rooms" / "r3"
        room_dir.mkdir(parents=True)
        config_dir = tmp_path / "config"
        _write_platform(config_dir, currency="CNY", input_price=4, output_price=0)

        usage = {"provider": "deepseek", "model": "deepseek-chat", "prompt_tokens": 250_000, "completion_tokens": 0}
        record_room_usage(room_dir, usage, config_dir=config_dir)
        record_room_usage(room_dir, usage, config_dir=config_dir)

        data = load_room_usage(room_dir)
        assert data["requests"] == 2
        assert data["total_tokens"] == 500_000
        assert data["costs"] == {"CNY": 2.0}

    def test_corrupted_usage_file_falls_back_to_zero(self, tmp_path: Path):
        room_dir = tmp_path / "rooms" / "r4"
        room_dir.mkdir(parents=True)
        (room_dir / "usage.json").write_text("{not json", encoding="utf-8")

        data = load_room_usage(room_dir, "r4")
        assert data["requests"] == 0
        assert data["total_tokens"] == 0
        assert data["costs"] == {}

    def test_telemetry_hooks_room_usage_when_app_context_exists(self, tmp_path: Path):
        app = Flask(__name__)
        app.config["ROOMS_DIR"] = tmp_path / "rooms"
        app.config["CONFIG_DIR"] = tmp_path / "config"
        _write_platform(app.config["CONFIG_DIR"], currency="USD", input_price=1, output_price=0)

        with app.app_context():
            record_ai_usage(
                tmp_path / "logs",
                {
                    "room_id": "r6",
                    "provider": "deepseek",
                    "model": "deepseek-chat",
                    "prompt_tokens": 2_000_000,
                    "completion_tokens": 0,
                },
            )

        data = load_room_usage(app.config["ROOMS_DIR"] / "r6", "r6")
        assert data["requests"] == 1
        assert data["total_tokens"] == 2_000_000
        assert data["costs"] == {"USD": 2.0}

    def test_total_tokens_falls_back_to_prompt_plus_completion(self, tmp_path: Path):
        room_dir = tmp_path / "rooms" / "r5"
        room_dir.mkdir(parents=True)

        record_room_usage(room_dir, {"prompt_tokens": 10, "completion_tokens": 5})

        data = load_room_usage(room_dir)
        assert data["total_tokens"] == 15


class TestPricingDetails:
    def test_cached_tokens_use_cache_price(self):
        pricing = {"input_price": 2.0, "output_price": 0.0, "cache_hit_price": 0.5}

        # 1M 输入中 500k 命中缓存：500k*2 + 500k*0.5 = 1 + 0.25。
        assert _request_cost(pricing, 1_000_000, 0, 500_000) == 1.25

    def test_cached_price_falls_back_to_input_price(self):
        pricing = {"input_price": 2.0, "output_price": 0.0}

        assert _request_cost(pricing, 1_000_000, 0, 500_000) == 2.0

    def test_cached_tokens_cannot_exceed_prompt(self):
        pricing = {"input_price": 1.0, "cache_hit_price": 0.0, "output_price": 0.0}

        assert _request_cost(pricing, 100_000, 0, 500_000) == 0.0

    def test_peak_multiplier_applies_to_peak_hours(self):
        noon = time.struct_time((2026, 1, 1, 12, 0, 0, 0, 0, -1))
        pricing = {
            "input_price": 1.0,
            "output_price": 0.0,
            "peak_hours": "09:00-18:00",
            "peak_multiplier": 1.5,
            "offpeak_multiplier": 0.8,
        }

        assert _peak_multiplier(pricing, noon) == 1.5
        assert _request_cost(pricing, 1_000_000, 0, 0, noon) == 1.5

        night = time.struct_time((2026, 1, 1, 23, 0, 0, 0, 0, -1))
        assert _peak_multiplier(pricing, night) == 0.8

    def test_peak_hours_support_crossing_midnight(self):
        pricing = {"peak_hours": "22:00-06:00", "peak_multiplier": 1.2, "offpeak_multiplier": 1.0}

        assert _peak_multiplier(pricing, time.struct_time((2026, 1, 1, 23, 30, 0, 0, 0, -1))) == 1.2
        assert _peak_multiplier(pricing, time.struct_time((2026, 1, 1, 3, 0, 0, 0, 0, -1))) == 1.2
        assert _peak_multiplier(pricing, time.struct_time((2026, 1, 1, 12, 0, 0, 0, 0, -1))) == 1.0

    def test_peak_hours_absent_means_single_price(self):
        pricing = {"peak_hours": "", "peak_multiplier": 2.0, "offpeak_multiplier": 0.5}

        assert _peak_multiplier(pricing) == 1.0

    def test_platform_pricing_reads_cache_and_peak_fields(self, tmp_path: Path):
        platform_dir = tmp_path / "aiplatform"
        platform_dir.mkdir(parents=True)
        write_json_atomic(
            platform_dir / "deepseek.json",
            {
                "config": {"currency": "USD"},
                "models": [
                    {
                        "id": "deepseek-chat",
                        "input_price": 2,
                        "output_price": 8,
                        "cache_hit_price": 1,
                        "peak_hours": "09:00-18:00",
                        "peak_multiplier": 1.5,
                        "offpeak_multiplier": 0.8,
                    }
                ],
            },
        )

        pricing = _platform_pricing(tmp_path, "deepseek", "deepseek-chat")

        assert pricing["currency"] == "USD"
        assert pricing["input_price"] == 2.0
        assert pricing["output_price"] == 8.0
        assert pricing["cache_hit_price"] == 1.0
        assert pricing["peak_hours"] == "09:00-18:00"
        assert pricing["peak_multiplier"] == 1.5
        assert pricing["offpeak_multiplier"] == 0.8
