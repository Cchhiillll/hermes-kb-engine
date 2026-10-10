# Hermes KB Engine

> 面向 Hermes Agent 及自主 AI Agent 的自增长、自愈型生产级 Markdown 知识库引擎。

Hermes KB Engine 是一套经过生产环境长周期检验的 Agent 外部记忆与知识管理系统。它针对自主 Agent 长期运行中的四大顽疾——**语料噪声（GIGO）**、**时空错乱（旧笔记冒充现状）**、**上下文膨胀**以及**多端并发写入破坏**，提出了一整套解耦、自治且防腐烂的工程解决方案。

---

## 核心设计特性

1. **三层物理漏斗设计**：
   - **语料层 (`raw/conversations/`)**：纯追加（Append-Only）只读原始对话与事件，段落级全局唯一标号。
   - **索引层 (`kb.sqlite` + `QMD`)**：`kb.sqlite` 是 SQLite FTS5 全文索引；向量检索、混合排序与重排由 QMD 负责。
   - **活知识层 (`wiki/`)**：遵循 Karpathy LLM-Wiki 规范的结构化 Markdown，强双向链接，单页控制在 400 行以内。
2. **多源数据采集与标准化流转（Ingestion Pipeline）**：
   - **采集源适配**：原生支持本地 Hermes（`~/.hermes/state.db` 及 profiles）、Claude Code（`~/.claude/projects/`）、Codex（`~/.codex/sessions/`）以及外部同步的通用 JSONL 会话；通过 `tools/sync_mac.sh` 从另一台机器（如 Mac）增量拉取（主机、用户名由配置给出）。
   - **入库切片 (`kb build`)**：清洗掉系统提示词与工具裸输出，拆分成一问一答切片，分配唯一段 ID（`^[kb:段id]`），存入 `kb.sqlite` 的 chunks 全文索引表。
   - **两步提炼 (`wiki_extract.py`)**：定时扫描未读对话切片，由模型提炼出具备复用价值的事实、结论、偏好、踩坑与排障经验。
   - **受控并入 (`wiki_merge_feed.py`)**：基于排他文件锁原子化挂载到 `wiki/` 目录下的对应页（`projects/` 项目页、`entities/` 实体/服务页、`concepts/` 通用方法页）。
   - **反熵增重整 (`wiki_consolidate.py`)**：定期合并碎片页与收敛大页，带有反引号代码 70% 保留率的 Fail-Closed 硬门禁。
3. **时空彻底解耦**：
   - 静态工程经验与稳定事实沉淀入 Wiki。
   - 易变现场状态（服务连通性、网络、即时任务）由独立现场探针直出到现状看板（`now.md`），问即时现状直接读取看板，绝不让 Agent 依赖历史笔记猜测现实。
4. **两步受控提炼（Two-Stage Distillation）**：
   - **第一步（Extract）**：使用轻量极速模型进行纯文本无工具提取，输出结构化事实增量 JSON，避免 Agent 边查边改带来的 Token 巨额消耗与死循环。
   - **第二步（Merge）**：基于排他文件锁（`kb_lock.py`）原子化受控写入目标 Wiki 页面，彻底消除多端/并发冲突。
5. **反熵增与 Fail-Closed 门禁**：
   - **代码/反引号 70% 硬门禁**：`wiki_consolidate.py` 在重构或合并页面时，用正则提取重写前后反引号里的具体项（命令、路径、端口、版本、配置项）逐一比对，保留率低于 70% 拦截并打回，防止模型过度抽象。
   - **事实出处强绑定**：核心结论与操作强绑定段落级出处 `^[kb:来源:片段id]`，杜绝无依据幻觉。
6. **模型完全解耦**：
   - 核心流水线只认一个 OpenAI 兼容端点（例如 Gemini 中转）：`KB_MODEL` + `OPENAI_BASE_URL` + `OPENAI_API_KEY`。没配好时提炼/并入任务自动让路，不会悄悄改连别的地址。

---

## 目录结构

