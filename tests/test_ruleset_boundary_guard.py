"""规则集边界机械守卫（阶段 4 P0：启动路径注册表解耦）。

扫描 ``backend/trpg_server`` 全部模块的 import 期耦合：``ruleset.coc7`` /
``combat.rulesets.coc7`` 只允许出现在显式登记的白名单里。函数体内 / 延迟
import 同样算——AST 扫描覆盖 Import / ImportFrom 节点、
``import_module("...")`` 字符串调用与模块级字符串常量（装配点的插件模块
清单）。docstring 里的 ``:mod:`` 引用不算耦合，不会被误报。

白名单 = 按设计持有模块清单的 composition root（bootstrap）+ coc7 自身的
战斗实现与惰性 re-export。白名单外出现任何 import 期耦合直接失败，防止
解耦回潮；如确属新债务，先在 KNOWN_DEBT_FILES 登记并注明批次归属。

仿照 ``tests/test_plugin_kernel.py`` 的零依赖守卫手法：不 mock、不启动
服务，只做静态结构断言。
"""

from __future__ import annotations

import ast
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
BACKEND_ROOT = REPO_ROOT / "backend" / "trpg_server"

BANNED_PREFIXES = (
    "trpg_server.ruleset.coc7",
    "trpg_server.combat.rulesets.coc7",
)

# coc7 插件包自身不在扫描范围：插件引用自己的内部模块是设计本意。
PLUGIN_PACKAGE_DIRS = (
    BACKEND_ROOT / "ruleset" / "coc7",
)

# 显式登记的允许耦合位置（P1 批次后债务清零）：
# - ruleset/bootstrap.py：composition root，按设计持有插件模块清单
#   （``DEFAULT_PLUGIN_MODULES`` / ``TRPG_RULESET_PLUGINS``），这是全后端
#   唯一允许写出具体插件模块路径的位置。
# - combat/rulesets/coc7.py：coc7 的战斗实现本体。
# - combat/rulesets/__init__.py：惰性 re-export（``__getattr__`` 按需加载）。
# P1 清偿记录（2026-11-19）：room_chase / room_interlude / room_magic /
# room_rule_commands / room_sanity / agents/turn_pipeline 的 15 处
# （模块级 5 + 函数内 10）已全部改走注册表 + 能力层，条目随之删除。
KNOWN_DEBT_FILES = {
    "ruleset/bootstrap.py",
    "combat/rulesets/coc7.py",
    "combat/rulesets/__init__.py",
}


def _is_banned(target: str) -> bool:
    text = target.strip()
    return any(text == prefix or text.startswith(prefix + ".") for prefix in BANNED_PREFIXES)


def _import_targets(tree: ast.Module) -> set[str]:
    """全部 import 目标：含函数体内 / 延迟 import 与 ``import_module`` 字符串。"""
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            found.add(node.module)
        elif isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Attribute) and func.attr == "import_module":
                for arg in node.args:
                    if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                        found.add(arg.value)
    return found


def _module_level_strings(tree: ast.Module) -> set[str]:
    """模块级赋值里的字符串常量：装配点按设计持有插件模块清单。"""
    found: set[str] = set()
    for node in tree.body:
        values = []
        if isinstance(node, ast.Assign):
            values.append(node.value)
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            values.append(node.value)
        for value in values:
            for item in ast.walk(value):
                if isinstance(item, ast.Constant) and isinstance(item.value, str):
                    found.add(item.value)
    return found


def _backend_python_files() -> list[Path]:
    excluded = {path.resolve() for path in PLUGIN_PACKAGE_DIRS}
    files: list[Path] = []
    for path in sorted(BACKEND_ROOT.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        resolved = path.resolve()
        if any(parent in excluded for parent in resolved.parents):
            continue
        files.append(path)
    return files


def _scan_file(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    targets = _import_targets(tree) | _module_level_strings(tree)
    return {target for target in targets if _is_banned(target)}


def test_no_coc7_import_coupling_outside_whitelist() -> None:
    """红线：白名单外的任何文件出现 coc7 import 期耦合即失败。"""
    violations: dict[str, set[str]] = {}
    for path in _backend_python_files():
        rel = path.relative_to(BACKEND_ROOT).as_posix()
        bad = _scan_file(path)
        if bad and rel not in KNOWN_DEBT_FILES:
            violations[rel] = bad
    assert not violations, (
        "白名单外出现 ruleset.coc7 / combat.rulesets.coc7 import 期耦合"
        "（阶段 4 P0 禁止新增；如确属新债务，先在 KNOWN_DEBT_FILES 登记并注明批次归属）：\n"
        + "\n".join(f"  {rel}: {sorted(bad)}" for rel, bad in sorted(violations.items()))
    )


def test_known_debt_registry_stays_in_sync() -> None:
    """债务登记表与磁盘保持同步：引用的文件必须真实存在。"""
    missing = sorted(rel for rel in KNOWN_DEBT_FILES if not (BACKEND_ROOT / rel).exists())
    assert not missing, f"白名单引用了不存在的文件（债务已清理？请同步删除条目）：{missing}"


# 灰卡占位（declared）唯一允许的装配点：composition root。占位清单本身经
# ``RulesetRegistry(declared=...)`` 参数注入，核心函数体不得出现任何实例化。
DECLARED_ASSEMBLY_FILES = {
    "ruleset/bootstrap.py",
}


def _declared_construction_files() -> list[str]:
    """backend 内出现 ``DeclaredRuleset(...)`` 构造调用的文件（AST 扫描）。"""
    files: list[str] = []
    for path in _backend_python_files():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "DeclaredRuleset"
            ):
                files.append(path.relative_to(BACKEND_ROOT).as_posix())
                break
    return files


def test_declared_placeholder_construction_only_in_composition_root() -> None:
    """灰卡占位收编锚点：declared 占位不得写进任何核心函数体。

    占位清单只允许经装配点（``ruleset/bootstrap.py`` 的
    ``DEFAULT_DECLARED_RULESETS``）登记，再由 ``RulesetRegistry(declared=...)``
    参数化消费；白名单外任何 ``DeclaredRuleset`` 实例化直接失败，防止
    具体规则集 id 重新硬编码进核心模块（审计 §4.4 第 1 条）。
    """
    violations = [
        rel
        for rel in _declared_construction_files()
        if rel not in DECLARED_ASSEMBLY_FILES
    ]
    assert not violations, (
        "灰卡占位（DeclaredRuleset）只允许在装配点 ruleset/bootstrap.py 构造，"
        "核心模块不得写死占位规则集：\n  " + "\n  ".join(sorted(violations))
    )
