import os
import sys
import tempfile
import threading
from pathlib import Path


TESTS_DIR = Path(__file__).resolve().parent
REPO_ROOT = TESTS_DIR.parent

BACKEND_DIR = REPO_ROOT / "backend"
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))


def _inside_repo(path: Path) -> bool:
    try:
        path.resolve().relative_to(REPO_ROOT)
    except (OSError, ValueError):
        return False
    return True


def _writable(directory: Path, timeout: float = 2.0) -> bool:
    """在超时内真探一次写入，而不是只看路径是否在仓库内。

    受限环境（沙箱、只读挂载、权限收紧）里仓库外的临时目录可能写不进去；
    更麻烦的是有些沙箱对越界写入会直接阻塞而不是报错，所以探测必须带超时。
    """
    result: list[bool] = []

    def probe() -> None:
        try:
            directory.mkdir(parents=True, exist_ok=True)
            fd, name = tempfile.mkstemp(prefix=".trae-temp-probe-", dir=str(directory))
            os.close(fd)
            Path(name).unlink()
            result.append(True)
        except OSError:
            result.append(False)

    worker = threading.Thread(target=probe, daemon=True)
    worker.start()
    worker.join(timeout)
    if worker.is_alive():
        # 写探测卡住（受限沙箱会阻塞而非报错），按不可写处理；线程是 daemon，
        # 进程退出时不会等它。
        return False
    return bool(result and result[0])


def _temp_candidates() -> list[Path]:
    candidates: list[Path] = []
    try:
        candidates.append(Path(tempfile.gettempdir()))
    except OSError:
        pass
    if os.name == "nt":
        local_appdata = os.environ.get("LOCALAPPDATA")
        if local_appdata:
            candidates.append(Path(local_appdata) / "Temp")
        candidates.append(Path.home() / "AppData" / "Local" / "Temp")
    unique: list[Path] = []
    seen: set[str] = set()
    for candidate in candidates:
        key = os.path.normcase(os.path.abspath(str(candidate)))
        if key in seen:
            continue
        seen.add(key)
        unique.append(candidate)
    return unique


REPO_TEMP_DIR = REPO_ROOT / ".pytest-temp"


def _select_temp_root() -> Path:
    """优先选仓库外且真实可写的临时根，避免测试缓存落在仓库根。"""
    for candidate in _temp_candidates():
        if _inside_repo(candidate):
            continue
        if _writable(candidate):
            return candidate
    # 仓库外一个都写不进去时退回仓库内：先让测试跑起来，总比 basetemp 直接
    # 失败、整轮 65 个 ERROR 好；同时明确提示，避免误提交。
    print(
        "[conftest] 仓库外的临时目录均不可写，退回仓库内临时目录："
        f"{REPO_TEMP_DIR}（请勿提交该目录）",
        file=sys.stderr,
    )
    REPO_TEMP_DIR.mkdir(parents=True, exist_ok=True)
    return REPO_TEMP_DIR


TEMP_ROOT = _select_temp_root()

# 统一把 TEMP/TMP 与 pytest 的 basetemp 指到选定的临时根：既避免缓存落进
# 仓库根，也避免系统临时目录不可写时 mkstemp / basetemp 直接失败。
os.environ["TEMP"] = os.environ["TMP"] = str(TEMP_ROOT)
tempfile.tempdir = str(TEMP_ROOT)

# pytest 的默认 basetemp 取自 PYTEST_DEBUG_TEMPROOT / 系统临时目录，
# 显式指到上面选定的临时根，避免 pytest-of-* 落在仓库根。
os.environ["PYTEST_DEBUG_TEMPROOT"] = str(TEMP_ROOT)

# torch inductor 的缓存默认同样跟随系统临时目录，显式指到仓库外。
_inductor_cache = os.environ.get("TORCHINDUCTOR_CACHE_DIR")
if not _inductor_cache or _inside_repo(Path(_inductor_cache)):
    os.environ["TORCHINDUCTOR_CACHE_DIR"] = str(TEMP_ROOT / "torchinductor")

# 更新检查默认关闭，避免测试期间访问 GitHub 或写入 data/runtime/update。
os.environ.setdefault("AI_TRPG_DISABLE_UPDATE_CHECK", "1")
