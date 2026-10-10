"""测试里直接往 kb.sqlite 塞合成片段。"""
import hashlib


def add_chunks(kb, rows):
    """rows: [(id, user, reply), ...]，id 形如 src:session:seq。"""
    c = kb.conn()
    for i, (cid, user, reply) in enumerate(rows):
        src, session, seq = cid.rsplit(":", 2)
        c.execute("insert or replace into chunks values(?,?,?,?,?,?,?,?,?,?,?)",
                  (cid, src, session, "", 1000 + i, "2026-10-08", int(seq), user, reply, "[]",
                   hashlib.sha1(cid.encode()).hexdigest()))
    c.commit()
    return c
