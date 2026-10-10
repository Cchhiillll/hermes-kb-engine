import os
import subprocess


def test_lock_acquire_release(kbenv):
    kl = kbenv.load("kb_lock")
    ok, prev = kl.acquire("a", pid=str(os.getpid()))
    assert ok and prev is None
    ok, prev = kl.acquire("b", pid=str(os.getpid()))
    assert not ok and prev["holder"] == "a"
    assert kl.release("b") is False and kl.release("a") is True
    assert kl.acquire("b")[0]


def test_lock_dead_pid_is_reclaimed(kbenv):
    kl = kbenv.load("kb_lock")
    p = subprocess.Popen(["true"]); p.wait()
    assert kl.acquire("a", pid=str(p.pid))[0]
    ok, prev = kl.acquire("b")
    assert ok and prev["holder"] == "a"


def test_housekeep_index_map_and_log(kbenv):
    kbenv.write("projects/alpha.md", "---\ntitle: Alpha\n---\n> 一句话：[[concepts/x|某工具]]的说明\n", base=kbenv.wiki)
    kbenv.write("concepts/x.md", "---\ntitle: X 概念\n---\nbody\n", base=kbenv.wiki)
    log = "# log\n" + "".join(f"## [2026-10-{(i % 9) + 1:02d}] ingest | b{i}\n- x\n" for i in range(160))
    (kbenv.wiki / "log.md").write_text(log, encoding="utf-8")
    hk = kbenv.load("wiki_housekeep")
    changed, counts, total = hk.build_index()
    idx = (kbenv.wiki / "index.md").read_text(encoding="utf-8")
    assert changed and total == 2 and "[[projects/alpha]]：某工具的说明" in idx
    assert hk.build_index()[0] is False                 # 内容不变不重写
    assert hk.build_map(counts, total)
    assert hk.rotate_log() == 10
    assert (kbenv.wiki / "log.md").read_text(encoding="utf-8").count("## [") == 150
    assert (kbenv.wiki / "log-2026.md").read_text(encoding="utf-8").count("## [") == 10
