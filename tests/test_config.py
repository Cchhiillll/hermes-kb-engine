import os


def test_defaults_follow_home(kbenv):
    cfg = kbenv.load("kb_config").load()
    assert cfg.brain_dir == str(kbenv.brain)
    assert cfg.db == str(kbenv.db)
    assert cfg.wiki_dir == str(kbenv.wiki)
    assert cfg.project_root == ""
    assert cfg.local_prefix == "tp" and cfg.sync_prefix == "mac"


def test_env_overrides_and_legacy_names(kbenv):
    kbenv.mp.setenv("KB_BRAIN_DIR", str(kbenv.tmp / "b2"))
    kbenv.mp.setenv("KB_DB_PATH", str(kbenv.tmp / "legacy.sqlite"))
    kbenv.mp.setenv("WIKI_DIR", str(kbenv.tmp / "w"))
    cfg = kbenv.load("kb_config").load()
    assert cfg.raw_dir == str(kbenv.tmp / "b2" / "raw" / "conversations")
    assert cfg.db == str(kbenv.tmp / "legacy.sqlite")
    assert cfg.wiki_dir == str(kbenv.tmp / "w")
    kbenv.mp.setenv("KB_DB", str(kbenv.tmp / "new.sqlite"))
    assert kbenv.load("kb_config").load().db == str(kbenv.tmp / "new.sqlite")


def test_toml_file_and_secrets_ignored(kbenv):
    toml = kbenv.tmp / "c.toml"
    toml.write_text('brain_dir = "~/bb"\nproject_root = "/work/AI Native"\napi_key = "should-not-load"\n'
                    '[paths]\nsync_dir = "~/syncdir"\n', encoding="utf-8")
    kbenv.mp.setenv("KB_CONFIG", str(toml))
    kc = kbenv.load("kb_config")
    if kc.tomllib is None:
        return
    cfg = kc.load()
    assert cfg.brain_dir == str(kbenv.home / "bb")
    assert cfg.project_root == "/work/AI Native"
    assert cfg.sync_dir == str(kbenv.home / "syncdir")
    assert "api_key" not in kc._file_values()


def test_secret_from_env_or_hermes_env(kbenv):
    kc = kbenv.load("kb_config")
    assert kc.secret("OPENAI_API_KEY") == ""
    kbenv.write(".hermes/.env", "# c\nexport OPENAI_API_KEY='abc'\nOPENAI_BASE_URL=http://x/v1\n")
    assert kc.secret("OPENAI_API_KEY") == "abc"
    assert kc.hermes_env()["OPENAI_BASE_URL"] == "http://x/v1"
    kbenv.mp.setenv("OPENAI_API_KEY", "envwins")
    assert kc.secret("OPENAI_API_KEY") == "envwins"


def test_non_conversation_sql(kbenv):
    kc = kbenv.load("kb_config")
    assert "'page'" in kc.non_conversation_sql()
    assert kc.non_conversation_sql("k.src").startswith("k.src not in")
