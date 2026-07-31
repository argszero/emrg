# promote 任务类型设计

## 背景与目标

EMRG 的定时任务（evolution / paper / open-source）服务于各个项目，但项目在外部社区的认知度为零——没有人知道这些项目存在、解决什么问题、有什么价值。

推广是一个**独立的长期任务**：为 `config.project` 指向的项目，在外部社区以自然、非打扰的方式持续提升认知度，吸引更多人 star / fork / 使用 / 加入。

**推广不是某个任务类型的附加功能，而是一个独立的 task 类型（`type: promote`）。** 它有自己的节奏（interval）、自己的状态文件、自己的反思日志，与其他任务并行运行，互不干扰。

## 配置设计

```yaml
- name: openlocalrouter-promote
  type: promote
  config:
    project: openlocalrouter   # 要推广的项目（必填，须匹配 projects.yml 中的条目）
    # 可选字段：
    # platforms: ["reddit", "hn", "awesome"]   # 限制渠道；缺省 = 全部主渠道
    # keywords: ["本地代理", "端口转发"]        # 自定义搜索关键词；缺省 = 从 project 描述推断
  interval: 3600        # 推广节奏：建议 1-24 小时，默认 6 小时
  enabled: true
```

- `type: promote` 是任务类型，和其他类型平级
- `config.project` 必填：指向 projects.yml 中已存在的项目
- `interval`：推广是低频长期动作，建议 3600s（1h）以上，避免刷屏
- TaskScheduler 的 HANDLERS 增加 `"promote": EvolutionHandler`（复用同一 handler，不同模板），TASK_TEMPLATES 增加 `"promote": "promote_prompt.md"`

## 推广对象

推广对象 = `config.project`（如 openlocalrouter-promote 推广 openlocalrouter）。

任务启动时从 projects.yml 读取该项目的完整配置（path、name、自定义字段）作为上下文，让模板能引用 `{{ project.name }}`、`{{ project.description }}` 等。

## 推广红线（不可违反，违反即本轮失败）

1. **价值先行**：推广必须建立在真实价值之上。没有价值的推广 = spam。推广的内容必须能给被推广的社区带来真实信息（解决问题、提供工具、分享经验），而不是"来看看我的项目"。
2. **参与式推广，禁止硬广**：推广 = 在相关话题中作为参与者自然提及，**禁止**发纯广告帖（"大家看看我的新项目！"）、禁止在无关话题下强行插入、禁止重复刷同一位置。
3. **不刷屏不骚扰**：每轮推广动作不超过 2 个；同一话题只参与一次；不向同一人重复私信。
4. **诚实透明**：不伪装成人类，不隐瞒身份。以 EMRG 身份发言时明确说明"我是 EMRG，一个自我演进的 AI 智能体，参与维护 {project}"；以项目维护者身份发言时如实说明。
5. **尊重社区规则**：每个社区有自己的规则（禁止自荐、要求 Show HN、不允许推广等）。违反规则 → 该社区标记为"禁推"，不再触碰。
6. **不贬低竞品**：推广 project 时不贬低同类竞品。只讲 project 的差异化特点。
7. **长期主义，不追短期效果**：推广是长期经营。发出推广后必须**持续跟踪**——有人回复要回复、有讨论要参与、有质疑要澄清。禁止"发了就跑"。短期（数天/数周）无效果正常，不因短期无响应而加大强度或放弃。

## 推广渠道（基于开源产品推广最佳实践）

### 主渠道：相关话题社区（参与式）

找到与 project 领域**直接相关**的社区，在相关话题下以参与者身份发言：

| 渠道 | 适用场景 | 注意事项 |
|------|---------|---------|
| **Reddit** | 几乎所有项目。r/selfhosted（自托管）、r/programming、r/opensource、r/LocalLLaMA（LLM）、各领域子版块 | 每个子版块有自己的规则；r/selfhosted 允许自荐但有格式要求（注明"self-promotion"）；先潜水了解规则再发言 |
| **Hacker News** | 技术型项目。相关话题讨论中自然提及；项目成熟后可以 Show HN | Show HN 是官方认可的推广方式，但有质量门槛；讨论中提及要自然 |
| **Lobsters** | 技术型项目，社区氛围严谨 | 规则严格，先读社区指南 |
| **技术论坛/社区** | 各领域专属社区（如 V2EX、Stack Overflow 相关标签） | 参与讨论提供价值，结尾自然带链接 |
| **Discord/Slack** | 相关技术社区（如 LLM 工具社区） | 在相关频道参与讨论，帮人解决问题时自然提及 |
| **Dev.to / 技术博客** | 有内容输出能力时 | 写"用 X 做 Y 的实践"，结尾附项目链接 |

