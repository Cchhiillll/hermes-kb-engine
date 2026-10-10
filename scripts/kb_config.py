#!/usr/bin/env python3
"""知识库统一配置（10-10 起）：所有脚本从这里取路径、模型端点和开关，不再各自写死。

优先级：环境变量 > 配置文件 > 默认值。
配置文件：$KB_CONFIG，默认 ~/.config/hermes-kb/config.toml（需要 Python 3.11+ 的 tomllib；更低版本只认环境变量）。
密钥（OPENAI_API_KEY、YUQUE_TOKEN 等）**只从环境变量或 env 文件读**，配置文件里写了也不认，避免密钥进 git。

  python3 kb_config.py          打印当前生效的配置（不打印密钥）
"""
import os
import sys

try:
    import tomllib  # Python 3.11+
except ModuleNotFoundError:  # pragma: no cover - 3.10 兜底
    tomllib = None

# 不是对话的来源：这些段不进「读历史 / 提炼」队列，也不导出到 raw/conversations
NON_CONVERSATION_SRCS = ("page",)
SECRET_KEYS = {"api_key", "openai_api_key", "yuque_token", "token", "password"}

# 键 -> (环境变量名列表（前面的优先，后面的是兼容旧名）, 默认值)
_SPEC = {
    "brain_dir": (["KB_BRAIN_DIR"], "~/brain"),
    "db": (["KB_DB", "KB_DB_PATH"], "{brain_dir}/kb/kb.sqlite"),
    "wiki_dir": (["KB_WIKI_DIR", "WIKI_DIR"], "{brain_dir}/wiki"),
    "raw_dir": (["RAW_DIR"], "{brain_dir}/raw/conversations"),
    "batch_dir": (["KB_BATCH_DIR"], "{brain_dir}/kb/batches"),
    "stage_dir": (["KB_STAGE"], "{brain_dir}/kb/staging"),
    "lock_file": (["KB_LOCK"], "{brain_dir}/kb/write.lock"),
    "now_file": (["KB_NOW_FILE"], "{brain_dir}/kb/now.md"),
    "state_dir": (["KB_STATE_DIR"], "{brain_dir}/.state"),
    "sync_dir": (["MAC_SYNC_DIR"], "~/mac_agent_sync"),
    "hermes_home": (["HERMES_HOME"], "~/.hermes"),
    # 会话 cwd 落在这个目录下时，取下一级目录名当「项目」；空 = 不识别项目
    "project_root": (["KB_PROJECT_ROOT"], ""),
    # 来源前缀：本机读到的会话记成 <local_prefix>-xxx，从另一台机器同步来的记成 <sync_prefix>-xxx
    "local_prefix": (["KB_LOCAL_PREFIX"], "tp"),
    "sync_prefix": (["KB_SYNC_PREFIX"], "mac"),
    # Hermes 主库 / 各 profile 收哪些渠道的会话（逗号分隔）
    "hermes_sources": (["KB_HERMES_SOURCES"], "feishu,discord"),
    "profile_sources": (["KB_PROFILE_SOURCES"], "discord"),
    # OpenClaw 用户消息前缀（如「名字: 」），设置后入库时剥掉；空 = 不剥
    "openclaw_user_prefix": (["KB_OPENCLAW_USER_PREFIX"], ""),
    "mcp_url": (["KB_MCP_URL"], "http://127.0.0.1:8181/mcp"),
    # 模型：单一 OpenAI 兼容端点（例如 Gemini 中转）
    "model": (["KB_MODEL"], ""),
    "provider": (["KB_PROVIDER"], "custom"),
    "base_url": (["OPENAI_BASE_URL"], ""),
}


def _config_file():
    return os.path.expanduser(os.environ.get("KB_CONFIG") or "~/.config/hermes-kb/config.toml")


def _file_values():
    path = _config_file()
    if not tomllib or not os.path.exists(path):
        return {}
    try:
        with open(path, "rb") as f:
            data = tomllib.load(f)
    except (OSError, ValueError) as e:
        print(f"[kb_config] 配置文件读取失败，忽略：{path}（{e}）", file=sys.stderr)
        return {}
    flat = {}
    for k, v in data.items():
        if isinstance(v, dict):          # 允许 [paths] [model] 之类的分节，键名按 小节_键 或 键 都认
            for k2, v2 in v.items():
                flat.setdefault(k2, v2)
                flat[f"{k}_{k2}"] = v2
        else:
            flat[k] = v
    return {k: v for k, v in flat.items() if k.lower() not in SECRET_KEYS}


class Config(dict):
    __getattr__ = dict.__getitem__


def load():
    fv = _file_values()
    out = {}
    for key, (envs, default) in _SPEC.items():
        val = next((os.environ[e] for e in envs if os.environ.get(e)), None)
        if val is None:
            val = fv.get(key, default)
        out[key] = val
    # 展开 {brain_dir} 等占位符和 ~
    for _ in range(3):
        for k, v in out.items():
            if isinstance(v, str) and "{" in v:
                out[k] = v.format(**out)
    for k, v in out.items():
        if isinstance(v, str) and (v.startswith("~") or k.endswith("_dir") or k in ("db", "lock_file", "now_file")):
            out[k] = os.path.expanduser(v) if v else v
    return Config(out)


def hermes_env():
    """读 ~/.hermes/.env（KEY=VALUE 行），只用来补 OPENAI_* 这类端点配置。"""
    path = os.path.join(load().hermes_home, ".env")
    env = {}
    try:
        for line in open(path, encoding="utf-8", errors="ignore"):
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                env[k.strip().removeprefix("export ").strip()] = v.strip().strip("'\"")
    except OSError:
        pass
    return env


def secret(name):
    """密钥只从环境变量或 ~/.hermes/.env 取；取不到返回空串。"""
    return os.environ.get(name) or hermes_env().get(name, "")


def non_conversation_sql(col="src"):
    return f"{col} not in ({', '.join(repr(s) for s in NON_CONVERSATION_SRCS)})"


if __name__ == "__main__":
    c = load()
    print(f"配置文件：{_config_file()}（{'存在' if os.path.exists(_config_file()) else '不存在'}）")
    for k in sorted(c):
        print(f"  {k} = {c[k]}")
    print(f"  OPENAI_API_KEY = {'已设置' if secret('OPENAI_API_KEY') else '未设置'}")
