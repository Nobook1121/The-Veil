"""房间 AI 用量与花费统计。

覆盖：按每百万 token 单价计费、多币种、未配置单价只统计 token、
重复调用累加，以及损坏文件回退零值。
"""

from __future__ import annotations

from pathlib import Path

from trpg_server.json_store import write_json_atomic
from trpg_server.room_usage import load_room_usage, record_room_usage


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

    def test_total_tokens_falls_back_to_prompt_plus_completion(self, tmp_path: Path):
        room_dir = tmp_path / "rooms" / "r5"
        room_dir.mkdir(parents=True)

        record_room_usage(room_dir, {"prompt_tokens": 10, "completion_tokens": 5})

        data = load_room_usage(room_dir)
        assert data["total_tokens"] == 15
