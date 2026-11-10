import os
import sys
import tempfile
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


def _system_temp_dir() -> Path:
    """挑一个仓库之外的临时根目录，避免测试缓存落在仓库根。"""
    candidates = [Path(tempfile.gettempdir())]
    if os.name == "nt":
        local_appdata = os.environ.get("LOCALAPPDATA")
        if local_appdata:
            candidates.append(Path(local_appdata) / "Temp")
        candidates.append(Path.home() / "AppData" / "Local" / "Temp")
    for candidate in candidates:
        if not _inside_repo(candidate):
            return candidate
    return Path(tempfile.gettempdir())


TEMP_ROOT = _system_temp_dir()

# 运行环境若把 TEMP/TMP 指到仓库内，tempfile 的 mkstemp 等会直接在仓库根留文件。
if _inside_repo(Path(tempfile.gettempdir())):
    os.environ["TEMP"] = os.environ["TMP"] = str(TEMP_ROOT)
    tempfile.tempdir = str(TEMP_ROOT)

# pytest 的默认 basetemp 取自 PYTEST_DEBUG_TEMPROOT / 系统临时目录，
# 显式指向仓库外的临时根，避免 pytest-of-* 落在仓库根。
os.environ["PYTEST_DEBUG_TEMPROOT"] = str(TEMP_ROOT)

# torch inductor 的缓存默认同样跟随系统临时目录，显式指到仓库外。
_inductor_cache = os.environ.get("TORCHINDUCTOR_CACHE_DIR")
if not _inductor_cache or _inside_repo(Path(_inductor_cache)):
    os.environ["TORCHINDUCTOR_CACHE_DIR"] = str(TEMP_ROOT / "torchinductor")

# 更新检查默认关闭，避免测试期间访问 GitHub 或写入 data/runtime/update。
os.environ.setdefault("AI_TRPG_DISABLE_UPDATE_CHECK", "1")
