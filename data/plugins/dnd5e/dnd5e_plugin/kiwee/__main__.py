"""``python -m dnd5e_plugin.kiwee snapshot fetch/build`` 的包入口 shim。"""

from __future__ import annotations

import sys

from .snapshot import main

if __name__ == "__main__":
    sys.exit(main())
