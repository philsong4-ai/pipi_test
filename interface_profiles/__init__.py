"""interface_profiles: target_api 相关 prompt 与数据结构的 JSON 配置层。

公开接口：
- load_profile(target_api) → dict  加载 profile JSON
- list_profiles() → list[str]      枚举可用 target_api
- reload_profile(target_api)       清缓存重读
- load_persona_presets(target_api) → dict  动态 import persona_presets/<ta>.py

未知 target_api 一律回退 pipi，不抛异常。
"""
from ._loader import load_profile, list_profiles, reload_profile, load_persona_presets

__all__ = ["load_profile", "list_profiles", "reload_profile", "load_persona_presets"]
