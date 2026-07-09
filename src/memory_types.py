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
    # v4：内容与发生分离。source_uuid 是全局去重键（Claude 两族用原生 id，normalized/test 用确定性哈希）。
    source_uuid: str = ""
    parent_uuid: str = ""
    message_seq: int = 1  # 轮内位置：user=1，assistant=2..N
    line_no: int = 0       # 源文件追加序（JSONL）/ 会话内数组下标（导出）
    has_image: int = 0
    image_count: int = 0
    model: str = ""        # 官方 model id 原样（messages.model）


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
