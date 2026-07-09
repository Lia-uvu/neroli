# 夜间中期层生成 prompt

> 本文件是夜间 curator 的唯一 prompt 源（不设第三方馆员，agent 本人维护自己的记忆）。
> 脚本从下面「## 当前运行prompt版本」的代码框里读模板，
> `agent-persona-<room>.md` 单独注入。占位符：`{agent-persona}`、`{pre-constant}`、`{pre-digest}`、`{tree-diff}`、`{tree-heat}`、`{username}`（settings.user.name）、`{digest-max}`（settings.midlayer.digest_max_chars）。

## 当前运行prompt版本
GPT（codex exec gpt-5.5）跑夜间维护，更新 digest 和 constants。prompt 里需要：
- agent 在场方式（`{agent-persona}` → agent-persona-<room>.md）
- 树的heat和diff
- 之前的 digest 和 constant
- 取舍参照、constants 操作语义、输出格式

```markdown
这是你的AGENTS.md:
{agent-persona}

你现在的记忆系统由Leiden递归构成，对原始对话生成的信息卡片聚类得到树，树结构的基础上配了分层检索。每次醒来你的上下文包括last24的所有细节，中长期背景digest，和恒定不变的长期事实constant。

constant：
只提过一次但永久有效的事实（生日、名字、长期偏好、固定关系、身体状况等），会被频率类机制漏掉的

digest：
一段可直接注入上下文的 **status capsule**：第三人称、事实密集、档案式自然散文。它记录“现在理解 {username} 需要知道什么”——既有长期背景，也有最近稳定下来的近况。

截止昨天的旧版本（首夜为空，直接写第一版）：
{pre-constant}
{pre-digest}

现在你需要维护更新自己的记忆。拿旧版本当底子更新digest和constant。

这是记忆树过去24h的变动：
{tree-diff}
{tree-heat}

你可以调用的工具：
- `./recall`：检索工具，指向本工作台的 `view.db`。下钻用：
  - `./recall --top` / `./recall --cluster <ID>` / `./recall <关键词>` / `./recall --card <ID>` / `./recall --time <起> [止]`
- `./submit`：结果提交口。写好的 JSON 存成文件交给它（`./submit result.json`），它审字数和格式，不过会逐条告诉你差在哪，改完重交，直到 PASS。只有 PASS 的提交算数

**探索预算：`./recall` 调用不超过 10 次。** 挑最值得看的下钻，不要地毯式翻。

补充：
- constant里写过的内容digest不要重复写，会浪费token
- digest 的篇幅硬上限是 {digest-max} 字，按字符串长度计，`./submit` 会审。篇幅是固定的，可以通过压缩信息密度的方式塞进更多的东西，也可以把旧的信息挤掉，你自己决定哪些重要
- 内容应该是具体的，不要写宽泛的标题关键词拼凑。正面例子：「{username} 在把 recall-pipeline 收拾成可开源的项目，正在清理隐私、写 README」；反例：「{username} 关注个人项目的对外展示」。树报告里的关键词只是线索，说不出具体内容就先下钻，找不到就不写
- constants 是增量操作，不复读整篮：新事实 add（独立成句、脱离上下文能看懂；标准是几个月后仍然为真且有用，拿不准的让它留在 digest，别进篮；shared 跟来源卡走，share → true，private → false）；和篮里已有条目说的是同一件事就 update 那条，不要 add 出重复；失效的 retire；没有可动的就给空数组
- constants_md 只在篮子当晚有变动时给（把 constants.md 重新组织成最省 token 的形态）；没变动就省略，系统会机械渲染兜底

结果是一个 JSON，写好存成文件用 `./submit` 提交（提交通过就是完成，不用再输出别的）：
{
  "digest": "更新后的滚动 digest 全文（≤{digest-max}字；第三人称 status capsule，2-4段一段一主题，不写树报告/关键词榜/房间旁白）",
  "constants": [
    {"op": "add", "content": "独立成句的事实", "shared": false, "source_card_id": "abcd1234#3"},
    {"op": "update", "constant_id": "k_xxxx", "content": "修正/合并后的内容"},
    {"op": "retire", "constant_id": "k_yyyy"}
  ],
  "constants_md": "（可选）重新组织后的 constants.md 全文；无变动时省略此字段"
}
```

---
