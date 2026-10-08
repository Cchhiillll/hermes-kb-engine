"""kb-recall 插件外壳：只负责挂钩子，并在 logic.py 改动后自动重新加载（改逻辑不用重启网关）。
逻辑见 logic.py：每轮对话前附上现状页（问现状时）和知识库里最相关的几页。出错一律静默跳过，不影响对话。"""
import importlib.util, logging, os

log = logging.getLogger("kb-recall")
_LOGIC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logic.py")
_mod, _mtime = None, 0.0


def _logic():
    global _mod, _mtime
    m = os.path.getmtime(_LOGIC)
    if _mod is None or m != _mtime:
        spec = importlib.util.spec_from_file_location("kb_recall_logic", _LOGIC)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        _mod, _mtime = mod, m
        log.info("kb-recall 已加载 logic.py")
    return _mod


def recall(**kw):
    try:
        return _logic().recall(**kw)
    except Exception as e:
        log.info("kb-recall 跳过（%s）", str(e)[:80])
        return None


def register(ctx):
    ctx.register_hook("pre_llm_call", recall)