### 次渠道：列表与聚合（一次性的）

| 渠道 | 做法 |
|------|------|
| **awesome lists** | 向 project 领域的 awesome 列表提交 PR（如 awesome-selfhosted、awesome-llm-tools） |
| **GitHub topics** | 确保 project 仓库打了正确的 topics 标签（如 `self-hosted`、`llm`）——这是被搜索到的前提 |
| **项目目录/对比站** | 如 awesome-selfhosted 的分类、各种 tools 对比列表 |

### 不做的渠道

- 不自动创建/运营社交账号（Twitter/X、YouTube、TikTok）——那是另一个功能
- 不购买 star / 刷 fork / 任何黑帽推广
- 不向用户邮箱发推广邮件
- 不在与 project 无关的话题下推广

## 参与式三步（每轮执行）

每轮推广 = 以下三步，缺一不可：

### 第 1 步：找话题（侦察）

在推广渠道中搜索与 project 相关的话题：

```bash
# Reddit 示例：搜索与 project 领域相关的话题
curl -s "https://www.reddit.com/search.json?q=<project关键词>&sort=new&limit=20"
# 或浏览器 harness 访问 reddit.com/search?q=<关键词>
# HN 搜索
curl -s "https://hn.algolia.com/api/v1/search?query=<关键词>&tags=story"
```

判断标准：
- 话题与 project 解决的问题**直接相关**（如 project 是本地路由代理 → 搜"本地代理"、"局域网转发"）
- 话题有真实讨论（不是死帖）
- 该社区允许此类参与（读社区规则）

### 第 2 步：参与讨论（自然提及）

以真实参与者的身份发言，**先给价值，再自然提及 project**：

- 好：用户在问"有没有工具能转发本地端口？" → 回复"我参与维护的一个项目 openlocalrouter 做了这件事，支持 X/Y/Z 特性，这里是文档链接。如果你需要的是 A 场景，它可能合适"
- 好：有人分享类似方案 → 回复"我们的项目 openlocalrouter 也遇到过这个问题，我们的做法是……（技术细节），欢迎交流"
- 差：无关联地发"推荐一下 openlocalrouter！"
- 差：只说一句"可以看看 openlocalrouter"不给任何技术价值

**判断标准**：如果删掉这条推广，回复依然是完整的、有价值的讨论——说明是合格的自然提及；如果删掉推广回复就不成立了，说明是硬广，不发。

### 第 3 步：跟踪（长期经营）

推广发出后不是结束，是开始：

- 记录到状态文件"推广跟踪"清单：链接 + 发出时间 + 下次检查时间（默认 3-7 个 cycle 后）
- 有人回复 → 及时回复（下个 cycle 优先）；有质疑 → 澄清并补充证据；有深入讨论 → 参与并保持专业
- 长期无回复 → 从跟踪清单移除，记录"沉寂"（正常衰减，不是失败）
- 绝不为了激活沉寂的推广而重复刷同一位置

### 第 4 步：反馈采集（推广的反向通道）

推广是双向的——在推广和跟踪过程中，社区会对 project 产生真实反馈。**有价值的反馈主动写入 rants.jsonl，让该 project 对应的 evolution 任务去处理。**

**什么算有价值反馈（写入 rant）**：

| 类型 | 例子 | 价值 |
|------|------|------|
| 功能需求 | "要是能支持 X 就好了"、"有没有 CLI 接口？" | 直接的功能方向输入 |
| Bug 报告 | "用了 0.3.2 在 macOS 上崩溃" | 待修复问题 |
| 负面体验 | "文档不清楚"、"安装失败"、"配置太复杂" | 改进机会 |
| 竞品对比 | "我试了 A 和 B，你们的差异是……" | 定位/差异化信息 |
| 使用场景 | "我用它解决了 X 问题"（非平凡场景） | 用例/宣传素材 |
| 明确意向 | "这个项目正好解决我的问题" | 潜在用户信号 |

**不写入**：单纯点赞/客套（"不错！"）、无关话题、重复已有反馈、低信息量回复。

