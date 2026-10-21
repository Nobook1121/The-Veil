import json
import logging
import os
import stat
import tempfile
import time
from pathlib import Path

logger = logging.getLogger(__name__)

# Windows 上目标文件被其他进程/线程短暂持有（共享冲突）或带有只读属性时，
# os.replace 会抛 WinError 5/32。用递增间隔重试，避免一次瞬时占用就返回 500。
_REPLACE_RETRY_DELAYS = (0.0, 0.02, 0.05, 0.1, 0.2, 0.3, 0.5, 0.8)


def read_json(path, default=None):
    json_path = Path(path)
    if not json_path.exists():
        return default

    with json_path.open("r", encoding="utf-8") as file:
        return json.load(file)


def _clear_readonly(path):
    """目标文件被标记为只读时去掉只读属性，返回是否做了修改。"""
    try:
        mode = stat.S_IMODE(os.stat(path).st_mode)
        if mode & stat.S_IWRITE:
            return False
        os.chmod(path, mode | stat.S_IWRITE)
        return True
    except OSError:
        return False


def replace_with_retry(temp_path, json_path):
    """替换目标文件；Windows 共享冲突 / 只读属性时按递增间隔重试。"""
    last_error = None
    for attempt, delay in enumerate(_REPLACE_RETRY_DELAYS):
        if delay:
            time.sleep(delay)
        try:
            os.replace(temp_path, json_path)
            if attempt:
                logger.warning(
                    "json_replace_retried path=%s attempts=%s", json_path, attempt + 1
                )
            return
        except OSError as error:
            # 只有 Windows 的共享冲突/拒绝访问值得重试，其他错误直接抛出。
            if getattr(error, "winerror", None) not in (5, 32):
                raise
            last_error = error
            if attempt == 0:
                _clear_readonly(json_path)
    raise last_error


def write_json_atomic(path, data):
    json_path = Path(path)
    json_path.parent.mkdir(parents=True, exist_ok=True)

    temp_file = tempfile.NamedTemporaryFile(
        "w",
        encoding="utf-8",
        dir=json_path.parent,
        delete=False,
        suffix=".tmp",
    )
    temp_path = Path(temp_file.name)

    try:
        try:
            json.dump(data, temp_file, ensure_ascii=False, indent=2)
            temp_file.write("\n")
            temp_file.flush()
            os.fsync(temp_file.fileno())
        finally:
            temp_file.close()

        replace_with_retry(temp_path, json_path)
        temp_path = None
    finally:
        if temp_path is not None:
            try:
                temp_path.unlink()
            except OSError:
                # 清理失败不能覆盖原始异常（例如文件正被杀毒软件扫描）。
                logger.debug("json_temp_cleanup_failed path=%s", temp_path)
