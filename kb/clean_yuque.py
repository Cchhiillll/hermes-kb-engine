#!/usr/bin/env python3
"""语雀导出 → 检索用 Markdown（10-10）。

输入：导出工具（yuque-exporter / Elog）写出的原样 Markdown（KB_YUQUE_EXPORT_DIR，默认 ~/brain/sources/yuque-export）
输出：清洗后的 Markdown（KB_YUQUE_DIR，默认 ~/brain/sources/yuque），供 QMD「yuque」集合和 kb.py（来源 yuque-doc）使用。

做的事：
- 去掉语雀残留的 HTML：<a name> 锚点、<font>/<span>/<div> 等样式标签、<br>、lake 注释、&nbsp;、零宽字符
- 标题规范化（#标题 → # 标题），图片链接去掉语雀 CDN 的 #averageHue 等尾巴，相对图片路径改成导出目录里的绝对路径
- 统一 frontmatter：title / book / toc_path / doc_key / yuque_id / slug / url / updated_at / source_path / content_sha / generator
- 加上下文行（contextual retrieval）：H1 下写「> 上下文：知识库 › 目录 › 标题」，每个 ## 小节下写「> 上下文：标题 › 小节」，
  这样检索命中某一小节时也知道它属于哪篇、哪个目录
- 幂等：内容没变不重写；只删除自己生成的（带 generator 标记）且源文件已不在的文件，目录里别的东西不动

  clean_yuque.py [--src 导出目录] [--out 输出目录] [--dry]
"""
import hashlib
import os
import re
import sys
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
for _p in (os.path.join(_HERE, "..", "scripts"), os.path.expanduser("~/.hermes/scripts")):
    if os.path.isdir(_p) and _p not in sys.path:
        sys.path.insert(0, _p)
import kb_config
from kb_md import split_frontmatter, dump_frontmatter

GENERATOR = "hermes-kb/clean_yuque"
FENCE = re.compile(r"^\s*(```|~~~)")
CONTEXT = re.compile(r"^> 上下文：")

_TAGS_DROP = re.compile(r"</?(?:font|span|div|u|mark|center|label|colgroup|col)\b[^>]*>", re.I)
_ANCHOR = re.compile(r'<a\s+(?:name|id)="[^"]*"\s*>\s*</a>', re.I)
_BR = re.compile(r"<br\s*/?>", re.I)
_P = re.compile(r"</?p\b[^>]*>", re.I)
_COMMENT = re.compile(r"<!--.*?-->", re.S)
_IMG = re.compile(r"(!\[[^\]]*\]\()([^)\s]+)((?:\s+\"[^\"]*\")?\))")


def _img(m, src_file):
    url = m.group(2)
    if re.match(r"https?://", url):
        if "yuque" in url or "nlark" in url:
            url = url.split("#", 1)[0]
            url = re.sub(r"\?x-oss-process=[^&]*$", "", url)
    elif not url.startswith(("data:", "/")):
        url = os.path.normpath(os.path.join(os.path.dirname(src_file), url))
    return m.group(1) + url + m.group(3)


def clean_body(md, src_file=""):
    """清洗正文（不含 frontmatter）。代码块里的内容原样保留。"""
    out, in_code = [], False
    for line in md.replace("\r\n", "\n").split("\n"):
        if FENCE.match(line):
            in_code = not in_code
            out.append(line)
            continue
        if in_code:
            out.append(line)
            continue
        line = _ANCHOR.sub("", line)
        line = _BR.sub("\n", line)
        line = _P.sub("\n", line)
        line = _TAGS_DROP.sub("", line)
        line = line.replace("&nbsp;", " ").replace("\u200b", "").replace("\ufeff", "")
        line = re.sub(r"^(#{1,6})([^#\s])", r"\1 \2", line)
        line = _IMG.sub(lambda m: _img(m, src_file), line)
        if CONTEXT.match(line):          # 上一轮加的上下文行（重复清洗时）去掉，下面重新生成
            continue
        out.extend(x.rstrip() for x in line.split("\n"))
    text = "\n".join(out)
    text = _COMMENT.sub("", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip() + "\n"


def add_context(body, title, crumbs):
    """H1 保证存在且等于标题；H1 下写整篇的上下文，每个 ## 下写小节的上下文。"""
    lines = body.split("\n")
    i = next((k for k, l in enumerate(lines) if l.strip()), None)
    if i is not None and re.match(r"^# ", lines[i]):
        lines = lines[:i] + lines[i + 1:]
    out = [f"# {title}", "", "> 上下文：" + " › ".join(crumbs + [title]), ""]
    in_code = False
    for l in lines:
        out.append(l)
        if FENCE.match(l):
            in_code = not in_code
        elif not in_code and re.match(r"^## \S", l):
            out += ["", f"> 上下文：{title} › {l[3:].strip()}"]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(out)).strip() + "\n"


