#!/usr/bin/env python3
"""检索评测（10-10）：一组「问题 → 期望命中」对，跑 pass@1/5/10 和 MRR，和上次比有没有退步。

评测集（JSONL，每行一个）：{"q": "问题", "expect": ["wiki/projects/x.md", "yuque/书/文档.md", "tp-claude:会话:3"], "collections": ["wiki"], "backend": "kb"}
  - expect 里任一条命中就算对；文件按「集合/相对路径」写（结尾匹配即可），段 id 用 kb 后端
  - collections 可选（默认 kb-recall 的集合）；backend 可选（qmd 或 kb，默认取命令行 --backend）
  默认读 $KB_EVAL_DIR/golden.jsonl（~/brain/kb/eval/golden.jsonl）；格式示例见仓库 eval/golden.example.jsonl。

  kb_eval.py [--golden 文件] [--backend qmd|kb] [--k 10] [--no-save]

结果写 $KB_EVAL_DIR/last.json，历史追加到 history.jsonl。pass@5 比上次同一评测集、同一后端低超过 KB_EVAL_TOLERANCE（默认 0.05）
算退步：退出码 2，并在历史里记 regressed=true（学习闭环据此暂停「草稿 → 稳定」升级）。
"""
import hashlib
import importlib.util
import json
import os
import sys
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)
import kb_config

KS = (1, 5, 10)


def load_golden(path):
    items = []
    for n, line in enumerate(open(path, encoding="utf-8"), 1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        it = json.loads(line)
        if not it.get("q") or not it.get("expect"):
            raise ValueError(f"{path}:{n} 缺 q 或 expect")
        items.append(it)
    return items


def _recall_logic():
    """加载 kb-recall 的 logic.py（仓库里的那份，或 ~/.hermes/plugins/kb-recall 下的）。"""
    for p in (os.path.join(_HERE, "..", "plugins", "kb-recall", "logic.py"),
              os.path.join(kb_config.load().hermes_home, "plugins", "kb-recall", "logic.py")):
        p = os.path.realpath(p)
        if os.path.exists(p):
            spec = importlib.util.spec_from_file_location("kb_recall_logic_eval", p)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            return mod
    raise FileNotFoundError("找不到 kb-recall/logic.py")


def search_qmd(q, collections, k):
    lg = _recall_logic()
    hits = lg._query(q, collections or lg.COLLECTIONS) if hasattr(lg, "_query") else lg._search(q)
    return [(h.get("file") or "").removeprefix("qmd://") for h in hits][:k]


def search_kb(q, collections, k):
    sys.path.insert(0, os.path.join(_HERE, "..", "kb"))
    import kb
    return [r[1] for r in kb.search(q, k)]


BACKENDS = {"qmd": search_qmd, "kb": search_kb}


def rank_of(results, expect):
    for i, r in enumerate(results, 1):
        if any(r == e or r.endswith("/" + e) for e in expect):      # 完全相同，或期望省略了前面的集合/目录
            return i
    return None


def evaluate(items, backend="qmd", k=10, searcher=None):
    rows = []
    for it in items:
        be = it.get("backend") or backend
        fn = searcher or BACKENDS[be]
        try:
            res = fn(it["q"], it.get("collections"), max(k, max(KS)))
            err = ""
        except Exception as e:
            res, err = [], str(e)[:120]
        rows.append({"q": it["q"], "backend": be, "rank": rank_of(res, it["expect"]), "top": res[:5], "error": err})
    n = max(len(rows), 1)
    summary = {f"pass@{kk}": round(sum(1 for r in rows if r["rank"] and r["rank"] <= kk) / n, 4) for kk in KS}
    summary["mrr"] = round(sum(1 / r["rank"] for r in rows if r["rank"]) / n, 4)
    summary["n"] = len(rows)
    summary["errors"] = sum(1 for r in rows if r["error"])
    return summary, rows


def _history(path):
    try:
        return [json.loads(l) for l in open(path, encoding="utf-8") if l.strip()]
    except OSError:
        return []


def latest_regressed(eval_dir=None):
    """最近一次评测是否退步（学习闭环用：退步时暂停升级）。没跑过评测返回 False。"""
    h = _history(os.path.join(eval_dir or kb_config.load().eval_dir, "history.jsonl"))
    return bool(h and h[-1].get("regressed"))


def main(argv=None):
    a = sys.argv[1:] if argv is None else argv
    cfg = kb_config.load()
    opt = lambda f, d=None: a[a.index(f) + 1] if f in a else d
    golden = opt("--golden", os.path.join(cfg.eval_dir, "golden.jsonl"))
    backend = opt("--backend", "qmd")
    k = int(opt("--k", 10))
    if not os.path.exists(golden):
        print(f"没有评测集：{golden}（参考仓库 eval/golden.example.jsonl 写 20~50 条）")
        return 1
    items = load_golden(golden)
    summary, rows = evaluate(items, backend, k)
    gid = hashlib.sha1(open(golden, "rb").read()).hexdigest()[:10]
    hist_path = os.path.join(cfg.eval_dir, "history.jsonl")
    prev = next((h for h in reversed(_history(hist_path)) if h.get("golden") == gid and h.get("backend") == backend), None)
    tol = float(os.environ.get("KB_EVAL_TOLERANCE") or 0.05)
    regressed = bool(prev and summary["pass@5"] < prev["pass@5"] - tol)
    print(f"评测 {len(items)} 题（{backend}）：" + "，".join(f"{kk} {summary[kk]:.0%}" for kk in ("pass@1", "pass@5", "pass@10"))
          + f"，MRR {summary['mrr']:.3f}" + (f"，{summary['errors']} 题出错" if summary["errors"] else ""))
    for r in rows:
        if not r["rank"]:
            print(f"  ✗ {r['q']}" + (f"（出错：{r['error']}）" if r["error"] else f" → 前几名：{', '.join(r['top'][:3]) or '无'}"))
    if prev:
        print(f"上次 pass@5 {prev['pass@5']:.0%} → 这次 {summary['pass@5']:.0%}" + ("，退步了" if regressed else ""))
    if "--no-save" not in a:
        os.makedirs(cfg.eval_dir, exist_ok=True)
        rec = {"ts": time.strftime("%F %T"), "golden": gid, "backend": backend, **summary, "regressed": regressed}
        json.dump({"summary": rec, "rows": rows}, open(os.path.join(cfg.eval_dir, "last.json"), "w", encoding="utf-8"),
                  ensure_ascii=False, indent=1)
        with open(hist_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    return 2 if regressed else 0


if __name__ == "__main__":
    sys.exit(main())
