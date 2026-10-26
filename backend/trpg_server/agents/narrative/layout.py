"""确定性布局（设计文档 §8.2）。

仅用于可视化：按 act/area 分层，同一父节点下的子节点横向排列，
资产节点挂在所属定位节点下方。相同输入必须得到相同坐标。
"""
from __future__ import annotations

from trpg_server.agents.narrative.model import NarrativeBundle, NarrativeNode

_LAYER_HEIGHT = 180
_COLUMN_WIDTH = 220
_ASSET_GAP = 120
_TREE_KINDS = {"act", "area", "location", "ending"}


def apply_layout(bundle: NarrativeBundle) -> int:
    children: dict[str | None, list[NarrativeNode]] = {}
    for node in bundle.nodes:
        if node.kind not in _TREE_KINDS:
            continue
        children.setdefault(node.parent, []).append(node)
    for values in children.values():
        values.sort(key=lambda item: (item.order, item.id))

    positioned: set[str] = set()

    def place(node: NarrativeNode, x: int, y: int) -> None:
        node.layout = {"x": x, "y": y, "width": 180, "height": 80}
        positioned.add(node.id)
        kids = children.get(node.id) or []
        for index, child in enumerate(kids):
            place(child, x + index * _COLUMN_WIDTH, y + _LAYER_HEIGHT)

    for index, root in enumerate(children.get(None) or []):
        place(root, index * _COLUMN_WIDTH, 0)

    # 资产节点：挂在父节点下方；父节点不存在时本层追加。
    asset_y: dict[str | None, int] = {}
    for node in bundle.nodes:
        if node.kind in _TREE_KINDS:
            continue
        parent = node.parent if node.parent in positioned else None
        base = bundle.node(parent) if parent else None
        base_y = int(base.layout.get("y", 0)) + 110 if base else -110
        offset = asset_y.get(parent, 0)
        node.layout = {
            "x": int(base.layout.get("x", 0)) if base else offset,
            "y": base_y + offset,
            "width": 160,
            "height": 60,
        }
        asset_y[parent] = offset + _ASSET_GAP
        positioned.add(node.id)
    return len(positioned)


__all__ = ["apply_layout"]
