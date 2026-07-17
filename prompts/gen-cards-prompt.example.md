# 事件卡生成 prompt（example 模板）

> 这是随仓库维护和测试的默认 prompt。安装时复制为 `gen-cards-prompt.md` 生效
> （真文件 gitignore——这样各家部署可以有自己的边界措辞，但默认版开箱能用，不需要改）。
> bedrock `gen_cards.py` 从下面「## 当前运行prompt版本」的代码框里读模板，
> `agent-persona-<room>.md` 单独注入。占位符：`{agent-persona}`、`{existing_summary}`、`{conversation}`、`{username}`（settings.user.name）。

## 当前运行prompt版本
GPT（codex exec gpt-5.5）跑事件卡生成。prompt 里需要：
- agent 在场方式（`{agent-persona}` → agent-persona-<room>.md）
- 已冻结的卡（`{existing_summary}`）
- 当前窗口对话（`{conversation}`）
- share/private 划分标准、分段判据、输出格式

```markdown
这是你的AGENTS.md:
{agent-persona} 
前文摘要：
{existing_summary}

你和你的人类的对话记录：
{conversation}

这段对话即将从你的上下文里消失。以卡片的形式用对话原文的语言写下你最想记住的东西，给自然遗忘留下空间。
当你觉得话题转变较大，开始讨论和之前关系不大的事情，适合被当成独立小单元的时候，就另起一张卡片。判断依据在思维链里写下来。

格式要求：
- 大致轮数
- 简要说明发生了什么事（一两句，这行会进一份按时间排的流水账，你靠扫这份流水账知道家里发生了什么）
- 记忆正文（share/private）
- 讨论的对象标签（短关键词，别太多）

记忆正文的判断参照（仅供参考，你自己决定）：
- share 非空意味着别的 room/agent 能检索到这张卡的 headline + share；不是只暴露 share 正文
- 可以告诉普通好朋友的部分，写share
- 别的agent知道后像闯进我和{username}的房间，或会冒领这段亲密/经历/承诺的部分，写private
- 不确定可不可以给别的 agent 看时，写 private，share 留空

格式例子：
turns:13-20（例子）
headline:一句话
share:（可以为空）
private:（可以为空）
tags:词语/词语/词语
```
