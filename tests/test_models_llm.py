import io
import json


def test_pick_unconfigured_and_configured(kbenv):
    km = kbenv.load("kb_models")
    name, model, provider, why = km.pick()
    assert name is None and "KB_MODEL未设置" in why
    kbenv.mp.setenv("KB_MODEL", "gemini-test")
    kbenv.write(".hermes/.env", "OPENAI_BASE_URL=http://relay.invalid/v1\n")
    assert km.pick()[:2] == ("custom", "gemini-test")


def test_legacy_gemini_without_probe_is_not_ready(kbenv):
    kbenv.mp.setenv("KB_LEGACY_ROUTING", "1")
    km = kbenv.load("kb_models")
    ok, why = km.gemini()
    assert ok is False and "GEMINI_STATUS_CMD" in why        # 原来无条件返回可用
    kbenv.mp.setenv("GEMINI_STATUS_CMD", "echo '{\"gemini_usable\": 2}'")
    assert km.gemini()[0] is True
    assert km.pick()[0] == "gemini"


def test_llm_chat_request(kbenv, monkeypatch):
    kl = kbenv.load("kb_llm")
    try:
        kl.endpoint(); assert False
    except kl.LLMNotConfigured as e:
        assert "KB_MODEL" in str(e)
    kbenv.mp.setenv("KB_MODEL", "m1")
    kbenv.mp.setenv("OPENAI_BASE_URL", "http://relay.invalid/v1/")
    kbenv.mp.setenv("OPENAI_API_KEY", "k-test")
    seen = {}

    class R(io.BytesIO):
        def __enter__(self): return self
        def __exit__(self, *a): pass

    def fake(req, timeout):
        seen["url"], seen["auth"], seen["body"] = req.full_url, req.headers.get("Authorization"), json.loads(req.data)
        return R(json.dumps({"choices": [{"message": {"content": "hi"}}], "usage": {"total_tokens": 7}}).encode())

    monkeypatch.setattr(kl.urllib.request, "urlopen", fake)
    assert kl.chat([{"role": "user", "content": "x"}]) == ("hi", 7)
    assert seen["url"] == "http://relay.invalid/v1/chat/completions"
    assert seen["auth"] == "Bearer k-test" and seen["body"]["model"] == "m1"
