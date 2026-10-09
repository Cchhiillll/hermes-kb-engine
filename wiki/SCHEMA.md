# Wiki Schema

## Domain
Hermes 自增长知识库规范：记录和沉淀业务工程全生命周期的一切——项目、机器/节点、服务、网络、接口与账号、工具、重要架构决定、踩坑排障与通用方法论。
由 Hermes 自动化维护，随日常会话、新任务与外部数据源持续自增长与自愈。

## 原始资料层（Layer 1: Raw Conversations）
- 原始资料是用户在各个 Agent（Hermes、Claude Code、Codex 等）上的对话与工作记录，按会话存储于 `raw/conversations/<来源>/`。
- 物理特征：纯追加（Append-Only）、只读不可变。
- 检索方式：使用 QMD 混合检索：`qmd query "问题" -c raw`（检索原始对话），`-c wiki`（检索结构化知识库）。
- 出处锚点：每轮问答切片打上唯一标识，在知识库结论段尾死绑 `^[kb:段id]`。

## 规范约定（Conventions）
- 文件命名：小写字母、连字符，项目名使用标准名称。
- 元数据：每页头部必须包含标准 YAML frontmatter。
- 双向链接：页面之间使用 `[[页面名]]` 互链，每页至少保留 2 个关联出链。
- 增量维护：修改页面时同步更新 `updated` 字段；新增页面由脚本自动挂载至 `index.md`。
- 新旧演进：若出现结论冲突，以最新实测和最新出处为准，过时信息压缩沉淀至「历史」小节并注明被取代时间。
- 安全边界：严格禁止在知识库中明文写入密码、Token、API Key、私人敏感信息。

## Frontmatter 格式
```yaml
---
title: 页面标题
created: YYYY-MM-DD
updated: YYYY-MM-DD
type: project | entity | concept | query
tags: [project, machine, service, tool, rule, howto, fix]
sources: [kb:段id, ...]
confidence: high | medium | low
---
```

## 目录分层结构
- `projects/`：项目页（定义、当前现状、规则与决定、做法与排障、演进历史）
- `entities/`：实体页（节点、服务/容器、网站、渠道账号、工具与 Agent 运行时）
- `concepts/`：通用概念页（跨项目的通用工程方法论、SOP、架构模式与排障准则）
- `queries/`：高价值问答沉淀
- `_archive/`：已归档或被合并的旧页面

## 页面收敛门禁（Consolidation Thresholds）
- 单页规模：控制在 400 行以内，超过时进行精简收敛或按模块拆分。
- 避免碎片化：单一故障排障或单次配置写入对应项目/实体页，避免肆意建立孤立碎页。
- 70% 代码保留门禁：重构和合并页面时，AST 词法级反引号配置/命令代码保留率低于 70% 自动打回。

## 渐进式披露机制
- `_meta/map.md`：全局雷达地图，严格控制在 ≤30 行，提供最高信噪比的主题空间导航。
- `index.md`：全量页面目录，由 `wiki_housekeep.py` 自动化聚合生成，严禁手工修改。
