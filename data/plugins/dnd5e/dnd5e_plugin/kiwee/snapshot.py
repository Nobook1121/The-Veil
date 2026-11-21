"""kiwee 深库快照脚本：fetch（GitHub 稀疏检出）与 build（SQLite 建库）。

用法（把插件目录挂上 sys.path，例如 ``cd data/plugins/dnd5e`` 后）::

    python -m dnd5e_plugin.kiwee snapshot fetch [--ref cn2.0] [--force]
    python -m dnd5e_plugin.kiwee snapshot build [--no-fts]
    # 等价直跑模块：python -m dnd5e_plugin.kiwee.snapshot fetch / build

获取走 **GitHub 稀疏检出版**（``tjliqy/5etools-cn`` @ cn2.0，data/ 约 117MB）：
整仓约 760MB（含图片资产），``--filter=blob:none`` + sparse-checkout 只拉
v1 收录口径的 data/ 子集。**绝不批量爬活站**——robots.txt 对
``https://5e.kiwee.top/data/`` 是 Disallow，活站 JSON 只作单文件按需兜底
（本刀不实现，见调研文档 §6 方案 B）。

v1 收录范围（范围修订见 docs/plans/dnd5e-vtt-refactor-plan.md §4 kiwee 段）：
spells/、bestiary/、items.json、feats.json、races.json、class/、
backgrounds.json、conditionsdiseases.json、tables.json、variantrules.json。
**不收** adventure/ 与 book/（模组全文 48MB+23MB，留待后续刀）。

产物（**不进 git**，.gitignore 覆盖 kiwee-data/；许可见 NOTICE-KIWEE.md）::

    data/plugins/dnd5e/kiwee-data/raw/          # 5etools-cn data/ 子集
    data/plugins/dnd5e/kiwee-data/kiwee.sqlite3 # 建好的深库（FTS5）

fetch 断点语义：raw/ 已存在（有真实 json）则跳过整次抓取；--force 重抓。
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from .db import (
    SOURCE_REF_DEFAULT,
    SOURCE_REPO_URL,
    build_kiwee_db,
)

# v1 收录口径的稀疏检出 patterns（.gitignore 语义，锚定仓库根）。
# 与 db.CATEGORY_SPECS 的文件 glob 保持同一范围；改范围两处一起改。
SPARSE_PATTERNS: tuple[str, ...] = (
    "/data/spells/**",
    "/data/bestiary/**",
    "/data/class/**",
    "/data/items.json",
    "/data/feats.json",
    "/data/races.json",
    "/data/backgrounds.json",
    "/data/conditionsdiseases.json",
    "/data/tables.json",
    "/data/variantrules.json",
)

_PROVENANCE_NAME = ".provenance.json"


# --- 路径 -------------------------------------------------------------------


def kiwee_data_dir() -> Path:
    """本地生成物根：``data/plugins/dnd5e/kiwee-data/``（不进 git）。"""
    return Path(__file__).resolve().parents[2] / "kiwee-data"


def raw_dir_default() -> Path:
    return kiwee_data_dir() / "raw"


def db_path_default() -> Path:
    return kiwee_data_dir() / "kiwee.sqlite3"


# --- fetch ------------------------------------------------------------------


def _run_git(args: list[str], *, capture: bool = False) -> str:
    try:
        proc = subprocess.run(
            args,
            capture_output=capture,
            text=capture,
            check=False,
        )
    except FileNotFoundError as exc:
        raise RuntimeError("未找到 git 命令；fetch 需要 git ≥ 2.25（sparse-checkout）。") from exc
    if proc.returncode != 0:
        stderr = (proc.stderr or "") if capture else "（进度见上方 git 输出）"
        raise RuntimeError(f"git 命令失败（exit {proc.returncode}）：{' '.join(args[:3])}…\n{stderr}")
    return (proc.stdout or "").strip() if capture else ""


def _raw_has_snapshot(raw_dir: Path) -> bool:
    if not raw_dir.is_dir():
        return False
    return any(p.is_file() and p.suffix == ".json" and p.name != _PROVENANCE_NAME for p in raw_dir.rglob("*"))


def _copy_data_tree(src: Path, dst: Path) -> tuple[int, int]:
    """把稀疏检出的 ``staging/data/`` 原样搬进 raw/，返回 (文件数, 字节数)。"""
    files = bytes_total = 0
    for item in sorted(src.rglob("*")):
        if not item.is_file():
            continue
        target = dst / item.relative_to(src)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(item, target)
        files += 1
        bytes_total += item.stat().st_size
    return files, bytes_total


def fetch_snapshot(
    raw_dir: str | Path,
    *,
    repo: str = SOURCE_REPO_URL,
    ref: str = SOURCE_REF_DEFAULT,
    force: bool = False,
) -> dict[str, Any]:
    """稀疏检出 5etools-cn 的 data/ 到 raw/。

    断点：raw/ 已有快照且未 --force → 直接跳过（返回 status=skipped）。
    """
    raw_path = Path(raw_dir)
    if _raw_has_snapshot(raw_path) and not force:
        return {
            "status": "skipped",
            "reason": "raw/ 已存在，跳过抓取（断点续跑；重新抓取请加 --force）",
            "raw_dir": str(raw_path),
        }

    if force and raw_path.exists():
        shutil.rmtree(raw_path)

    staging = raw_path.parent / ".staging-5etools-cn"
    if staging.exists():
        shutil.rmtree(staging)
    staging.parent.mkdir(parents=True, exist_ok=True)

    started = time.monotonic()
    try:
        _run_git(
            [
                "git",
                "clone",
                "--depth",
                "1",
                "--filter=blob:none",
                "--sparse",
                "--branch",
                ref,
                repo,
                str(staging),
            ]
        )
        _run_git(["git", "-C", str(staging), "sparse-checkout", "set", "--no-cone", *SPARSE_PATTERNS])
        commit = _run_git(["git", "-C", str(staging), "rev-parse", "HEAD"], capture=True)

        data_src = staging / "data"
        if not data_src.is_dir():
            raise RuntimeError("稀疏检出后未见 data/ 目录——patterns 或上游布局可能已变化。")
        files, bytes_total = _copy_data_tree(data_src, raw_path)

        provenance = {
            "repo": repo,
            "ref": ref,
            "commit": commit,
            "fetched_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "files": files,
            "bytes": bytes_total,
            "scope": "v1：spells/bestiary/items/feats/races/class/backgrounds/conditionsdiseases/tables/variantrules；不含 adventure/book",
        }
        raw_path.mkdir(parents=True, exist_ok=True)
        (raw_path / _PROVENANCE_NAME).write_text(
            json.dumps(provenance, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    finally:
        shutil.rmtree(staging, ignore_errors=True)

    return {
        "status": "ok",
        "raw_dir": str(raw_path),
        "repo": repo,
        "ref": ref,
        "commit": commit,
        "files": files,
        "bytes": bytes_total,
        "duration_ms": int((time.monotonic() - started) * 1000),
    }


# --- build ------------------------------------------------------------------


def build_snapshot(
    raw_dir: str | Path,
    db_path: str | Path,
    *,
    use_fts: bool | None = None,
) -> dict[str, Any]:
    """raw/ → kiwee.sqlite3；统计与降级信息见 db.build_kiwee_db。"""
    return build_kiwee_db(raw_dir, db_path, use_fts=use_fts)


# --- CLI --------------------------------------------------------------------


def _fmt_bytes(n: int) -> str:
    value = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{value:.1f} {unit}" if unit != "B" else f"{int(value)} B"
        value /= 1024
    return f"{n} B"


def _print_fetch_result(result: dict[str, Any]) -> None:
    if result["status"] == "skipped":
        print(f"[kiwee] fetch: 跳过——{result['reason']}")
        return
    print(
        f"[kiwee] fetch: 完成——文件 {result['files']} 个 / {_fmt_bytes(result['bytes'])}，"
        f"ref={result['ref']}，commit={result['commit'][:12]}，"
        f"耗时 {result['duration_ms'] / 1000:.1f}s → {result['raw_dir']}"
    )


def _print_build_result(stats: dict[str, Any]) -> None:
    cats = stats["categories"]
    cat_line = " / ".join(f"{cat} {n}" for cat, n in sorted(cats.items()) if n)
    print(
        f"[kiwee] build: 完成——条目 {stats['entries']}（{cat_line}），"
        f"机翻标记 {stats['machine_translated']}，重复跳过 {stats['duplicates']}，"
        f"FTS5={'开启' if stats['use_fts'] else '关闭（LIKE 降级）'}，"
        f"耗时 {stats['duration_ms'] / 1000:.1f}s，库 {_fmt_bytes(stats['db_bytes'])} → {stats['db_path']}"
    )


def main(argv: list[str] | None = None) -> int:
    """CLI 入口；兼容 ``snapshot fetch`` 与直接 ``fetch`` 两种写法。"""
    args = list(sys.argv[1:] if argv is None else argv)
    if args and args[0] == "snapshot":
        args = args[1:]

    parser = argparse.ArgumentParser(
        prog="python -m dnd5e_plugin.kiwee",
        description="kiwee 深库快照工具：fetch 拉取 GitHub 数据，build 建本地 SQLite 库。",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_fetch = sub.add_parser("fetch", help="从 tjliqy/5etools-cn 稀疏检出 data/ 到 kiwee-data/raw/")
    p_fetch.add_argument("--ref", default=SOURCE_REF_DEFAULT, help=f"分支/tag（默认 {SOURCE_REF_DEFAULT}）")
    p_fetch.add_argument("--repo", default=SOURCE_REPO_URL, help="上游仓库（默认官方镜像仓库）")
    p_fetch.add_argument("--raw-dir", default=str(raw_dir_default()), help="快照目录（默认 data/plugins/dnd5e/kiwee-data/raw）")
    p_fetch.add_argument("--force", action="store_true", help="raw/ 已存在时强制重抓")

    p_build = sub.add_parser("build", help="把 raw/ 建成 kiwee.sqlite3（FTS5 优先，不可用降级 LIKE）")
    p_build.add_argument("--raw-dir", default=str(raw_dir_default()), help="快照目录")
    p_build.add_argument("--db", default=str(db_path_default()), help="库文件路径（默认 data/plugins/dnd5e/kiwee-data/kiwee.sqlite3）")
    p_build.add_argument("--no-fts", action="store_true", help="禁用 FTS5（测试降级路径用）")

    parsed = parser.parse_args(args)

    if parsed.command == "fetch":
        try:
            result = fetch_snapshot(
                parsed.raw_dir,
                repo=parsed.repo,
                ref=parsed.ref,
                force=parsed.force,
            )
        except RuntimeError as exc:
            print(f"[kiwee] fetch: 失败——{exc}", file=sys.stderr)
            return 1
        _print_fetch_result(result)
        return 0

    if parsed.command == "build":
        try:
            stats = build_snapshot(parsed.raw_dir, parsed.db, use_fts=False if parsed.no_fts else None)
        except (FileNotFoundError, RuntimeError) as exc:
            print(f"[kiwee] build: 失败——{exc}", file=sys.stderr)
            return 1
        _print_build_result(stats)
        return 0

    parser.error(f"未知子命令：{parsed.command}")
    return 2


if __name__ == "__main__":
    sys.exit(main())
