"""外链曲目许可过滤。

为什么把许可单独成模块：

- 许可判定同时被「检索结果过滤」「缓存命中回放」「代理下载」三处使用，
  任何一处放宽都会让不合规曲目漏到播放器里；
- 归一化必须容忍各曲库五花八门的写法（URL / 短标签 / 带版本号 / 带 CC 前缀），
  但不能把 ``nc`` / ``nd`` 之类误判成可商用可改编的白名单项；
- 缺署名信息的曲目一律不播（宁可回到程序化底床，也不做「无署名播放」）。

白名单固定为 ``cc0`` / ``by`` / ``by-sa``，与设计文档 §6.3 一致。
"""
from __future__ import annotations

import re
from typing import Any, Iterable

ALLOWED_LICENSES: tuple[str, ...] = ("cc0", "by", "by-sa")
LICENSE_LABELS = {
    "cc0": "CC0 1.0",
    "by": "CC BY 4.0",
    "by-sa": "CC BY-SA 4.0",
    "by-nc": "CC BY-NC",
    "by-nd": "CC BY-ND",
    "by-nc-sa": "CC BY-NC-SA",
    "by-nc-nd": "CC BY-NC-ND",
    "unknown": "未知许可",
}
_REQUIRED_FIELDS = ("title", "artist", "license_url", "source_url", "attribution")

# 归一化中间结果：把各种写法折成「去版本号、去 cc 前缀、保留 -nc/-nd 组合」。
_STRICT_RE = re.compile(r"(by|cc)[- ]?(nc|nd|sa)?(?:[- ]?(nc|nd|sa))?", re.IGNORECASE)


def normalize_license(value: Any) -> str:
    """把任意许可写法折成白名单用到的短标签；无法识别时返回 ``unknown``。

    识别顺序：先看 URL 路径（``/publicdomain/zero/``、``/licenses/by-sa/4.0/``），
    再退回短标签。任何含 ``nc`` 或 ``nd`` 的组合都保持原样，便于调用方记录
    「为什么被拒绝」，但不会进入白名单。
    """
    text = str(value or "").strip().lower()
    if not text:
        return "unknown"
    if "publicdomain/zero" in text or "public-domain" in text or "publicdomain" in text:
        return "cc0"
    if "creativecommons.org" in text or "licenses/" in text:
        path = text.split("licenses/", 1)[-1] if "licenses/" in text else text
        token = path.split("/", 1)[0].strip()
        token = token.replace("_", "-")
        if token in {"by", "by-sa", "by-nc", "by-nd", "by-nc-sa", "by-nc-nd"}:
            return token
        return "unknown"
    cleaned = text.replace("cc-", "").replace("cc ", "").replace("_", "-").strip()
    cleaned = re.sub(r"\s+", "-", cleaned)
    # 去掉版本号 / deed 之类的尾巴：「CC BY-ND 4.0」「by-sa-3.0-unported」都归到短标签。
    cleaned = re.sub(
        r"(?:-(?:v?\d+(?:\.\d+)*|unported|international|legalcode|deed(?:\.\w+)?))+$",
        "",
        cleaned,
    ).strip("-")
    if cleaned in {"0", "zero", "cc0", "public-domain", "publicdomain"}:
        return "cc0"
    if cleaned in {"by", "by-sa", "by-nc", "by-nd", "by-nc-sa", "by-nc-nd"}:
        return cleaned
    match = _STRICT_RE.fullmatch(cleaned)
    if match:
        first, second = match.group(2), match.group(3)
        parts = [part for part in (first, second) if part]
        return "by" + ("" if not parts else "-" + "-".join(sorted(set(parts))))
    return "unknown"


def allowed_licenses(value: Any) -> list[str]:
    """解析配置里的许可白名单；非法项被丢弃，空表回落到默认白名单。"""
    if isinstance(value, (list, tuple)):
        raw = [str(item or "") for item in value]
    else:
        raw = str(value or "").split(",")
    result: list[str] = []
    for item in raw:
        canonical = normalize_license(item)
        if canonical in ALLOWED_LICENSES and canonical not in result:
            result.append(canonical)
    return result or list(ALLOWED_LICENSES)


def license_label(canonical: str) -> str:
    return LICENSE_LABELS.get(canonical, "未知许可")


def build_attribution(title: str, artist: str, canonical: str, source: str = "") -> str:
    """生成最小可用的署名串；缺任一必需字段时返回空串。"""
    clean_title = str(title or "").strip()
    clean_artist = str(artist or "").strip()
    if not clean_title or not clean_artist or canonical not in ALLOWED_LICENSES:
        return ""
    label = license_label(canonical)
    source_part = f" · {str(source).strip()}" if str(source or "").strip() else ""
    return f"{clean_title} — {clean_artist} ({label}{source_part})"


def missing_attribution_fields(track: dict[str, Any]) -> list[str]:
    """列出缺失的署名必需字段，便于日志记录与测试断言。"""
    return [
        field
        for field in _REQUIRED_FIELDS
        if not str(track.get(field) or "").strip()
    ]


def is_playable(track: dict[str, Any], allow: Iterable[str] | None = None) -> tuple[bool, str]:
    """判断曲目能否进入播放候选；返回 ``(可用, 原因)``。"""
    canonical = normalize_license(track.get("license"))
    if canonical not in set(allow or ALLOWED_LICENSES):
        return False, f"license_rejected:{canonical}"
    # 署名串是可推导的：只要标题/作者/许可/来源齐全就能生成；
    # 真正缺信息（例如曲库只给了一个裸流地址）才拒绝播放。
    missing = [
        field
        for field in ("title", "artist", "license_url", "source_url")
        if not str(track.get(field) or "").strip()
    ]
    if missing:
        return False, "attribution_incomplete"
    if not str(track.get("attribution") or "").strip():
        derived = build_attribution(
            str(track.get("title") or ""),
            str(track.get("artist") or ""),
            canonical,
            str(track.get("provider") or "").title(),
        )
        if not derived:
            return False, "attribution_incomplete"
    stream = str(track.get("stream_url") or "").strip().lower()
    if not stream.startswith("https://"):
        return False, "insecure_stream"
    return True, "ok"
