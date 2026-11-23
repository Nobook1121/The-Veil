"""进程退出后替换程序文件并重新启动（应用内一键更新的收尾助手）。

由 ``update_service.apply_update`` 以独立进程启动：

    python scripts/apply_update.py --pending <pending.json> --parent-pid <pid> --launch

只用标准库；日志写到 data/runtime/update/apply.log，失败时保留 pending，
下次启动的早鸟钩子会再试一次。
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))


def log(message: str, log_path: Path) -> None:
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{stamp}] {message}"
    print(line)
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with log_path.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")
    except OSError:
        pass


def wait_for_exit(pid: int, timeout: float = 180.0) -> bool:
    """等待指定进程退出；Windows 用 OpenProcess，其它平台用信号 0 探测。"""
    if pid <= 0:
        return True
    if os.name == "nt":
        import ctypes

        SYNCHRONIZE = 0x00100000
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(SYNCHRONIZE, False, int(pid))
        if not handle:
            return True
        try:
            result = kernel32.WaitForSingleObject(handle, int(timeout * 1000))
            return result == 0  # WAIT_OBJECT_0
        finally:
            kernel32.CloseHandle(handle)

    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            os.kill(pid, 0)
        except OSError:
            return True
        time.sleep(0.5)
    return False


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--pending", required=True)
    parser.add_argument("--parent-pid", type=int, default=0)
    parser.add_argument("--launch", action="store_true")
    args = parser.parse_args()

    pending = Path(args.pending)
    log_path = ROOT / "data/runtime/update/apply.log"
    log(f"等待主进程退出（pid={args.parent_pid}）", log_path)
    if not wait_for_exit(args.parent_pid):
        log("主进程未在 180 秒内退出，放弃本次替换（pending 保留）", log_path)
        return 2

    from trpg_server.update_apply import apply_pending  # noqa: PLC0415 - 延迟导入

    try:
        result = apply_pending(pending, root=ROOT)
        log(f"替换完成：{result}", log_path)
    except Exception as exc:  # noqa: BLE001 - 助手必须把失败写进日志而不是静默
        log(f"替换失败：{exc}", log_path)
        return 3

    if args.launch:
        server = ROOT / "server.py"
        log("重新启动 server.py", log_path)
        creation = 0
        if os.name == "nt":
            creation = 0x00000008 | 0x00000200  # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
        with (ROOT / "data/runtime/update/server.log").open("a", encoding="utf-8") as output:
            subprocess.Popen(
                [sys.executable, str(server)],
                cwd=str(ROOT),
                stdout=output,
                stderr=subprocess.STDOUT,
                creationflags=creation,
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
