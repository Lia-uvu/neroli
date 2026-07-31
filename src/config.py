from __future__ import annotations

import functools
import json
import re
from pathlib import Path

MEMORY = Path(__file__).resolve().parents[1]
ROOMS_FILE = MEMORY / "config" / "rooms.json"
SETTINGS_FILE = MEMORY / "config" / "settings.json"

_data = json.loads(ROOMS_FILE.read_text(encoding="utf-8"))

ROOMS: tuple[str, ...] = tuple(room["name"] for room in _data["rooms"])
if not ROOMS:
    raise RuntimeError(f"{ROOMS_FILE} 至少要配置一个房间")
PROJECT_DIRS: tuple[Path, ...] = tuple(Path(room["project_dir"]) for room in _data["rooms"])
ROOM_DIRS: dict[str, Path] = {room["name"]: Path(room["room_dir"]) for room in _data["rooms"]}
ROOM_SLUGS: dict[str, str] = {room["name"]: Path(room["room_dir"]).name for room in _data["rooms"]}
DEFAULT_ROOM: str = ROOMS[0]


def room_for_source_file(source_file: str | None) -> str | None:
    """把一条 turn 的 source JSONL 路径映射回房间（按 rooms.json 的 project_dir 前缀）。

    落在任何房间 project_dir 之外的来源（导出文件、测试 txt 等）返回 None，让调用方
    自行决定回退——房间是从消息来源自动派生的，不再硬编码。
    """
    if not source_file:
        return None
    path = Path(source_file)
    for name, project_dir in zip(ROOMS, PROJECT_DIRS):
        if path.is_relative_to(project_dir):
            return name
    return None


def room_for_source_route(source: str, source_route: str) -> str:
    """Resolve an adapter-owned route through this instance's private policy.

    The adapter declares where an event came from; only the local Neroli instance
    decides which privacy room that route belongs to. Unknown routes are hard
    failures so private material never falls through to DEFAULT_ROOM.
    """
    routes = load_settings().get("ingest", {}).get("source_routes", {})
    source_map = routes.get(source) if isinstance(routes, dict) else None
    room = source_map.get(source_route) if isinstance(source_map, dict) else None
    if not isinstance(room, str) or not room:
        raise ValueError(
            f"no ingest.source_routes policy for source={source!r}, "
            f"source_route={source_route!r}"
        )
    if room not in ROOMS:
        raise ValueError(
            f"ingest.source_routes maps source={source!r}, "
            f"source_route={source_route!r} to unknown room {room!r}"
        )
    return room


def load_settings() -> dict:
    return json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))


# ── 模型调用方式 ─────────────────────────────────────────────────────────────
# provider 是「怎么够到模型」，全管线（出卡/实体裁判/夜间 curator）共用一份：
# cli = codex exec / claude -p 等 headless CLI（prompt 走 stdin）；api = OpenAI 兼容
# chat 接口（密钥在 .env）；ollama = 本地。模型名各调用点自己配，这里只管通道。

def model_provider() -> str:
    return load_settings().get("model_access", {}).get("provider", "cli")


def custom_cli_cmd(model_name: str) -> str | None:
    """model_access.cli_cmd 非空时按模板生成完整命令（{model} 被调用点的模型名替换）；
    空则返回 None，调用方回退到自建 codex exec。用 claude -p 等非 codex CLI 填这里。"""
    tpl = load_settings().get("model_access", {}).get("cli_cmd", "")
    return tpl.replace("{model}", model_name) if tpl else None


# ── 称呼 / 屏蔽词表 ──────────────────────────────────────────────────────────
# agent 对用户的称呼是配置不是代码：名字进 prompts（{username} 占位符）和对话渲染，
# 小写别名进屏蔽词表——称呼和一切共现，不屏蔽会污染共现统计、树 tag 和实体建图。

# 称呼在 ingest 的逐消息热路径上用，进程内缓存（进程短命，仍满足「改了下次触发生效」）。

@functools.lru_cache(maxsize=1)
def user_name() -> str:
    return load_settings().get("user", {}).get("name", "User")


@functools.lru_cache(maxsize=1)
def agent_name() -> str:
    return load_settings().get("agent", {}).get("name", "Assistant")


@functools.lru_cache(maxsize=1)
def user_source_label() -> str:
    """turns/messages 的 source 列里 user 侧的标签（历史数据用称呼小写）。"""
    u = load_settings().get("user", {})
    return u.get("source_label") or u.get("name", "user").lower()


# 语言级的中文虚词/泛词，引擎自带；个人化的部分（称呼、情绪词）住 settings。
_BASE_GENERIC = {
    "我", "你", "她", "他", "我们", "今天", "现在", "ai",
    "的", "了", "和", "是", "在", "有", "会", "要", "想", "事", "事情",
    "一个", "这个", "那个", "时候", "感觉", "觉得", "知道", "东西", "问题",
}


def blocked_names() -> set[str]:
    """用户与 agent 的称呼别名（小写），共现/树 tag/建图统一屏蔽。"""
    s = load_settings()
    names: set[str] = set()
    for side in ("user", "agent"):
        cfg = s.get(side, {})
        names.update(a.lower() for a in cfg.get("aliases", []) if a)
        if cfg.get("name"):
            names.add(cfg["name"].lower())
    return names


def generic_tags() -> set[str]:
    """建图/实体提取前过滤的背景词：内置虚词 + 称呼 + settings 补充。"""
    extra = load_settings().get("vocab", {}).get("generic_extra", [])
    return _BASE_GENERIC | blocked_names() | {w.lower() for w in extra if w}


def stop_tags() -> set[str]:
    """共现统计 / 树 tag 的完整屏蔽集：背景词 + 个人情绪词表（全小写）。"""
    affect = load_settings().get("vocab", {}).get("affect", [])
    return generic_tags() | {w.lower() for w in affect if w}


_USERNAME_RE = re.compile(r"\{user[-_]?name\}")


def fill_username(text: str) -> str:
    """把 prompt 模板里的 {username} / {user-name} / {user_name} 占位符换成称呼。"""
    return _USERNAME_RE.sub(user_name(), text)
