"""内置音色人设（8 个 NPC + 1 个旁白）。

设计约定：
- profile 只描述「怎么读」：pitch/rate 与偏好系统音色关键词，不绑定具体浏览器语音；
- 同一 NPC 的自动分配必须稳定：性别/年龄关键词命中优先，否则用名字哈希在候选集合内取模；
- 本模块不依赖 Flask，也不读写磁盘，房间显式映射由 :mod:`voice.config` 负责。
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import asdict, dataclass
from typing import Any, Iterable

KINDS = ("narrator", "npc")
GENDERS = ("male", "female", "neutral")
AGE_GROUPS = ("teen", "youth", "middle", "elder", "adult")

# 中文标签的 i18n key 交给前端统一翻译；后端只输出稳定 id。
PROFILE_I18N_KEYS = {
    "narrator": "voice.profile.narrator",
    "npc_male_teen": "voice.profile.npc_male_teen",
    "npc_male_youth": "voice.profile.npc_male_youth",
    "npc_male_middle": "voice.profile.npc_male_middle",
    "npc_male_elder": "voice.profile.npc_male_elder",
    "npc_female_teen": "voice.profile.npc_female_teen",
    "npc_female_youth": "voice.profile.npc_female_youth",
    "npc_female_middle": "voice.profile.npc_female_middle",
    "npc_female_elder": "voice.profile.npc_female_elder",
}


@dataclass(frozen=True)
class VoiceProfile:
    id: str
    label: str
    kind: str
    gender: str
    age_group: str
    pitch: float
    rate: float
    preferred_voices: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["preferred_voices"] = list(self.preferred_voices)
        data["i18n_key"] = PROFILE_I18N_KEYS.get(self.id, f"voice.profile.{self.id}")
        return data


# 偏好规则按顺序尝试：先精确的中文/微软音色名，再性别关键词，最后交给前端默认音色。
# 浏览器实际可用音色因系统而异，匹配失败只会记录降级原因，不影响出声。
PROFILES: tuple[VoiceProfile, ...] = (
    VoiceProfile(
        "narrator", "旁白", "narrator", "neutral", "adult", 0.95, 0.95,
        ("Yunxi", "云希", "Xiaoxiao", "晓晓", "Huihui", "慧慧", "zh-CN", "Chinese"),
    ),
    VoiceProfile(
        "npc_male_teen", "少年（男）", "npc", "male", "teen", 1.25, 1.05,
        ("Yunxi", "云希", "Kangkang", "康康", "Male", "男", "zh-CN"),
    ),
    VoiceProfile(
        "npc_male_youth", "青年（男）", "npc", "male", "youth", 1.0, 1.0,
        ("Yunjian", "云健", "Yunxi", "云希", "Kangkang", "康康", "Male", "男", "zh-CN"),
    ),
    VoiceProfile(
        "npc_male_middle", "中年（男）", "npc", "male", "middle", 0.82, 0.95,
        ("Yunyang", "云扬", "Yunjian", "云健", "Kangkang", "康康", "Male", "男", "zh-CN"),
    ),
    VoiceProfile(
        "npc_male_elder", "老年（男）", "npc", "male", "elder", 0.65, 0.85,
        ("Yunyang", "云扬", "Kangkang", "康康", "Male", "男", "zh-CN"),
    ),
    VoiceProfile(
        "npc_female_teen", "少女", "npc", "female", "teen", 1.45, 1.05,
        ("Xiaoyi", "晓伊", "Xiaoxiao", "晓晓", "Female", "女", "zh-CN"),
    ),
    VoiceProfile(
        "npc_female_youth", "青年（女）", "npc", "female", "youth", 1.22, 1.0,
        ("Xiaoxiao", "晓晓", "Xiaoyi", "晓伊", "Huihui", "慧慧", "Female", "女", "zh-CN"),
    ),
    VoiceProfile(
        "npc_female_middle", "中年（女）", "npc", "female", "middle", 1.05, 0.95,
        ("Huihui", "慧慧", "Xiaoxiao", "晓晓", "Female", "女", "zh-CN"),
    ),
    VoiceProfile(
        "npc_female_elder", "老年（女）", "npc", "female", "elder", 0.88, 0.85,
        ("Huihui", "慧慧", "Xiaoxiao", "晓晓", "Female", "女", "zh-CN"),
    ),
)

PROFILE_MAP: dict[str, VoiceProfile] = {item.id: item for item in PROFILES}
NPC_PROFILES: tuple[VoiceProfile, ...] = tuple(item for item in PROFILES if item.kind == "npc")
PROFILE_IDS: tuple[str, ...] = tuple(item.id for item in PROFILES)
NPC_PROFILE_IDS: tuple[str, ...] = tuple(item.id for item in NPC_PROFILES)

# 英文词用边界匹配，避免 "female" 里命中 "male"、"woman" 里命中 "man"。
_MALE_PATTERN = re.compile(
    r"男|先生|父亲|兄长|叔叔|爷爷|他|\bmale\b|\bman\b|\bboy\b",
    re.IGNORECASE,
)
_FEMALE_PATTERN = re.compile(
    r"女|女士|母亲|姐姐|阿姨|奶奶|她|\bfemale\b|\bwoman\b|\bgirl\b",
    re.IGNORECASE,
)
_TEEN_HINTS = ("少年", "少女", "孩子", "男孩", "女孩", "child", "teen", "young", "萝莉", "正太")
_YOUTH_HINTS = ("青年", "年轻", "小伙", "姑娘", "adult", "young adult")
_MIDDLE_HINTS = ("中年", "壮年", "大叔", "大婶", "middle", "middle-aged")
_ELDER_HINTS = ("老年", "老人", "老者", "老太", "爷爷", "奶奶", "elder", "old", "senior")

_BRACKET_COMMENT = re.compile(r"[（(][^）)]*[）)]")
_SPACES = re.compile(r"\s+")


def normalize_speaker_name(name: Any) -> str:
    """归一说话人名字：去括号注释、去空白、小写，用于映射与别名比对。"""
    text = _BRACKET_COMMENT.sub("", str(name or ""))
    return _SPACES.sub("", text).strip().lower()


def _stable_index(name: str, candidates: Iterable[VoiceProfile]) -> int:
    digest = hashlib.sha1(normalize_speaker_name(name).encode("utf-8")).hexdigest()
    return int(digest[:8], 16) % max(1, len(list(candidates)))


def detect_gender(text: str) -> str | None:
    source = str(text or "")
    if _FEMALE_PATTERN.search(source):
        return "female"
    if _MALE_PATTERN.search(source):
        return "male"
    return None


def detect_age_group(text: str) -> str | None:
    lowered = str(text or "").lower()
    if any(hint in lowered for hint in _ELDER_HINTS):
        return "elder"
    if any(hint in lowered for hint in _MIDDLE_HINTS):
        return "middle"
    if any(hint in lowered for hint in _TEEN_HINTS):
        return "teen"
    if any(hint in lowered for hint in _YOUTH_HINTS):
        return "youth"
    return None


def auto_profile_for(name: str, npc: dict[str, Any] | None = None) -> VoiceProfile:
    """稳定分配：性别 + 年龄段关键词优先，缺失项用名字哈希补齐。

    同名 NPC 在任何回合、任何进程重启后都会得到同一 profile，避免声音漂移。
    """
    data = npc if isinstance(npc, dict) else {}
    descriptors = " ".join(
        str(data.get(key) or "")
        for key in ("name", "title", "gender", "sex", "age", "age_group", "description", "persona", "aliases")
    )
    # aliases 可能是列表，单独拼接，避免 dict/list 的 str 形式干扰关键词。
    aliases = data.get("aliases")
    if isinstance(aliases, (list, tuple)):
        descriptors += " " + " ".join(str(item or "") for item in aliases)
    gender = detect_gender(descriptors)
    age_group = detect_age_group(descriptors)

    candidates = list(NPC_PROFILES)
    if gender in {"male", "female"}:
        candidates = [item for item in candidates if item.gender == gender]
    if age_group:
        aged = [item for item in candidates if item.age_group == age_group]
        if aged:
            return aged[_stable_index(name, aged)]
    return candidates[_stable_index(name, candidates)]


def get_profile(profile_id: str | None) -> VoiceProfile:
    text = str(profile_id or "").strip().lower()
    return PROFILE_MAP.get(text, PROFILE_MAP["narrator"])


def narrator_profile() -> VoiceProfile:
    return PROFILE_MAP["narrator"]
