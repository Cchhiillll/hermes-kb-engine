# Wiki Schema

## Domain
chillwang 的知识库：他做过、正在做的一切——项目、机器、服务、网站、渠道和账号、工具、做过的决定、定下的规则、踩过的坑和修法、做事的方法。
由 Hermes 维护，Hermes 靠它了解 chillwang 的全部事情，并随着新对话、新任务不断长大。

## 原始资料（Layer 1）
- 原始资料是 chillwang 在所有 agent 上的对话，按会话一个 Markdown 文件，在 `raw/conversations/<来源>/`
  （Mac 的 Claude Code / Codex / DSH，ThinkPad 的 Hermes / 小火龙 / 佐佐木），每晚 3 点自动导出新的。只读，不改。
- 检索用 qmd：`qmd query "问题" -c raw`（原始对话）、`-c wiki`（知识库页面）；看全文 `qmd get <文件>`。
- 出处标记：文件里 `## 日期 · 片段id` 是一轮问答；页面结论段尾标 `^[kb:片段id]`。

## Conventions
- 文件名：小写、连字符，中文项目名可保留原名（如 `3xui-node.md`、`软路由.md`）。
- 每页开头 YAML frontmatter（见下）。
- 页与页用 `[[页面名]]` 互链，每页至少 2 个出链。
- 改页时更新 `updated` 日期；新页加进 `index.md` 对应分区；每次动作追加到 `log.md`。
- 新旧冲突：以日期新的为准，旧的挪到页内「历史」一节并注明何时被什么取代；拿不准就两种都写并标 `contested: true`。
- 不写密钥、密码、token。IP、端口、路径、域名可以写（这是他自己的内部库）。
- 感情、健康等私人细节不写；和工作有关的个人打算一句带过。

## Frontmatter
```yaml
---
title: 页面标题
created: YYYY-MM-DD
updated: YYYY-MM-DD
type: project | entity | concept | query
tags: [只用下面分类里的]
sources: [kb:片段id, ...]
confidence: high | medium | low
---
```

## Tag Taxonomy
- 项目：project, product, site
- 基础设施：machine, server, network, proxy, container, service
- 模型与渠道：model, relay, channel, account, pricing
- 工具与 agent：agent, tool, config, skill
- 知识：decision, rule, howto, problem, fix, history
- 其他：work, plan, research

## 目录结构
- `projects/` 项目页（每个项目一页：是什么、现状、规则、决定、做法与排障、历史）
- `entities/` 实体页（每台机器、每个服务/容器、每个网站、每个渠道/账号、每个工具/agent；它的做法与排障也写在这页）
- `concepts/` 概念页（只放跨项目通用的方法和经验；某个项目或服务自己的做法、排障写进那个项目/实体页）
- `queries/` 值得留下的问答结论
- `_archive/` 被完全取代的旧页

## Page Thresholds
- 开新页：先找已有页，能并进去就并进去。项目、实体在 2 段以上对话里出现才开页；concepts/ 只给跨项目通用、在多段对话里反复出现的方法开页。
- 不开页：一次性的排障、某个项目/服务自己的做法（写进那个项目/实体页的「做法与排障」节）、顺带一提的、琐碎的、和他无关的。
- 文件名用主题本身，不加「规程」「铁律」「准则」「规范」这类后缀；同一主题只留一页。
- 加进已有页：提到已有页里的东西。
- 页面太长：先精简（合并重复、过时内容挪进「历史」），超过约 400 行仍确属两个独立主题才拆。
- 归档：内容被完全取代时挪进 `_archive/`，从 index 移除；被合并掉的页由脚本挪进 `_archive/merged/`。
- 收敛（10-04 起）：深加工任务每轮领一组同主题碎页合并成一页（wiki_consolidate.py），出处 ^[kb:段id] 一个不丢。

## 渐进式披露（知识库越大越要守）
- `_meta/map.md` 是地图：≤30 行，只写知识库有哪几大块、每块是什么、各有多少页、去 index.md 哪个分区找。
  它会被放进你每次都读的上下文，所以必须短；库再大也不许超过 30 行。
- `index.md` 和 `_meta/map.md` 由脚本按各页的「> 一句话」自动生成，不手改；所以每页页首都要有这句摘要。
- 找东西：地图 → index.md 对应分区 → 打开页面 → 需要原文再 `kb search` / `kb open`。

## 做法沉淀成技能
- 读对话时发现一种可以复用的处理方式（某类问题怎么排查、某类事怎么办、某个系统怎么操作），
  除了写进 concepts/ 的做法页，还要用 skill_manage 建或更新一个技能：description 写清"什么情况下用"，
  正文写步骤、坑、要读知识库哪几页（[[页面名]]）。技能平时只露名字和描述，用到才加载——这就是处理不同事情的入口。
- 技能名用 `chillwang-` 开头，便于区分。
