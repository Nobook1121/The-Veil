"""首次部署引导里的 Embedding 模型配置逻辑。

把「自动下载本地模型 / 手动上传本地模型 / 启用云端 embedding」三件事从路由里
拆出来，既避免 ``routes/setup.py`` 越写越大，也便于单独测试：

* :class:`EmbeddingDownloadJob` 在后台线程里把模型拉取到本地目录，并把进度
  暴露给轮询接口；
* :func:`store_uploaded_files` 把用户拖入的 zip 压缩包或散落文件落盘到模型目录；
* :func:`set_cloud_embedding_enabled` 把开关写回 ``general.toml`` 的 ``[ai]`` 段。
"""

from __future__ import annotations

import logging
import re
import shutil
import threading
import zipfile
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import quote

import requests

from trpg_server.ai_platform_config import load_platform_config
from trpg_server.security import safe_join
from trpg_server.settings import SETUP_EMBEDDING_UPLOAD_MAX_BYTES

logger = logging.getLogger(__name__)

# 压缩包解压后的总体积上限（按上传大小的 4 倍估算，足够覆盖正常模型仓库，
# 同时拦住 zip bomb 把磁盘写满）。
EXTRACT_MAX_BYTES = SETUP_EMBEDDING_UPLOAD_MAX_BYTES * 4

EMBEDDING_REPO_PATTERN = re.compile(r"^[A-Za-z0-9_.-]{1,64}/[A-Za-z0-9_.-]{1,96}$")
# 优先官方站点，失败时回落到国内镜像，避免部分网络环境无法直连 HuggingFace。
DOWNLOAD_HOSTS: tuple[str, ...] = ("https://huggingface.co", "https://hf-mirror.com")
CONNECT_TIMEOUT = 30
READ_TIMEOUT = 600
CHUNK_SIZE = 1024 * 1024
# 判定「本地模型可用」时，权重文件需要匹配这些常见后缀。
MODEL_WEIGHT_SUFFIXES = (".safetensors", ".bin", ".pt", ".onnx", ".msgpack", ".h5")

# 常见平台给出一组可直接采用的 embedding 默认值（模型 ID, 向量维度）。
# 未列出的平台需要用户自行填写，避免写出错误维度导致向量库与模型不匹配。
EMBEDDING_PLATFORM_DEFAULTS: dict[str, tuple[str, int]] = {
    "openai": ("text-embedding-3-small", 1536),
    "aliyun": ("text-embedding-v3", 1024),
    "siliconflow": ("BAAI/bge-m3", 1024),
    "openrouter": ("openai/text-embedding-3-small", 1536),
}

_EMBEDDING_PATH_SUFFIX = re.compile(r"/chat/completions/?$")


class EmbeddingSetupError(Exception):
    """上传 / 配置阶段可以直接反馈给用户的错误。"""


# --------------------------------------------------------------------------- #
# 本地模型状态
# --------------------------------------------------------------------------- #
def local_model_present(path: str | Path) -> bool:
    """模型目录里同时存在配置文件与权重文件时，才认为可以加载。"""
    root = Path(path)
    if not root.is_dir():
        return False
    if not (root / "config.json").is_file():
        return False
    return any(
        item.is_file() and item.suffix.lower() in MODEL_WEIGHT_SUFFIXES
        for item in root.rglob("*")
    )


def count_model_files(path: str | Path) -> int:
    root = Path(path)
    if not root.is_dir():
        return 0
    return sum(1 for item in root.rglob("*") if item.is_file())


