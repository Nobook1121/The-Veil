"""本地多音色 TTS 模型管理：清单、下载、校验、安装与移除。

选型结论（详见 ``docs/the-veil.wiki`` 与发布变更日志）：

- 载体：``sherpa-onnx``（Apache-2.0，CPU 推理，Python wheel 内置 onnxruntime）；
- 权重：``kokoro-int8-multi-lang-v1_1``（Kokoro-82M-v1.1-zh 的 int8 导出，Apache-2.0，
  24kHz，103 个说话人，其中中文女声 55 个、中文男声 45 个）；
- 排除：AISHELL-3 VITS 多说话人模型（数据集 Apache-2.0 但导出权重仅 8kHz，音质与年龄区分度差）；
  任何 CC-BY-NC 的中文音色权重一律不采用。

设计约定：
- **不把权重放进仓库**：安装目录固定在运行时数据目录 ``data/runtime/voice/models/<model>``；
- 下载走 HuggingFace 镜像，失败可切回官方站点；先写临时目录并逐文件校验大小/哈希，
  成功后整体替换，失败不会破坏已有安装；
- 许可证文本与来源链接随安装落盘（``LICENSE.txt`` / ``INSTALL.json``）。
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import threading
import time
from pathlib import Path
from typing import Any, Callable
from urllib.parse import quote

import requests

from trpg_server.settings import RUNTIME_DIR

logger = logging.getLogger(__name__)

MODEL_ID = "kokoro-int8-multi-lang-v1_1"
MODEL_LABEL = "Kokoro-82M-v1.1-zh (int8)"
MODEL_LICENSE = "Apache-2.0"
MODEL_REPO = "csukuangfj/kokoro-int8-multi-lang-v1_1"
MODEL_SOURCES = (
    "https://hf-mirror.com",
    "https://huggingface.co",
)
MODEL_SOURCE_DISPLAY = "https://huggingface.co/" + MODEL_REPO
MODEL_WEIGHTS_LICENSE_SOURCE = "https://huggingface.co/hexgrad/Kokoro-82M-v1.1-zh"
MODEL_ENGINE_LICENSE_SOURCE = "https://github.com/k2-fsa/sherpa-onnx"
SAMPLE_RATE = 24000
SPEAKER_COUNT = 103

# 安装所需的最小文件集；espeak-ng-data 只保留中英文音素化所需的核心文件，
# 避免下载 300+ 个无用语言包（也降低镜像限流风险）。
REQUIRED_FILES = (
    "model.int8.onnx",
    "voices.bin",
    "tokens.txt",
    "lexicon-us-en.txt",
    "lexicon-zh.txt",
)
ESPEAK_FILES = (
    "espeak-ng-data/phondata",
    "espeak-ng-data/phondata-manifest",
    "espeak-ng-data/phonindex",
    "espeak-ng-data/phontab",
    "espeak-ng-data/intonations",
    "espeak-ng-data/en_dict",
    "espeak-ng-data/cmn_dict",
    "espeak-ng-data/lang/gmw/en",
    "espeak-ng-data/lang/gmw/en-US",
    "espeak-ng-data/lang/sit/cmn",
    "espeak-ng-data/lang/sit/cmn-Latn-pinyin",
)
REQUIRED_DIRS = ("espeak-ng-data/lang/gmw", "espeak-ng-data/lang/sit")

INSTALL_META = "INSTALL.json"
LICENSE_FILE = "LICENSE.txt"

# 9 个人设的默认真实说话人（sid）。按 F0 采样排序：同龄段内用更高 F0 表示更年轻，
# 更低 F0 表示更年长；旁白单独使用成熟女声，避免与 4 个女性 NPC 撞声。
# 站点可用 ``voice_local_speakers`` 覆盖，房间可在 voice.json 的 ``local_speakers`` 再覆盖。
DEFAULT_SPEAKERS: dict[str, int] = {
    "narrator": 57,          # zf_099  F0≈179Hz 成熟女声
    "npc_male_teen": 89,     # zm_065  F0≈171Hz
    "npc_male_youth": 76,    # zm_045  F0≈146Hz
    "npc_male_middle": 82,   # zm_056  F0≈139Hz
    "npc_male_elder": 62,    # zm_013  F0≈122Hz
    "npc_female_teen": 38,   # zf_071  F0≈261Hz
    "npc_female_youth": 19,  # zf_027  F0≈240Hz
    "npc_female_middle": 24, # zf_039  F0≈214Hz
    "npc_female_elder": 46,  # zf_079  F0≈149Hz
}

# sid -> 说话人名（Kokoro v1_1 的编号并非连续，按官方映射表登记实际使用到的音色）。
SPEAKER_NAMES: dict[int, str] = {
    7: "zf_005",
    19: "zf_027",
    24: "zf_039",
    38: "zf_071",
    46: "zf_079",
    52: "zf_088",
    57: "zf_099",
    62: "zm_013",
    76: "zm_045",
    82: "zm_056",
    89: "zm_065",
    95: "zm_082",
    102: "zm_100",
}

_INSTALL_LOCK = threading.Lock()
_INSTALL_STATE: dict[str, Any] = {"running": False, "started_at": "", "message": "", "error": ""}


def models_root() -> Path:
    return RUNTIME_DIR / "voice" / "models"


def model_dir(model_id: str = MODEL_ID) -> Path:
    return models_root() / str(model_id or MODEL_ID).strip()


def _meta_path(directory: Path) -> Path:
    return directory / INSTALL_META


def speaker_name(sid: int) -> str:
    if sid in SPEAKER_NAMES:
        return SPEAKER_NAMES[sid]
    if 3 <= sid <= 57:
        return f"zf_{sid - 2:03d}"
    if 58 <= sid <= 102:
        return f"zm_{sid - 57:03d}"
    return f"sid_{sid}"


def speaker_gender(sid: int) -> str:
    if 3 <= sid <= 57:
        return "female"
    if 58 <= sid <= 102:
        return "male"
    return "neutral"


def speaker_catalog() -> list[dict[str, Any]]:
    return [
        {"sid": sid, "name": speaker_name(sid), "gender": speaker_gender(sid)}
        for sid in range(3, SPEAKER_COUNT)
    ]


def installed_status(model_id: str = MODEL_ID) -> dict[str, Any]:
    """检查安装完整性；缺失或缺体积不符时视为未安装。"""
    directory = model_dir(model_id)
    meta = _read_meta(directory)
    missing: list[str] = []
    for name in (*REQUIRED_FILES, *ESPEAK_FILES):
        if not (directory / name).is_file():
            missing.append(name)
    for name in REQUIRED_DIRS:
        if not (directory / name).is_dir():
            missing.append(name + "/")
    sizes = meta.get("files") if isinstance(meta.get("files"), dict) else {}
    bad: list[str] = []
    for name, expected in sizes.items():
        path = directory / str(name)
        try:
            if path.is_file() and int(expected) > 0 and path.stat().st_size != int(expected):
                bad.append(str(name))
        except OSError:
            bad.append(str(name))
    installed = not missing and not bad and bool(meta)
    return {
        "model": str(model_id or MODEL_ID),
        "label": MODEL_LABEL,
        "installed": installed,
        "path": str(directory),
        "missing": missing,
        "size_mismatch": bad,
        "bytes": int(meta.get("total_bytes") or 0) if meta else 0,
        "installed_at": str(meta.get("installed_at") or ""),
        "source": str(meta.get("source") or MODEL_SOURCE_DISPLAY),
        "license": MODEL_LICENSE,
        "sample_rate": SAMPLE_RATE,
        "speakers": SPEAKER_COUNT,
        "voices": speaker_catalog(),
        "install": dict(_INSTALL_STATE),
    }


def _read_meta(directory: Path) -> dict[str, Any]:
    try:
        data = json.loads(_meta_path(directory).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _fetch_tree(base_url: str, timeout: int = 30) -> list[dict[str, Any]]:
    url = f"{base_url.rstrip('/')}/api/models/{MODEL_REPO}/tree/main?recursive=true"
    response = requests.get(url, timeout=timeout)
    response.raise_for_status()
    data = response.json()
    if not isinstance(data, list):
        raise ValueError("unexpected model tree response")
    return [item for item in data if isinstance(item, dict) and item.get("type") == "file"]


def _wanted(path: str) -> bool:
    return path in REQUIRED_FILES or path in ESPEAK_FILES


def _download_file(
    base_url: str,
    entry: dict[str, Any],
    target: Path,
    *,
    timeout: int,
    retries: int = 3,
) -> None:
    path = str(entry.get("path") or "")
    expected_size = int(entry.get("size") or 0)
    lfs = entry.get("lfs") if isinstance(entry.get("lfs"), dict) else {}
    expected_sha = str(lfs.get("oid") or "")
    if expected_size <= 0:
        expected_size = int(lfs.get("size") or 0)
    url = f"{base_url.rstrip('/')}/{MODEL_REPO}/resolve/main/{quote(path)}?download=true"
    target.parent.mkdir(parents=True, exist_ok=True)
    temp = target.with_suffix(target.suffix + ".part")
    last_error: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            if temp.exists() and expected_size > 0 and temp.stat().st_size == expected_size:
                break
            with requests.get(
                url,
                stream=True,
                timeout=timeout,
                headers={"User-Agent": "the-veil-voice/1.0"},
            ) as response:
                if response.status_code == 429:
                    retry_after = response.headers.get("Retry-After")
                    wait = float(retry_after) if str(retry_after or "").isdigit() else 8.0 * attempt
                    raise RuntimeError(f"mirror rate limited, retry in {wait:.0f}s")
                response.raise_for_status()
                with temp.open("wb") as handle:
                    for chunk in response.iter_content(chunk_size=1024 * 256):
                        if chunk:
                            handle.write(chunk)
            size = temp.stat().st_size
            if expected_size > 0 and size != expected_size:
                raise ValueError(f"size mismatch got={size} expected={expected_size}")
            if expected_sha and _sha256(temp) != expected_sha:
                raise ValueError("sha256 mismatch")
            os.replace(temp, target)
            return
        except Exception as exc:  # noqa: BLE001 - 网络下载需要统一重试
            last_error = exc
            logger.warning(
                "voice_model_download retry file=%s attempt=%s error=%s", path, attempt, exc
            )
            if temp.exists():
                try:
                    temp.unlink()
                except OSError:
                    pass
            if "rate limited" in str(last_error):
                time.sleep(min(30.0, 8.0 * attempt))
            else:
                time.sleep(min(5, attempt))
    raise RuntimeError(f"下载失败 {path}: {last_error}")


def install(
    model_id: str = MODEL_ID,
    *,
    progress: Callable[[str], None] | None = None,
    timeout: int = 60,
    force: bool = False,
) -> tuple[bool, str]:
    """下载并校验模型；成功才替换正式目录，失败保持旧安装可用。"""
    target_id = str(model_id or MODEL_ID).strip()
    if target_id != MODEL_ID:
        return False, f"未知模型: {target_id}（当前仅支持 {MODEL_ID}）"
    directory = model_dir(target_id)
    with _INSTALL_LOCK:
        if _INSTALL_STATE.get("running"):
            return False, "已有安装任务在进行中。"
        if installed_status(target_id)["installed"] and not force:
            return True, f"模型已安装：{directory}"
        _INSTALL_STATE.update(
            {
                "running": True,
                "started_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                "message": "准备下载",
                "error": "",
            }
        )
        temp_root = models_root() / f".tmp-{target_id}-{os.getpid()}"
        last_error: Exception | None = None
        try:
            tree: list[dict[str, Any]] = []
            base = ""
            for candidate in MODEL_SOURCES:
                try:
                    if progress:
                        progress(f"读取模型清单: {candidate}")
                    tree = _fetch_tree(candidate, timeout=30)
                    base = candidate
                    break
                except Exception as exc:  # noqa: BLE001
                    last_error = exc
                    logger.warning("voice_model_tree failed base=%s error=%s", candidate, exc)
            if not base:
                raise RuntimeError(f"无法获取模型清单: {last_error}")
            wanted = [item for item in tree if _wanted(str(item.get("path") or ""))]
            if len(wanted) < len(REQUIRED_FILES):
                raise RuntimeError("模型清单缺少必要文件")
            if temp_root.exists():
                shutil.rmtree(temp_root, ignore_errors=True)
            temp_root.mkdir(parents=True, exist_ok=True)
            files: dict[str, int] = {}
            total = 0
            for index, entry in enumerate(wanted, start=1):
                path = str(entry.get("path") or "")
                if progress:
                    progress(f"下载 {index}/{len(wanted)}: {path}")
                _download_file(base, entry, temp_root / path, timeout=timeout, retries=5)
                size = (temp_root / path).stat().st_size
                files[path] = size
                total += size
                time.sleep(0.15)
            # 许可证与元数据落盘，便于分发审计。
            license_text = (
                f"{MODEL_LABEL}\n"
                f"Model weights license: {MODEL_LICENSE}\n"
                f"Weights source: {MODEL_SOURCE_DISPLAY}\n"
                f"Upstream model card: {MODEL_WEIGHTS_LICENSE_SOURCE}\n"
                f"Runtime (sherpa-onnx) license: Apache-2.0 {MODEL_ENGINE_LICENSE_SOURCE}\n\n"
                "本目录中的权重文件按 Apache-2.0 使用与再分发；完整许可证文本请访问上述链接。\n"
            )
            (temp_root / LICENSE_FILE).write_text(license_text, encoding="utf-8")
            meta = {
                "model": target_id,
                "label": MODEL_LABEL,
                "license": MODEL_LICENSE,
                "source": f"{base}/{MODEL_REPO}",
                "display_source": MODEL_SOURCE_DISPLAY,
                "sample_rate": SAMPLE_RATE,
                "speakers": SPEAKER_COUNT,
                "files": files,
                "total_bytes": total,
                "installed_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            }
            (temp_root / INSTALL_META).write_text(
                json.dumps(meta, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
            )
            if directory.exists():
                backup = models_root() / f".old-{target_id}-{int(time.time())}"
                try:
                    os.replace(directory, backup)
                except OSError:
                    shutil.rmtree(directory, ignore_errors=True)
                os.replace(temp_root, directory)
                shutil.rmtree(backup, ignore_errors=True)
            else:
                directory.parent.mkdir(parents=True, exist_ok=True)
                os.replace(temp_root, directory)
            _INSTALL_STATE.update(
                {"running": False, "message": f"安装完成（{total / 1024 / 1024:.1f} MB）", "error": ""}
            )
            logger.info("voice_model_install model=%s bytes=%s dir=%s", target_id, total, directory)
            return True, f"模型安装完成：{directory}（{total / 1024 / 1024:.1f} MB）"
        except Exception as exc:  # noqa: BLE001
            shutil.rmtree(temp_root, ignore_errors=True)
            _INSTALL_STATE.update({"running": False, "message": "安装失败", "error": str(exc)})
            logger.error("voice_model_install failed model=%s error=%s", target_id, exc)
            return False, f"模型安装失败：{exc}"
        finally:
            if not _INSTALL_STATE.get("running"):
                pass


def install_in_background(model_id: str = MODEL_ID, *, force: bool = False) -> tuple[bool, str]:
    """设置页按钮使用：立即返回，后台线程下载。"""
    if _INSTALL_STATE.get("running"):
        return True, "安装已在进行中。"
    thread = threading.Thread(
        target=install, kwargs={"model_id": model_id, "force": force}, daemon=True
    )
    thread.start()
    return True, "已开始后台安装。"


def remove(model_id: str = MODEL_ID) -> tuple[bool, str]:
    target_id = str(model_id or MODEL_ID).strip()
    directory = model_dir(target_id)
    if not directory.exists():
        return True, f"模型未安装：{directory}"
    with _INSTALL_LOCK:
        if _INSTALL_STATE.get("running"):
            return False, "安装任务进行中，暂不能移除。"
        try:
            shutil.rmtree(directory)
        except OSError as exc:
            logger.error("voice_model_remove failed dir=%s error=%s", directory, exc)
            return False, f"移除失败：{exc}"
    logger.info("voice_model_remove model=%s dir=%s", target_id, directory)
    return True, f"已移除模型：{directory}"
