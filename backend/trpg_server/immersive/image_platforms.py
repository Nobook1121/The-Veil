"""生图模型平台配置：多条目、预设、默认平台与旧配置迁移。

这是实时配图「外部生图」的唯一事实来源：

- 每条平台一份公开配置（名称、协议、Base URL、模型、尺寸、超时、启用状态），
  密钥单独存放在运行时目录，列表接口只回掩码；
- 默认平台记录在 ``[immersive] illustration_platform_id``，写入沿用
  :mod:`trpg_server.config.writer`（tomlkit 保留注释），不再出现第二份参数；
- 旧的单条配置（``illustration_base_url`` / ``illustration_model`` /
  ``illustration_size`` / ``api_key``）只在首次迁移时读取一次，迁移后清空
  密钥与地址，运行期只认平台表；
- 所有写操作写审计日志（谁改的、改了什么、结果），密钥不回显。

本模块不依赖 Flask：目录由调用方注入，便于控制台、后台线程与测试复用。
"""
from __future__ import annotations

import re
import time
from copy import deepcopy
from pathlib import Path
from typing import Any

import requests

from trpg_server.agents.http_retry import post_with_retry
from trpg_server.config import load_settings, update_sections
from trpg_server.config.paths import general_config_path
from trpg_server.immersive import audit
from trpg_server.json_store import read_json, write_json_atomic
from trpg_server.settings import RUNTIME_DIR

# 目前只实现 OpenAI 兼容的 /v1/images/generations；新增协议时在此登记并在
# ``_images_endpoint`` 里补端点即可，前端下拉与校验会自动跟随。
PROTOCOLS: tuple[str, ...] = ("openai_images",)
PROTOCOL_LABELS = {"openai_images": "OpenAI 兼容 /v1/images/generations"}

# 内置预设：只用于「快速填空」，管理员可改成自己的中转/网关地址。
PRESETS: tuple[dict[str, Any], ...] = (
    {
        "preset": "openai",
        "name": "OpenAI",
        "protocol": "openai_images",
        "base_url": "https://api.openai.com",
        "model": "gpt-image-1",
        "size": "1024x1024",
        "timeout": 120,
    },
    {
        "preset": "siliconflow",
        "name": "SiliconFlow",
        "protocol": "openai_images",
        "base_url": "https://api.siliconflow.cn",
        "model": "Kwai-Kolors/Kolors",
        "size": "1024x1024",
        "timeout": 120,
    },
    {
        "preset": "aliyun",
        "name": "阿里云百炼",
        "protocol": "openai_images",
        "base_url": "https://dashscope.aliyuncs.com/compatible-mode",
        "model": "wanx2.1-t2i-turbo",
        "size": "1024x1024",
        "timeout": 150,
    },
    {
        "preset": "zhipu",
        "name": "智谱 GLM",
        "protocol": "openai_images",
        "base_url": "https://open.bigmodel.cn/api/paas/v4",
        "model": "cogview-3-flash",
        "size": "1024x1024",
        "timeout": 150,
    },
    {
        "preset": "custom",
        "name": "自定义 / 中转",
        "protocol": "openai_images",
        "base_url": "",
        "model": "",
        "size": "1024x1024",
        "timeout": 120,
    },
)

_ID_RE = re.compile(r"^[a-zA-Z0-9_-]{1,64}$")
_SIZE_RE = re.compile(r"^(\d{2,5}x\d{2,5}|auto)$")
_LEGACY_PLATFORM_ID = "legacy-default"


def _root(config_dir: Any) -> Path:
    if config_dir:
        return Path(config_dir)
    from trpg_server.config.paths import resolve_config_dir

    return Path(resolve_config_dir(None))


def public_dir(config_dir: Any = None) -> Path:
    return _root(config_dir) / "imageplatform"


def secret_dir(config_dir: Any = None, secret_dir_override: Any = None) -> Path:
    """密钥目录默认在运行时目录（不会被静态路由直出）；测试可显式注入。"""
    if secret_dir_override:
        return Path(secret_dir_override)
    return RUNTIME_DIR / "config" / "imageplatform"


def _public_path(config_dir: Any, platform_id: str) -> Path:
    return public_dir(config_dir) / f"{platform_id}.json"


def _secret_path(config_dir: Any, platform_id: str, secret_dir_override: Any = None) -> Path:
    return secret_dir(config_dir, secret_dir_override) / f"{platform_id}.json"


