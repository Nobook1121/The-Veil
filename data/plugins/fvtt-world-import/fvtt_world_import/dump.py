"""开发用 CLI：把 FVTT 世界包的 LevelDB 集合导出为 JSON。

用法::

    python -m fvtt_world_import.dump <zip 或解包目录> <输出目录>

- zip 输入会解包到临时目录再读；
- 每个集合输出 ``<collection>.json``（文档数组，含 ``_id``；点号集合如
  ``journal.pages`` 落盘为 ``journal.pages.json``）；
- 另输出 ``summary.json``（每集合文档数与耗时）。
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import time
import zipfile

from fvtt_world_import.leveldb import DbLimits, LevelDbError, LevelDbReader

WORLD_MARKER = "world.json"


def _extract_if_zip(source: str, workdir: str) -> str:
    if os.path.isdir(source):
        return source
    if not os.path.isfile(source):
        raise SystemExit(f"输入不存在：{source}")
    with zipfile.ZipFile(source) as archive:
        names = archive.namelist()
        if WORLD_MARKER not in names:
            raise SystemExit("zip 里没有 world.json——不是 FVTT 世界包")
        archive.extractall(workdir)
    for root, _dirs, files in os.walk(workdir):
        if WORLD_MARKER in files:
            return root
    return workdir


def _data_root(root: str) -> str:
    candidate = os.path.join(root, "data")
    if os.path.isdir(candidate):
        return candidate
    raise SystemExit(f"找不到 data/ 目录：{root}")


def main(argv: list[str] | None = None) -> int:
    args = argv if argv is not None else sys.argv[1:]
    if len(args) != 2:
        print(__doc__, file=sys.stderr)
        return 2
    source, out_dir = args
    os.makedirs(out_dir, exist_ok=True)

    started = time.time()
    merged: dict[str, list[dict]] = {}
    summary: dict[str, dict] = {}
    with tempfile.TemporaryDirectory(prefix="fvtt-dump-") as workdir:
        root = _extract_if_zip(source, workdir)
        data_root = _data_root(root)
        for collection in sorted(os.listdir(data_root)):
            db_dir = os.path.join(data_root, collection)
            if not os.path.isdir(db_dir):
                continue
            t0 = time.time()
            try:
                docs = LevelDbReader(db_dir, limits=DbLimits()).dump()
            except LevelDbError as exc:
                summary[collection] = {"error": str(exc)}
                continue
            for name, bucket in docs.items():
                merged.setdefault(name, []).extend(bucket)
                summary[name] = {
                    "count": len(merged[name]),
                    "seconds": round(time.time() - t0, 3),
                }
    for name, bucket in merged.items():
        with open(os.path.join(out_dir, f"{name}.json"), "w", encoding="utf-8") as handle:
            json.dump(bucket, handle, ensure_ascii=False, indent=2)
    summary["_meta"] = {
        "source": os.path.basename(source),
        "seconds": round(time.time() - started, 3),
    }
    with open(os.path.join(out_dir, "summary.json"), "w", encoding="utf-8") as handle:
        json.dump(summary, handle, ensure_ascii=False, indent=2)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
