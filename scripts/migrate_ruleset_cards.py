"""把旧角色卡一次性迁移到「信封 + sheet」结构。

默认只做 dry-run 预览；确认后加 ``--yes`` 才写入。

用法：
    python scripts/migrate_ruleset_cards.py
    python scripts/migrate_ruleset_cards.py --yes
    python scripts/migrate_ruleset_cards.py --characters-dir <PATH> --gallery-dir <PATH>
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "backend") not in sys.path:
    sys.path.insert(0, str(ROOT / "backend"))

from trpg_server.ruleset.migrations import migrate_character_cards  # noqa: E402
from trpg_server.settings import CHARACTER_GALLERY_DIR, CHARACTERS_DIR  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--characters-dir", default=str(CHARACTERS_DIR))
    parser.add_argument("--gallery-dir", default=str(CHARACTER_GALLERY_DIR))
    parser.add_argument("--yes", "-y", action="store_true", help="实际写入（默认仅预览）")
    args = parser.parse_args(argv)

    report = migrate_character_cards(
        Path(args.characters_dir),
        gallery_dir=Path(args.gallery_dir),
        dry_run=not args.yes,
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["counts"].get("failed", 0) == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