# --------------------------------------------------------------------------- #
# 后台下载任务
# --------------------------------------------------------------------------- #
class EmbeddingDownloadJob:
    """串行化的后台下载任务，任何时刻最多只有一个下载在跑。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._state = "idle"
        self._message = ""
        self._error = ""
        self._host = ""
        self._total = 0
        self._completed = 0
        self._thread: threading.Thread | None = None

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "state": self._state,
                "message": self._message,
                "error": self._error,
                "host": self._host,
                "total_files": self._total,
                "completed_files": self._completed,
                "running": self._state == "running",
            }

    def start(self, dest_dir: str | Path, repo: str) -> bool:
        """启动下载；已有任务在跑时返回 ``False``。"""
        with self._lock:
            if self._state == "running":
                return False
            self._state = "running"
            self._message = "正在连接模型仓库…"
            self._error = ""
            self._host = ""
            self._total = 0
            self._completed = 0
        self._thread = threading.Thread(
            target=self._run,
            args=(Path(dest_dir), repo),
            name="embedding-download",
            daemon=True,
        )
        self._thread.start()
        return True

    def _run(self, dest_dir: Path, repo: str) -> None:
        failures: list[str] = []
        for host in DOWNLOAD_HOSTS:
            try:
                self._download_from(host, repo, dest_dir)
                return
            except Exception as exc:  # 换一个镜像继续尝试，最终统一汇总失败原因
                logger.warning("embedding_download_host_failed host=%s repo=%s error=%s", host, repo, exc)
                failures.append(f"{host}: {exc}")
        with self._lock:
            self._state = "error"
            self._message = "模型下载失败"
            self._error = "; ".join(failures) or "unknown error"

    def _download_from(self, host: str, repo: str, dest_dir: Path) -> None:
        with self._lock:
            self._host = host
            self._message = f"正在从 {host} 获取文件列表…"
        names = _list_repo_files(host, repo)
        if not names:
            raise RuntimeError("仓库没有可下载的文件")
        dest_dir.mkdir(parents=True, exist_ok=True)
        with self._lock:
            self._total = len(names)
            self._completed = 0
            self._message = f"正在从 {host} 下载 {len(names)} 个文件…"
        for name in names:
            _download_repo_file(host, repo, name, dest_dir)
            with self._lock:
                self._completed += 1
        with self._lock:
            self._state = "done"
            self._message = "模型下载完成"
            self._error = ""


embedding_job = EmbeddingDownloadJob()


def _list_repo_files(host: str, repo: str) -> list[str]:
    response = requests.get(f"{host}/api/models/{repo}", timeout=CONNECT_TIMEOUT)
    response.raise_for_status()
    payload = response.json()
    siblings = payload.get("siblings") if isinstance(payload, dict) else None
    names: list[str] = []
    for item in siblings or []:
        if isinstance(item, dict) and item.get("rfilename"):
            names.append(str(item["rfilename"]))
    return names


def _download_repo_file(host: str, repo: str, name: str, dest_dir: Path) -> None:
    url = f"{host}/{repo}/resolve/main/{quote(name, safe='/')}"
    target = safe_join(dest_dir, *name.split("/"))
    target.parent.mkdir(parents=True, exist_ok=True)
    temp_path = target.with_name(target.name + ".part")
    with requests.get(url, timeout=(CONNECT_TIMEOUT, READ_TIMEOUT), stream=True) as response:
        response.raise_for_status()
        with open(temp_path, "wb") as handle:
            for chunk in response.iter_content(CHUNK_SIZE):
                if chunk:
                    handle.write(chunk)
    temp_path.replace(target)


# --------------------------------------------------------------------------- #
# 上传模型文件
# --------------------------------------------------------------------------- #
def store_uploaded_files(files: Iterable[Any], dest_dir: str | Path) -> dict[str, Any]:
    """把上传的模型文件写入模型目录。

    支持两种形态：单个 ``.zip`` 压缩包（常见于从 HuggingFace 下载整个仓库后打包），
    或多个散文件（浏览器拖拽目录或批量选择文件）。压缩包 / 目录若带有一层顶层
    文件夹，会自动展开，保证 ``config.json`` 落在模型目录根部。
    """
    dest = Path(dest_dir)
    uploads = [item for item in files if item is not None and getattr(item, "filename", "")]
    if not uploads:
        raise EmbeddingSetupError("请选择要上传的模型文件或压缩包。")

    dest.mkdir(parents=True, exist_ok=True)
    if len(uploads) == 1 and str(uploads[0].filename).lower().endswith(".zip"):
        written = _extract_zip(uploads[0].stream, dest)
    else:
        written = _write_loose_files([(str(item.filename), item.stream) for item in uploads], dest)
    if written <= 0:
        raise EmbeddingSetupError("上传内容里没有可用的模型文件。")

    present = local_model_present(dest)
    logger.info("embedding_upload_stored files=%s present=%s", written, present)
    return {"written": written, "local_model_present": present, "local_model_files": count_model_files(dest)}


def _safe_parts(raw_name: str) -> list[str]:
    parts = [part for part in re.split(r"[\\/]+", raw_name) if part not in ("", ".", "..")]
    return [part for part in parts if part != "__MACOSX"]


def _common_top_folder(names: list[str]) -> str | None:
    """当所有文件都位于同一个顶层目录下时返回该目录名，用于自动展开一层。"""
    tops = {name.split("/", 1)[0] for name in names if "/" in name}
    if len(tops) != 1:
        return None
    top = next(iter(tops))
    if top and all(name.startswith(f"{top}/") for name in names):
        return top
    return None


def _extract_zip(stream: Any, dest: Path) -> int:
    try:
        archive = zipfile.ZipFile(stream)
    except zipfile.BadZipFile as exc:
        raise EmbeddingSetupError("压缩包无法解析，请重新压缩后再试。") from exc

    members = [member for member in archive.infolist() if not member.is_dir()]
    if not members:
        raise EmbeddingSetupError("压缩包里没有文件。")
    if sum(max(0, member.file_size) for member in members) > EXTRACT_MAX_BYTES:
        raise EmbeddingSetupError("压缩包解压后体积超过上限。")

    normalized = {member.filename: _safe_parts(member.filename) for member in members}
    top = _common_top_folder([name for name in normalized if "/" in name])

    written = 0
    extracted_bytes = 0
    for member in members:
        parts = normalized[member.filename]
        if not parts:
            continue
        if top and parts[0] == top:
            parts = parts[1:]
        if not parts:
            continue
        try:
            target = safe_join(dest, *parts)
        except ValueError as exc:
            raise EmbeddingSetupError("压缩包里包含非法路径。") from exc
        target.parent.mkdir(parents=True, exist_ok=True)
        with archive.open(member) as source, open(target, "wb") as out:
            while True:
                chunk = source.read(1024 * 1024)
                if not chunk:
                    break
                extracted_bytes += len(chunk)
                # 声明大小可以伪造，按实际写入字节数再兜底一次。
                if extracted_bytes > EXTRACT_MAX_BYTES:
                    out.close()
                    target.unlink(missing_ok=True)
                    raise EmbeddingSetupError("压缩包解压后体积超过上限。")
                out.write(chunk)
        written += 1
    return written


def _write_loose_files(entries: list[tuple[str, Any]], dest: Path) -> int:
    top = _common_top_folder([name for name, _ in entries if "/" in name])
    written = 0
    for name, stream in entries:
        parts = _safe_parts(name)
        if not parts:
            continue
        if top and parts[0] == top:
            parts = parts[1:]
        if not parts:
            continue
        try:
            target = safe_join(dest, *parts)
        except ValueError as exc:
            raise EmbeddingSetupError("上传文件名包含非法路径。") from exc
        target.parent.mkdir(parents=True, exist_ok=True)
        with open(target, "wb") as out:
            shutil.copyfileobj(stream, out)
        written += 1
    return written


# --------------------------------------------------------------------------- #
# 云端 embedding
# --------------------------------------------------------------------------- #
def derive_embedding_base_url(chat_url: str) -> str:
    """由对话接口地址推导 embedding 接口地址（OpenAI 兼容约定）。"""
    url = (chat_url or "").strip()
    if not url:
        return ""
    if url.endswith("/embeddings"):
        return url
    if _EMBEDDING_PATH_SUFFIX.search(url):
        return _EMBEDDING_PATH_SUFFIX.sub("/embeddings", url)
    return url.rstrip("/") + "/embeddings"


def build_embedding_platform_options(platform_dir: str | Path, secret_dir: str | Path) -> list[dict[str, Any]]:
    """列出已启用的 AI 平台及其可用的 embedding 默认值，供引导页选择。"""
    platform_root = Path(platform_dir)
    if not platform_root.exists():
        return []
    options: list[dict[str, Any]] = []
    for public_path in sorted(platform_root.glob("*.json")):
        if public_path.name == "default-request.json":
            continue
        config = load_platform_config(public_path, Path(secret_dir) / public_path.name)
        if not config.get("enabled"):
            continue
        platform_id = str(config.get("platform") or public_path.stem)
        platform_config = config.get("config") if isinstance(config.get("config"), dict) else {}
        embedding = config.get("embedding") if isinstance(config.get("embedding"), dict) else {}
        default_model, default_dimensions = EMBEDDING_PLATFORM_DEFAULTS.get(platform_id, ("", 0))
        options.append(
            {
                "platform": platform_id,
                "name": str(config.get("name") or platform_id),
                "has_api_key": bool(platform_config.get("api_key")),
                "embedding_base_url": str(
                    embedding.get("base_url") or derive_embedding_base_url(str(platform_config.get("base_url") or ""))
                ),
                "embedding_model": str(embedding.get("model") or default_model),
                "embedding_dimensions": int(embedding.get("dimensions") or default_dimensions or 0),
            }
        )
    return options


def set_cloud_embedding_enabled(config_path: str | Path, enabled: bool = True) -> None:
    """在 ``general.toml`` 的 ``[ai]`` 段写入 ``cloud_embedding_enabled``，保留其余内容。"""
    path = Path(config_path)
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    lines = _upsert_toml_bool(lines, "ai", "cloud_embedding_enabled", enabled)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_name(path.name + ".tmp")
    temp_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    temp_path.replace(path)


def _upsert_toml_bool(lines: list[str], section: str, key: str, value: bool) -> list[str]:
    entry = f"{key} = {'true' if value else 'false'}"
    start: int | None = None
    end = len(lines)
    for index, raw in enumerate(lines):
        stripped = raw.split("#", 1)[0].strip()
        if not (stripped.startswith("[") and stripped.endswith("]")):
            continue
        name = stripped[1:-1].strip().strip('"').strip("'")
        if start is None:
            if name == section:
                start = index
        else:
            end = index
            break

    if start is None:
        if lines and lines[-1].strip():
            lines.append("")
        lines.append(f"[{section}]")
        lines.append(entry)
        return lines

    for index in range(start + 1, end):
        stripped = lines[index].split("#", 1)[0].strip()
        if "=" in stripped and stripped.split("=", 1)[0].strip().strip('"').strip("'") == key:
            lines[index] = entry
            return lines
    lines.insert(end, entry)
    return lines
