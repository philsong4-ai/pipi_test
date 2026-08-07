"""
同步画像 md 文件到数据库
python3 sync_personas.py [--user-id N]
默认 --user-id 1（admin）。
"""
import sqlite3
import os
import re
import sys

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB = os.path.join(BASE_DIR, "test.db")
MD_DIR = BASE_DIR

PERSONA_FILES = {
    "xiaojuzi": "小橘子.md",
    "zhishi": "芝士.md",
    "qingcheng": "青橙.md",
}

SIMPLE_KEYS = {
    "角色名": "name",
    "一句话": "nickname",
    "姓名": "real_name",
    "性别": "gender",
    "年龄": "age",
    "城市": "city",
    "职业": "occupation",
    "教育背景": "education",
    "家庭/婚姻": "family_status",
    "月收入/消费": "income_range",
    "消费习惯": "spending_desc",
    "常用设备": "devices",
    "主要使用场景": "usage_scenes",
    "核心目标": "core_goal",
    "短期诉求": "short_goal",
    "长期诉求": "long_goal",
    "主要痛点": "pain_points",
    "关键约束": "constraints",
    "风险偏好": "risk_profile",
    "兴趣标签": "interests",
    "语言风格": "language_style",
    "典型对话样本": "sample_dialog",
    "信息获取方式": "info_sources",
    "决策方式": "decision_style",
    "关系推进节奏": "relation_pace",
    "场景偏好": "scene_pref",
    "对皮皮的期待TOP3": "top_expectations",
    "可能踩雷的点": "minefields",
    "最适合测试的维度": "test_dimensions",
    "对话注入策略": "inject_strategy",
    "对比配对": "compare_with",
    "关系阶段模拟": "relation_stages",
}

def parse_md(filepath):
    """解析 md 文件的表格，返回 {字段: 内容} 字典"""
    data = {}
    with open(filepath, "r", encoding="utf-8") as f:
        for line in f:
            m = re.match(r'\|\s*\*\*([^*]+)\*\*\s*\|\s*(.*?)\s*\|', line)
            if m:
                key = m.group(1).strip()
                value = m.group(2).strip()
                if key in SIMPLE_KEYS:
                    db_key = SIMPLE_KEYS[key]
                    if db_key == "age":
                        nums = re.findall(r'\d+', value)
                        value = int(nums[0]) if nums else None
                    data[db_key] = value
    return data

def sync(user_id: int = 1):
    conn = sqlite3.connect(DB)
    cur = conn.cursor()

    fields = [
        "id", "name", "nickname", "real_name", "gender", "age",
        "city", "occupation", "education", "family_status", "income_range",
        "device_id", "spending_style", "spending_desc", "devices", "usage_scenes",
        "core_goal", "short_goal", "long_goal", "pain_points", "constraints", "risk_profile",
        "interests", "language_style", "sample_dialog", "info_sources", "decision_style",
        "relation_pace", "scene_pref", "top_expectations", "minefields",
        "test_dimensions", "inject_strategy", "compare_with", "relation_stages", "user_id",
    ]

    placeholders = ", ".join([f":{f}" for f in fields])
    col_names = ", ".join(fields)

    for pid, filename in PERSONA_FILES.items():
        filepath = os.path.join(MD_DIR, filename)
        if not os.path.exists(filepath):
            print(f"  ⚠️ 文件不存在: {filename}")
            continue

        data = parse_md(filepath)
        if not data:
            print(f"  ❌ 未能解析: {filename}")
            continue

        device_map = {
            "xiaojuzi": "TEST_DEV_HUARONG_xiaojuzi",
            "zhishi": "TEST_DEV_HUARONG_zhishi",
            "qingcheng": "TEST_DEV_HUARONG_qingcheng",
        }
        data["id"] = pid
        data["device_id"] = device_map.get(pid, "")

        raw_name = data.get("name", pid)
        clean_name = raw_name.split("（")[0].split("(")[0].strip()
        data["name"] = clean_name

        style_map = {"小橘子": "冲动型", "芝士": "悦己型", "青橙": "品质型"}
        data["spending_style"] = style_map.get(clean_name, "")
        data["user_id"] = user_id

        sql = f"INSERT OR REPLACE INTO personas ({col_names}) VALUES ({placeholders})"
        cur.execute(sql, data)
        print(f"  ✅ {pid} ({data.get('name', '?')})")

    conn.commit()
    conn.close()
    print(f"\n🎉 同步完成")

if __name__ == "__main__":
    uid = 1
    if "--user-id" in sys.argv:
        idx = sys.argv.index("--user-id")
        if idx + 1 < len(sys.argv):
            try:
                uid = int(sys.argv[idx + 1])
            except ValueError:
                print(f"⚠️ 无效的 --user-id 参数：{sys.argv[idx + 1]}，使用默认 1")
    print(f"🔄 同步画像到 user_id={uid}")
    sync(uid)