```text
hermes-kb-engine/
├── ARCHITECTURE.md            # 深度架构设计 RFC、理论模型与设计哲学
├── README.md                  # 快速入门与工程概览
├── LICENSE                    # MIT
├── config.example.toml        # 配置模板（复制到 ~/.config/hermes-kb/config.toml）
├── env.example                # 密钥与端点环境变量模板（只写名字，值自己填）
├── cron/
│   └── hermes_kb_jobs.json    # Hermes Cron 任务导出模板（含完整 Prompt）
├── kb/                        # 底层索引与切片
│   ├── kb.py                  # 各来源解析 → 问答切片 → 脱敏 → SQLite FTS5
│   ├── export_raw.py          # 切片按会话导出 Markdown 原始层（只删自己生成的文件）
│   └── qmd_embed_all.sh       # QMD 批量向量化（没算完以非 0 退出）
├── plugins/
│   └── kb-recall/             # Hermes pre_llm_call 插件：现状页 + 知识库相关页注入
├── scripts/                   # 提炼、门禁、并发控制与维护
│   ├── kb_config.py           # 统一配置（环境变量 > 配置文件 > 默认值）
│   ├── kb_llm.py              # 唯一的模型调用口（OpenAI 兼容端点）
│   ├── kb_models.py           # 模型是否就绪（没配好就让路）
│   ├── kb_lock.py             # 知识库单写入锁
│   ├── kb_stall_alert.py      # 积压停工报警
│   ├── qmd_refresh.sh         # 每小时 QMD 增量刷新 + wiki Git 快照
│   ├── wiki_feed.py           # 送料 / 交卷闸门（出处、无可记、新页命名）
│   ├── wiki_extract.py        # 第一步：纯文本批量提炼（解析容错、失败减半）
│   ├── wiki_merge_feed.py     # 第二步：排他锁出料，交给 Hermes 并入
│   ├── wiki_consolidate.py    # 碎页合并 / 大页重整 + 70% 门禁
│   ├── wiki_consolidate_grok.py / wiki_consolidate_luna.py  # 旧的按额度分路入口（可选）
│   └── wiki_housekeep.py      # index.md / _meta/map.md / log.md 轮换（不调模型）
├── tests/                     # pytest 测试（只用合成数据，模型和 QMD 全部打桩）
├── pyproject.toml             # pytest 配置与测试依赖
├── .github/workflows/ci.yml   # CI：语法检查 + shellcheck + pytest（Python 3.11~3.13）
├── tools/
│   ├── check.sh               # 本地 / CI 统一检查入口
│   ├── nightly.sh             # 夜间流水线入口（任一步失败则非 0 退出）
│   └── sync_mac.sh            # 从另一台机器增量拉取各 Agent 会话
└── wiki/                      # 知识库规范与初始骨架
    ├── SCHEMA.md
    ├── _meta/map.md
    └── log.md
```

**仓库外的依赖**（生产环境里有，本仓库不包含）：`~/brain/kb/now.md` 现状探针脚本（每 15 分钟实测写入）、Hermes 的 `llm-wiki` 技能、飞书卡片模块 `feishu_card.py`（停工报警用，缺失时只打印）。

---

## 快速开始

### 1. 运行依赖

- **Python**：3.10+（只用标准库；读 TOML 配置文件需要 3.11+，3.10 下只认环境变量）
- **QMD**：本地混合检索器（BM25 + 向量 + 重排，自带 MCP 服务）
- **Git**、`rsync`/`ssh`（跨机同步时）、`zstd`（收录 DSH 会话时）

### 2. 部署与目录初始化

```bash
# 1. 克隆仓库
git clone https://github.com/Cchhiillll/hermes-kb-engine.git ~/hermes-kb-engine

# 2. 创建运行时目录
mkdir -p ~/brain/raw/conversations ~/brain/kb ~/brain/wiki/{projects,entities,concepts,_meta}

# 3. 初始化知识库元数据
cp ~/hermes-kb-engine/wiki/SCHEMA.md ~/brain/wiki/
cp ~/hermes-kb-engine/wiki/_meta/map.md ~/brain/wiki/_meta/map.md
cp ~/hermes-kb-engine/wiki/log.md ~/brain/wiki/log.md

# 4. 脚本软链接到 Hermes 脚本目录（cron 任务按这个路径调用）
mkdir -p ~/.hermes/scripts
ln -sf ~/hermes-kb-engine/scripts/* ~/.hermes/scripts/

# 5. 配置
mkdir -p ~/.config/hermes-kb
cp ~/hermes-kb-engine/config.example.toml ~/.config/hermes-kb/config.toml   # 路径、项目根目录、同步前缀等
cp ~/hermes-kb-engine/env.example ~/.config/hermes-kb/env                   # 端点与密钥（nightly.sh 会自动 source）
python3 ~/hermes-kb-engine/scripts/kb_config.py                              # 查看生效配置（不打印密钥）
```

### 3. 配置 QMD 检索空间

```bash
qmd collection add wiki ~/brain/wiki
qmd collection add raw ~/brain/raw/conversations
qmd mcp --http --daemon        # kb-recall 默认连 http://127.0.0.1:8181/mcp（KB_MCP_URL 可改）
```

### 4. 配置项与模型

所有路径和开关集中在 `scripts/kb_config.py`，优先级：**环境变量 > `~/.config/hermes-kb/config.toml` > 默认值**。密钥只从环境变量或 `~/.hermes/.env` 读取，配置文件里写了也不认。

