"""控制台参数解析的共享实现。

房间控制台各域统一使用纯词参数：

- 开关直接写词，例如 ``yes`` / ``json`` / ``weapon``；
- 带值参数写成 ``key value``，例如 ``difficulty hard`` / ``qty 2``；
- 旧的 ``--flag`` / ``--key=value`` / ``--key value`` 写法继续兼容；
- ``-5`` / ``-12.5`` 这类负数始终按位置参数处理，不会被当成开关。

各域只声明自己的词表（``ConsoleArgSpec``），真正的分词与兼容逻辑集中在这里，
避免每个域各写一份。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Iterable

NEGATIVE_NUMBER = re.compile(r"-\d+(?:\.\d+)?$")


@dataclass(frozen=True)
class ConsoleArgSpec:
    """某个域 / 子命令认识的纯词参数。"""

    flags: frozenset[str] = field(default_factory=frozenset)
    values: frozenset[str] = field(default_factory=frozenset)
    trailing: frozenset[str] = field(default_factory=frozenset)

    @classmethod
    def of(
        cls,
        *,
        flags: Iterable[str] = (),
        values: Iterable[str] = (),
        trailing: Iterable[str] = (),
    ) -> "ConsoleArgSpec":
        return cls(
            flags=frozenset(str(word).lower() for word in flags),
            values=frozenset(str(word).lower() for word in values),
            trailing=frozenset(str(word).lower() for word in trailing),
        )

    def merged(self, other: "ConsoleArgSpec") -> "ConsoleArgSpec":
        return ConsoleArgSpec(
            flags=self.flags | other.flags,
            values=self.values | other.values,
            trailing=self.trailing | other.trailing,
        )


EMPTY_SPEC = ConsoleArgSpec()


@dataclass
class ParsedConsoleArgs:
    """解析结果。

    ``flags`` 延续旧实现使用的 ``--flag`` / ``--key=value`` 形式，
    ``options`` 提供 ``key -> value``，``legacy_args`` 是把纯词还原成旧写法后的
    参数列表（供还需要透传给旧解析器的场景使用）。
    """

    positionals: list[str]
    flags: set[str]
    options: dict[str, str]
    legacy_args: list[str]


def is_negative_number(token: str) -> bool:
    return bool(NEGATIVE_NUMBER.fullmatch(token))


def first_positional(args: Iterable[str]) -> str:
    """取出第一个位置参数（跳过选项与负数以外的开关），用于子命令判定。"""
    for raw in args:
        token = str(raw)
        if token.startswith("-") and not is_negative_number(token):
            continue
        return token
    return ""


def _is_legacy_flag(token: str) -> bool:
    return token.startswith("-") and not is_negative_number(token)


def parse_console_args(args: Iterable[str], spec: ConsoleArgSpec = EMPTY_SPEC) -> ParsedConsoleArgs:
    """按纯词规则解析控制台参数，同时兼容旧 ``--`` 写法。"""
    tokens = [str(token) for token in args]
    positionals: list[str] = []
    flags: set[str] = set()
    options: dict[str, str] = {}
    legacy_args: list[str] = []
    seen_positional = False
    index = 0
    while index < len(tokens):
        token = tokens[index]
        lowered = token.lower()

        # 旧写法：--key=value
        if token.startswith("--") and "=" in token:
            key, _, value = token.partition("=")
            key = key[2:].lower()
            options[key] = value
            flags.add(f"--{key}={value}")
            flags.add(f"--{key}")
            legacy_args.append(token)
            index += 1
            continue

        # 旧写法：--key value（仅当该 key 是本域认识的值选项）
        if token.startswith("--") and lowered[2:] in spec.values and index + 1 < len(tokens):
            key = lowered[2:]
            value = tokens[index + 1]
            options[key] = value
            flags.add(f"--{key}={value}")
            flags.add(f"--{key}")
            legacy_args.extend([f"--{key}", value])
            index += 2
            continue

        # 旧写法：--flag / -y；负数保持位置参数
        if _is_legacy_flag(token):
            flags.add(lowered)
            legacy_args.append(lowered)
            index += 1
            continue

        # 第一个位置参数一律视为子命令 / 主体，避免与同名值选项冲突
        if not seen_positional:
            positionals.append(token)
            legacy_args.append(token)
            seen_positional = True
            index += 1
            continue

        if lowered in spec.flags:
            flags.add(f"--{lowered}")
            legacy_args.append(f"--{lowered}")
            index += 1
            continue

        if lowered in spec.values and index + 1 < len(tokens):
            value = tokens[index + 1]
            options[lowered] = value
            flags.add(f"--{lowered}={value}")
            flags.add(f"--{lowered}")
            legacy_args.extend([f"--{lowered}", value])
            index += 2
            continue

        if lowered in spec.trailing:
            rest = tokens[index + 1:]
            tail_flags: list[str] = []
            # 允许把 yes / persist 这类开关写在原因、备注之后
            while rest:
                tail = rest[-1]
                tail_key = tail.lstrip("-").lower()
                if tail_key in spec.flags and (_is_legacy_flag(tail) or tail.lower() in spec.flags):
                    rest.pop()
                    tail_flags.insert(0, tail)
                else:
                    break
            value = " ".join(rest)
            options[lowered] = value
            flags.add(f"--{lowered}={value}")
            flags.add(f"--{lowered}")
            legacy_args.extend([f"--{lowered}", *rest])
            for tail in tail_flags:
                if _is_legacy_flag(tail):
                    flags.add(tail.lower())
                    legacy_args.append(tail.lower())
                else:
                    flags.add(f"--{tail.lower()}")
                    legacy_args.append(f"--{tail.lower()}")
            index = len(tokens)
            continue

        positionals.append(token)
        legacy_args.append(token)
        index += 1
    return ParsedConsoleArgs(positionals, flags, options, legacy_args)
