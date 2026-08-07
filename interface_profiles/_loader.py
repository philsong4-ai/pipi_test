"""Profile 配置化加载器。

按 target_api 加载 interface_profiles/<target_api>.json，模块级 dict 缓存。
未知 target_api 回退 pipi，避免线上炸。
"""
import json
import os
import importlib
from typing import Dict, List

_PROFILE_DIR = os.path.dirname(os.path.abspath(__file__))
_CACHE: Dict[str, Dict] = {}


def load_profile(target_api: str = "pipi") -> Dict:
    """按 target_api 加载 profile JSON。

    模块级缓存（进程生命周期内只读一次磁盘）。
    未知 target_api 或文件缺失回退 pipi，打 warning 不抛异常。
    """
    ta = (target_api or "pipi").lower()
    if ta not in _CACHE:
        path = os.path.join(_PROFILE_DIR, f"{ta}.json")
        if not os.path.exists(path):
            print(f"[PROFILE] {ta} not found, fallback to pipi", flush=True)
            ta = "pipi"
            path = os.path.join(_PROFILE_DIR, "pipi.json")
        with open(path, "r", encoding="utf-8") as f:
            _CACHE[ta] = json.load(f)
    return _CACHE[ta]


def list_profiles() -> List[str]:
    """枚举所有可用 profile code，供前端选择 target_api 用。"""
    if not os.path.isdir(_PROFILE_DIR):
        return ["pipi"]
    return sorted(
        f[:-5] for f in os.listdir(_PROFILE_DIR)
        if f.endswith(".json") and f != "schema.md"
    )


def reload_profile(target_api: str = "pipi") -> Dict:
    """强制重新读磁盘（开发/调试用，生产不必调）。"""
    _CACHE.pop(target_api, None)
    return load_profile(target_api)


def load_persona_presets(target_api: str = "pipi") -> Dict:
    """按 target_api 动态 import persona_presets/<target_api>.py。

    ImportError 时回退 pipi 模块。返回模块的 PERSONA_TEMPLATES 字典。
    保留 lambda 结构以支持随机化字段。
    """
    ta = (target_api or "pipi").lower()
    try:
        mod = importlib.import_module(f"interface_profiles.persona_presets.{ta}")
    except ImportError:
        print(f"[PROFILE] persona_presets.{ta} not found, fallback to pipi", flush=True)
        mod = importlib.import_module("interface_profiles.persona_presets.pipi")
    return mod.PERSONA_TEMPLATES
