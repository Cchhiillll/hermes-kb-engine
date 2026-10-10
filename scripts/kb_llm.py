#!/usr/bin/env python3
"""知识库唯一的模型调用口（10-10）：一个 OpenAI 兼容端点（例如 Gemini 中转）。

  KB_MODEL          模型名（必填）
  OPENAI_BASE_URL   端点，例如 https://你的中转/v1（必填；也可写在 ~/.hermes/.env）
  OPENAI_API_KEY    密钥（环境变量或 ~/.hermes/.env；不写进配置文件）

不再有「Hermes 调用失败就悄悄改连一个写死的第三方地址」的回退：没配好就直接报错，由调用方决定让路。
测试里用 monkeypatch 替换 chat()，不连真模型。
"""
import json
import os
import sys
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import kb_config


class LLMNotConfigured(RuntimeError):
    pass


def endpoint():
    """返回 (base_url, api_key, model)；缺哪个就抛 LLMNotConfigured。"""
    cfg = kb_config.load()
    base = (cfg.base_url or kb_config.hermes_env().get("OPENAI_BASE_URL", "")).rstrip("/")
    key = kb_config.secret("OPENAI_API_KEY")
    model = cfg.model
    missing = [n for n, v in (("KB_MODEL", model), ("OPENAI_BASE_URL", base)) if not v]
    if missing:
        raise LLMNotConfigured("缺少 " + "、".join(missing))
    return base, key, model


def chat(messages, model=None, temperature=0.2, timeout=300):
    """调一次 chat/completions，返回 (文本, 总 token 数)。"""
    base, key, default_model = endpoint()
    payload = json.dumps({"model": model or default_model, "messages": messages,
                          "temperature": temperature}).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    if key:
        headers["Authorization"] = f"Bearer {key}"
    req = urllib.request.Request(f"{base}/chat/completions", data=payload, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    text = data["choices"][0]["message"]["content"] or ""
    tok = (data.get("usage") or {}).get("total_tokens", 0) or 0
    return text, tok


if __name__ == "__main__":
    try:
        b, k, m = endpoint()
        print(f"端点：{b}  模型：{m}  密钥：{'已设置' if k else '未设置'}")
    except LLMNotConfigured as e:
        print(f"未配置：{e}"); sys.exit(1)
