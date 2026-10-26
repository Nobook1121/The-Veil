"""房间指标（§10.6）与轨迹文件（§10.5）测试。"""
import json

from trpg_server.agents.narrative.metrics import bump, load_metrics, metrics_path, observe
from trpg_server.agents.narrative.trace import append_trace, read_trace, trace_path


def test_metrics_counters_and_averages(tmp_path):
    bump(tmp_path, turn_count=2, guard_denied=1, unknown=9)
    bump(tmp_path, turn_count=1)
    metrics = load_metrics(tmp_path)
    assert metrics["turn_count"] == 3
    assert metrics["guard_denied"] == 1
    assert "unknown" not in json.loads(metrics_path(tmp_path).read_text(encoding="utf-8"))

    observe(tmp_path, {"context_tokens": 100, "latency_ms": 400})
    observe(tmp_path, {"context_tokens": 200, "latency_ms": 200})
    metrics = load_metrics(tmp_path)
    assert metrics["avg_context_tokens"] == 150.0
    assert metrics["avg_latency_ms"] == 300.0


def test_metrics_tolerates_corrupt_file(tmp_path):
    metrics_path(tmp_path).write_text("{not json", encoding="utf-8")
    metrics = load_metrics(tmp_path)
    assert metrics["turn_count"] == 0


def test_trace_append_read_and_filter(tmp_path):
    append_trace(tmp_path, {"turn": 1, "branch": "investigate"})
    append_trace(tmp_path, {"turn": 2, "branch": "move"})
    append_trace(tmp_path, {"turn": 2, "branch": "investigate"})
    rows = read_trace(tmp_path)
    assert [row["branch"] for row in rows] == ["investigate", "move", "investigate"]
    assert len(read_trace(tmp_path, turn=2)) == 2
    assert read_trace(tmp_path, limit=1)[0]["turn"] == 2
    assert trace_path(tmp_path).is_file()


def test_trace_trim_keeps_latest(tmp_path):
    for turn in range(6):
        append_trace(tmp_path, {"turn": turn}, limit=3)
    rows = read_trace(tmp_path)
    assert [row["turn"] for row in rows] == [3, 4, 5]
