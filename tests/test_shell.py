"""shell 脚本：用假的 qmd（PATH 里的桩脚本）跑 nightly，不依赖真 QMD。"""
import os
import shutil
import subprocess
import sys

import pytest

from conftest import REPO, claude_jsonl


def _env(kbenv, path_extra=None, **kw):
    bindir = kbenv.tmp / "bin"
    bindir.mkdir(exist_ok=True)
    py = bindir / "python3"
    if not py.exists():
        py.symlink_to(sys.executable)
    path = os.pathsep.join([str(bindir)] + ([str(path_extra)] if path_extra else []) + ["/usr/bin", "/bin"])
    env = {"HOME": str(kbenv.home), "PATH": path, "KB_CONFIG": str(kbenv.tmp / "none.toml"), "LANG": "C.UTF-8"}
    env.update(kw)
    return env


def _stub(kbenv, name, body):
    d = kbenv.tmp / "stubs"
    d.mkdir(exist_ok=True)
    p = d / name
    p.write_text("#!/usr/bin/env bash\n" + body + "\n")
    p.chmod(0o755)
    return d


def test_nightly_with_stub_qmd(kbenv):
    claude_jsonl(kbenv.home / ".claude/projects/p/s1.jsonl", [("问题", "回答")])
    stubs = _stub(kbenv, "qmd", f'echo "$@" >> {kbenv.tmp}/qmd.calls\n[ "$1" = status ] && echo "Pending: 0"\nexit 0')
    r = subprocess.run(["bash", f"{REPO}/tools/nightly.sh"], env=_env(kbenv, stubs), capture_output=True, text=True)
    log = (kbenv.brain / ".state/nightly.log").read_text(encoding="utf-8")
    assert r.returncode == 0, log
    assert "✓ kb build" in log and "✓ qmd update" in log and "全部成功" in log
    assert kbenv.db.exists()
    assert "update" in (kbenv.tmp / "qmd.calls").read_text()


def test_nightly_without_qmd_fails_loudly(kbenv):
    r = subprocess.run(["bash", f"{REPO}/tools/nightly.sh"], env=_env(kbenv), capture_output=True, text=True)
    assert r.returncode != 0                                   # 原来缺组件静默跳过、返回 0
    assert "没有找到 qmd" in (kbenv.brain / ".state/nightly.log").read_text(encoding="utf-8")


def test_nightly_step_failure_propagates(kbenv):
    stubs = _stub(kbenv, "qmd", 'exit 3')
    r = subprocess.run(["bash", f"{REPO}/tools/nightly.sh"], env=_env(kbenv, stubs), capture_output=True, text=True)
    assert r.returncode != 0
    assert "✗ qmd update 退出码 3" in (kbenv.brain / ".state/nightly.log").read_text(encoding="utf-8")


def test_sync_mac_unconfigured_skips(kbenv):
    r = subprocess.run(["bash", f"{REPO}/tools/sync_mac.sh"], env=_env(kbenv), capture_output=True, text=True)
    assert r.returncode == 0
    assert "未配置 MAC_USER" in (kbenv.home / ".hermes/mac_sync.log").read_text(encoding="utf-8")


def test_no_personal_info_left():
    """仓库里不应再有个人路径、局域网 IP。"""
    bad = ("192.168.", "/Users/", "/Documents/文稿", "-ai-cli-env")
    hits = []
    for root, dirs, files in os.walk(REPO):
        dirs[:] = [d for d in dirs if d not in (".git", "__pycache__", ".venv", "tests", ".pytest_cache")]
        for f in files:
            if not f.endswith((".py", ".sh", ".md", ".json", ".toml", ".example")):
                continue
            t = open(os.path.join(root, f), encoding="utf-8", errors="ignore").read()
            hits += [f"{f}:{b}" for b in bad if b in t]
    assert not hits, hits