def presets() -> list[dict[str, Any]]:
    return [deepcopy(item) for item in PRESETS]


def _slug(text: str) -> str:
    value = re.sub(r"[^a-zA-Z0-9_-]+", "-", str(text or "").strip().lower()).strip("-")
    return value[:48]


def _mask(key: str) -> str:
    text = str(key or "")
    if not text:
        return ""
    if len(text) < 10:
        return "已配置"
    return f"{text[:4]}…{text[-4:]}"


def _as_int(value: Any, default: int, minimum: int, maximum: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        number = default
    return max(minimum, min(maximum, number))


def _validate(payload: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    name = str(payload.get("name") or "").strip()
    if not name:
        errors.append("名称不能为空")
    protocol = str(payload.get("protocol") or "openai_images").strip()
    if protocol not in PROTOCOLS:
        errors.append(f"协议必须是 {list(PROTOCOLS)}")
    base_url = str(payload.get("base_url") or "").strip()
    if base_url and not base_url.startswith(("http://", "https://")):
        errors.append("Base URL 必须以 http:// 或 https:// 开头")
    model = str(payload.get("model") or "").strip()
    if not model:
        errors.append("模型名不能为空")
    size = str(payload.get("size") or "1024x1024").strip()
    if not _SIZE_RE.match(size):
        errors.append("尺寸格式应为 1024x1024 或 auto")
    return errors


def _normalized(payload: dict[str, Any], platform_id: str) -> dict[str, Any]:
    now = time.strftime("%Y-%m-%d %H:%M:%S")
    return {
        "id": platform_id,
        "name": str(payload.get("name") or platform_id).strip()[:80],
        "protocol": str(payload.get("protocol") or "openai_images").strip(),
        "base_url": str(payload.get("base_url") or "").strip().rstrip("/")[:512],
        "model": str(payload.get("model") or "").strip()[:120],
        "size": str(payload.get("size") or "1024x1024").strip()[:32],
        "timeout": _as_int(payload.get("timeout"), 120, 20, 600),
        "enabled": bool(payload.get("enabled", True)),
        "created_at": str(payload.get("created_at") or now)[:32],
        "updated_at": now,
    }


def _load_raw(config_dir: Any, platform_id: str) -> dict[str, Any] | None:
    if not _ID_RE.match(str(platform_id or "")):
        return None
    data = read_json(_public_path(config_dir, platform_id), default=None)
    return data if isinstance(data, dict) else None


def default_platform_id(config_dir: Any = None) -> str:
    settings = load_settings(_root(config_dir))
    raw = settings.data.get("immersive", {})
    section = raw if isinstance(raw, dict) else {}
    return str(section.get("illustration_platform_id") or "").strip()


def _with_secret(config_dir: Any, platform: dict[str, Any], secret_dir_override: Any, include_secret: bool) -> dict[str, Any]:
    platform_id = str(platform.get("id") or "")
    secret = read_json(_secret_path(config_dir, platform_id, secret_dir_override), default={})
    api_key = str(secret.get("api_key") or "") if isinstance(secret, dict) else ""
    result = dict(platform)
    result["has_api_key"] = bool(api_key)
    result["api_key_masked"] = _mask(api_key)
    if include_secret:
        result["api_key"] = api_key
    return result


def list_platforms(config_dir: Any = None, secret_dir_override: Any = None) -> list[dict[str, Any]]:
    """列出全部平台（公开视图，密钥只回掩码），并标记当前默认平台。"""
    target = public_dir(config_dir)
    default_id = default_platform_id(config_dir)
    items: list[dict[str, Any]] = []
    if target.exists():
        for path in sorted(target.glob("*.json")):
            platform_id = path.stem
            if platform_id == "default":
                continue
            raw = _load_raw(config_dir, platform_id)
            if raw is None:
                continue
            platform = _with_secret(config_dir, raw, secret_dir_override, include_secret=False)
            platform["is_default"] = platform_id == default_id
            items.append(platform)
    return items


def get_platform(config_dir: Any = None, platform_id: str = "", secret_dir_override: Any = None, *, include_secret: bool = True) -> dict[str, Any] | None:
    raw = _load_raw(config_dir, platform_id)
    if raw is None:
        return None
    platform = _with_secret(config_dir, raw, secret_dir_override, include_secret=include_secret)
    platform["is_default"] = platform_id == default_platform_id(config_dir)
    return platform


def save_platform(
    config_dir: Any,
    payload: dict[str, Any],
    *,
    secret_dir_override: Any = None,
    actor: dict[str, Any] | str | None = None,
    audit_path: Path | None = None,
) -> dict[str, Any]:
    """新增或更新一条平台；返回公开视图。校验失败抛 ``ValueError``。"""
    raw_id = str(payload.get("id") or "").strip()
    platform_id = raw_id or _slug(str(payload.get("name") or "")) or f"platform-{int(time.time())}"
    if not _ID_RE.match(platform_id):
        raise ValueError("平台 ID 只能包含字母、数字、下划线与短横线（1-64 位）")
    errors = _validate(payload)
    if errors:
        raise ValueError("；".join(errors))

    existing = _load_raw(config_dir, platform_id) or {}
    merged = dict(existing)
    merged.update(payload)
    merged["created_at"] = existing.get("created_at")
    normalized = _normalized(merged, platform_id)

    key_state = "unchanged"
    api_key = payload.get("api_key")
    clear_key = bool(payload.get("clear_key"))
    if clear_key:
        secret_file = _secret_path(config_dir, platform_id, secret_dir_override)
        secret_file.unlink(missing_ok=True)
        key_state = "cleared"
    elif isinstance(api_key, str) and api_key.strip():
        secret_file = _secret_path(config_dir, platform_id, secret_dir_override)
        secret_file.parent.mkdir(parents=True, exist_ok=True)
        write_json_atomic(secret_file, {"api_key": api_key.strip()[:4096]})
        key_state = "updated"

    public_path = _public_path(config_dir, platform_id)
    public_path.parent.mkdir(parents=True, exist_ok=True)
    write_json_atomic(public_path, normalized)

    audit.record(
        "platform.save",
        actor=actor,
        target=f"imageplatform:{platform_id}",
        detail={
            "name": normalized["name"],
            "protocol": normalized["protocol"],
            "base_url": normalized["base_url"],
            "model": normalized["model"],
            "size": normalized["size"],
            "timeout": normalized["timeout"],
            "enabled": normalized["enabled"],
            "key_state": key_state,
            "created": not bool(existing),
        },
        path=audit_path,
    )
    return get_platform(config_dir, platform_id, secret_dir_override, include_secret=False) or {}


def set_platform_secret(
    config_dir: Any,
    platform_id: str,
    api_key: str | None,
    *,
    secret_dir_override: Any = None,
    actor: dict[str, Any] | str | None = None,
    audit_path: Path | None = None,
) -> dict[str, Any] | None:
    """只轮换/清除某条平台的密钥，不改其他字段；密钥仅落运行时目录。"""
    platform = get_platform(config_dir, platform_id, secret_dir_override, include_secret=False)
    if platform is None:
        return None
    key_state = "cleared"
    if api_key is not None and str(api_key).strip():
        secret_file = _secret_path(config_dir, platform_id, secret_dir_override)
        secret_file.parent.mkdir(parents=True, exist_ok=True)
        write_json_atomic(secret_file, {"api_key": str(api_key).strip()[:4096]})
        key_state = "updated"
    else:
        _secret_path(config_dir, platform_id, secret_dir_override).unlink(missing_ok=True)
    audit.record(
        f"platform.secret_{key_state}",
        actor=actor,
        target=f"imageplatform:{platform_id}",
        detail={"key_state": key_state},
        path=audit_path,
    )
    return get_platform(config_dir, platform_id, secret_dir_override, include_secret=False)


def delete_platform(
    config_dir: Any,
    platform_id: str,
    *,
    secret_dir_override: Any = None,
    actor: dict[str, Any] | str | None = None,
    audit_path: Path | None = None,
) -> bool:
    if not _ID_RE.match(str(platform_id or "")):
        return False
    public_file = _public_path(config_dir, platform_id)
    if not public_file.exists():
        return False
    public_file.unlink(missing_ok=True)
    _secret_path(config_dir, platform_id, secret_dir_override).unlink(missing_ok=True)
    if default_platform_id(config_dir) == platform_id:
        _write_config({"illustration_platform_id": "", "illustration_provider": "procedural"}, config_dir)
    audit.record(
        "platform.delete",
        actor=actor,
        target=f"imageplatform:{platform_id}",
        detail={"deleted": True},
        path=audit_path,
    )
    return True


def _write_config(values: dict[str, Any], config_dir: Any = None) -> None:
    update_sections(general_config_path(_root(config_dir)), {"immersive": values})


def set_default_platform(
    config_dir: Any,
    platform_id: str,
    *,
    actor: dict[str, Any] | str | None = None,
    audit_path: Path | None = None,
) -> bool:
    if _load_raw(config_dir, platform_id) is None:
        return False
    _write_config({"illustration_provider": "platform", "illustration_platform_id": platform_id}, config_dir)
    audit.record(
        "platform.default",
        actor=actor,
        target=f"imageplatform:{platform_id}",
        detail={"is_default": True},
        path=audit_path,
    )
    return True


def resolve_platform(
    config_dir: Any,
    settings: dict[str, Any] | None = None,
    *,
    secret_dir_override: Any = None,
) -> dict[str, Any] | None:
    """解析当前生效的外部生图平台：显式默认 → 第一个启用的平台。"""
    if settings and str(settings.get("illustration_provider") or "") == "procedural":
        return None
    platform_id = str((settings or {}).get("illustration_platform_id") or "") or default_platform_id(config_dir)
    if platform_id:
        platform = get_platform(config_dir, platform_id, secret_dir_override)
        if platform and platform.get("enabled", True):
            return platform
    target = public_dir(config_dir)
    if not target.exists():
        return None
    for path in sorted(target.glob("*.json")):
        platform = get_platform(config_dir, path.stem, secret_dir_override)
        if platform and platform.get("enabled", True):
            return platform
    return None


_VERSION_SUFFIX_RE = re.compile(r"/v\d+$", re.IGNORECASE)


def _api_endpoint(base_url: str, suffix: str) -> str:
    """把 Base URL 规范成 ``…/v1<suffix>``（允许只填主机名或带版本号的前缀）。"""
    text = str(base_url or "").strip().rstrip("/")
    if not text:
        return ""
    if text.endswith(suffix):
        return text
    if _VERSION_SUFFIX_RE.search(text):
        return f"{text}{suffix}"
    return f"{text}/v1{suffix}"


def _images_endpoint(base_url: str) -> str:
    return _api_endpoint(base_url, "/images/generations")


def _models_endpoint(base_url: str) -> str:
    return _api_endpoint(base_url, "/models")


def probe_platform(
    config_dir: Any,
    platform_id: str,
    *,
    secret_dir_override: Any = None,
    post: Any = None,
    get: Any = None,
    timeout: int = 15,
    allow_generation: bool = True,
) -> dict[str, Any]:
    """连接测试：优先 ``GET /v1/models``，失败时退回一次最小生图探测。

    - 列出模型是免费且低延迟的，能在多数网关直接给出「这个 Key 能用」；
    - 部分生图端点不提供模型列表，此时才真正生成一张最小图（会产生一次费用，
      界面上必须提示）。
    """
    platform = get_platform(config_dir, platform_id, secret_dir_override, include_secret=True)
    if not platform:
        return {"ok": False, "method": "none", "error": "platform_not_found", "platform_id": platform_id}
    base_url = str(platform.get("base_url") or "").strip()
    api_key = str(platform.get("api_key") or "").strip()
    if not base_url:
        return {"ok": False, "method": "none", "error": "base_url_missing", "platform_id": platform_id}
    if not api_key:
        return {"ok": False, "method": "none", "error": "api_key_missing", "platform_id": platform_id}
    headers = {"Authorization": f"Bearer {api_key}"}
    started = time.monotonic()

    sender = get or requests.get
    try:
        response = sender(_models_endpoint(base_url), headers=headers, timeout=timeout)
        if int(getattr(response, "status_code", 0)) == 200:
            data = response.json()
            raw_models = data.get("data") if isinstance(data, dict) else None
            models: list[str] = []
            if isinstance(raw_models, list):
                for item in raw_models:
                    identifier = item.get("id") if isinstance(item, dict) else item
                    if identifier:
                        models.append(str(identifier))
            return {
                "ok": True,
                "method": "models",
                "models": models[:40],
                "elapsed_ms": round((time.monotonic() - started) * 1000, 1),
                "platform_id": platform_id,
                "note": "通过模型列表探测；未产生生图费用",
            }
    except Exception:  # noqa: BLE001 - 模型列表不可用是常态，继续做生成探测
        pass

    if not allow_generation:
        return {
            "ok": False,
            "method": "models",
            "error": "models_unavailable",
            "elapsed_ms": round((time.monotonic() - started) * 1000, 1),
            "platform_id": platform_id,
            "note": "模型列表不可用；已跳过最小生图探测",
        }

    payload = {
        "model": platform.get("model") or "gpt-image-1",
        "prompt": "connectivity probe: a plain grey gradient rectangle, no text, no watermark",
        "size": platform.get("size") or "1024x1024",
        "n": 1,
        "response_format": "b64_json",
    }
    probe_timeout = max(20, min(120, int(platform.get("timeout") or 60)))
    try:
        response = post_with_retry(
            _images_endpoint(base_url),
            headers={**headers, "Content-Type": "application/json"},
            payload=payload,
            timeout=probe_timeout,
            max_retries=0,
            post=post,
            label="immersive_image_probe",
        )
        status = int(getattr(response, "status_code", 0))
        if status >= 400:
            return {
                "ok": False,
                "method": "generation",
                "error": f"upstream_http_{status}:{str(getattr(response, 'text', ''))[:160]}",
                "elapsed_ms": round((time.monotonic() - started) * 1000, 1),
                "platform_id": platform_id,
            }
        data = response.json()
        items = data.get("data") if isinstance(data, dict) else None
        if not isinstance(items, list) or not items:
            return {
                "ok": False,
                "method": "generation",
                "error": "empty_image_response",
                "elapsed_ms": round((time.monotonic() - started) * 1000, 1),
                "platform_id": platform_id,
            }
        return {
            "ok": True,
            "method": "generation",
            "models": [],
            "elapsed_ms": round((time.monotonic() - started) * 1000, 1),
            "platform_id": platform_id,
            "note": "通过最小生图探测（会产生一次生图费用）",
        }
    except Exception as exc:  # noqa: BLE001 - 外部服务不可控
        return {
            "ok": False,
            "method": "generation",
            "error": str(exc)[:180],
            "elapsed_ms": round((time.monotonic() - started) * 1000, 1),
            "platform_id": platform_id,
        }


def migrate_legacy(
    config_dir: Any,
    *,
    secret_dir_override: Any = None,
    actor: dict[str, Any] | str | None = None,
    audit_path: Path | None = None,
) -> str | None:
    """把旧版单条生图配置迁移为一条平台记录。

    只在「平台表为空且旧配置确实填了地址与密钥」时执行一次；迁移后清空旧密钥与
    地址，运行期只读平台表，避免两套真相。返回迁移出的平台 ID（未迁移为 ``None``）。
    """
    target = public_dir(config_dir)
    if target.exists() and any(path.stem != "default" for path in target.glob("*.json")):
        return None
    settings = load_settings(_root(config_dir))
    raw = settings.data.get("immersive", {})
    section = raw if isinstance(raw, dict) else {}
    provider = str(section.get("illustration_provider") or "").strip()
    base_url = str(section.get("illustration_base_url") or "").strip()
    api_key = str(section.get("api_key") or "").strip()
    if provider not in {"openai_compatible", "platform"} or not base_url or not api_key:
        return None
    payload = {
        "id": _LEGACY_PLATFORM_ID,
        "name": "旧配置迁移",
        "protocol": "openai_images",
        "base_url": base_url,
        "model": str(section.get("illustration_model") or "dall-e-3"),
        "size": str(section.get("illustration_size") or "1024x1024"),
        "timeout": _as_int(section.get("illustration_timeout"), 120, 20, 600),
        "enabled": True,
        "api_key": api_key,
    }
    save_platform(config_dir, payload, secret_dir_override=secret_dir_override, actor=actor, audit_path=audit_path)
    _write_config(
        {
            "illustration_provider": "platform",
            "illustration_platform_id": _LEGACY_PLATFORM_ID,
            # 旧字段只用于迁移，迁移后清空密钥与地址，避免被继续当作第二份配置。
            "illustration_base_url": "",
            "api_key": "",
        },
        config_dir,
    )
    audit.record(
        "platform.migrate_legacy",
        actor=actor,
        target=f"imageplatform:{_LEGACY_PLATFORM_ID}",
        detail={"model": payload["model"], "size": payload["size"], "key_state": "migrated"},
        path=audit_path,
    )
    return _LEGACY_PLATFORM_ID
