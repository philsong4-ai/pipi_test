#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
清理已遗忘的事实记忆
定时任务：每天凌晨 3 点运行
crontab: 0 3 * * * cd /opt/pipi-test/web && python3 cleanup_forgotten_facts.py >> /tmp/cleanup_facts.log 2>&1
"""
import os
import sys
from datetime import datetime

# MySQL 配置
MYSQL_CONFIG = {
    "host": os.environ.get("MYSQL_HOST", "localhost"),
    "user": os.environ.get("MYSQL_USER", "pipi"),
    "password": os.environ.get("MYSQL_PASSWORD", "<DB_PASSWORD>"),
    "database": os.environ.get("MYSQL_DATABASE", "pipi_test"),
    "charset": "utf8mb4",
}

# 清理阈值（比正常遗忘阈值 0.1 更低，保守一点）
CLEANUP_THRESHOLD = 0.05


def get_db_connection():
    import pymysql
    return pymysql.connect(**MYSQL_CONFIG, cursorclass=pymysql.cursors.DictCursor)


def calculate_weight(created_at, decay_rate):
    """计算当前权重"""
    if isinstance(created_at, str):
        try:
            created_at = datetime.strptime(created_at[:19], "%Y-%m-%d %H:%M:%S")
        except:
            return 1.0

    days_passed = (datetime.now() - created_at).days
    if days_passed <= 0:
        return 1.0

    return float(decay_rate) ** days_passed


def cleanup_forgotten_facts():
    """清理权重低于阈值的 event/temporary 类型事实"""
    conn = get_db_connection()
    cursor = conn.cursor()

    print(f"[{datetime.now()}] 开始清理已遗忘事实...")

    # 查询所有活跃的 event/temporary 类型事实
    cursor.execute("""
        SELECT uf.id, uf.persona_id, uf.category, uf.fact_key, uf.fact_value,
               uf.fact_type, uf.memory_level, uf.created_at,
               COALESCE(mdr.decay_rate, 0.95) as decay_rate
        FROM user_facts uf
        LEFT JOIN memory_decay_rules mdr ON uf.memory_level = mdr.level
        WHERE uf.is_active = 1
          AND uf.fact_type IN ('event', 'temporary')
    """)

    facts = cursor.fetchall()
    print(f"  找到 {len(facts)} 条 event/temporary 类型事实")

    to_deactivate = []
    for f in facts:
        weight = calculate_weight(f["created_at"], f["decay_rate"])
        if weight < CLEANUP_THRESHOLD:
            to_deactivate.append({
                "id": f["id"],
                "persona_id": f["persona_id"],
                "category": f["category"],
                "fact_key": f["fact_key"],
                "fact_value": f["fact_value"][:50],
                "weight": round(weight, 4)
            })

    if not to_deactivate:
        print(f"  没有需要清理的事实")
        conn.close()
        return 0

    print(f"  准备清理 {len(to_deactivate)} 条已遗忘事实:")
    for item in to_deactivate[:10]:  # 只打印前10条
        print(f"    - [{item['persona_id']}] {item['category']}.{item['fact_key']}: {item['fact_value']} (weight={item['weight']})")
    if len(to_deactivate) > 10:
        print(f"    ... 还有 {len(to_deactivate) - 10} 条")

    # 批量更新
    ids = [item["id"] for item in to_deactivate]
    placeholders = ",".join(["%s"] * len(ids))
    cursor.execute(f"UPDATE user_facts SET is_active = 0 WHERE id IN ({placeholders})", ids)
    conn.commit()

    print(f"  已清理 {len(to_deactivate)} 条事实")
    conn.close()
    return len(to_deactivate)


if __name__ == "__main__":
    try:
        count = cleanup_forgotten_facts()
        print(f"[{datetime.now()}] 清理完成，共处理 {count} 条")
    except Exception as e:
        print(f"[{datetime.now()}] 清理失败: {e}")
        sys.exit(1)
