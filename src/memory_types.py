from __future__ import annotations

from dataclasses import dataclass


@dataclass
class Message:
    role: str
    speaker: str
    text: str
    timestamp: str
    session_id: str
    seq: int
    round: int
    label: str
    source_file: str = ""
    source: str = "opus-legacy"
    # v4：内容与发生分离。v2 adapter 用 source+native_message_id 的 canonical hash。
    source_uuid: str = ""
    parent_uuid: str = ""
    message_seq: int = 1  # 轮内位置：user=1，assistant=2..N
    line_no: int = 0       # 源文件追加序（JSONL）/ 会话内数组下标（导出）
    has_image: int = 0
    image_count: int = 0
    model: str = ""        # 官方 model id 原样（messages.model）
    provider: str = ""     # adapter 提交的原始 provider（messages.provider）
    native_message_id: str = ""
    native_parent_message_id: str = ""
    native_session_id: str = ""
    native_parent_session_id: str = ""
    parent_session_id: str = ""  # Neroli canonical parent session id
    source_route: str = ""
    room: str = ""         # 本机 ingestion policy 的解析结果，不由 adapter 直接指定


@dataclass
class SegmentSpec:
    start_round: int
    end_round: int
    label: str


@dataclass
class ConstantFact:
    module: str
    content: str


@dataclass
class Fact:
    content: str
    audience: str = "shared"


@dataclass
class CombinedResult:
    segment: SegmentSpec
    facts: list[Fact]
    constants: list[ConstantFact]
