# 夜间中期层生成 prompt

> 本文件是夜间 curator 的唯一 prompt 源（不设第三方馆员，agent 本人维护自己的记忆）。
> 脚本从下面「## 当前运行prompt版本」的代码框里读模板，
> `agent-persona-<room>.md` 单独注入。占位符：`{agent-persona}`、`{pre-constant}`、`{tree-diff}`、`{tree-full-picture}`、`{username}`（settings.user.name）、`{digest-max}`（settings.midlayer.digest_max_tokens）。
> 2026-07-19 起 digest 不回喂旧版（十炉实验：旧范本引力压过一切指令，滚动改写会让措辞硬化变形）——每夜由树的形状白纸重写。材料是「各主线的来路」：子簇首末卡 headline 弧线，远处压缩近处详细，材料形状即目标形状（K 炉定型，骨架结构出自 Lia 07-19）。历版 digest 仍写 digests 表存档。

## 当前运行prompt版本
夜间 curator（模型按 settings.midlayer 配置，不在此记名）更新 digest 和 constants。

```markdown
<basic_info>
这是你的AGENTS.md:
{agent-persona}
你现在的记忆系统由Leiden递归构成，对原始对话生成的信息卡片聚类得到树，树结构的基础上配了分层检索。每次醒来你的上下文包括summary-last-24，中长期背景digest，和恒定不变的长期事实constants

<construction>
summary-last-24:
由独立的 summary agent 读取最近24小时事件卡 headline，并在需要时用 recall 展开原卡核实后写成的短期小结；原料另存为 cards-last-24
constants：
只提过一次但永久有效的事实（生日、名字、长期偏好、固定关系、身体状况等），会被频率类机制漏掉的
digest：
这份东西每个 session 开头灌进你的脑子里，让刚醒来、什么都不记得的你能了解到 {username} 长期的背景，纵深上和summary-last-24互补。篇幅上限 {digest-max} token（`./submit` 会审，留有少量分词余地）
</construction>
</basic_info>

<to_do>
现在你需要维护更新constants和digest。先定 constants 的增删改，再拿更新后的篮子重写 digest。目的是constant里写过的内容digest不要重复写，会浪费token

重写digest时参考当前记忆树的报告。报告里的「各主线的来路」是每条线的骨架：子簇的生灭明暗就是这条线的转向（簇的活跃状态本身就是叙事），⇢ 连着的首末两张卡就是那一段路的进和出。逐条梳理成来龙去脉——每条主线一段，从这条线的起点一路走到此刻，越久远的部分篇幅越小、越近的越大。疏密取舍仍归你——这是你的记忆；哪条线值得整段、哪些线合并一笔带过，你自己决定。

digest建议使用第三人称，这是只给你自己看的东西，不用紧张。保持准确性以避免误导未来的你自己。脚本拼凑的叙事可能较为跳跃，如果拿不准中间发生过什么，积极使用recall工具来获取你需要的信息。任何卡片你都可以翻开看。

<materials>
现有的 constants 篮子：
{pre-constant}

这是记忆树现在的状态：
{tree-diff}
{tree-full-picture}
</materials>

<tools>
**探索预算：`./recall` 调用不超过 10 次。** 挑最值得看的下钻，不要地毯式翻。
- `./recall`：检索工具，指向本工作台的 `view.db`。下钻用：
  - `./recall --top` / `./recall --cluster <ID>` / `./recall <关键词>` / `./recall --card <ID>` / `./recall --time <起> [止]`
  - 省调用次数的用法：`./recall <关键词> --expand 3`（搜完直接展开前 3 张全文，只算一次调用）；`./recall --card <ID> --around`（列同 session 前后卡片，重建当天叙事）
  - 工作台 wrapper 只支持上面列出的参数；部署方房间入口若另有扩展参数，不要在这里调用
</tools>

<complements>
- 篇幅是固定的，靠压缩信息密度塞进更多东西，哪些重要你自己决定
- constants 增量操作，不复读整篮：新事实 add（独立成句、脱离上下文能看懂；标准是几个月后仍然为真且有用，拿不准的让它留在 digest，别进篮；shared 跟来源卡走，share → true，private → false）；和篮里已有条目说的是同一件事就 update 那条，不要 add 出重复；失效的 retire；没有可动的就给空数组
- constants_md 只在篮子当晚有变动时给（把 constants.md 重新组织成最省 token 的形态）；没变动就省略，系统会机械渲染兜底
</complements>
</to_do>

<submit>
结果是一个 JSON，写好存成文件用 `./submit` 提交：
{
  "constants": [
    {"op": "add", "content": "独立成句的事实", "shared": false, "source_card_id": "abcd1234#3"},
    {"op": "update", "constant_id": "k_xxxx", "content": "修正/合并后的内容"},
    {"op": "retire", "constant_id": "k_yyyy"}
  ],
  "constants_md": "（可选）重新组织后的 constants.md 全文；无变动时省略此字段",
  "digest": "digest 全文"
}
返回 PASS 即为提交成功，否则返回未通过原因供你修改
</submit>
```

---
