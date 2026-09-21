# Neroli

[English](README.md) | 中文

**面向 AI Agent 的本地优先长期记忆引擎**

Neroli 将多个来源的对话转换成持久、带时间锚点的事件卡，让 AI Agent 无需在每次会话中重新载入全部聊天记录，也能延续跨会话上下文。这是一个已经实际运行的系统工程项目，重点处理可靠的记忆管线、多分辨率检索、明确的隐私边界，以及可替换的模块结构。

## 工程亮点

- **持久的多来源记忆。** Source adapter 将不同 runtime 的日志归一成不可变对话树和有序 turns；滚动生成管线进一步将它们转化为带时间、来源、标签和 viewer 级 shared/private 内容的叙事事件卡。事件卡再按自然事件线组织，检索时可以从一个主题一路展开它随时间发展的完整脉络。
- **双轴检索。** 全文搜索与时间上下文组成一条轴；由 embedding、实体共现和递归 Leiden 社区发现生成的层次事件图组成另一条轴。Agent 可以按关键词或时间横向寻找，也可以从顶层社区进入具体事件线，沿时间线逐张展开。
- **容错的 LLM 管线。** 模型输出先暂存并通过结构校验，再原子替换既有记忆；每次物理调用都单独留审计记录。格式错误或空响应不会覆盖上一份有效结果。
- **解耦、可验证的架构。** 七个模块通过带版本的 SQLite contract 交换数据，后台处理与来源解耦；可见性过滤集中在存储层，MCP 只提供有界只读接口，并由 **144 项自动化测试**覆盖关键行为。

## 系统结构

```text
source adapters
  -> 不可变对话树 + observations
  -> messages / 有序 turns
  -> 事件卡（headline、叙事、时间、来源、可见性）
       |-> FTS + 时间线检索
       |-> 规范实体
       |-> 带权 Card 图
       |-> 自适应递归 Leiden 层次
       |-> 近期与长期上下文投影
  -> viewer-bound CLI / 只读 MCP consumer
```

事件卡是持久记忆层。FTS 表、实体映射、图社区和生成的上下文文件都是可重建的派生视图。存储与索引分离，因此索引实验即使失败、漂移或被整体替换，也不会破坏叙事记忆本身。

canonical store 是一个 SQLite 数据库。模块之间通过持久化 schema 交换数据而不是互相 import；只有顶层编排层负责组合它们。

## 几个关键设计判断

### 让事件边界在尾部保持可塑

固定窗口很容易从事件中间切断。Neroli 会冻结已经稳定的事件卡，但在新 turns 到来时重新喂入并改写最后一张卡。只有完整替代结果解析成功后，旧卡才会被删除，因此格式错误或空的模型响应不能擦掉有效记忆。

### 接收不同来源，也允许自行扩展 adapter

Neroli 可以直接摄入已经支持来源的原始 JSON/JSONL 文件，不要求先把历史整理成统一格式。要接入新的聊天平台或 agent runtime，可以实现自己的 source adapter，将原始记录翻译成 Neroli 的公开 conversation-tree 或 normalized contract；后续出卡、索引、检索和上下文生成不需要跟着改写。

### 图的深度由局部结构决定

每张事件卡是一个图节点。边同时使用 mean-centered embedding 近邻和稀有实体共现。Leiden 在每个社区内部递归运行，只有当局部 modularity 超过 degree-preserving null model 的基线时才接受继续分裂。连贯区域可以保持宽泛，混杂区域则自然长出更深层级。

### Agent 默认拿不到主数据库

每张卡属于一个 agent workspace，并区分 shared 与 private 文本。Agent 默认拿到的是已经按 viewer 过滤的上下文或有界只读检索接口，而不是主数据库。Nightly Curator 也只在过滤后的工作台副本里运行：不可见卡整行缺席，他房可见卡的 private 字段为空。Search、近期上下文、树快照和 Curator 共用同一条存储层可见性规则。

## 模块与测试

系统拆成七个边界明确的模块：

- **Ingest**：接收不同来源，保存原始消息、turns 和 conversation tree；
- **History**：从 conversation tree 还原和浏览原始对话；
- **Card Gen**：从新增 turns 生成叙事事件卡；
- **Index**：解析实体、建图并生成递归 Leiden 事件层次；
- **Context**：生成可直接注入 agent 的近期上下文；
- **Midlayer**：在事件层次上维护较长期的 digest 与 constants；
- **Search**：在上述数据层上提供关键词、时间、事件线和原文下钻。

每个模块都有对应的自动化测试，模块之间的公开 contract 另有跨模块测试保护。

## 设计历史

Neroli 并非一开始就采用现在的图架构。早期版本依次探索过 embedding 绝对阈值、质心归属、重叠信号，以及多种短期/长期上下文方案，最终才把叙事存储和检索索引彻底分开。

公开设计史由两份私人工作文档翻译并去敏而来。它们保留实验、被否决的替代方案和决策演进，但移除了个人对话素材：

- [设计历史入口](DESIGN-HISTORY/README.md)
- [v1：事件式记忆与向量空间聚类](DESIGN-HISTORY/design-v1.html)
- [v2：Leiden 图索引与中期层](DESIGN-HISTORY/design-leiden-v2.html)

它们解释系统如何走到今天，不是当前行为的规范。当前事实仍以架构、schema、代码和测试为准。

## 仓库导览

- [技术总览](TECHNICAL-OVERVIEW.md) — 当前已经实现的设计和算法
- [架构](ARCHITECTURE.md) — 模块归属、数据流与边界
- [SQLite schema](schema.md) — 持久化结构和 migrations
- [Source adapter boundary](docs/source-adapter-boundary.md) — 来源事实与 Neroli policy 的分界
- [Conversation-tree contract](docs/conversation-tree-adapter-contract.md) — canonical node、parent 与 observation 模型
- [Normalized adapter contract](docs/normalized-adapter-contract.md) — 线性来源兼容 contract
- [运维入口](skills/ops/SKILL.md) — 按所属模块组织的 runbook

## 运行

安装采用 agent-assisted workflow，而不是固定安装向导。将 [`skills/install/SKILL.md`](skills/install/SKILL.md) 交给 coding agent；它会执行 preflight、询问部署所需的隐私与模型选择、填写私有配置，并逐阶段验证 checkpoint。所有产生模型费用的任务默认关闭，必须显式启用。

运行完整测试：

```sh
python3 -m unittest discover -s tests -v
```

## License

代码采用 [PolyForm Noncommercial 1.0.0](LICENSE)；文档与媒体采用 [CC BY-NC-SA 4.0](LICENSE-DOCS.md)。
