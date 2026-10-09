# Hermes KB Engine

> 面向 Hermes Agent 及自主 AI Agent 的自增长、自愈型生产级 Markdown 知识库引擎。

Hermes KB Engine 是一套经过生产环境长周期检验的 Agent 外部记忆与知识管理系统。它针对自主 Agent 长期运行中的四大顽疾——**语料噪声（GIGO）**、**时空错乱（旧笔记冒充现状）**、**上下文膨胀**以及**多端并发写入破坏**，提出了一整套解耦、自治且防腐烂的工程解决方案。

---

## 核心设计特性

1. **三层物理漏斗设计**：
   - **语料层 (`raw/conversations/`)**：纯追加（Append-Only）只读原始对话与事件，段落级全局唯一标号。
   - **索引层 (`kb.sqlite` + `QMD`)**：BM25 全文索引 + 向量混合检索，毫秒级主题定位与语义坐标寻址。
   - **活知识层 (`wiki/`)**：遵循 Karpathy LLM-Wiki 规范的结构化 Markdown，强双向链接，单页控制在 400 行以内。
2. **时空彻底解耦**：
   - 静态工程经验与稳定事实沉淀入 Wiki。
   - 机器、服务、网络、任务等易变现场状态交由 15 分钟级探针脚本（`now_status.py`）直出到 `now.md`。问即时现状直接读取看板，绝不让 Agent 依赖历史笔记猜测现实。
3. **两步受控提炼（Two-Stage Distillation）**：
   - **第一步（Extract）**：使用轻量极速模型进行纯文本无工具提取，输出结构化事实增量 JSON，避免 Agent 边查边改带来的 Token 巨额消耗与死循环。
   - **第二步（Merge）**：基于排他文件锁（`kb_lock.py`）原子化受控写入目标 Wiki 页面，彻底消除多端/并发冲突。
4. **反熵增与 Fail-Closed 门禁**：
   - **代码/反引号 70% 硬门禁**：`wiki_consolidate.py` 在重构或合并页面时严格比对 AST 词法，技术参数/代码保留率低于 70% 物理拦截并打回，防止模型过度抽象。
   - **事实出处强绑定**：核心结论与操作强绑定段落级出处 `^[kb:来源:片段id]`，杜绝无依据幻觉。
5. **模型完全解耦**：
   - 核心流水线不绑定特定厂商或昂贵 API，通过环境变量（`KB_MODEL`、`KB_PROVIDER`、`OPENAI_BASE_URL`）自由指定主力与兜底模型。

---

## 目录结构

```text
hermes-kb-engine/
├── ARCHITECTURE.md          # 深度架构设计 RFC、理论模型与设计哲学
├── README.md                # 快速入门与工程概览
├── cron/                    # 定时任务编排与标准 Prompt
│   └── hermes_kb_jobs.json  # Hermes Cron 任务导出模板
├── kb/                      # 底层索引与切片核心
│   ├── export_raw.py        # 原始会话清洗与标准格式导出
│   ├── kb.py                # 问答对切片与 SQLite 索引构建
│   └── qmd_embed_all.sh     # QMD 批量向量化脚本
├── plugins/                 # Hermes 网关与运行时插件
│   └── kb-recall/           # 拦截器：意图分流（现状探针 vs 经验检索召回）
├── scripts/                 # 提炼、门禁、看板与并发控制脚本
│   ├── kb_lock.py           # 基于文件锁与超时的并发写入控制器
│   ├── kb_models.py         # 多模型调用抽象与环境变量解析
│   ├── kb_stall_alert.py    # 积压监控与报警探针
│   ├── now_status.py        # 15 分钟现场状态探针（生成 now.md）
│   ├── qmd_refresh.sh       # QMD 增量索引刷新器
│   ├── wiki_consolidate.py  # 页面重整合并门禁脚本（含 70% 校验）
│   ├── wiki_extract.py      # 第一步：纯文本批量提炼
│   └── wiki_merge_feed.py   # 第二步：排他锁增量入库
├── tools/                   # 流水线与跨端同步工具
│   ├── nightly.sh           # 夜间全自动流水线入口
│   └── sync_mac.sh          # 跨机会话同步脚本
└── wiki/                    # 知识库规范与初始骨架
    ├── SCHEMA.md            # Wiki 文档排版规范与 Frontmatter 约定
    ├── _meta/map.md         # 全局雷达索引地图模板
    └── log.md               # 更新流水账规范
```

---

## 快速开始

### 1. 运行依赖

- **Python**：3.10+（依赖 `requests` 等基础库）
- **QMD**：本地混合检索器（用于向量与 BM25 检索）
- **Git**：版本控制与同步

### 2. 部署与目录初始化

在目标设备的用户主目录下创建标准知识库骨架：

```bash
# 1. 克隆仓库
git clone https://github.com/Cchhiillll/hermes-kb-engine.git ~/hermes-kb-engine

# 2. 创建运行时目录
mkdir -p ~/brain/raw/conversations
mkdir -p ~/brain/kb
mkdir -p ~/brain/wiki/{projects,entities,concepts,_meta}

# 3. 初始化知识库元数据
cp ~/hermes-kb-engine/wiki/SCHEMA.md ~/brain/wiki/
cp ~/hermes-kb-engine/wiki/_meta/map.md ~/brain/wiki/_meta/map.md
cp ~/hermes-kb-engine/wiki/log.md ~/brain/wiki/log.md

# 4. 配置脚本路径（建议软链接至 ~/.hermes/scripts/ 或加入 PATH）
mkdir -p ~/.hermes/scripts
ln -sf ~/hermes-kb-engine/scripts/* ~/.hermes/scripts/
```

### 3. 配置 QMD 检索空间

```bash
qmd collection add wiki ~/brain/wiki
qmd collection add raw ~/brain/raw/conversations
```

### 4. 环境变量与模型配置

通过环境变量解耦大模型后端，支持接入任何 OpenAI 兼容接口或本地模型网关：

```bash
# 知识库推理模型（默认可配置为极速高性价比模型，如 grok-4.6 / deepseek / gemini）
export KB_MODEL="grok-4.6"
export KB_PROVIDER="custom"
export OPENAI_BASE_URL="https://your-api-gateway.com/v1"
export OPENAI_API_KEY="your-api-key"
```

### 5. 定时调度接入

将定时任务注册到系统 Crontab 或 Hermes 定时调度器：

- **每 15 分钟**：运行 `now_status.py` 刷新现场状态并生成 `~/brain/kb/now.md`。
- **每 30 分钟**：运行 `wiki_extract.py` 消化待处理批次。
- **每日凌晨 03:00**：执行 `tools/nightly.sh`，完成多端会话归集、增量分块（`kb build`）与 QMD 索引重建。

---

## 完整架构细节

关于本系统的深层设计哲学、状态机转移逻辑、各层数据交互图及门禁实现规范，请参阅完整技术文档：
👉 [ARCHITECTURE.md](./ARCHITECTURE.md)

---

## License

MIT License