def doc_meta(fm, rel, src_file, base_url=""):
    parts = rel.split(os.sep)
    book = fm.get("book") or (parts[0] if len(parts) > 1 else "语雀")
    toc = parts[1:-1] if len(parts) > 1 else []
    title = (fm.get("title") or os.path.splitext(parts[-1])[0]).strip()
    yid = str(fm.get("yuque_id") or fm.get("doc_id") or fm.get("id") or "").strip()
    slug = str(fm.get("slug") or fm.get("urlname") or "").strip()
    updated = str(fm.get("updated_at") or fm.get("updated") or fm.get("date") or "").strip()
    if not updated:
        updated = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(os.path.getmtime(src_file)))
    url = fm.get("url") or ""
    if not url and base_url and slug:
        url = f"{base_url.rstrip('/')}/{fm.get('book_slug') or book}/{slug}"
    key_src = yid if re.fullmatch(r"[0-9A-Za-z\-]{1,16}", yid) else ""
    doc_key = key_src or "h" + hashlib.sha1(f"{book}/{'/'.join(toc)}/{title}".encode()).hexdigest()[:10]
    return {"title": title, "book": book, "toc_path": " / ".join(toc), "doc_key": doc_key, "yuque_id": yid,
            "slug": slug, "url": url, "updated_at": updated}


def is_ours(path):
    try:
        head = open(path, encoding="utf-8", errors="ignore").read(1500)
    except OSError:
        return False
    return f'generator: "{GENERATOR}"' in head or f"generator: {GENERATOR}" in head


def convert(src_file, rel, base_url=""):
    raw = open(src_file, encoding="utf-8", errors="ignore").read()
    fm, body = split_frontmatter(raw)
    meta = doc_meta(fm, rel, src_file, base_url)
    crumbs = [meta["book"]] + ([x.strip() for x in meta["toc_path"].split(" / ")] if meta["toc_path"] else [])
    text = add_context(clean_body(body, src_file), meta["title"], crumbs)
    meta["source_path"] = rel
    meta["content_sha"] = hashlib.sha1(text.encode()).hexdigest()[:12]
    meta["generator"] = GENERATOR
    return dump_frontmatter(meta) + "\n" + text


def run(src, out, dry=False):
    cfg = kb_config.load()
    keep, wrote, same = set(), 0, 0
    for root, dirs, files in os.walk(src):
        dirs[:] = sorted(d for d in dirs if not d.startswith("."))
        for f in sorted(files):
            if not f.endswith(".md") or f.startswith("."):
                continue
            sf = os.path.join(root, f)
            rel = os.path.relpath(sf, src)
            dst = os.path.join(out, rel)
            keep.add(os.path.abspath(dst))
            text = convert(sf, rel, cfg.yuque_base_url)
            if os.path.exists(dst) and open(dst, encoding="utf-8", errors="ignore").read() == text:
                same += 1
                continue
            if os.path.exists(dst) and not is_ours(dst):
                print(f"跳过：{dst} 不是本脚本生成的，不覆盖")
                continue
            wrote += 1
            if not dry:
                os.makedirs(os.path.dirname(dst), exist_ok=True)
                tmp = dst + ".tmp"
                open(tmp, "w", encoding="utf-8").write(text)
                os.replace(tmp, dst)
    gone = 0
    for root, dirs, files in os.walk(out, topdown=False):
        for f in files:
            p = os.path.abspath(os.path.join(root, f))
            if f.endswith(".md") and p not in keep and is_ours(p):
                gone += 1
                if not dry:
                    os.remove(p)
        if not dry and root != out and not os.listdir(root):
            os.rmdir(root)
    print(f"语雀清洗：{len(keep)} 篇，写入 {wrote}，未变 {same}，删除 {gone}" + ("（试跑，未落盘）" if dry else ""))
    return wrote, same, gone


def main(argv=None):
    a = sys.argv[1:] if argv is None else argv
    cfg = kb_config.load()
    src = a[a.index("--src") + 1] if "--src" in a else cfg.yuque_export_dir
    out = a[a.index("--out") + 1] if "--out" in a else cfg.yuque_dir
    if not os.path.isdir(src):
        print(f"没有语雀导出目录：{src}（先运行 tools/sync_yuque.sh）")
        return 0
    if os.path.abspath(src) == os.path.abspath(out):
        print("导出目录和输出目录不能是同一个"); return 1
    run(src, out, "--dry" in a)
    return 0


if __name__ == "__main__":
    sys.exit(main())