| 环境变量 | 作用 | 默认 |
|---|---|---|
| `KB_BRAIN_DIR` | 知识库根目录 | `~/brain` |
| `KB_DB`（兼容 `KB_DB_PATH`） | 切片数据库 | `$KB_BRAIN_DIR/kb/kb.sqlite` |
| `KB_WIKI_DIR`（兼容 `WIKI_DIR`） | Wiki 目录 | `$KB_BRAIN_DIR/wiki` |
| `RAW_DIR` | 原始层导出目录 | `$KB_BRAIN_DIR/raw/conversations` |
| `MAC_SYNC_DIR` | 另一台机器同步来的会话 | `~/mac_agent_sync` |
| `KB_PROJECT_ROOT` | 会话 cwd 在此目录下时取下一级目录名当「项目」 | 空（不识别） |
| `KB_LOCAL_PREFIX` / `KB_SYNC_PREFIX` | 本机 / 同步来源的前缀 | `tp` / `mac` |
| `KB_HERMES_SOURCES` / `KB_PROFILE_SOURCES` | 收录 Hermes 主库 / 各 profile 的哪些渠道 | `feishu,discord` / `discord` |
| `KB_MCP_URL` | QMD MCP 地址 | `http://127.0.0.1:8181/mcp` |
| `KB_RECALL_PLATFORMS` / `KB_RECALL_COLLECTIONS` | kb-recall 生效平台 / 检索集合 | `feishu,cli` / `wiki` |
| `MAC_USER` / `MAC_HOSTS` | sync_mac 的远端用户名 / 候选主机（空格分隔） | 空（不同步） |
| `KB_ENV_FILE` | nightly.sh 启动时 source 的 env 文件 | `~/.config/hermes-kb/env` |
| `QMD_BIN` / `KB_SKIP_QMD` | qmd 路径 / 显式跳过 QMD 步骤 | `qmd` / `0` |

模型只认一个 OpenAI 兼容端点（例如 Gemini 中转）：

```bash
export KB_MODEL="gemini-2.5-flash"                 # 按你的中转实际提供的模型名填写
export OPENAI_BASE_URL="https://your-relay.example.com/v1"
export OPENAI_API_KEY="..."                        # 放环境变量或 ~/.hermes/.env，不要写进仓库
python3 ~/.hermes/scripts/kb_models.py             # 显示是否就绪
```

旧的按 Grok / luna 额度分路的逻辑保留为可选（`KB_LEGACY_ROUTING=1`），依赖 Hermes 内部模块和非官方接口，不推荐。

### 5. 定时调度接入

将定时任务注册到系统 Crontab 或 Hermes 定时调度器（可直接导入 `cron/hermes_kb_jobs.json`）：

- **每 10 分钟**：运行 `wiki_merge_feed.py`，由 Hermes 执行语义原子并入并保留出处段 ID。
- **每 30 分钟**：运行 `wiki_extract.py` 消化待处理切片；运行 `kb_stall_alert.py` 检查是否停工。
- **每 4 小时**：运行 `wiki_consolidate.py` 进行碎页合并与大页四段式重整（70% 具体项保留门禁）。
- **每日凌晨 03:00**：执行 `tools/nightly.sh`，完成会话同步、增量分块（`kb build`）、原始层导出、QMD 索引更新与 Git 自动备份；任何一步失败都会以非 0 退出并写入 `~/brain/.state/nightly.log`。

### 6. 会话 ID 与旧出处兼容

`kb build` 给**新会话**分配更长的会话 ID（超过 16 位时取前 8 位 + 6 位哈希），避免原来 8 位截断导致的撞车误删；**已收录过的文件沿用当时的来源和会话 ID**（从 `files` / `chunks` 表反查），所以知识库里已有的 `^[kb:来源:会话:序号]` 出处全部继续有效，不需要迁移。
同一文件只会被收录一次（原来本机 `~/.claude`、`~/.zcode` 会被当成两个来源各扫一遍）；新收录的本机会话记为 `tp-*`、同步来的记为 `mac-*`（前缀可配置）。

### 7. 测试与检查

```bash
python3 -m venv .venv && .venv/bin/pip install "pytest>=8" "shellcheck-py>=0.10"
PATH=.venv/bin:$PATH PYTHON=.venv/bin/python bash tools/check.sh   # 语法 + shellcheck + pytest
```

- 测试只用合成数据：每个用例一个临时 HOME，模型调用（`kb_llm.chat`）和 QMD（PATH 里的桩脚本 / mock 的 MCP 请求）全部打桩，不连任何真实服务。
- 覆盖：各来源解析与去重、会话 ID 撞车与旧出处沿用、脱敏、导出不误删、`--done` 闸门、提炼容错与失败减半、合并/重整 70% 门禁与提交冲突、写锁、index/log 维护、kb-recall、nightly 失败退出码、仓库里不残留个人路径。
- 每个 PR 都由 CI 跑同一套检查（`.github/workflows/ci.yml`）。

---

## 完整架构细节

关于本系统的深层设计哲学、状态机转移逻辑、各层数据交互图及门禁实现规范，请参阅完整技术文档：
👉 [ARCHITECTURE.md](./ARCHITECTURE.md)

---

## License

MIT License