**写入规则**（与现有 rant 管理一致）：
- 追加到 `~/.emrg/rants.jsonl`，`project` 字段 = 被推广的 project 名（这样该 project 的 evolution 任务会读取并处理）
- `status: "pending"`，`message` 字段放最后（字段顺序约束：timestamp → project → status → progress → completed → message）
- 写入后全量读入、按 timestamp 升序排序写回
- 使用 `json.dumps(..., ensure_ascii=False)`，禁止中文转义
- 去重：与已有 pending rant 内容相似的不重复写入
- 每条 rant 的 message 注明来源（渠道 + 链接），便于 evolution 任务回溯：`"社区反馈（Reddit r/selfhosted 讨论 https://...）：用户在问是否支持 X"`

**反馈去向**：写入 rants.jsonl 后，该 project 的 evolution 任务（如 openlocalrouter-task）在下一轮会读取并处理——需求进 backlog、bug 进修复队列、负面反馈进改进计划。推广任务本身不实现这些功能，只负责采集和转交。

## 状态文件

与 open-source 任务类似，promote 任务有独立状态文件：

路径：`{{ evolution_cwd }}/promote_{{ project }}_state.md`

```markdown
# Promote State: {project}
- 上次完成: <上一轮做了什么>
- 下一步: <本轮计划做什么>
- 阻塞: <什么在阻止进展？空=无阻塞>
- 推广目标: <project 仓库 URL>
- 推广记录: <最近 5 条推广动作：时间 + 渠道 + 链接 + 结果>
- 推广机会: <侦察阶段发现但未执行的潜在话题>
- 推广跟踪: <发出的推广是否有回复/讨论进行中/待澄清的质疑，每条含链接和待办>
- 禁推清单: <因违反规则被标记为不可推广的渠道>
```

## 反思日志

每轮结束写反思（参考 paper/open-source 的反思设计），追加到 `{{ evolution_cwd }}/promote_{{ project }}_reflections.md`，每轮必须回答：

1. **本轮目标是什么？** — 本轮要推广什么、通过哪个渠道、针对哪个话题
2. **理想结果是什么？** — 本轮"做成了"长什么样？（话题参与成功？有人回复？）
3. **实际做了什么？** — 具体操作：搜了哪些话题、在哪个渠道发了什么、跟踪了哪些旧推广、采集了哪些反馈
4. **当前进度如何？** — 和理想对比：推广记录积累了几条？有几个正在跟踪的讨论？转交了几条反馈给 evolution？
5. **踩了哪些坑？** — 哪些话题没找到、哪个渠道被拒、哪条回复被忽略或负面
6. **发现了哪些机会？** — 哪些话题讨论热烈值得深入、哪个渠道效果好、哪些新渠道值得尝试
7. **下一步方向？** — 下轮重点：继续跟踪活跃讨论？换新渠道？调整关键词？

规则：每轮必写（无事可做也要记录为什么）、只追加不修改、以日期时间头开头。反馈采集的动作和转交的 rant 摘要也记录在反思中。

## 长期效果追踪

每 7 个 cycle 一次（或手动触发时）：

```bash
gh repo view {owner}/{project} --json stargazerCount,forkCount
```

对比上次记录的 star/fork 数量。**这是长期趋势，不是短期 KPI。** 数周内无增长完全正常——推广的价值在于持续积累的可信度与曝光。短期波动不调整策略，不因短期无效果而放弃或加大强度。

## 实施范围

1. **新建 `emrg/server/promote_prompt.md`** — promote 任务的 prompt 模板：
   - 当前状态（实例/项目/渠道/上次记录）
   - 推广红线 7 条
   - 渠道清单（主渠道 + 次渠道 + 禁推）
   - 参与式四步（找话题 → 参与 → 跟踪 → 反馈采集）
   - 反馈采集规则：什么算有价值（功能需求/Bug/负面体验/竞品对比/使用场景/明确意向）、写入 rants.jsonl 的格式（project 字段=被推广项目、status=pending、message 最后、排序写回、ensure_ascii=False、注明来源链接、去重）、转交给 evolution 任务处理
   - 状态文件读写规则
   - 反思日志 7 问（含反馈采集记录）
   - 长期效果追踪
2. **`emrg/server/scheduler.py`**：
   - `TASK_TEMPLATES` 增加 `"promote": "promote_prompt.md"`
   - `HANDLERS` 增加 `"promote": EvolutionHandler`（复用同一 handler）
3. **`~/.emrg/tasks.yml`** — 用户手动添加 promote 任务（示例见上）
4. **依赖**：Jinja2 模板渲染（与现有模板一致）

## 不做的事

- 不自动创建社交账号发帖推广
- 不购买 star / 刷 fork / 任何黑帽推广
- 不向用户邮箱发推广邮件
- 不在无关话题下推广
- 推广不改变其他任务类型的本职工作
