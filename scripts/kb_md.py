#!/usr/bin/env python3
"""Markdown 小工具（10-10）：frontmatter 读写。只处理「键: 值」一层，够用即可，不引第三方 YAML 库。"""
import json
import re

_FM = re.compile(r"\A---\r?\n(.*?)\r?\n---\r?\n?", re.S)


def split_frontmatter(text):
    """返回 (dict, 正文)。值去掉引号；JSON 风格的带引号字符串按 JSON 解码；列表、嵌套忽略。"""
    m = _FM.match(text or "")
    if not m:
        return {}, text or ""
    fm = {}
    for line in m.group(1).splitlines():
        mm = re.match(r"^([A-Za-z_][\w\-]*):\s*(.*)$", line)
        if not mm:
            continue
        k, v = mm.group(1), mm.group(2).strip()
        if v.startswith('"') and v.endswith('"') and len(v) >= 2:
            try:
                v = json.loads(v)
            except ValueError:
                v = v[1:-1]
        elif v.startswith("'") and v.endswith("'") and len(v) >= 2:
            v = v[1:-1].replace("''", "'")
        fm[k] = v
    return fm, text[m.end():]


def dump_frontmatter(fm):
    """有序写出；字符串一律 JSON 引号（也是合法 YAML），空值跳过。"""
    lines = ["---"]
    for k, v in fm.items():
        if v is None or v == "":
            continue
        lines.append(f"{k}: {json.dumps(v, ensure_ascii=False) if isinstance(v, str) else v}")
    lines.append("---")
    return "\n".join(lines) + "\n"
