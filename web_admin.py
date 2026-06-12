#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
皮皮虾侦探 - Flask 版本
启动: gunicorn -w 2 -b 0.0.0.0:8080 web_admin:app
v20260519b: 评测使用 qwen3.6-plus 模型
"""
import json
import os
import sys
import random
import threading
import datetime

# 给 print() 加时间戳
import builtins
_print = builtins.print

def _tsprint(*args, **kwargs):
    ts = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    _print(f"[{ts}]", *args, **kwargs)

builtins.print = _tsprint

from flask import Flask, request, jsonify, send_file

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), os.pardir))
import pipi_api

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
HTML_PATH = os.path.join(BASE_DIR, "index.html")

# ─── 数据库配置 ─────────────────────────────────
# MySQL 配置（默认启用）
USE_MYSQL = os.environ.get("USE_MYSQL", "1") == "1"
MYSQL_CONFIG = {
    "host": os.environ.get("MYSQL_HOST", "localhost"),
    "user": os.environ.get("MYSQL_USER", "pipi"),
    "password": os.environ.get("MYSQL_PASSWORD", "<DB_PASSWORD>"),
    "database": os.environ.get("MYSQL_DATABASE", "pipi_test"),
    "charset": "utf8mb4",
}
# SQLite 配置（备用）
SQLITE_DB = os.path.join(os.path.dirname(BASE_DIR), "test.db")

def get_db_connection():
    """获取数据库连接，支持 MySQL 和 SQLite"""
    if USE_MYSQL:
        import pymysql
        conn = pymysql.connect(**MYSQL_CONFIG, cursorclass=pymysql.cursors.DictCursor)
        return conn
    else:
        import sqlite3
        conn = sqlite3.connect(SQLITE_DB)
        conn.row_factory = sqlite3.Row
        return conn

def execute_query(conn, sql, params=None, fetch_one=False, fetch_all=False):
    """统一执行查询，兼容 MySQL 和 SQLite"""
    params = params or ()
    if USE_MYSQL:
        # MySQL: 使用 %s 占位符
        sql = sql.replace("?", "%s")
        # MySQL 不支持 PRAGMA
        if "PRAGMA" in sql:
            return []
        cursor = conn.cursor()
        cursor.execute(sql, params)
        if fetch_one:
            return cursor.fetchone()
        elif fetch_all:
            return cursor.fetchall()
        return cursor
    else:
        # SQLite: 使用 ? 占位符
        if fetch_one:
            return conn.execute(sql, params).fetchone()
        elif fetch_all:
            return conn.execute(sql, params).fetchall()
        return conn.execute(sql, params)

def get_lastrowid(cursor):
    """获取最后插入的 ID"""
    if USE_MYSQL:
        return cursor.lastrowid
    else:
        return cursor.lastrowid

def row_to_dict(row):
    """将数据库行转换为字典，处理 MySQL Decimal 类型"""
    if row is None:
        return None
    if USE_MYSQL:
        from decimal import Decimal
        d = dict(row) if row else None
        if d:
            for k, v in d.items():
                if isinstance(v, Decimal):
                    d[k] = float(v)
        return d
    else:
        return dict(row) if row else None

app = Flask(__name__)


def get_api_url_by_code(api_code: str) -> str:
    """根据接口代码获取 API URL，找不到返回 None"""
    if not api_code:
        return None
    conn = get_db_connection()
    row = execute_query(conn,
        "SELECT base_url FROM api_endpoints WHERE code = %s AND is_active = 1" if USE_MYSQL else
        "SELECT base_url FROM api_endpoints WHERE code = ? AND is_active = 1",
        (api_code,), fetch_one=True)
    conn.close()
    if row:
        return row["base_url"] if isinstance(row, dict) else row[0]
    return None


def get_api_config_by_code(api_code: str):
    """根据接口代码获取 API URL、API Key 和自定义请求头，返回 (url, key, headers)
    auth_config JSON 格式: {"api_key": "xxx", "headers": {"X-Custom": "val"}}
    """
    if not api_code:
        return None, None, {}
    conn = get_db_connection()
    row = execute_query(conn,
        "SELECT base_url, auth_config FROM api_endpoints WHERE code = %s AND is_active = 1" if USE_MYSQL else
        "SELECT base_url, auth_config FROM api_endpoints WHERE code = ? AND is_active = 1",
        (api_code,), fetch_one=True)
    conn.close()
    if row:
        row = row_to_dict(row) if not isinstance(row, dict) else row
        base_url = row.get("base_url")
        auth_config = row.get("auth_config")
        api_key = None
        headers = {}
        if auth_config:
            try:
                config = json.loads(auth_config) if isinstance(auth_config, str) else auth_config
                api_key = config.get("api_key")
                headers = config.get("headers", {})
            except:
                pass
        return base_url, api_key, headers
    return None, None, {}


# ─── 数据库初始化 ─────────────────────────────────
_initialized = False

def _ensure_tables():
    """MySQL 表已在迁移时创建，此函数仅用于验证连接"""
    global _initialized
    if _initialized:
        return
    try:
        conn = get_db_connection()
        execute_query(conn, "SELECT 1 FROM personas LIMIT 1", fetch_one=True)

        # 检查并添加 persona_score 和 persona_reason 列（如果不存在）
        try:
            execute_query(conn, "SELECT persona_score FROM auto_evaluation LIMIT 1", fetch_one=True)
        except:
            try:
                execute_query(conn, "ALTER TABLE auto_evaluation ADD COLUMN persona_score INT")
                execute_query(conn, "ALTER TABLE auto_evaluation ADD COLUMN persona_reason TEXT")
                conn.commit()
                print("[STARTUP] Added persona_score and persona_reason columns", flush=True)
            except Exception as e:
                print(f"[STARTUP] Could not add persona columns: {e}", flush=True)

        # 服务启动时恢复被中断的任务：重新拉起 worker
        conn2 = get_db_connection()
        stalled = execute_query(conn2,
            "SELECT id FROM growth_tasks WHERE status = 'running'",
            fetch_all=True)
        if stalled:
            stalled_ids = [row["id"] for row in stalled]
            execute_query(conn2,
                "UPDATE growth_tasks SET status = 'pending', error_message = 'auto-recovered after service restart' WHERE status = 'running'")
            conn2.commit()
            print(f"[STARTUP] Recovered {len(stalled_ids)} stalled growth tasks: {stalled_ids}", flush=True)
            for tid in stalled_ids:
                threading.Thread(target=_growth_worker, args=(tid,), daemon=True).start()
            print(f"[STARTUP] Respawned {len(stalled_ids)} growth workers", flush=True)
        conn2.close()

        conn.close()
        _initialized = True
        print(f"[DB] Using {'MySQL' if USE_MYSQL else 'SQLite'}", flush=True)
    except Exception as e:
        print(f"[DB ERROR] {e}", flush=True)
        _initialized = True


# ─── 路由 ─────────────────────────────────────────

@app.before_request
def before_request():
    _ensure_tables()


@app.route("/")
def index():
    return send_file(HTML_PATH)


@app.route("/api/personas", methods=["GET"])
def get_personas():
    """获取用户画像列表，支持分页和搜索"""
    page = request.args.get("page", 1, type=int)
    per_page = request.args.get("per_page", 20, type=int)
    search = request.args.get("search", "").strip()

    per_page = min(per_page, 100)  # 限制最大每页数量
    offset = (page - 1) * per_page

    conn = get_db_connection()

    # 构建查询
    if search:
        # 搜索 id 或 name
        placeholder = "%s" if USE_MYSQL else "?"
        count_sql = f"SELECT COUNT(*) as total FROM personas WHERE id LIKE {placeholder} OR name LIKE {placeholder}"
        search_param = f"%{search}%"
        total_row = execute_query(conn, count_sql, (search_param, search_param), fetch_one=True)
        total = row_to_dict(total_row)["total"]

        data_sql = f"""
            SELECT * FROM personas
            WHERE id LIKE {placeholder} OR name LIKE {placeholder}
            ORDER BY created_at DESC, id DESC
            LIMIT {placeholder} OFFSET {placeholder}
        """
        rows = execute_query(conn, data_sql, (search_param, search_param, per_page, offset), fetch_all=True)
    else:
        count_sql = "SELECT COUNT(*) as total FROM personas"
        total_row = execute_query(conn, count_sql, fetch_one=True)
        total = row_to_dict(total_row)["total"]

        data_sql = "SELECT * FROM personas ORDER BY created_at DESC, id DESC LIMIT ? OFFSET ?"
        rows = execute_query(conn, data_sql, (per_page, offset), fetch_all=True)

    conn.close()

    return jsonify({
        "items": [row_to_dict(r) for r in rows],
        "total": total,
        "page": page,
        "per_page": per_page,
        "total_pages": (total + per_page - 1) // per_page
    })


@app.route("/api/personas/<pid>", methods=["GET"])
def get_persona(pid):
    conn = get_db_connection()
    row = execute_query(conn, "SELECT * FROM personas WHERE id=?", (pid,), fetch_one=True)
    conn.close()
    if row:
        return jsonify(row_to_dict(row))
    return jsonify({"error": "not found"}), 404


@app.route("/api/personas", methods=["POST"])
@app.route("/api/personas/<pid>", methods=["PUT"])
def upsert_persona(pid=None):
    """
    创建或更新用户画像

    请求参数 (JSON):
        id: str - 用户ID（POST时可选，PUT时从URL获取）
        name: str - 用户名称
        auto_fill: bool - 创建后是否自动填充缺失字段（可选，默认false）
        fill_speed: str - 自动填充速度 fast/normal/slow（可选，默认normal）
        ...其他 persona 字段
    """
    data = request.get_json() or {}
    if pid is None:
        pid = data.get("id")

    auto_fill = data.get("auto_fill", False)
    fill_speed = data.get("fill_speed", "normal")

    fields = [
        "id", "name", "nickname", "real_name", "gender", "age",
        "city", "occupation", "education", "family_status", "income_range",
        "device_id", "spending_style", "spending_desc", "devices", "usage_scenes",
        "core_goal", "short_goal", "long_goal", "pain_points", "constraints",
        "risk_profile", "interests", "language_style", "sample_dialog",
        "info_sources", "decision_style", "relation_pace", "scene_pref",
        "top_expectations", "minefields", "test_dimensions", "inject_strategy",
        "compare_with", "relation_stages", "target_api",
    ]

    # 如果没有 device_id，自动生成
    if not data.get("device_id"):
        import time as time_module
        data["device_id"] = f"TEST_DEV_{int(time_module.time())}_{random.randint(1000,9999)}"

    conn = get_db_connection()
    kv = {f: data.get(f, "") for f in fields}
    kv["id"] = pid
    values = [kv[f] for f in fields]
    cols = ", ".join(fields)
    if USE_MYSQL:
        ph = ", ".join(["%s"] * len(fields))
        update_clause = ", ".join([f"{f}=%s" for f in fields if f != "id"])
        sql = f"INSERT INTO personas ({cols}) VALUES ({ph}) ON DUPLICATE KEY UPDATE {update_clause}"
        execute_query(conn, sql.replace("?", "%s"), values + values[1:])
    else:
        ph = ", ".join(["?"] * len(fields))
        execute_query(conn, f"INSERT OR REPLACE INTO personas ({cols}) VALUES ({ph})", values)
    conn.commit()
    conn.close()

    result = {"ok": True, "persona_id": pid}

    # 如果开启自动填充，启动填充任务
    if auto_fill:
        messages = _generate_messages_for_missing_fields(pid)
        if messages:
            conn = get_db_connection()
            cur = execute_query(conn,
                "INSERT INTO growth_tasks (persona_id, speed, status, total_messages) VALUES (?,?,?,?)",
                (pid, fill_speed, "pending", len(messages)))
            task_id = get_lastrowid(cur)

            for idx, msg in enumerate(messages):
                execute_query(conn,
                    "INSERT INTO growth_progress (task_id, message_index, user_message, status) VALUES (?,?,?,?)",
                    (task_id, idx, msg, "pending"))

            conn.commit()
            conn.close()

            # 启动后台线程
            t = threading.Thread(target=_growth_worker, args=(task_id,), daemon=True)
            t.start()

            result["auto_fill"] = {
                "task_id": task_id,
                "total_messages": len(messages),
                "status": "started"
            }

    return jsonify(result)


@app.route("/api/personas/<pid>", methods=["DELETE"])
def delete_persona(pid):
    """删除用户及其所有关联数据"""
    conn = get_db_connection()

    # 先删除 growth_progress（依赖 growth_tasks）
    execute_query(conn, """
        DELETE FROM growth_progress WHERE task_id IN (
            SELECT id FROM growth_tasks WHERE persona_id=?
        )
    """, (pid,))

    # 删除关联数据
    execute_query(conn, "DELETE FROM growth_tasks WHERE persona_id=?", (pid,))
    execute_query(conn, "DELETE FROM chat_feedback WHERE message_id IN (SELECT id FROM chat_messages WHERE persona_id=?)", (pid,))
    execute_query(conn, "DELETE FROM chat_messages WHERE persona_id=?", (pid,))
    execute_query(conn, "DELETE FROM user_facts WHERE persona_id=?", (pid,))
    execute_query(conn, "DELETE FROM personas WHERE id=?", (pid,))

    conn.commit()
    conn.close()
    return jsonify({"ok": True})


@app.route("/api/personas/batch", methods=["POST"])
def batch_create_personas():
    """
    统一批量创建用户 API（整合 batch_create 和 multi_create）

    请求参数 (JSON):
        模式1 - 单配置批量创建（原 batch_create）:
            count: int - 创建数量（1-20）
            prefix: str - 用户名前缀（默认"用户"）
            speed: str - 速度（fast/normal/slow）
            template_id: str - 可选，使用模板预填充
            categories: list - 可选，只填充特定类别

        模式2 - 多配置分别创建（原 multi_create）:
            configs: list - 用户配置列表
                [{"name": "小明", "speed": "normal", "template_id": "young_worker"}, ...]

    返回:
        {"created_count": 3, "personas": [{"persona_id": "...", "task_id": 1, "name": "..."}, ...]}
    """
    import time as time_module

    data = request.get_json() or {}
    configs = data.get("configs")
    timestamp = int(time_module.time())
    results = []

    # 模式2：多配置分别创建
    if configs:
        if not isinstance(configs, list) or len(configs) > 20:
            return jsonify({"error": "configs must be a list with max 20 items"}), 400
    else:
        # 模式1：单配置批量创建
        count = data.get("count", 1)
        prefix = data.get("prefix", "用户")
        speed = data.get("speed", "normal")
        template_id = data.get("template_id")
        categories = data.get("categories")

        if not isinstance(count, int) or count < 1 or count > 20:
            return jsonify({"error": "count must be 1-20"}), 400

        # 转换为 configs 格式统一处理
        configs = [
            {"name": f"{prefix}{i+1}", "speed": speed, "template_id": template_id, "categories": categories}
            for i in range(count)
        ]

    conn = get_db_connection()

    for i, cfg in enumerate(configs):
        name = cfg.get("name", f"用户{i+1}")
        speed = cfg.get("speed", "normal")
        template_id = cfg.get("template_id")
        categories = cfg.get("categories")
        target_api = cfg.get("target_api", "pipi")

        # 生成唯一 ID
        persona_id = f"auto_{timestamp}_{i+1}"
        device_id = f"TEST_DEV_HUARONG_{timestamp}_{i+1}_{random.randint(1000,9999)}"

        # 创建最基本的用户画像
        fields = ["id", "name", "device_id", "target_api"]
        values = [persona_id, name, device_id, target_api]

        if USE_MYSQL:
            ph = ", ".join(["%s"] * len(fields))
            execute_query(conn, f"INSERT INTO personas ({', '.join(fields)}) VALUES ({ph})", tuple(values))
        else:
            ph = ", ".join(["?"] * len(fields))
            execute_query(conn, f"INSERT INTO personas ({', '.join(fields)}) VALUES ({ph})", tuple(values))

        conn.commit()

        # 生成基于缺失字段的消息
        messages = _generate_messages_for_missing_fields(persona_id, categories=categories, template_id=template_id)

        if not messages:
            results.append({
                "persona_id": persona_id,
                "task_id": None,
                "name": name,
                "device_id": device_id,
                "message_count": 0
            })
            continue

        # 创建成长任务
        cur = execute_query(conn,
            "INSERT INTO growth_tasks (persona_id, speed, status, total_messages) VALUES (?,?,?,?)",
            (persona_id, speed, "pending", len(messages)))
        task_id = get_lastrowid(cur)

        for idx, msg in enumerate(messages):
            if isinstance(msg, str) and msg.strip():
                execute_query(conn,
                    "INSERT INTO growth_progress (task_id, message_index, user_message, status) VALUES (?,?,?,?)",
                    (task_id, idx, msg.strip(), "pending"))

        conn.commit()

        results.append({
            "persona_id": persona_id,
            "task_id": task_id,
            "name": name,
            "device_id": device_id,
            "speed": speed,
            "message_count": len(messages)
        })

        # 启动后台线程
        t = threading.Thread(target=_growth_worker, args=(task_id,), daemon=True)
        t.start()

    conn.close()

    return jsonify({
        "created_count": len(results),
        "personas": results
    })


@app.route("/api/chat_history/<pid>", methods=["GET"])
def get_chat_history(pid):
    conn = get_db_connection()
    rows = execute_query(conn,
        "SELECT id, role, user_name as user, text, created_at FROM chat_messages "
        "WHERE persona_id=? ORDER BY id DESC LIMIT 100", (pid,), fetch_all=True)
    messages = [row_to_dict(r) for r in reversed(list(rows))]
    msg_ids = [m["id"] for m in messages]
    if msg_ids:
        placeholders = ",".join(["%s" if USE_MYSQL else "?"] * len(msg_ids))
        feedback_rows = execute_query(conn,
            f"SELECT message_id, feedback_type, comment FROM chat_feedback WHERE message_id IN ({placeholders})",
            tuple(msg_ids), fetch_all=True)
        feedback_map = {fr["message_id"]: {"type": fr["feedback_type"], "comment": fr["comment"]} for fr in feedback_rows}
        for m in messages:
            if m["id"] in feedback_map:
                m["feedback"] = feedback_map[m["id"]]
    conn.close()
    return jsonify(messages)


@app.route("/api/chat_history/<pid>", methods=["DELETE"])
def delete_chat_history(pid):
    conn = get_db_connection()
    execute_query(conn, "DELETE FROM chat_messages WHERE persona_id=?", (pid,))
    conn.commit()
    conn.close()
    return jsonify({"ok": True})


@app.route("/api/chat", methods=["POST"])
def chat():
    data = request.get_json() or {}
    persona_id = data.get("persona_id")
    message = data.get("message", "")
    result = call_api(persona_id, message)
    return jsonify(result)


@app.route("/api/test/chat", methods=["POST"])
def test_chat():
    """
    自动化测试聊天接口 - 单条消息

    请求参数 (JSON):
        persona_id: str - 用户ID（必填，需先创建用户画像）
        message: str - 用户消息（必填）
        extract_facts: bool - 是否提取事实（可选，默认 true）

    返回:
        {
            "persona_id": "xiaojuzi",
            "user_message": "你好",
            "reply": "你好呀！",
            "reply_id": 123,
            "facts_extracted": [...],
            "error": null
        }

    示例:
        curl -X POST http://<SERVER_IP>:8080/api/test/chat \\
            -H "Content-Type: application/json" \\
            -d '{"persona_id": "xiaojuzi", "message": "我今天好开心"}'
    """
    data = request.get_json() or {}
    persona_id = data.get("persona_id", "").strip()
    message = data.get("message", "").strip()
    extract_facts = data.get("extract_facts", True)
    override_target_api = data.get("target_api")  # 可选：覆盖 persona 的 target_api

    if not persona_id:
        return jsonify({"error": "persona_id is required"}), 400
    if not message:
        return jsonify({"error": "message is required"}), 400

    # 获取用户画像
    conn = get_db_connection()
    row = execute_query(conn, "SELECT * FROM personas WHERE id=?", (persona_id,), fetch_one=True)
    conn.close()

    if not row:
        return jsonify({"error": f"persona_id '{persona_id}' not found"}), 404

    persona_data = row_to_dict(row)
    device_id = persona_data.get("device_id", persona_id)
    name = persona_data.get("name", persona_id)
    target_api = override_target_api or persona_data.get("target_api", "pipi")

    # 获取目标接口配置
    api_url, api_key, api_headers = get_api_config_by_code(target_api)

    # 保存用户消息
    save_chat_msg(persona_id, "user", name, message)

    # 构建请求并调用目标接口
    import time as _time
    system_prompt = pipi_api.build_system_prompt(persona_data, device_id)
    api_messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": message},
    ]
    result = pipi_api.call_pipi_stream(api_messages, device_id=device_id, api_url=api_url, api_key=api_key, extra_headers=api_headers)
    _ttfb = result.get("ttfb_ms")
    _total = result.get("response_time_ms")
    print(f"[TIMING] {persona_id} SE-web: TTFB={_ttfb}ms total={_total}ms", flush=True)

    reply_text = result.get("full_text", "")
    reply_id = None
    facts_extracted = []

    if reply_text:
        reply_id = save_chat_msg(persona_id, "pipi", "秋秋", reply_text)

        # 事实提取
        if extract_facts:
            try:
                conn = get_db_connection()
                fact_rows = execute_query(conn,
                    "SELECT id, category, fact_key, entity_name, fact_value FROM user_facts WHERE persona_id=? AND is_active=1",
                    (persona_id,), fetch_all=True)
                existing_facts = [row_to_dict(r) for r in fact_rows]
                history_rows = execute_query(conn,
                    "SELECT role, text FROM chat_messages WHERE persona_id=? ORDER BY id DESC LIMIT 10",
                    (persona_id,), fetch_all=True)
                conn.close()

                chat_history_for_extract = []
                for row in reversed(history_rows):
                    prefix = "用户: " if row["role"] == "user" else "秋秋: "
                    chat_history_for_extract.append(prefix + row["text"])

                _t_fact = _time.time()
                llm_config = get_llm_config()
                facts = pipi_api.extract_facts_from_message(
                    message, persona_data, existing_facts, chat_history=chat_history_for_extract, **llm_config["fact_extract"])
                _fact_elapsed = _time.time() - _t_fact
                print(f"[TIMING] {persona_id} 事实提取: {_fact_elapsed:.1f}s", flush=True)

                if facts:
                    for f in facts:
                        related_ids = f.pop("related_to", [])
                        if related_ids:
                            f["related_fact_ids"] = ",".join(str(x) for x in related_ids)
                        f["persona_id"] = persona_id
                        f["confidence"] = "implicit"
                        f["source_session"] = persona_id
                        f["source_text"] = message
                        _create_fact(f)
                        facts_extracted.append({
                            "category": f.get("category"),
                            "fact_key": f.get("fact_key"),
                            "fact_value": f.get("fact_value")
                        })
            except Exception as e:
                print(f"[TEST CHAT FACT ERROR] {persona_id}: {e}", flush=True)

    return jsonify({
        "persona_id": persona_id,
        "user_message": message,
        "reply": reply_text,
        "reply_id": reply_id,
        "facts_extracted": facts_extracted,
        "ttfb_ms": result.get("ttfb_ms"),
        "total_ms": result.get("response_time_ms"),
        "error": result.get("error")
    })


@app.route("/api/fact_options", methods=["GET"])
def get_fact_options():
    conn = get_db_connection()
    rows = execute_query(conn,
        "SELECT DISTINCT category, fact_key, fact_value FROM user_facts WHERE is_active=1 ORDER BY category, fact_key, fact_value",
        fetch_all=True)
    result = {}
    for r in rows:
        cat = r["category"]
        if cat not in result:
            result[cat] = {"keys": set(), "values": set()}
        result[cat]["keys"].add(r["fact_key"])
        result[cat]["values"].add(r["fact_value"])
    conn.close()
    out = {cat: {"keys": sorted(v["keys"]), "values": sorted(v["values"])} for cat, v in result.items()}
    return jsonify(out)


@app.route("/api/facts", methods=["GET"])
def get_all_facts():
    conn = get_db_connection()
    rows = execute_query(conn, "SELECT * FROM user_facts ORDER BY persona_id, is_active DESC, created_at DESC", fetch_all=True)
    result = [row_to_dict(r) for r in rows]
    conn.close()
    return jsonify(result)


@app.route("/api/facts_count", methods=["GET"])
def get_facts_count():
    conn = get_db_connection()
    rows = execute_query(conn,
        "SELECT persona_id, COUNT(*) as total, SUM(CASE WHEN is_active=1 THEN 1 ELSE 0 END) as active FROM user_facts GROUP BY persona_id",
        fetch_all=True)
    result = {r["persona_id"]: {"total": r["total"], "active": r["active"]} for r in rows}
    conn.close()
    return jsonify(result)


@app.route("/api/facts/persona/<pid>", methods=["GET"])
def get_facts_by_persona(pid):
    conn = get_db_connection()
    rows = execute_query(conn,
        "SELECT * FROM user_facts WHERE persona_id=? ORDER BY is_active DESC, created_at DESC", (pid,), fetch_all=True)
    result = [row_to_dict(r) for r in rows]
    conn.close()
    return jsonify(result)


@app.route("/api/facts/<fid>", methods=["GET"])
def get_fact(fid):
    conn = get_db_connection()
    row = execute_query(conn, "SELECT * FROM user_facts WHERE id=?", (fid,), fetch_one=True)
    conn.close()
    return jsonify(row_to_dict(row) if row else {})


@app.route("/api/facts", methods=["POST"])
def create_fact():
    data = request.get_json() or {}
    _create_fact(data)
    return jsonify({"ok": True})


@app.route("/api/facts/<fid>", methods=["PUT"])
def update_fact(fid):
    data = request.get_json() or {}
    conn = get_db_connection()
    execute_query(conn,
        "UPDATE user_facts SET category=?, fact_key=?, fact_value=?, confidence=?, occurred_at=?, emotion_tag=?, related_fact_ids=?, source_case=?, source_session=?, source_text=? WHERE id=?",
        (data.get("category",""), data.get("fact_key",""), data.get("fact_value",""),
         data.get("confidence","explicit"), data.get("occurred_at",""), data.get("emotion_tag",""),
         data.get("related_fact_ids",""), data.get("source_case",""), data.get("source_session",""),
         data.get("source_text",""), fid))
    conn.commit()
    conn.close()
    return jsonify({"ok": True})


@app.route("/api/facts/<fid>", methods=["DELETE"])
def delete_fact(fid):
    conn = get_db_connection()
    execute_query(conn, "UPDATE user_facts SET is_active=0 WHERE id=?", (fid,))
    conn.commit()
    conn.close()
    return jsonify({"ok": True})


@app.route("/api/feedback/<msg_id>", methods=["GET"])
def get_feedback(msg_id):
    conn = get_db_connection()
    row = execute_query(conn, "SELECT * FROM chat_feedback WHERE message_id=?", (msg_id,), fetch_one=True)
    conn.close()
    return jsonify(row_to_dict(row) if row else {})


@app.route("/api/feedback", methods=["POST"])
def save_feedback():
    data = request.get_json() or {}
    message_id = data.get("message_id")
    persona_id = data.get("persona_id", "")
    feedback_type = data.get("feedback_type", "")
    comment = data.get("comment")
    action = data.get("action", "")

    if not message_id or not feedback_type:
        return jsonify({"ok": False})

    conn = get_db_connection()
    existing = execute_query(conn,
        "SELECT feedback_type, comment FROM chat_feedback WHERE message_id=?", (message_id,), fetch_one=True)
    old_comment = existing["comment"] if existing else ""

    if action == "delete":
        comment = ""
    elif action == "edit":
        comment = comment or ""
    elif action == "append" and comment:
        comment = (old_comment + "\n" + comment) if old_comment else comment
    elif comment is None or not comment:
        comment = old_comment

    execute_query(conn, "DELETE FROM chat_feedback WHERE message_id=?", (message_id,))
    execute_query(conn,
        "INSERT INTO chat_feedback (message_id, persona_id, feedback_type, comment) VALUES (?,?,?,?)",
        (message_id, persona_id, feedback_type, comment))
    conn.commit()
    conn.close()
    return jsonify({"ok": True})


# ─── 遗忘机制配置 API ─────────────────────────────────────

@app.route("/api/memory/config", methods=["GET"])
def get_memory_config():
    """获取遗忘机制配置"""
    conn = get_db_connection()

    # 全局配置
    config_rows = execute_query(conn, "SELECT `key`, value, description FROM memory_config", fetch_all=True)
    config = {r["key"]: {"value": r["value"], "description": r["description"]} for r in config_rows}

    # 层级规则
    rules_rows = execute_query(conn, "SELECT * FROM memory_decay_rules ORDER BY decay_start_days", fetch_all=True)
    rules = [row_to_dict(r) for r in rules_rows]

    # 类别映射
    mapping_rows = execute_query(conn, "SELECT * FROM memory_category_mapping ORDER BY category", fetch_all=True)
    mapping = [row_to_dict(r) for r in mapping_rows]

    conn.close()
    return jsonify({"config": config, "rules": rules, "mapping": mapping})


@app.route("/api/memory/config", methods=["POST"])
def set_memory_config():
    """更新遗忘机制配置"""
    data = request.get_json() or {}
    conn = get_db_connection()

    # 更新全局配置
    if "config" in data:
        for key, value in data["config"].items():
            if USE_MYSQL:
                execute_query(conn, "UPDATE memory_config SET value = %s WHERE `key` = %s", (value, key))
            else:
                execute_query(conn, "UPDATE memory_config SET value = ? WHERE key = ?", (value, key))

    # 更新层级规则
    if "rules" in data:
        for rule in data["rules"]:
            execute_query(conn, """
                UPDATE memory_decay_rules
                SET decay_start_days = ?, decay_rate = ?, forget_threshold = ?
                WHERE level = ?
            """, (rule["decay_start_days"], rule["decay_rate"], rule["forget_threshold"], rule["level"]))

    # 更新类别映射
    if "mapping" in data:
        for m in data["mapping"]:
            if USE_MYSQL:
                execute_query(conn, """
                    INSERT INTO memory_category_mapping (category, memory_level, description)
                    VALUES (%s, %s, %s)
                    ON DUPLICATE KEY UPDATE memory_level = %s, description = %s
                """, (m["category"], m["memory_level"], m.get("description", ""),
                      m["memory_level"], m.get("description", "")))
            else:
                execute_query(conn, """
                    INSERT OR REPLACE INTO memory_category_mapping (category, memory_level, description)
                    VALUES (?, ?, ?)
                """, (m["category"], m["memory_level"], m.get("description", "")))

    conn.commit()
    conn.close()
    return jsonify({"ok": True})


@app.route("/api/memory/stats/<persona_id>", methods=["GET"])
def get_memory_stats(persona_id):
    """获取用户记忆统计（含遗忘状态）"""
    conn = get_db_connection()

    facts = execute_query(conn, """
        SELECT uf.id, uf.category, uf.fact_key, uf.entity_name, uf.fact_value, uf.memory_level,
               uf.weight, uf.created_at, uf.is_active,
               COALESCE(mdr.forget_threshold, 0.1) as forget_threshold
        FROM user_facts uf
        LEFT JOIN memory_decay_rules mdr ON uf.memory_level = mdr.level
        WHERE uf.persona_id = ?
        ORDER BY uf.is_active DESC, uf.created_at DESC
    """, (persona_id,), fetch_all=True)

    result = []
    for f in facts:
        current_weight = _calculate_fact_weight(row_to_dict(f))
        threshold = float(f["forget_threshold"]) if f["forget_threshold"] else 0.1
        is_forgotten = current_weight < threshold

        result.append({
            "id": f["id"],
            "category": f["category"],
            "fact_key": f["fact_key"],
            "fact_value": f["fact_value"],
            "memory_level": f["memory_level"],
            "original_weight": float(f["weight"]) if f["weight"] else 1.0,
            "current_weight": round(current_weight, 4),
            "forget_threshold": threshold,
            "is_active": bool(f["is_active"]),
            "is_forgotten": is_forgotten,
            "created_at": str(f["created_at"])
        })

    conn.close()

    # 统计
    stats = {
        "total": len(result),
        "active": sum(1 for r in result if r["is_active"]),
        "forgotten": sum(1 for r in result if r["is_forgotten"]),
        "by_level": {}
    }
    for r in result:
        level = r["memory_level"]
        if level not in stats["by_level"]:
            stats["by_level"][level] = {"total": 0, "forgotten": 0}
        stats["by_level"][level]["total"] += 1
        if r["is_forgotten"]:
            stats["by_level"][level]["forgotten"] += 1

    return jsonify({"facts": result, "stats": stats})


# ─── 自动评测 API ─────────────────────────────────────

@app.route("/api/eval/config", methods=["GET"])
def get_eval_config():
    conn = get_db_connection()
    row = execute_query(conn, "SELECT value FROM eval_config WHERE `key`='auto_eval_enabled'", fetch_one=True)
    conn.close()
    enabled = row["value"] == 'true' if row else False
    return jsonify({"auto_eval_enabled": enabled})


@app.route("/api/eval/config", methods=["POST"])
def set_eval_config():
    data = request.get_json() or {}
    enabled = data.get("auto_eval_enabled", False)
    conn = get_db_connection()
    if USE_MYSQL:
        execute_query(conn, "REPLACE INTO eval_config (`key`, value) VALUES ('auto_eval_enabled', %s)",
                     ('true' if enabled else 'false',))
    else:
        execute_query(conn, "INSERT OR REPLACE INTO eval_config (key, value) VALUES ('auto_eval_enabled', ?)",
                     ('true' if enabled else 'false',))
    conn.commit()
    conn.close()
    return jsonify({"ok": True, "auto_eval_enabled": enabled})


# ─── LLM 模型配置 API ───────────────────────────────

DEFAULT_LLM_CONFIG = {
    "case_gen":       {"model": "qwen3.6-plus",  "temperature": 0.3, "max_tokens": 8192, "timeout": 180},
    "case_regenerate": {"model": "qwen3.6-plus",  "temperature": 0.3, "max_tokens": 8192, "timeout": 180},
    "fact_extract":   {"model": "qwen3.6-plus",  "temperature": 0.3, "max_tokens": 8192, "timeout": 60},
    "eval_batch":     {"model": "qwen3.6-plus",  "temperature": 0.3, "max_tokens": 8192, "timeout": 90},
    "eval_case":      {"model": "qwen3.6-plus",  "temperature": 0.3, "max_tokens": 8192, "timeout": 60},
    "eval_realtime":  {"model": "qwen3.6-plus",  "temperature": 0.3, "max_tokens": 8192, "timeout": 90},
    "case_review":    {"model": "deepseek-v4-pro","temperature": 0.3, "max_tokens": 8192, "timeout": 120},
}

# 兼容用：保留旧名称引用，旧版存的是纯字符串 model 名
DEFAULT_LLM_MODELS = {k: v["model"] for k, v in DEFAULT_LLM_CONFIG.items()}


def get_llm_config(key=None):
    """读取 LLM 配置，返回完整 dict 或单个方法的 dict。
    兼容旧存储格式（值仅为字符串 model 名）。
    """
    conn = get_db_connection()
    row = execute_query(conn, "SELECT value FROM eval_config WHERE `key`='llm_models'", fetch_one=True)
    conn.close()
    config = {}
    if row and row["value"]:
        try:
            raw = json.loads(row["value"]) if isinstance(row["value"], str) else row["value"]
        except:
            raw = {}
        # 兼容旧格式：值可能是字符串，补齐为完整 dict
        for k, v in DEFAULT_LLM_CONFIG.items():
            stored = raw.get(k)
            if stored is None:
                config[k] = dict(v)
            elif isinstance(stored, str):
                # 旧格式：只有模型名字符串
                config[k] = dict(v)
                config[k]["model"] = stored
            elif isinstance(stored, dict):
                config[k] = dict(v)
                config[k].update({kk: stored[kk] for kk in ("model", "temperature", "max_tokens", "timeout") if kk in stored})
            else:
                config[k] = dict(v)
    else:
        config = {k: dict(v) for k, v in DEFAULT_LLM_CONFIG.items()}
    if key:
        return config.get(key, dict(DEFAULT_LLM_CONFIG[key]))
    return config


@app.route("/api/llm/config", methods=["GET"])
def api_get_llm_config():
    return jsonify(get_llm_config())


@app.route("/api/llm/config", methods=["POST"])
def api_set_llm_config():
    data = request.get_json() or {}
    conn = get_db_connection()
    config = {}
    for k in DEFAULT_LLM_CONFIG:
        entry = data.get(k)
        if isinstance(entry, dict):
            config[k] = {
                "model": entry.get("model", DEFAULT_LLM_CONFIG[k]["model"]).strip() or DEFAULT_LLM_CONFIG[k]["model"],
                "temperature": float(entry.get("temperature", DEFAULT_LLM_CONFIG[k]["temperature"])),
                "max_tokens": int(entry.get("max_tokens", DEFAULT_LLM_CONFIG[k]["max_tokens"])),
                "timeout": int(entry.get("timeout", DEFAULT_LLM_CONFIG[k]["timeout"])),
            }
        else:
            config[k] = dict(DEFAULT_LLM_CONFIG[k])
    if USE_MYSQL:
        execute_query(conn, "REPLACE INTO eval_config (`key`, value) VALUES ('llm_models', %s)",
                     (json.dumps(config, ensure_ascii=False),))
    else:
        execute_query(conn, "INSERT OR REPLACE INTO eval_config (key, value) VALUES ('llm_models', ?)",
                     (json.dumps(config, ensure_ascii=False),))
    conn.commit()
    conn.close()
    return jsonify({"ok": True, "config": config})


@app.route("/api/eval/<int:message_id>", methods=["GET"])
def get_evaluation(message_id):
    conn = get_db_connection()
    placeholder = "%s" if USE_MYSQL else "?"
    row = execute_query(conn, f"SELECT * FROM auto_evaluation WHERE message_id={placeholder}", (message_id,), fetch_one=True)
    conn.close()
    return jsonify(row_to_dict(row) if row else {})


@app.route("/api/eval/batch_get", methods=["GET"])
def batch_get_evaluations():
    """批量获取多条消息的评测数据"""
    ids_str = request.args.get("ids", "")
    if not ids_str:
        return jsonify({})
    try:
        ids = [int(i.strip()) for i in ids_str.split(",") if i.strip()]
    except ValueError:
        return jsonify({})
    if not ids:
        return jsonify({})

    conn = get_db_connection()
    placeholders = ",".join(["%s" if USE_MYSQL else "?"] * len(ids))
    rows = execute_query(conn, f"SELECT * FROM auto_evaluation WHERE message_id IN ({placeholders})", tuple(ids), fetch_all=True)
    conn.close()

    result = {}
    for row in rows:
        result[row["message_id"]] = row_to_dict(row)
    return jsonify(result)


# ─── 评测统计 API ─────────────────────────────────

@app.route("/api/eval/stats/trend", methods=["GET"])
def eval_stats_trend():
    """评分趋势统计"""
    days = request.args.get("days", 30, type=int)
    persona_id = request.args.get("persona_id", "").strip()

    conn = get_db_connection()

    if persona_id:
        rows = execute_query(conn, """
            SELECT DATE(created_at) as date,
                   AVG(total_score) as total,
                   AVG(memory_score) as memory,
                   AVG(emotion_score) as emotion,
                   AVG(quality_score) as quality,
                   AVG(persona_score) as persona,
                   COUNT(*) as count
            FROM auto_evaluation
            WHERE created_at >= DATE_SUB(NOW(), INTERVAL ? DAY)
              AND persona_id = ?
            GROUP BY DATE(created_at)
            ORDER BY date
        """, (days, persona_id), fetch_all=True)
    else:
        rows = execute_query(conn, """
            SELECT DATE(created_at) as date,
                   AVG(total_score) as total,
                   AVG(memory_score) as memory,
                   AVG(emotion_score) as emotion,
                   AVG(quality_score) as quality,
                   AVG(persona_score) as persona,
                   COUNT(*) as count
            FROM auto_evaluation
            WHERE created_at >= DATE_SUB(NOW(), INTERVAL ? DAY)
            GROUP BY DATE(created_at)
            ORDER BY date
        """, (days,), fetch_all=True)

    conn.close()

    dates, total, memory, emotion, quality, persona, counts = [], [], [], [], [], [], []
    for r in rows:
        r = row_to_dict(r)
        dates.append(str(r["date"])[5:] if r["date"] else "")  # MM-DD 格式
        total.append(round(float(r["total"] or 0), 1))
        memory.append(round(float(r["memory"] or 0), 1))
        emotion.append(round(float(r["emotion"] or 0), 1))
        quality.append(round(float(r["quality"] or 0), 1))
        persona.append(round(float(r["persona"] or 0), 1))
        counts.append(int(r["count"] or 0))

    return jsonify({
        "dates": dates,
        "total": total,
        "memory": memory,
        "emotion": emotion,
        "quality": quality,
        "persona": persona,
        "counts": counts
    })


@app.route("/api/eval/stats/distribution", methods=["GET"])
def eval_stats_distribution():
    """各维度分数分布"""
    persona_id = request.args.get("persona_id", "").strip()

    conn = get_db_connection()

    def get_distribution(column):
        if persona_id:
            rows = execute_query(conn, f"""
                SELECT
                    SUM(CASE WHEN {column} <= 4 THEN 1 ELSE 0 END) as low,
                    SUM(CASE WHEN {column} >= 5 AND {column} <= 6 THEN 1 ELSE 0 END) as medium,
                    SUM(CASE WHEN {column} >= 7 AND {column} <= 8 THEN 1 ELSE 0 END) as high,
                    SUM(CASE WHEN {column} >= 9 THEN 1 ELSE 0 END) as excellent
                FROM auto_evaluation
                WHERE persona_id = ?
            """, (persona_id,), fetch_one=True)
        else:
            rows = execute_query(conn, f"""
                SELECT
                    SUM(CASE WHEN {column} <= 4 THEN 1 ELSE 0 END) as low,
                    SUM(CASE WHEN {column} >= 5 AND {column} <= 6 THEN 1 ELSE 0 END) as medium,
                    SUM(CASE WHEN {column} >= 7 AND {column} <= 8 THEN 1 ELSE 0 END) as high,
                    SUM(CASE WHEN {column} >= 9 THEN 1 ELSE 0 END) as excellent
                FROM auto_evaluation
            """, fetch_one=True)
        r = row_to_dict(rows)
        return {
            "low": int(r["low"] or 0),
            "medium": int(r["medium"] or 0),
            "high": int(r["high"] or 0),
            "excellent": int(r["excellent"] or 0)
        }

    result = {
        "memory": get_distribution("memory_score"),
        "emotion": get_distribution("emotion_score"),
        "quality": get_distribution("quality_score"),
        "persona": get_distribution("persona_score")
    }

    conn.close()
    return jsonify(result)


@app.route("/api/eval/stats/reasons", methods=["GET"])
def eval_stats_reasons():
    """扣分原因统计"""
    persona_id = request.args.get("persona_id", "").strip()

    conn = get_db_connection()

    def get_reasons(column):
        if persona_id:
            rows = execute_query(conn, f"""
                SELECT {column} as reason, COUNT(*) as count
                FROM auto_evaluation
                WHERE {column} IS NOT NULL AND {column} != '' AND persona_id = ?
                GROUP BY {column}
                ORDER BY count DESC
                LIMIT 10
            """, (persona_id,), fetch_all=True)
        else:
            rows = execute_query(conn, f"""
                SELECT {column} as reason, COUNT(*) as count
                FROM auto_evaluation
                WHERE {column} IS NOT NULL AND {column} != ''
                GROUP BY {column}
                ORDER BY count DESC
                LIMIT 10
            """, fetch_all=True)
        return [{"reason": row_to_dict(r)["reason"], "count": int(row_to_dict(r)["count"])} for r in rows]

    result = {
        "memory": get_reasons("memory_reason"),
        "emotion": get_reasons("emotion_reason"),
        "quality": get_reasons("quality_reason"),
        "persona": get_reasons("persona_reason")
    }

    conn.close()
    return jsonify(result)


@app.route("/api/eval/stats/by_user", methods=["GET"])
def eval_stats_by_user():
    """用户评分对比"""
    conn = get_db_connection()

    rows = execute_query(conn, """
        SELECT e.persona_id, p.name,
               AVG(e.total_score) as avg_score,
               AVG(e.memory_score) as avg_memory,
               AVG(e.emotion_score) as avg_emotion,
               AVG(e.quality_score) as avg_quality,
               AVG(e.persona_score) as avg_persona,
               COUNT(*) as eval_count
        FROM auto_evaluation e
        LEFT JOIN personas p ON e.persona_id = p.id
        GROUP BY e.persona_id
        ORDER BY avg_score DESC
    """, fetch_all=True)

    conn.close()

    users = []
    for r in rows:
        r = row_to_dict(r)
        users.append({
            "persona_id": r["persona_id"],
            "name": r["name"] or r["persona_id"],
            "avg_score": round(float(r["avg_score"] or 0), 2),
            "avg_memory": round(float(r["avg_memory"] or 0), 2),
            "avg_emotion": round(float(r["avg_emotion"] or 0), 2),
            "avg_quality": round(float(r["avg_quality"] or 0), 2),
            "avg_persona": round(float(r["avg_persona"] or 0), 2),
            "count": int(r["eval_count"] or 0)
        })

    return jsonify({"users": users})


@app.route("/api/eval/score", methods=["POST"])
def eval_score():
    """
    对外评分接口 - 评测AI回复质量

    请求参数 (JSON):
        reply_text: str - AI的回复文本（必填）
        user_message: str - 用户的消息（必填）
        chat_history: list - 对话历史，格式: ["用户: xxx", "秋秋: xxx", ...]（可选）
        user_facts: list - 用户事实，格式: [{"category": "pet", "fact_key": "name", "fact_value": "豆豆"}, ...]（可选）
        persona_data: dict - 用户画像，格式: {"name": "xxx", "age": "xx", ...}（可选）

    返回:
        {
            "memory_score": 10,
            "memory_reason": "",
            "emotion_score": 8,
            "emotion_reason": "扣分原因...",
            "quality_score": 9,
            "quality_reason": "扣分原因...",
            "total_score": 9.0,
            "error": null
        }
    """
    data = request.get_json() or {}

    reply_text = data.get("reply_text", "")
    user_message = data.get("user_message", "")
    chat_history = data.get("chat_history", [])
    user_facts = data.get("user_facts", [])
    persona_data = data.get("persona_data")

    if not reply_text:
        return jsonify({"error": "reply_text is required"}), 400
    if not user_message:
        return jsonify({"error": "user_message is required"}), 400

    llm_config = get_llm_config()
    result = pipi_api.evaluate_chat_reply(
        reply_text=reply_text,
        user_message=user_message,
        chat_history=chat_history,
        user_facts=user_facts,
        persona_data=persona_data,
        **llm_config["eval_realtime"]
    )

    return jsonify(result)


@app.route("/api/simulate/chat", methods=["POST"])
def simulate_chat():
    """
    模拟用户与玩偶聊天接口 - 批量发送消息并获取回复

    请求参数 (JSON):
        persona_id: str - 用户ID（必填，需要先在系统中创建用户画像）
        messages: list - 要发送的消息列表（必填）
        auto_eval: bool - 是否自动评测回复（可选，默认 false）
        delay_ms: int - 每条消息之间的延迟毫秒数（可选，默认 0）

    返回:
        {
            "persona_id": "xiaojuzi",
            "conversations": [
                {
                    "user_message": "你好",
                    "reply": "你好呀！",
                    "reply_id": 123,
                    "eval": {"total_score": 9.0, ...}  // 如果 auto_eval=true
                },
                ...
            ],
            "total_messages": 3,
            "error": null
        }
    """
    import time as time_module

    data = request.get_json() or {}
    persona_id = data.get("persona_id", "")
    messages = data.get("messages", [])
    auto_eval = data.get("auto_eval", False)
    delay_ms = data.get("delay_ms", 0)

    if not persona_id:
        return jsonify({"error": "persona_id is required"}), 400
    if not messages or not isinstance(messages, list):
        return jsonify({"error": "messages must be a non-empty list"}), 400

    # 获取用户画像
    conn = get_db_connection()
    row = execute_query(conn, "SELECT * FROM personas WHERE id=?", (persona_id,), fetch_one=True)
    conn.close()

    if not row:
        return jsonify({"error": f"persona_id '{persona_id}' not found"}), 404

    persona_data = row_to_dict(row)
    device_id = persona_data.get("device_id", persona_id)
    name = persona_data.get("name", persona_id)
    target_api = persona_data.get("target_api", "pipi")
    api_url, api_key, api_headers = get_api_config_by_code(target_api)

    conversations = []

    for i, msg in enumerate(messages):
        if not isinstance(msg, str) or not msg.strip():
            continue

        user_message = msg.strip()

        # 保存用户消息
        save_chat_msg(persona_id, "user", name, user_message)

        # 构建请求
        system_prompt = pipi_api.build_system_prompt(persona_data, device_id)
        api_messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_message},
        ]

        # 调用目标接口
        result = pipi_api.call_pipi_stream(api_messages, device_id=device_id, api_url=api_url, api_key=api_key, extra_headers=api_headers)

        reply_text = result.get("full_text", "")
        reply_id = None
        eval_result = None

        if reply_text:
            reply_id = save_chat_msg(persona_id, "pipi", "秋秋", reply_text)

            # 先提取事实（同步），确保评测时有最新事实
            try:
                conn = get_db_connection()
                fact_rows = execute_query(conn,
                    "SELECT id, category, fact_key, entity_name, fact_value FROM user_facts WHERE persona_id=? AND is_active=1",
                    (persona_id,), fetch_all=True)
                existing_facts = [row_to_dict(r) for r in fact_rows]
                history_rows = execute_query(conn,
                    "SELECT role, text FROM chat_messages WHERE persona_id=? ORDER BY id DESC LIMIT 10",
                    (persona_id,), fetch_all=True)
                conn.close()

                chat_history_for_extract = []
                for row in reversed(history_rows):
                    prefix = "用户: " if row["role"] == "user" else "秋秋: "
                    chat_history_for_extract.append(prefix + row["text"])

                llm_config = get_llm_config()
                facts = pipi_api.extract_facts_from_message(
                    user_message, persona_data, existing_facts, chat_history=chat_history_for_extract, **llm_config["fact_extract"])

                if facts:
                    for f in facts:
                        related_ids = f.pop("related_to", [])
                        if related_ids:
                            f["related_fact_ids"] = ",".join(str(x) for x in related_ids)
                        f["persona_id"] = persona_id
                        f["confidence"] = "implicit"
                        f["source_session"] = persona_id
                        f["source_text"] = user_message
                        _create_fact(f)
            except Exception as e:
                print(f"[SIMULATE FACT ERROR] {persona_id}: {e}", flush=True)

            # 自动评测（事实提取后）
            if auto_eval:
                conn = get_db_connection()
                context = _build_eval_context(conn, persona_id, reply_id)
                conn.close()

                llm_config2 = get_llm_config()
                eval_result = pipi_api.evaluate_chat_reply(
                    reply_text=reply_text,
                    user_message=user_message,
                    chat_history=context.get("chat_history", []),
                    user_facts=context.get("user_facts", []),
                    persona_data=context.get("persona_data"),
                    **llm_config2["eval_realtime"]
                )

                if eval_result and eval_result.get("total_score") is not None:
                    _save_evaluation(reply_id, persona_id, eval_result, context)

        conv = {
            "index": i,
            "user_message": user_message,
            "reply": reply_text,
            "reply_id": reply_id,
            "error": result.get("error")
        }
        if auto_eval and eval_result:
            conv["eval"] = eval_result

        conversations.append(conv)

        # 延迟
        if delay_ms > 0 and i < len(messages) - 1:
            time_module.sleep(delay_ms / 1000.0)

    return jsonify({
        "persona_id": persona_id,
        "conversations": conversations,
        "total_messages": len(conversations),
        "error": None
    })


@app.route("/api/eval/batch", methods=["POST"])
def batch_evaluate():
    """批量评测未评测的回复"""
    data = request.get_json() or {}
    persona_id = data.get("persona_id")
    limit = data.get("limit", 50)

    conn = get_db_connection()
    query = """
        SELECT m.id, m.persona_id, m.text, m.created_at
        FROM chat_messages m
        LEFT JOIN auto_evaluation e ON m.id = e.message_id
        WHERE m.role = 'pipi' AND e.id IS NULL
    """
    params = []
    if persona_id:
        query += " AND m.persona_id = ?"
        params.append(persona_id)
    query += " ORDER BY m.id DESC LIMIT ?"
    params.append(limit)

    pending = execute_query(conn, query, tuple(params), fetch_all=True)
    conn.close()

    if pending:
        t = threading.Thread(target=_batch_evaluate_worker, args=([row_to_dict(p) for p in pending],), daemon=True)
        t.start()

    return jsonify({"pending_count": len(pending), "status": "processing"})


def _batch_evaluate_worker(pending_msgs):
    """后台批量评测工作线程"""
    for msg in pending_msgs:
        try:
            msg_id = msg['id']
            persona_id = msg['persona_id']
            reply_text = msg['text']

            # 获取这条回复前的用户消息
            conn = get_db_connection()
            user_msg_row = execute_query(conn,
                "SELECT text FROM chat_messages WHERE persona_id=? AND id < ? AND role='user' ORDER BY id DESC LIMIT 1",
                (persona_id, msg_id), fetch_one=True)
            user_message = user_msg_row['text'] if user_msg_row else ""

            # 构建上下文
            context = _build_eval_context(conn, persona_id, msg_id)
            conn.close()

            # 调用评测
            llm_config = get_llm_config()
            eval_result = pipi_api.evaluate_chat_reply(
                reply_text=reply_text,
                user_message=user_message,
                chat_history=context['chat_history'],
                user_facts=context['user_facts'],
                persona_data=context['persona_data'],
                **llm_config["eval_batch"]
            )

            # 保存结果
            _save_evaluation(msg_id, persona_id, eval_result, context)
            print(f"[BATCH EVAL] {msg_id} => {eval_result.get('total_score')}")

        except Exception as e:
            print(f"[BATCH EVAL ERROR] {msg.get('id')}: {e}")


def _build_eval_context(conn, persona_id, current_msg_id):
    """构建评测上下文"""
    from datetime import datetime, timedelta

    today = datetime.now().strftime("%Y-%m-%d")

    # 获取当天对话
    today_msgs = execute_query(conn, """
        SELECT id, role, text, created_at FROM chat_messages
        WHERE persona_id = ? AND id < ? AND date(created_at) = ?
        ORDER BY id
    """, (persona_id, current_msg_id, today), fetch_all=True)
    today_msgs = [row_to_dict(m) for m in today_msgs]

    # 如果当天不足 6 条（3轮），补取昨天的
    if len(today_msgs) < 6:
        yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
        older_msgs = execute_query(conn, """
            SELECT id, role, text, created_at FROM chat_messages
            WHERE persona_id = ? AND date(created_at) = ?
            ORDER BY id DESC LIMIT 10
        """, (persona_id, yesterday), fetch_all=True)
        older_msgs = [row_to_dict(m) for m in older_msgs]
        today_msgs = list(reversed(older_msgs)) + today_msgs

    # 格式化对话历史（含时间间隔标记）
    chat_history = _format_history_with_gaps(today_msgs)

    # 获取用户事实（过滤已遗忘的）
    facts = execute_query(conn, """
        SELECT uf.id, uf.category, uf.fact_key, uf.entity_name, uf.fact_value, uf.memory_level, uf.weight, uf.created_at, uf.fact_type,
               COALESCE(mdr.forget_threshold, 0.1) as forget_threshold
        FROM user_facts uf
        LEFT JOIN memory_decay_rules mdr ON uf.memory_level = mdr.level
        WHERE uf.persona_id = ? AND uf.is_active = 1
    """, (persona_id,), fetch_all=True)

    # 计算当前权重，过滤已遗忘的事实（permanent/habitual 永不遗忘）
    user_facts = []
    for f in facts:
        current_weight = _calculate_fact_weight(f)
        threshold = float(f["forget_threshold"]) if f["forget_threshold"] else 0.1
        fact_type = f.get("fact_type") or "permanent"
        is_permanent = fact_type in ("permanent", "habitual")
        if is_permanent or current_weight >= threshold:
            user_facts.append({
                "id": f["id"],
                "category": f["category"],
                "fact_key": f["fact_key"],
                "entity_name": f.get("entity_name", "") or "",
                "fact_value": f["fact_value"],
                "memory_level": f["memory_level"],
                "weight": current_weight
            })

    # 获取用户画像
    persona = execute_query(conn, "SELECT * FROM personas WHERE id = ?", (persona_id,), fetch_one=True)
    persona_data = row_to_dict(persona) if persona else None

    return {
        "chat_history": chat_history,
        "user_facts": user_facts,
        "persona_data": persona_data
    }


def _calculate_fact_weight(fact):
    """计算事实当前权重（基于遗忘机制）"""
    from datetime import datetime

    # 获取衰减规则
    conn = get_db_connection()
    rule = execute_query(conn, """
        SELECT decay_start_days, decay_rate FROM memory_decay_rules WHERE level = ?
    """, (fact.get("memory_level", "medium"),), fetch_one=True)
    conn.close()

    if not rule:
        return float(fact.get("weight", 1.0))

    decay_start_days = rule["decay_start_days"]
    decay_rate = float(rule["decay_rate"])

    # 计算距离创建的天数
    created_at = fact.get("created_at")
    if isinstance(created_at, str):
        try:
            created_at = datetime.strptime(created_at[:19], "%Y-%m-%d %H:%M:%S")
        except:
            return float(fact.get("weight", 1.0))

    days_passed = (datetime.now() - created_at).days

    # 未到衰减期，返回原权重
    if days_passed <= decay_start_days:
        return float(fact.get("weight", 1.0))

    # 计算衰减后权重
    decay_days = days_passed - decay_start_days
    base_weight = float(fact.get("weight", 1.0))
    current_weight = base_weight * (decay_rate ** decay_days)

    return current_weight


def _get_active_facts(persona_id, include_forgotten=False):
    """获取用户的有效事实（过滤已遗忘的）

    Args:
        persona_id: 用户ID
        include_forgotten: 是否包含已遗忘的事实（用于调试）

    Returns:
        list: 有效事实列表，每项包含 id, category, fact_key, fact_value, effective_weight
    """
    conn = get_db_connection()

    # 查询所有 is_active=1 的事实，包含计算权重所需字段
    rows = execute_query(conn, """
        SELECT uf.id, uf.category, uf.fact_key, uf.entity_name, uf.fact_value,
               uf.memory_level, uf.weight, uf.created_at, uf.fact_type,
               COALESCE(mdr.forget_threshold, 0.1) as forget_threshold
        FROM user_facts uf
        LEFT JOIN memory_decay_rules mdr ON uf.memory_level = mdr.level
        WHERE uf.persona_id = ? AND uf.is_active = 1
    """, (persona_id,), fetch_all=True)
    conn.close()

    result = []
    for row in rows:
        fact = row_to_dict(row)
        effective_weight = _calculate_fact_weight(fact)
        threshold = float(row["forget_threshold"]) if row["forget_threshold"] else 0.1

        # permanent/habitual 类型永不遗忘
        is_permanent = fact.get("fact_type") in ("permanent", "habitual")
        is_forgotten = (not is_permanent) and (effective_weight < threshold)

        if include_forgotten or not is_forgotten:
            result.append({
                "id": row["id"],
                "category": row["category"],
                "fact_key": row["fact_key"],
                "fact_value": row["fact_value"],
                "effective_weight": round(effective_weight, 4),
                "is_forgotten": is_forgotten
            })

    # 按权重排序，高权重优先
    result.sort(key=lambda x: x["effective_weight"], reverse=True)
    return result


def _format_history_with_gaps(msgs):
    """格式化对话历史，超过 2 小时插入时间分隔"""
    from datetime import datetime
    result = []
    prev_time = None
    for m in msgs:
        try:
            curr_time = datetime.strptime(m["created_at"][:19], "%Y-%m-%d %H:%M:%S")
            if prev_time and (curr_time - prev_time).total_seconds() > 7200:
                hours = int((curr_time - prev_time).total_seconds() / 3600)
                result.append(f"—— 间隔 {hours} 小时 ——")
            prefix = "用户: " if m["role"] == "user" else "秋秋: "
            result.append(prefix + m["text"])
            prev_time = curr_time
        except:
            prefix = "用户: " if m["role"] == "user" else "秋秋: "
            result.append(prefix + m["text"])
    return result


def _save_evaluation(message_id, persona_id, eval_result, context):
    """保存评测结果"""
    if eval_result.get("error"):
        return

    conn = get_db_connection()
    execute_query(conn, """
        INSERT INTO auto_evaluation
        (message_id, persona_id, memory_score, memory_reason, emotion_score, emotion_reason,
         quality_score, quality_reason, persona_score, persona_reason, total_score, eval_context)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        message_id, persona_id,
        eval_result.get("memory_score"),
        eval_result.get("memory_reason", ""),
        eval_result.get("emotion_score"),
        eval_result.get("emotion_reason", ""),
        eval_result.get("quality_score"),
        eval_result.get("quality_reason", ""),
        eval_result.get("persona_score"),
        eval_result.get("persona_reason", ""),
        eval_result.get("total_score"),
        json.dumps({"facts_count": len(context.get("user_facts", [])), "history_len": len(context.get("chat_history", []))}, ensure_ascii=False)
    ))
    conn.commit()
    conn.close()


def is_eval_enabled():
    """检查评测开关是否开启"""
    conn = get_db_connection()
    row = execute_query(conn, "SELECT value FROM eval_config WHERE `key`='auto_eval_enabled'", fetch_one=True)
    conn.close()
    return row and row["value"] == 'true'


# ─── 辅助函数 ─────────────────────────────────────

# 事实字段到 personas 表字段的映射
# 当提取的 (category, fact_key) 匹配时，同步更新 personas 表
# 事实字段到 personas 表字段的映射（适配细化后的 key）
PERSONA_FIELD_MAP = {
    # living
    ("living", "city"): "city",
    # work
    ("work", "occupation"): "occupation",
    # basic (暂无对应的 fact category，保留兼容)
    ("basic", "age"): "age",
    ("basic", "gender"): "gender",
    ("basic", "education"): "education",
    # family - 细化后的 key
    ("family", "family_relation"): "family_status",
    ("family", "hometown"): "city",  # 老家可更新 city（如果没有 living.city）
    # hobby - 细化后的 key
    ("hobby", "favorite_entertainment"): "interests",
    # preference - 新增
    ("preference", "favorite_activity"): "interests",
}


def _get_memory_level_for_category(conn, category):
    """根据类别获取记忆层级"""
    row = execute_query(conn,
        "SELECT memory_level FROM memory_category_mapping WHERE category = ?",
        (category,), fetch_one=True)
    if row:
        return row["memory_level"]
    # 获取默认层级
    default = execute_query(conn,
        "SELECT value FROM memory_config WHERE `key` = 'default_memory_level'",
        fetch_one=True)
    return default["value"] if default else "medium"


def _create_fact(data):
    import time
    persona_id = data.get("persona_id", "")
    category = data.get("category", "")
    fact_key = data.get("fact_key", "")
    fact_value = data.get("fact_value", "")
    confidence = data.get("confidence", "explicit")
    occurred_at = data.get("occurred_at", "")
    emotion_tag = data.get("emotion_tag", "")
    related_fact_ids = data.get("related_fact_ids", "")
    source_case = data.get("source_case", "")
    source_session = data.get("source_session", "")
    source_text = data.get("source_text", "")

    conn = get_db_connection()

    # 优先使用 LLM 返回的 memory_level，否则根据 category 推断
    memory_level = data.get("memory_level")
    if not memory_level or memory_level not in ("short", "medium", "long", "permanent"):
        memory_level = _get_memory_level_for_category(conn, category)

    # 根据 memory_level 自动推断 fact_type（如果未指定）
    fact_type = data.get("fact_type")
    if not fact_type:
        if memory_level == "permanent":
            fact_type = "permanent"
        elif memory_level in ("short", "medium"):
            fact_type = "event"
        else:  # long
            fact_type = "habitual"

    # 获取 entity_name（用于多实体区分）
    entity_name = data.get("entity_name", "")

    # ══════════════════════════════════════════════════════════════
    # 混合去重策略：
    # 1. entity_name 不同 → 不覆盖（不同实体独立）
    # 2. permanent/habitual → 直接覆盖同 (category, key, entity_name)
    # 3. event (short/medium) → 同一天覆盖，不同天并存
    # ══════════════════════════════════════════════════════════════

    today = time.strftime("%Y-%m-%d")

    if fact_type in ("permanent", "habitual"):
        # permanent/habitual: 覆盖同 (category, key, entity_name)
        existing = execute_query(conn,
            "SELECT id, fact_value FROM user_facts WHERE persona_id=? AND category=? AND fact_key=? AND COALESCE(entity_name,'')=? AND is_active=1",
            (persona_id, category, fact_key, entity_name), fetch_all=True)
        for row in existing:
            if row["fact_value"] != fact_value:
                execute_query(conn, "UPDATE user_facts SET is_active=0 WHERE id=?", (row["id"],))
                print(f"[FACT DEDUP] {category}.{fact_key}({entity_name or '-'}): '{row['fact_value'][:30]}' -> '{fact_value[:30]}'")
        has_same = any(row["fact_value"] == fact_value for row in existing)
        if has_same:
            conn.close()
            return
    else:
        # event (short/medium): 同一天内覆盖，不同天并存
        existing = execute_query(conn,
            "SELECT id, fact_value, DATE(created_at) as created_date FROM user_facts WHERE persona_id=? AND category=? AND fact_key=? AND COALESCE(entity_name,'')=? AND is_active=1",
            (persona_id, category, fact_key, entity_name), fetch_all=True)
        for row in existing:
            row = row_to_dict(row) if not isinstance(row, dict) else row
            created_date = str(row.get("created_date", ""))[:10]
            # 同一天且值不同 → 覆盖
            if created_date == today and row["fact_value"] != fact_value:
                execute_query(conn, "UPDATE user_facts SET is_active=0 WHERE id=?", (row["id"],))
                print(f"[FACT DEDUP TODAY] {category}.{fact_key}: '{row['fact_value'][:30]}' -> '{fact_value[:30]}'")
            # 同一天且值相同 → 跳过
            elif created_date == today and row["fact_value"] == fact_value:
                conn.close()
                return
        # 不同天的记录保留，新增当天记录

    execute_query(conn,
        "INSERT INTO user_facts (persona_id, category, fact_key, entity_name, fact_value, fact_type, confidence, occurred_at, emotion_tag, related_fact_ids, source_case, source_session, source_text, memory_level, weight) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,1.0)",
        (persona_id, category, fact_key, entity_name, fact_value, fact_type, confidence, occurred_at, emotion_tag, related_fact_ids, source_case, source_session, source_text, memory_level))
    entity_tag = f"[{entity_name}]" if entity_name else ""
    print(f"[FACT INSERT] {category}.{fact_key}{entity_tag}({fact_type}) = {fact_value[:50]}")

    # 同步更新 personas 表（如果字段匹配映射，且为 permanent 类型）
    if fact_type == "permanent":
        persona_field = PERSONA_FIELD_MAP.get((category, fact_key))
        if persona_field and persona_id:
            execute_query(conn,
                f"UPDATE personas SET {persona_field} = ? WHERE id = ?",
                (fact_value, persona_id))
            print(f"[FACT->PERSONA] {category}.{fact_key} -> personas.{persona_field} = {fact_value}")

    conn.commit()
    conn.close()


def save_chat_msg(persona_id, role, user_name, text):
    conn = get_db_connection()
    cur = execute_query(conn,
        "INSERT INTO chat_messages (persona_id, role, user_name, text) VALUES (?,?,?,?)",
        (persona_id, role, user_name, text))
    msg_id = get_lastrowid(cur)
    conn.commit()
    conn.close()
    return msg_id


def call_api(persona_id, message):
    uid = persona_id if persona_id else "guest"
    uname = uid.split('/')[-1] if '/' not in uid else uid
    save_chat_msg(uid, "user", uname, message)

    if persona_id == "__guest__":
        device_id = "TEST_DEV_HUARONG_guest_" + str(random.randint(10000000, 99999999))
        persona_data = None
        name = "游客"
        api_url, api_key = None, None  # 使用默认
    else:
        conn = get_db_connection()
        row = execute_query(conn, "SELECT * FROM personas WHERE id=?", (persona_id,), fetch_one=True)
        conn.close()
        if not row:
            return {"error": "用户不存在"}
        persona_data = row_to_dict(row)
        device_id = persona_data["device_id"]
        name = persona_data["name"]
        target_api = persona_data.get("target_api", "pipi")
        api_url, api_key, api_headers = get_api_config_by_code(target_api)

    system_prompt = pipi_api.build_system_prompt(persona_data, device_id)
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": message},
    ]

    print(f"[CALL API] persona_id={persona_id} device_id={device_id} msg={message[:50]}", flush=True)
    result = pipi_api.call_pipi_stream(messages, device_id=device_id, api_url=api_url, api_key=api_key, extra_headers=api_headers)
    if result.get("full_text"):
        msg_id = save_chat_msg(persona_id or "guest", "pipi", "秋秋", result["full_text"])
        result["message_id"] = msg_id

        # 实时评测（如果开关开启）
        eval_on = is_eval_enabled()
        print(f"[EVAL CHECK] persona_id={persona_id} eval_enabled={eval_on}", flush=True)
        if persona_id and persona_id != "__guest__" and eval_on:
            print(f"[EVAL THREAD START] msg_id={msg_id}", flush=True)
            t_eval = threading.Thread(
                target=_evaluate_and_save,
                args=(msg_id, persona_id, message, result["full_text"], persona_data),
                daemon=True
            )
            t_eval.start()

    result["user"] = name

    if persona_id and persona_id != "__guest__":
        t = threading.Thread(target=_extract_and_save, args=(persona_id, message, persona_data), daemon=True)
        t.start()

    return result


def _evaluate_and_save(msg_id, persona_id, user_message, reply_text, persona_data):
    """实时评测并保存结果"""
    try:
        conn = get_db_connection()
        context = _build_eval_context(conn, persona_id, msg_id)
        conn.close()

        llm_config = get_llm_config()
        eval_result = pipi_api.evaluate_chat_reply(
            reply_text=reply_text,
            user_message=user_message,
            chat_history=context['chat_history'],
            user_facts=context['user_facts'],
            persona_data=context['persona_data'],
            **llm_config["eval_realtime"]
        )

        _save_evaluation(msg_id, persona_id, eval_result, context)
        print(f"[REALTIME EVAL] {msg_id} => {eval_result.get('total_score')}", flush=True)

    except Exception as e:
        print(f"[REALTIME EVAL ERROR] {msg_id}: {e}", flush=True)


def _extract_and_save(persona_id, message, persona_data):
    try:
        conn = get_db_connection()
        fact_rows = execute_query(conn,
            "SELECT id, category, fact_key, entity_name, fact_value FROM user_facts WHERE persona_id=? AND is_active=1",
            (persona_id,), fetch_all=True)
        existing_facts = [row_to_dict(r) for r in fact_rows]
        history_rows = execute_query(conn,
            "SELECT role, text FROM chat_messages WHERE persona_id=? ORDER BY id DESC LIMIT 10",
            (persona_id,), fetch_all=True)
        conn.close()

        chat_history = []
        for row in reversed(history_rows):
            prefix = "用户: " if row["role"] == "user" else "秋秋: "
            chat_history.append(prefix + row["text"])

        llm_config = get_llm_config()
        facts = pipi_api.extract_facts_from_message(
            message, persona_data, existing_facts, chat_history=chat_history, **llm_config["fact_extract"])

        print(f"[FACT EXTRACT] {persona_id} history_len: {len(chat_history)} msg: {repr(message[:50])} => {json.dumps(facts, ensure_ascii=False)}")

        if facts:
            for f in facts:
                related_ids = f.pop("related_to", [])
                if related_ids:
                    f["related_fact_ids"] = ",".join(str(x) for x in related_ids)
                f["persona_id"] = persona_id
                f["confidence"] = "implicit"
                f["source_session"] = persona_id
                f["source_text"] = message
                _create_fact(f)

    except Exception as e:
        import traceback
        print(f"[FACT EXTRACT ERROR] {persona_id} {repr(message[:50])} {e}\n{traceback.format_exc()}")


# ─── 用户成长 API ─────────────────────────────────────

# 成长消息模板
GROWTH_MESSAGE_TEMPLATES = {
    "基本信息": [
        "我叫{name}，今年{age}岁了",
        "我住在{city}，是做{occupation}的",
        "我{family_status}",
    ],
    "兴趣爱好": [
        "我平时喜欢{hobby}",
        "最近迷上了{interest}",
        "周末一般会{weekend_activity}",
    ],
    "宠物": [
        "我养了一只{pet_type}，叫{pet_name}",
        "我的{pet_name}特别{pet_trait}",
        "{pet_name}今天{pet_action}",
    ],
    "情感": [
        "今天心情{mood}，因为{mood_reason}",
        "最近工作{work_status}，有点{feeling}",
        "想起{memory}，感觉{emotion}",
    ],
    "日常": [
        "今天{daily_event}",
        "刚才{recent_action}",
        "准备{plan}",
    ],
}


@app.route("/api/growth/templates", methods=["GET"])
def get_growth_templates():
    """获取成长消息模板"""
    return jsonify(GROWTH_MESSAGE_TEMPLATES)


@app.route("/api/growth/batch_create", methods=["POST"])
def batch_create_growth():
    """
    [已废弃] 请使用 POST /api/personas/batch 代替

    批量创建用户并启动自动填充任务

    请求参数 (JSON):
        count: int - 创建数量（1-10）
        prefix: str - 用户名前缀
        messages: list - 消息列表
        speed: str - 创建速度

    返回:
        {"created_count": 3, "task_ids": [1,2,3], "persona_ids": ["auto_xxx_1", ...]}
    """
    import time as time_module

    data = request.get_json() or {}
    count = data.get("count", 3)
    prefix = data.get("prefix", "用户")
    messages = data.get("messages", [])
    speed = data.get("speed", "normal")
    target_api = data.get("target_api", "pipi")

    if not isinstance(count, int) or count < 1 or count > 10:
        return jsonify({"error": "count must be 1-10"}), 400
    if not messages or not isinstance(messages, list):
        return jsonify({"error": "messages required"}), 400

    timestamp = int(time_module.time())
    created_personas = []
    task_ids = []

    conn = get_db_connection()

    for i in range(count):
        # 生成用户ID和名称
        persona_id = f"auto_{timestamp}_{i+1}"
        persona_name = f"{prefix}{i+1}"
        # 生成唯一 device_id
        device_id = f"TEST_DEV_HUARONG_{timestamp}_{i+1}_{random.randint(1000,9999)}"

        # 只创建最基本的用户画像（id + name + device_id）
        # 其他用户信息全部通过对话提取存入 user_facts
        fields = ["id", "name", "device_id", "target_api"]
        values = [persona_id, persona_name, device_id, target_api]

        if USE_MYSQL:
            ph = ", ".join(["%s"] * len(fields))
            sql = f"INSERT INTO personas ({', '.join(fields)}) VALUES ({ph})"
            execute_query(conn, sql, tuple(values))
        else:
            ph = ", ".join(["?"] * len(fields))
            execute_query(conn, f"INSERT INTO personas ({', '.join(fields)}) VALUES ({ph})", tuple(values))

        created_personas.append({"id": persona_id, "name": persona_name, "device_id": device_id})

    conn.commit()
    conn.close()

    # 为每个用户启动成长任务
    for p in created_personas:
        conn = get_db_connection()
        cur = execute_query(conn,
            "INSERT INTO growth_tasks (persona_id, speed, status, total_messages) VALUES (?,?,?,?)",
            (p["id"], speed, "pending", len(messages)))
        task_id = get_lastrowid(cur)

        for idx, msg in enumerate(messages):
            if isinstance(msg, str) and msg.strip():
                execute_query(conn,
                    "INSERT INTO growth_progress (task_id, message_index, user_message, status) VALUES (?,?,?,?)",
                    (task_id, idx, msg.strip(), "pending"))

        conn.commit()
        conn.close()

        task_ids.append(task_id)

        # 启动后台线程
        t = threading.Thread(target=_growth_worker, args=(task_id,), daemon=True)
        t.start()

    return jsonify({
        "created_count": len(created_personas),
        "task_ids": task_ids,
        "personas": created_personas
    })


@app.route("/api/growth/start", methods=["POST"])
def start_growth():
    """
    启动用户成长任务

    请求参数 (JSON):
        persona_id: str - 用户ID（必填）
        messages: list - 用户消息列表（必填）
        speed: str - 成长速度: fast/normal/slow（可选，默认 normal）

    返回:
        {"task_id": 123, "status": "pending", "total_messages": 10}
    """
    data = request.get_json() or {}
    persona_id = data.get("persona_id", "")
    messages = data.get("messages", [])
    speed = data.get("speed", "normal")

    if not persona_id:
        return jsonify({"error": "persona_id is required"}), 400
    if not messages or not isinstance(messages, list):
        return jsonify({"error": "messages must be a non-empty list"}), 400

    # 检查用户是否存在
    conn = get_db_connection()
    row = execute_query(conn, "SELECT id FROM personas WHERE id=?", (persona_id,), fetch_one=True)
    if not row:
        conn.close()
        return jsonify({"error": f"persona_id '{persona_id}' not found"}), 404

    # 创建成长任务
    cur = execute_query(conn,
        "INSERT INTO growth_tasks (persona_id, speed, status, total_messages) VALUES (?,?,?,?)",
        (persona_id, speed, "pending", len(messages)))
    task_id = get_lastrowid(cur)

    # 创建进度记录
    for i, msg in enumerate(messages):
        if isinstance(msg, str) and msg.strip():
            execute_query(conn,
                "INSERT INTO growth_progress (task_id, message_index, user_message, status) VALUES (?,?,?,?)",
                (task_id, i, msg.strip(), "pending"))

    conn.commit()
    conn.close()

    # 启动后台执行线程
    t = threading.Thread(target=_growth_worker, args=(task_id,), daemon=True)
    t.start()

    return jsonify({
        "task_id": task_id,
        "status": "pending",
        "total_messages": len(messages)
    })


@app.route("/api/growth/status/<int:task_id>", methods=["GET"])
def get_growth_status(task_id):
    """
    获取成长任务状态

    返回:
        {
            "task_id": 123,
            "persona_id": "xiaojuzi",
            "status": "running",
            "speed": "normal",
            "total_messages": 10,
            "completed_messages": 3,
            "facts_extracted": 5,
            "progress": 30.0,
            "created_at": "2024-01-01 12:00:00"
        }
    """
    conn = get_db_connection()
    row = execute_query(conn, "SELECT * FROM growth_tasks WHERE id=?", (task_id,), fetch_one=True)
    conn.close()

    if not row:
        return jsonify({"error": "task not found"}), 404

    task = row_to_dict(row)
    total = task["total_messages"] or 1
    completed = task["completed_messages"] or 0
    task["progress"] = round(completed / total * 100, 1)

    return jsonify(task)


@app.route("/api/growth/result/<int:task_id>", methods=["GET"])
def get_growth_result(task_id):
    """
    获取成长任务详细结果

    返回:
        {
            "task": {...},
            "progress": [
                {"message_index": 0, "user_message": "...", "reply_text": "...", "facts_json": "...", "status": "completed"},
                ...
            ],
            "extracted_facts": [...]
        }
    """
    conn = get_db_connection()

    # 获取任务信息
    task_row = execute_query(conn, "SELECT * FROM growth_tasks WHERE id=?", (task_id,), fetch_one=True)
    if not task_row:
        conn.close()
        return jsonify({"error": "task not found"}), 404

    task = row_to_dict(task_row)

    # 获取进度详情
    progress_rows = execute_query(conn,
        "SELECT * FROM growth_progress WHERE task_id=? ORDER BY message_index",
        (task_id,), fetch_all=True)
    progress = [row_to_dict(r) for r in progress_rows]

    # 获取该用户的所有事实（成长期间新增的）
    persona_id = task["persona_id"]
    created_at = task["created_at"]
    facts_rows = execute_query(conn,
        "SELECT * FROM user_facts WHERE persona_id=? AND created_at >= ? ORDER BY created_at",
        (persona_id, created_at), fetch_all=True)
    extracted_facts = [row_to_dict(r) for r in facts_rows]

    conn.close()

    return jsonify({
        "task": task,
        "progress": progress,
        "extracted_facts": extracted_facts
    })


def _generate_persona_profile(name=""):
    """调用 LLM 生成完整的用户画像"""
    system_prompt = """你是用户画像生成器。为AI陪伴玩偶产品生成虚拟用户。

目标用户：15-34岁女性，三线及以上城市，喜欢毛绒玩具和宠物。
消费特点：视觉吸引→情绪共鸣→瞬间下单，颜值正义，情绪消费。
喜欢品牌：泡泡玛特、Jellycat、完美日记、潘多拉等。
兴趣：改娃/OC创作、MBTI/塔罗/星座、重度小红书+B站用户。

要求：字段间有逻辑关联，像真实的人。直接返回JSON。"""

    user_prompt = f"""为"{name or '用户'}"生成画像，返回JSON：
{{"nickname":"一句话描述","real_name":"姓名","gender":"女","age":"年龄","city":"城市","hometown":"老家省份","occupation":"职业","education":"学历","family_status":"家庭状态如独生子女/有姐姐","relationship":"感情状态如单身/有对象","personality":"性格特点如比较内向/挺外向","income_range":"月收入","spending_style":"消费风格","spending_desc":"消费习惯举例","devices":"常用设备","usage_scenes":"使用场景","core_goal":"核心目标","short_goal":"短期诉求","long_goal":"长期诉求","pain_points":"痛点","constraints":"约束","risk_profile":"风险偏好","interests":"兴趣标签","language_style":"语言风格和常用语气词","sample_dialog":"典型对话1-2句","info_sources":"信息来源","decision_style":"决策方式","relation_pace":"关系节奏","scene_pref":"场景偏好","top_expectations":"期待TOP3","minefields":"踩雷点","pet_type":"宠物类型","pet_name":"宠物名","pet_age":"宠物年龄","pet_trait":"宠物特点","favorite_drink":"爱喝的","favorite_food":"爱吃的","spicy_preference":"吃辣偏好","current_hobby":"当前爱好","learning":"在学什么","favorite_singer":"喜欢的歌手","best_friend":"好友名","stress_relief":"解压方式","work_time":"上班时间","lunch_habit":"午餐习惯","commute":"通勤方式"}}"""

    try:
        result = pipi_api.call_llm_simple(system_prompt, user_prompt, timeout=60)
        print(f"[PERSONA GEN] LLM result len={len(result) if result else 0}", flush=True)
        if result:
            # 提取 JSON（处理 ```json ... ``` 格式）
            import re
            # 先去掉 markdown 代码块标记
            clean = re.sub(r'```json\s*', '', result)
            clean = re.sub(r'```\s*', '', clean)
            match = re.search(r'\{[\s\S]*\}', clean)
            if match:
                try:
                    profile = json.loads(match.group())
                    # 确保 age 是字符串
                    if 'age' in profile:
                        profile['age'] = str(profile['age'])
                    print(f"[PERSONA GEN] parsed profile keys={list(profile.keys())[:5]}...", flush=True)
                    return profile
                except json.JSONDecodeError as je:
                    print(f"[PERSONA GEN] JSON decode error: {je}, text: {match.group()[:200]}", flush=True)
            else:
                print(f"[PERSONA GEN] no JSON found in result: {result[:100]}", flush=True)
    except Exception as e:
        print(f"[PERSONA GEN ERROR] {e}", flush=True)

    # 降级到随机生成
    print(f"[PERSONA GEN] fallback to random", flush=True)
    return _generate_persona_profile_fallback()


def _generate_persona_profile_fallback():
    """随机生成用户画像（降级方案）"""
    def pick(arr):
        return arr[random.randint(0, len(arr) - 1)]

    return {
        "age": pick(["23", "25", "27", "28", "30", "32", "35"]),
        "gender": pick(["男", "女"]),
        "city": pick(["北京", "上海", "深圳", "杭州", "成都", "广州"]),
        "hometown": pick(["山东", "河南", "四川", "湖南", "江苏", "浙江"]),
        "occupation": pick(["程序员", "设计师", "产品经理", "教师", "医生", "会计"]),
        "education": pick(["本科", "硕士", "大专"]),
        "family_status": pick(["独生子女", "有哥哥", "有姐姐"]),
        "relationship": pick(["单身", "有对象"]),
        "interests": pick(["看电影,听音乐", "打游戏,看书", "跑步,健身"]),
        "language_style": pick(["活泼开朗", "温和内敛", "幽默风趣"]),
        "personality": pick(["比较内向", "挺外向的", "有点社恐"]),
        "pet_type": pick(["猫", "狗", ""]),
        "pet_name": pick(["豆豆", "毛毛", "球球"]),
        "pet_age": pick(["1岁", "2岁", "3岁"]),
        "pet_trait": pick(["特别粘人", "很调皮", "超级可爱"]),
        "favorite_drink": pick(["咖啡", "奶茶", "可乐"]),
        "favorite_food": pick(["火锅", "烧烤", "日料"]),
        "spicy_preference": pick(["不吃辣", "无辣不欢", "微辣"]),
        "current_hobby": pick(["看电影", "听音乐", "打游戏"]),
        "learning": pick(["吉他", "烘焙", "画画", "摄影"]),
        "favorite_singer": pick(["周杰伦", "五月天", "陈奕迅"]),
        "best_friend": pick(["阿明", "小美", "老张"]),
        "stress_relief": pick(["听音乐", "吃东西", "睡觉"]),
        "work_time": pick(["8点", "9点", "9点半"]),
        "lunch_habit": pick(["点外卖", "去食堂", "自己带饭"]),
        "commute": pick(["走路10分钟", "地铁半小时", "骑车15分钟"]),
    }


# ─── 基于缺失字段生成对话 ─────────────────────────────────

# personas 表字段 → 对话模板映射
PERSONA_FIELD_TEMPLATES = {
    "age": ["我今年{val}岁", "我{val}岁了"],
    "city": ["我现在在{val}工作", "我住在{val}"],
    "occupation": ["我是做{val}的", "我的工作是{val}"],
    "education": ["我是{val}学历", "我读的{val}"],
    "family_status": ["我是{val}", "我家里{val}"],
    "income_range": ["我月薪大概{val}", "我收入{val}左右"],
    "interests": ["我平时喜欢{val}", "我的爱好是{val}"],
}

# user_facts (category.key) → 对话模板映射
FACT_KEY_TEMPLATES = {
    # pet
    ("pet", "name"): ["我养了一只宠物叫{val}", "我家有只{val}"],
    ("pet", "age"): ["我家宠物{val}了", "{val}了我家小家伙"],
    ("pet", "personality"): ["我家宠物{val}", "它性格{val}"],
    # food（使用新 key）
    ("food", "favorite_food"): ["我最爱吃{val}", "我特别喜欢吃{val}"],
    ("food", "favorite_cuisine"): ["我喜欢吃{val}", "我偏爱{val}"],
    ("food", "dislike_food"): ["我不爱吃{val}", "我不太喜欢{val}"],
    ("food", "dietary_restriction"): ["我{val}", "我不吃{val}"],
    ("food", "health_restriction"): ["我{val}不能吃", "因为{val}我不能吃"],
    ("food", "breakfast_habit"): ["我早餐一般吃{val}", "早上我通常{val}"],
    ("food", "lunch_habit"): ["我中午一般{val}", "午饭我经常{val}"],
    # preference
    ("preference", "favorite_drink"): ["我最喜欢喝{val}", "我每天都要来杯{val}"],
    ("preference", "favorite_color"): ["我最喜欢{val}色", "我偏爱{val}"],
    ("preference", "favorite_activity"): ["我喜欢{val}", "我平时爱{val}"],
    ("preference", "weekend_activity"): ["周末我一般{val}", "休息日我喜欢{val}"],
    # living
    ("living", "city"): ["我在{val}工作", "我现在在{val}"],
    ("living", "housing_type"): ["我现在{val}", "我是{val}的"],
    ("living", "commute_method"): ["我上班{val}", "我通勤靠{val}"],
    ("living", "commute_duration"): ["我上班路上要{val}", "通勤大概{val}"],
    ("living", "neighborhood"): ["我住在{val}附近", "我家在{val}那边"],
    # family（使用新 key）
    ("family", "father"): ["我爸{val}", "我父亲{val}"],
    ("family", "mother"): ["我妈{val}", "我母亲{val}"],
    ("family", "sibling"): ["我{val}", "我家里{val}"],
    ("family", "family_structure"): ["我是{val}", "我家{val}"],
    ("family", "hometown"): ["我老家是{val}的", "我是{val}人"],
    # relationship（使用新 key）
    ("relationship", "romantic_status"): ["我现在{val}", "感情方面我{val}"],
    ("relationship", "social_tendency"): ["我{val}", "社交方面我{val}"],
    ("relationship", "social_circle_size"): ["我朋友{val}", "我{val}"],
    ("relationship", "social_circle_depth"): ["我朋友{val}", "交友方面{val}"],
    ("relationship", "person"): ["我有个好朋友叫{val}", "我认识一个朋友{val}"],
    ("relationship", "person_relation"): ["我和{val}", "我跟{val}"],
    # work（使用新 key）
    ("work", "company"): ["我在{val}工作", "我公司是{val}"],
    ("work", "daily_schedule"): ["我每天{val}上班", "我的作息是{val}"],
    ("work", "colleague_relation"): ["我同事{val}", "公司同事{val}"],
    ("work", "work_stress"): ["工作压力{val}", "最近工作{val}"],
    # health
    ("health", "sleep_habit"): ["我一般{val}睡觉", "我睡眠{val}"],
    ("health", "exercise_type"): ["我喜欢{val}", "我平时会{val}"],
    ("health", "exercise_frequency"): ["我{val}运动", "锻炼方面我{val}"],
    ("health", "stress_relief"): ["压力大的时候我会{val}", "我解压方式是{val}"],
    # hobby（使用新 key）
    ("hobby", "favorite_movie"): ["我平时喜欢看{val}", "我爱看{val}"],
    ("hobby", "favorite_music"): ["我喜欢听{val}", "我爱听{val}的歌"],
    ("hobby", "favorite_singer"): ["我特别喜欢{val}", "我是{val}的粉丝"],
    ("hobby", "current_watching"): ["我最近在追{val}", "我正在看{val}"],
    ("hobby", "sport_type"): ["我喜欢{val}", "我平时会{val}"],
    ("hobby", "instrument"): ["我最近在学{val}", "我对{val}感兴趣"],
    ("hobby", "art_skill"): ["我会{val}", "我喜欢{val}"],
    ("hobby", "collection_type"): ["我喜欢收集{val}", "我在收{val}"],
    # emotion
    ("emotion", "long_term_state"): ["我这个人{val}", "性格上我比较{val}"],
}

# ─── 用户类型预设模板 ─────────────────────────────────
# 目标用户群体：15-34岁女性，三线及以上城市，喜欢毛绒玩具，有宠物或喜欢宠物
# 消费特点：视觉吸引→情绪共鸣→社交谈资→瞬间下单，颜值正义，情绪消费
PERSONA_TEMPLATES = {
    # ===== 核心目标用户 =====
    "plush_lover_student": {
        "name": "毛绒控学生党",
        "description": "18-24岁女性，大学生或刚毕业，喜欢毛绒玩具和改娃，重度小红书/B站用户",
        "profile": {
            "age": lambda: str(random.randint(18, 24)),
            "gender": lambda: "女",
            "city": lambda: random.choice(["杭州", "成都", "南京", "武汉", "西安", "长沙", "郑州", "合肥"]),
            "occupation": lambda: random.choice(["学生", "实习生", "刚毕业找工作"]),
            "education": lambda: random.choice(["本科在读", "研究生在读", "本科"]),
            "family_status": lambda: random.choice(["独生子女", "有姐姐", "有弟弟"]),
            "interests": lambda: random.choice(["收集毛绒玩具", "改娃", "追星", "看动漫", "刷小红书"]),
        },
        "facts": {
            ("living", "city"): lambda: random.choice(["杭州", "成都", "南京", "武汉", "西安", "长沙"]),
            ("living", "housing_type"): lambda: random.choice(["住宿舍", "在外租房"]),
            ("relationship", "romantic_status"): lambda: random.choice(["单身", "有对象", "暗恋中"]),
            ("relationship", "social_tendency"): lambda: random.choice(["有点社恐", "i人", "慢热"]),
            ("preference", "favorite_drink"): lambda: random.choice(["奶茶", "柠檬茶", "果茶"]),
            ("hobby", "collection_type"): lambda: random.choice(["Jellycat", "泡泡玛特盲盒", "毛绒玩具", "棉花娃娃"]),
            ("hobby", "art_skill"): lambda: random.choice(["给娃娃换装", "画画", "做手账", "捏OC"]),
            ("hobby", "current_watching"): lambda: random.choice(["动漫", "韩剧", "综艺", "up主视频"]),
            ("pet", "name"): lambda: random.choice(["猫叫奶茶", "狗叫布丁", "仓鼠叫团子", "想养但没养"]),
            ("health", "stress_relief"): lambda: random.choice(["刷小红书", "看B站", "抱玩偶", "和闺蜜聊天"]),
            ("emotion", "long_term_state"): lambda: random.choice(["有点emo", "敏感", "容易被治愈", "需要陪伴"]),
        },
        "categories": ["living", "relationship", "preference", "hobby", "pet", "health", "emotion"],
    },
    "plush_lover_worker": {
        "name": "毛绒控打工人",
        "description": "22-30岁女性，职场新人或小白领，用毛绒玩具治愈自己，喜欢颜值好物",
        "profile": {
            "age": lambda: str(random.randint(22, 30)),
            "gender": lambda: "女",
            "city": lambda: random.choice(["杭州", "成都", "苏州", "南京", "武汉", "长沙", "厦门", "青岛"]),
            "occupation": lambda: random.choice(["设计师", "运营", "新媒体", "行政", "教师", "护士"]),
            "education": lambda: random.choice(["本科", "大专", "硕士"]),
            "family_status": lambda: random.choice(["独生子女", "有兄弟姐妹"]),
            "interests": lambda: random.choice(["收集毛绒玩具", "逛街买好看的东西", "追剧", "拍照打卡"]),
        },
        "facts": {
            ("living", "city"): lambda: random.choice(["杭州", "成都", "苏州", "南京", "武汉", "长沙"]),
            ("living", "housing_type"): lambda: random.choice(["租房", "和朋友合租", "住家里"]),
            ("relationship", "romantic_status"): lambda: random.choice(["单身", "有对象", "刚分手"]),
            ("relationship", "social_tendency"): lambda: random.choice(["有点社恐", "工作外向生活内向", "慢热"]),
            ("work", "work_stress"): lambda: random.choice(["压力挺大", "最近有点累", "还好"]),
            ("preference", "favorite_drink"): lambda: random.choice(["奶茶", "咖啡", "柠檬水"]),
            ("hobby", "collection_type"): lambda: random.choice(["Jellycat", "宜家玩偶", "名创优品玩偶", "棉花娃娃"]),
            ("hobby", "favorite_movie"): lambda: random.choice(["爱情片", "治愈系电影", "动漫电影"]),
            ("pet", "name"): lambda: random.choice(["猫叫年糕", "狗叫麻薯", "养了只猫", "想养猫但租房不让"]),
            ("health", "stress_relief"): lambda: random.choice(["买好看的东西", "吃甜食", "抱玩偶", "刷小红书"]),
            ("emotion", "long_term_state"): lambda: random.choice(["需要被治愈", "容易焦虑", "期待被认同"]),
        },
        "categories": ["living", "work", "relationship", "preference", "hobby", "pet", "health", "emotion"],
    },
    "pet_mom": {
        "name": "宠物铲屎官",
        "description": "20-32岁女性，有猫/狗，把宠物当孩子养，喜欢给毛孩子买东西",
        "profile": {
            "age": lambda: str(random.randint(20, 32)),
            "gender": lambda: "女",
            "city": lambda: random.choice(["杭州", "成都", "深圳", "广州", "南京", "苏州", "厦门"]),
            "occupation": lambda: random.choice(["设计师", "产品经理", "运营", "程序员", "自由职业"]),
            "education": lambda: random.choice(["本科", "硕士"]),
            "family_status": lambda: random.choice(["独生子女", "有兄弟姐妹"]),
            "interests": lambda: random.choice(["撸猫撸狗", "给宠物买东西", "拍宠物视频", "宠物社交"]),
        },
        "facts": {
            ("living", "city"): lambda: random.choice(["杭州", "成都", "深圳", "广州", "南京"]),
            ("living", "housing_type"): lambda: random.choice(["租房", "自己的房子"]),
            ("relationship", "romantic_status"): lambda: random.choice(["单身", "有对象", "已婚"]),
            ("pet", "name"): lambda: random.choice(["猫叫芋圆", "猫叫糯米", "狗叫可乐", "两只猫叫奶茶和布丁"]),
            ("pet", "personality"): lambda: random.choice(["超级粘人", "高冷但傲娇", "调皮捣蛋", "特别乖"]),
            ("preference", "favorite_drink"): lambda: random.choice(["咖啡", "奶茶"]),
            ("hobby", "collection_type"): lambda: random.choice(["宠物用品", "毛绒玩具", "猫咪周边"]),
            ("health", "stress_relief"): lambda: random.choice(["撸猫", "撸狗", "和毛孩子玩"]),
            ("emotion", "long_term_state"): lambda: random.choice(["宠物就是我的精神支柱", "有它们就很治愈"]),
        },
        "categories": ["living", "relationship", "pet", "preference", "hobby", "health", "emotion"],
    },
    "emotional_consumer": {
        "name": "情绪消费玩家",
        "description": "22-34岁女性，为颜值和情绪价值买单，喜欢MBTI/塔罗/星座等",
        "profile": {
            "age": lambda: str(random.randint(22, 34)),
            "gender": lambda: "女",
            "city": lambda: random.choice(["杭州", "成都", "上海", "深圳", "重庆", "长沙", "武汉"]),
            "occupation": lambda: random.choice(["设计师", "新媒体运营", "市场", "HR", "老师", "自由职业"]),
            "education": lambda: random.choice(["本科", "硕士", "大专"]),
            "family_status": lambda: random.choice(["独生子女", "有兄弟姐妹"]),
            "interests": lambda: random.choice(["MBTI社交", "塔罗占卜", "星座运势", "心理测试"]),
        },
        "facts": {
            ("living", "city"): lambda: random.choice(["杭州", "成都", "上海", "深圳", "重庆"]),
            ("living", "housing_type"): lambda: random.choice(["租房", "和朋友合租", "自己的房子"]),
            ("relationship", "romantic_status"): lambda: random.choice(["单身", "有对象", "暧昧中"]),
            ("relationship", "social_tendency"): lambda: random.choice(["INFP", "INFJ", "ENFP", "有点敏感"]),
            ("preference", "favorite_drink"): lambda: random.choice(["奶茶", "咖啡", "气泡水"]),
            ("hobby", "collection_type"): lambda: random.choice(["潘多拉手链", "香薰蜡烛", "塔罗牌", "好看的本子"]),
            ("hobby", "art_skill"): lambda: random.choice(["画画", "做手账", "摄影", "写日记"]),
            ("pet", "name"): lambda: random.choice(["养了猫", "想养宠物", "云吸猫中"]),
            ("health", "stress_relief"): lambda: random.choice(["测塔罗", "看星座运势", "买好看的东西", "和朋友倾诉"]),
            ("emotion", "long_term_state"): lambda: random.choice(["需要被理解", "敏感细腻", "容易共情", "期待被治愈"]),
        },
        "categories": ["living", "relationship", "preference", "hobby", "pet", "health", "emotion"],
    },
    # ===== 原有模板（更新 key 名）=====
    "young_worker": {
        "name": "年轻白领",
        "description": "22-30岁，一线城市工作，租房，单身或恋爱中",
        "profile": {
            "age": lambda: str(random.randint(22, 30)),
            "city": lambda: random.choice(["北京", "上海", "深圳", "杭州", "广州"]),
            "occupation": lambda: random.choice(["程序员", "设计师", "产品经理", "运营", "销售"]),
            "education": lambda: random.choice(["本科", "硕士"]),
            "family_status": lambda: random.choice(["独生子女", "有兄弟姐妹"]),
        },
        "facts": {
            ("living", "housing_type"): lambda: "租房",
            ("relationship", "romantic_status"): lambda: random.choice(["单身", "有对象"]),
            ("work", "work_stress"): lambda: random.choice(["压力挺大", "还好", "比较忙"]),
            ("preference", "favorite_drink"): lambda: random.choice(["咖啡", "奶茶"]),
            ("health", "exercise_frequency"): lambda: random.choice(["偶尔健身", "基本不运动", "每周跑步"]),
        },
        "categories": ["work", "living", "food", "preference", "relationship", "health"],
    },
    "student": {
        "name": "大学生",
        "description": "18-24岁，在校学生，住宿舍或租房",
        "profile": {
            "age": lambda: str(random.randint(18, 24)),
            "city": lambda: random.choice(["北京", "上海", "武汉", "南京", "成都", "西安"]),
            "occupation": lambda: "学生",
            "education": lambda: random.choice(["本科在读", "研究生在读"]),
            "family_status": lambda: random.choice(["独生子女", "有兄弟姐妹"]),
        },
        "facts": {
            ("living", "housing_type"): lambda: random.choice(["住宿舍", "在外租房"]),
            ("relationship", "romantic_status"): lambda: random.choice(["单身", "有对象"]),
            ("work", "work_stress"): lambda: random.choice(["学业压力大", "比较轻松", "考研中"]),
            ("preference", "favorite_drink"): lambda: random.choice(["奶茶", "可乐", "柠檬水"]),
            ("hobby", "favorite_movie"): lambda: random.choice(["动漫", "韩剧", "综艺"]),
        },
        "categories": ["living", "food", "preference", "relationship", "hobby"],
    },
    "new_mom": {
        "name": "新手妈妈",
        "description": "25-35岁，已婚有孩子，关注育儿和家庭",
        "profile": {
            "age": lambda: str(random.randint(25, 35)),
            "city": lambda: random.choice(["北京", "上海", "杭州", "成都", "广州", "深圳"]),
            "occupation": lambda: random.choice(["全职妈妈", "产品经理", "教师", "会计"]),
            "education": lambda: random.choice(["本科", "硕士", "大专"]),
            "family_status": lambda: "有孩子",
        },
        "facts": {
            ("living", "housing_type"): lambda: random.choice(["自己买的房", "和父母住"]),
            ("relationship", "romantic_status"): lambda: "已婚",
            ("family", "child"): lambda: random.choice(["有个1岁的宝宝", "孩子2岁了", "孩子上幼儿园"]),
            ("health", "sleep_habit"): lambda: random.choice(["睡眠不太好", "经常被孩子吵醒"]),
            ("health", "stress_relief"): lambda: random.choice(["刷手机", "追剧", "买东西"]),
        },
        "categories": ["family", "living", "health", "food", "preference"],
    },
    "senior_worker": {
        "name": "职场老人",
        "description": "30-40岁，有一定职场经验，可能已婚",
        "profile": {
            "age": lambda: str(random.randint(30, 40)),
            "city": lambda: random.choice(["北京", "上海", "深圳", "杭州", "广州"]),
            "occupation": lambda: random.choice(["技术总监", "项目经理", "部门主管", "资深工程师"]),
            "education": lambda: random.choice(["本科", "硕士", "博士"]),
            "family_status": lambda: random.choice(["已婚", "有孩子"]),
        },
        "facts": {
            ("living", "housing_type"): lambda: random.choice(["自己买的房", "还在还房贷"]),
            ("relationship", "romantic_status"): lambda: random.choice(["已婚", "有对象"]),
            ("work", "work_stress"): lambda: random.choice(["压力很大", "责任重", "还好习惯了"]),
            ("preference", "favorite_drink"): lambda: random.choice(["咖啡", "茶"]),
            ("health", "exercise_frequency"): lambda: random.choice(["每周健身", "没时间运动", "周末打球"]),
        },
        "categories": ["work", "living", "family", "health", "preference"],
    },
}


def _get_persona_template(template_id):
    """获取用户预设模板"""
    return PERSONA_TEMPLATES.get(template_id)


def _apply_template_to_random_values(template):
    """将模板的值生成器应用到随机值"""
    result = {}
    # 应用 profile 字段
    for field, gen in template.get("profile", {}).items():
        result[field] = gen() if callable(gen) else gen
    return result


# 随机值生成器
RANDOM_VALUES = {
    "age": lambda: str(random.randint(22, 35)),
    "city": lambda: random.choice(["北京", "上海", "深圳", "杭州", "成都", "广州", "南京", "武汉"]),
    "occupation": lambda: random.choice(["程序员", "设计师", "产品经理", "教师", "医生", "会计", "销售", "运营"]),
    "education": lambda: random.choice(["本科", "硕士", "大专", "博士"]),
    "family_status": lambda: random.choice(["独生子女", "有哥哥", "有姐姐", "有弟弟", "有妹妹"]),
    "income_range": lambda: random.choice(["8000-12000", "12000-20000", "20000-30000"]),
    "interests": lambda: random.choice(["看电影", "听音乐", "打游戏", "跑步", "看书", "旅游"]),
    # fact keys（使用新 key 名）
    ("pet", "name"): lambda: random.choice(["猫叫毛毛", "狗叫豆豆", "猫叫团子", "狗叫球球"]),
    ("pet", "age"): lambda: random.choice(["1岁", "2岁", "3岁", "半岁"]),
    ("pet", "personality"): lambda: random.choice(["特别粘人", "很调皮", "比较高冷", "超级可爱"]),
    ("food", "favorite_food"): lambda: random.choice(["火锅", "烧烤", "日料", "川菜", "粤菜", "面食"]),
    ("food", "dislike_food"): lambda: random.choice(["香菜", "苦瓜", "内脏", "榴莲"]),
    ("food", "dietary_restriction"): lambda: random.choice(["不吃辣", "不吃葱姜蒜", "不吃香菜"]),
    ("food", "health_restriction"): lambda: random.choice(["海鲜过敏", "乳糖不耐", "胃不好不能吃辣"]),
    ("food", "breakfast_habit"): lambda: random.choice(["包子豆浆", "面包牛奶", "煎饼果子", "不吃早餐"]),
    ("food", "lunch_habit"): lambda: random.choice(["点外卖", "去食堂", "自己带饭", "出去吃"]),
    ("preference", "favorite_drink"): lambda: random.choice(["咖啡", "奶茶", "可乐", "柠檬水", "茶"]),
    ("preference", "favorite_color"): lambda: random.choice(["蓝", "白", "黑", "粉", "绿"]),
    ("preference", "favorite_activity"): lambda: random.choice(["看电影", "逛街", "宅家", "户外"]),
    ("preference", "weekend_activity"): lambda: random.choice(["睡懒觉", "约朋友", "宅家追剧", "出去玩"]),
    ("living", "city"): lambda: random.choice(["北京", "上海", "深圳", "杭州", "成都", "广州"]),
    ("living", "housing_type"): lambda: random.choice(["租房住", "自己买的房", "住家里", "和朋友合租"]),
    ("living", "commute_method"): lambda: random.choice(["坐地铁", "骑车", "走路", "开车", "公交"]),
    ("living", "commute_duration"): lambda: random.choice(["半小时", "一小时", "10分钟", "40分钟"]),
    ("living", "neighborhood"): lambda: random.choice(["市中心", "郊区", "老城区", "新区"]),
    ("family", "father"): lambda: random.choice(["在老家", "退休了", "还在工作"]),
    ("family", "mother"): lambda: random.choice(["在老家", "退休了", "还在工作"]),
    ("family", "sibling"): lambda: random.choice(["有个姐姐", "有个哥哥", "有个弟弟", "有个妹妹", "独生子女"]),
    ("family", "family_structure"): lambda: random.choice(["独生子女", "家里老大", "家里最小"]),
    ("family", "hometown"): lambda: random.choice(["江苏", "浙江", "山东", "四川", "湖北", "河南", "广东"]),
    ("relationship", "romantic_status"): lambda: random.choice(["单身", "有对象", "已婚", "刚分手"]),
    ("relationship", "social_tendency"): lambda: random.choice(["有点社恐", "比较外向", "有点内向", "慢热"]),
    ("relationship", "social_circle_size"): lambda: random.choice(["朋友不多", "朋友挺多", "几个死党"]),
    ("relationship", "social_circle_depth"): lambda: random.choice(["都交心", "泛泛之交", "有几个特别好的"]),
    ("relationship", "person"): lambda: random.choice(["阿明", "小美", "老王", "阿花", "小李"]),
    ("relationship", "person_relation"): lambda: random.choice(["认识好几年了", "大学同学", "同事"]),
    ("work", "company"): lambda: random.choice(["互联网公司", "外企", "国企", "创业公司", "事业单位"]),
    ("work", "daily_schedule"): lambda: random.choice(["9点", "8点半", "10点", "弹性"]),
    ("work", "colleague_relation"): lambda: random.choice(["人都挺好", "关系一般", "有几个玩得好的"]),
    ("work", "work_stress"): lambda: random.choice(["挺大的", "还好", "最近比较忙", "不算大"]),
    ("health", "sleep_habit"): lambda: random.choice(["11点", "12点", "1点", "10点半"]),
    ("health", "exercise_type"): lambda: random.choice(["跑步", "瑜伽", "游泳", "健身"]),
    ("health", "exercise_frequency"): lambda: random.choice(["每周两三次", "偶尔", "基本不运动"]),
    ("health", "stress_relief"): lambda: random.choice(["听音乐", "睡觉", "吃东西", "打游戏", "看剧"]),
    ("hobby", "favorite_movie"): lambda: random.choice(["爱情片", "喜剧", "悬疑片", "动漫电影"]),
    ("hobby", "favorite_music"): lambda: random.choice(["流行音乐", "民谣", "古风", "电子音乐"]),
    ("hobby", "favorite_singer"): lambda: random.choice(["周杰伦", "Taylor Swift", "毛不易", "薛之谦"]),
    ("hobby", "current_watching"): lambda: random.choice(["一部韩剧", "一部国产剧", "综艺", "动漫"]),
    ("hobby", "sport_type"): lambda: random.choice(["跑步", "游泳", "篮球", "羽毛球", "瑜伽"]),
    ("hobby", "instrument"): lambda: random.choice(["吉他", "钢琴", "尤克里里"]),
    ("hobby", "art_skill"): lambda: random.choice(["画画", "摄影", "手工", "烘焙"]),
    ("hobby", "collection_type"): lambda: random.choice(["手办", "盲盒", "球鞋", "唱片"]),
    ("emotion", "long_term_state"): lambda: random.choice(["比较乐观", "有点内向", "挺外向", "慢热"]),
}


def _generate_messages_for_missing_fields(persona_id, categories=None, template_id=None):
    """
    根据 personas 和 user_facts 中缺失的字段，生成针对性的对话内容

    参数:
        persona_id: 用户ID
        categories: 指定要填充的类别列表，如 ["food", "living"]，为 None 则填充所有
        template_id: 用户类型模板 ID，如 "young_worker", "student"
    """
    import random

    conn = get_db_connection()

    # 1. 获取 personas 表中的空字段
    persona_row = execute_query(conn, "SELECT * FROM personas WHERE id=?", (persona_id,), fetch_one=True)
    if not persona_row:
        conn.close()
        return []

    persona = row_to_dict(persona_row)
    missing_persona_fields = []
    for field in ["age", "city", "occupation", "education", "family_status", "interests"]:
        val = persona.get(field, "")
        if not val or val.strip() == "":
            missing_persona_fields.append(field)

    # 2. 获取 user_facts 中已有的 (category, key) 组合
    fact_rows = execute_query(conn,
        "SELECT DISTINCT category, fact_key FROM user_facts WHERE persona_id=? AND is_active=1",
        (persona_id,), fetch_all=True)
    existing_facts = set((r["category"], r["fact_key"]) for r in fact_rows)
    conn.close()

    # 3. 获取模板配置（如果指定了模板）
    template = _get_persona_template(template_id) if template_id else None
    template_facts = template.get("facts", {}) if template else {}
    template_categories = template.get("categories", []) if template else []

    # 如果指定了模板但没指定 categories，使用模板的 categories
    if template and not categories:
        categories = template_categories

    # 4. 定义需要覆盖的 fact keys（按类别分组，使用新 key 名）
    all_fact_keys_by_category = {
        "food": [("food", "favorite_food"), ("food", "dietary_restriction"), ("food", "breakfast_habit"), ("food", "lunch_habit")],
        "preference": [("preference", "favorite_drink"), ("preference", "weekend_activity")],
        "living": [("living", "city"), ("living", "housing_type"), ("living", "commute_method"), ("living", "commute_duration")],
        "family": [("family", "family_structure"), ("family", "sibling"), ("family", "hometown")],
        "relationship": [("relationship", "romantic_status"), ("relationship", "social_tendency"), ("relationship", "social_circle_depth"), ("relationship", "person")],
        "work": [("work", "daily_schedule"), ("work", "work_stress"), ("work", "colleague_relation")],
        "health": [("health", "sleep_habit"), ("health", "stress_relief"), ("health", "exercise_type")],
        "hobby": [("hobby", "favorite_movie"), ("hobby", "favorite_singer"), ("hobby", "current_watching"), ("hobby", "instrument"), ("hobby", "collection_type")],
        "emotion": [("emotion", "long_term_state")],
        "pet": [("pet", "name"), ("pet", "personality")],
    }

    # 根据指定的 categories 筛选
    if categories:
        target_fact_keys = []
        for cat in categories:
            target_fact_keys.extend(all_fact_keys_by_category.get(cat, []))
    else:
        # 默认填充所有类别
        target_fact_keys = []
        for keys in all_fact_keys_by_category.values():
            target_fact_keys.extend(keys)

    missing_fact_keys = [k for k in target_fact_keys if k not in existing_facts]

    # 5. 生成对话消息
    messages = []

    # 5.1 补充 personas 字段
    for field in missing_persona_fields:
        if field in PERSONA_FIELD_TEMPLATES:
            templates = PERSONA_FIELD_TEMPLATES[field]
            # 优先使用模板的值生成器
            if template and field in template.get("profile", {}):
                gen = template["profile"][field]
                val = gen() if callable(gen) else gen
            elif field in RANDOM_VALUES:
                val = RANDOM_VALUES[field]()
            else:
                continue
            msg = random.choice(templates).format(val=val)
            messages.append(msg)

    # 5.2 补充 user_facts 字段
    for cat_key in missing_fact_keys:
        if cat_key in FACT_KEY_TEMPLATES:
            templates = FACT_KEY_TEMPLATES[cat_key]
            # 优先使用模板的值生成器
            if cat_key in template_facts:
                gen = template_facts[cat_key]
                val = gen() if callable(gen) else gen
            elif cat_key in RANDOM_VALUES:
                val = RANDOM_VALUES[cat_key]()
            else:
                continue
            msg = random.choice(templates).format(val=val)
            messages.append(msg)

    # 6. 添加一些情感类消息（不检查缺失，用于丰富对话）
    emotion_messages = [
        "今天心情还不错",
        "最近工作有点忙",
        "本来想早点下班，结果加班到很晚",
        "周末终于可以休息了",
    ]
    messages.extend(random.sample(emotion_messages, min(2, len(emotion_messages))))

    # 7. 打乱顺序
    random.shuffle(messages)

    cat_str = ",".join(categories) if categories else "all"
    print(f"[AUTO POPULATE] persona={persona_id} template={template_id} categories={cat_str} missing_persona={missing_persona_fields} missing_facts={len(missing_fact_keys)} total_msgs={len(messages)}")

    return messages


def _generate_messages_from_persona(persona, categories, custom_messages=None):
    """根据 persona 生成对话消息"""
    msgs = []

    templates = {
        "基本信息": [
            f"我今年{persona.get('age')}岁",
            f"我在{persona.get('city')}工作，是做{persona.get('occupation')}的",
            f"我老家是{persona.get('hometown')}的",
            f"我是{persona.get('family_status')}",
            f"我现在{persona.get('relationship')}",
        ],
        "兴趣爱好": [
            f"我平时喜欢{persona.get('current_hobby')}",
            f"周末一般会宅在家或者约朋友",
            f"最近在学{persona.get('learning')}，感觉还挺有意思",
            f"我特别喜欢{persona.get('favorite_singer')}的歌，听了好多年了",
        ],
        "宠物": [],
        "饮食偏好": [
            f"我最喜欢喝{persona.get('favorite_drink')}，几乎每天都要来一杯",
            f"我最爱吃{persona.get('favorite_food')}",
            f"我{persona.get('spicy_preference')}",
            "早餐一般吃包子或者面包",
        ],
        "情感": [
            "今天心情还不错",
            "最近工作有点忙，压力挺大的",
            f"我这个人{persona.get('personality')}",
            f"压力大的时候我会{persona.get('stress_relief')}",
            "本来今天想早点下班，结果加班到很晚",
        ],
        "日常": [
            f"我每天{persona.get('work_time')}上班",
            f"中午一般{persona.get('lunch_habit')}",
            f"我住的地方离公司{persona.get('commute')}",
            "最近在追一部剧，超好看",
        ],
        "社交关系": [
            f"我有个好朋友叫{persona.get('best_friend')}，认识好几年了",
            f"和{persona.get('best_friend')}经常一起吃饭",
            "我同事人都挺好的",
            "我朋友不多但都交心",
        ],
    }

    # 宠物类别特殊处理
    if persona.get("pet_type"):
        templates["宠物"] = [
            f"我养了一只{persona.get('pet_type')}，叫{persona.get('pet_name')}",
            f"{persona.get('pet_name')}今年{persona.get('pet_age')}了",
            f"我家{persona.get('pet_name')}{persona.get('pet_trait')}",
            f"今天{persona.get('pet_name')}一直在睡觉",
        ]
    else:
        templates["宠物"] = [
            "我没养宠物，不过挺喜欢猫的",
            "以后有条件想养一只猫",
        ]

    for cat in categories:
        if cat in templates:
            msgs.extend(templates[cat])

    if custom_messages:
        for msg in custom_messages:
            if isinstance(msg, str) and msg.strip():
                msgs.append(msg.strip())

    return msgs


@app.route("/api/growth/tasks", methods=["GET"])
def get_growth_tasks():
    """获取用户成长任务列表，支持分页和搜索"""
    page = request.args.get("page", 1, type=int)
    per_page = request.args.get("per_page", 20, type=int)
    search = request.args.get("search", "").strip()

    per_page = min(per_page, 100)
    offset = (page - 1) * per_page

    conn = get_db_connection()

    if search:
        placeholder = "%s" if USE_MYSQL else "?"
        count_sql = f"SELECT COUNT(*) as total FROM growth_tasks WHERE persona_id LIKE {placeholder}"
        search_param = f"%{search}%"
        total_row = execute_query(conn, count_sql, (search_param,), fetch_one=True)
        total = row_to_dict(total_row)["total"]

        data_sql = f"""
            SELECT t.id, t.persona_id, p.name as persona_name, t.speed, t.status,
                   t.total_messages, t.completed_messages, t.facts_extracted,
                   t.error_message, t.created_at, t.started_at, t.completed_at
            FROM growth_tasks t
            LEFT JOIN personas p ON t.persona_id = p.id
            WHERE t.persona_id LIKE {placeholder}
            ORDER BY t.id DESC
            LIMIT {placeholder} OFFSET {placeholder}
        """
        rows = execute_query(conn, data_sql, (search_param, per_page, offset), fetch_all=True)
    else:
        count_sql = "SELECT COUNT(*) as total FROM growth_tasks"
        total_row = execute_query(conn, count_sql, fetch_one=True)
        total = row_to_dict(total_row)["total"]

        data_sql = """
            SELECT t.id, t.persona_id, p.name as persona_name, t.speed, t.status,
                   t.total_messages, t.completed_messages, t.facts_extracted,
                   t.error_message, t.created_at, t.started_at, t.completed_at
            FROM growth_tasks t
            LEFT JOIN personas p ON t.persona_id = p.id
            ORDER BY t.id DESC
            LIMIT ? OFFSET ?
        """
        rows = execute_query(conn, data_sql, (per_page, offset), fetch_all=True)

    conn.close()

    return jsonify({
        "items": [row_to_dict(r) for r in rows],
        "total": total,
        "page": page,
        "per_page": per_page,
        "total_pages": (total + per_page - 1) // per_page
    })


@app.route("/api/growth/tasks/<int:task_id>/retry", methods=["POST"])
def retry_growth_task(task_id):
    """重新执行失败或 pending 状态的任务"""
    conn = get_db_connection()

    # 获取任务信息
    task = execute_query(conn, """
        SELECT * FROM growth_tasks WHERE id = ?
    """, (task_id,), fetch_one=True)

    if not task:
        conn.close()
        return jsonify({"error": "task not found"}), 404

    task = row_to_dict(task)
    if task["status"] not in ("pending", "failed"):
        conn.close()
        return jsonify({"error": f"cannot retry task in '{task['status']}' status"}), 400

    # 获取剩余未执行的消息
    persona_id = task["persona_id"]
    completed = task["completed_messages"] or 0

    # 从 personas 表获取 device_id
    persona = execute_query(conn, "SELECT device_id FROM personas WHERE id = ?", (persona_id,), fetch_one=True)
    if not persona:
        conn.close()
        return jsonify({"error": "persona not found"}), 404

    device_id = row_to_dict(persona)["device_id"]

    # 获取该用户已发送的消息数量
    sent_count = execute_query(conn, """
        SELECT COUNT(*) as cnt FROM chat_messages WHERE persona_id = ? AND role = 'user'
    """, (persona_id,), fetch_one=True)
    sent_count = row_to_dict(sent_count)["cnt"]

    # 重新获取消息模板（从 persona 重新生成）
    persona_data = execute_query(conn, "SELECT * FROM personas WHERE id = ?", (persona_id,), fetch_one=True)
    persona_data = row_to_dict(persona_data)

    # 更新任务状态为 running
    execute_query(conn, """
        UPDATE growth_tasks SET status = 'running', error_message = NULL, started_at = NOW()
        WHERE id = ?
    """, (task_id,))
    conn.commit()
    conn.close()

    # 启动后台线程继续执行
    t = threading.Thread(target=_growth_worker, args=(task_id,), daemon=True)
    t.start()

    return jsonify({"status": "retrying", "task_id": task_id, "from_message": sent_count})


@app.route("/api/growth/multi_create", methods=["POST"])
def multi_create_growth():
    """
    [已废弃] 请使用 POST /api/personas/batch 代替

    批量创建多个不同配置的用户

    请求参数 (JSON):
        configs: list - 用户配置列表
            [
                {"name": "小明", "speed": "normal", "categories": ["基本信息", "宠物"]},
                {"name": "小红", "speed": "fast", "categories": ["基本信息"], "custom_messages": ["我喜欢画画"]},
                ...
            ]

    返回:
        {"created_count": 3, "tasks": [{"persona_id": "...", "task_id": 1, "name": "..."}, ...]}
    """
    import time as time_module

    data = request.get_json() or {}
    configs = data.get("configs", [])

    if not configs or not isinstance(configs, list):
        return jsonify({"error": "configs required"}), 400
    if len(configs) > 20:
        return jsonify({"error": "max 20 users per batch"}), 400

    timestamp = int(time_module.time())
    results = []

    for i, cfg in enumerate(configs):
        name = cfg.get("name", f"用户{i+1}")
        speed = cfg.get("speed", "normal")
        categories = cfg.get("categories", [])
        custom_messages = cfg.get("custom_messages", [])
        target_api = cfg.get("target_api", "pipi")
        # 兼容旧的 messages 参数
        old_messages = cfg.get("messages", [])

        # 生成唯一 ID
        persona_id = f"auto_{timestamp}_{i+1}"
        device_id = f"TEST_DEV_HUARONG_{timestamp}_{i+1}_{random.randint(1000,9999)}"

        # 调用 LLM 生成完整的用户画像
        profile = _generate_persona_profile(name)

        # 根据 persona 生成消息
        if categories:
            messages = _generate_messages_from_persona({**profile, "name": name}, categories, custom_messages)
        elif old_messages:
            # 兼容旧接口
            messages = old_messages
        else:
            continue

        if not messages:
            continue

        conn = get_db_connection()

        # 创建完整的用户画像（按 personas 表字段）
        fields = [
            "id", "name", "device_id", "nickname", "real_name", "gender", "age",
            "city", "occupation", "education", "family_status", "income_range",
            "spending_style", "spending_desc", "devices", "usage_scenes",
            "core_goal", "short_goal", "long_goal", "pain_points", "constraints",
            "risk_profile", "interests", "language_style", "sample_dialog",
            "info_sources", "decision_style", "relation_pace", "scene_pref",
            "top_expectations", "minefields", "target_api"
        ]
        values = [
            persona_id, name, device_id,
            profile.get("nickname", ""),
            profile.get("real_name", ""),
            profile.get("gender", "女"),
            profile.get("age", ""),
            profile.get("city", ""),
            profile.get("occupation", ""),
            profile.get("education", ""),
            profile.get("family_status", ""),
            profile.get("income_range", ""),
            profile.get("spending_style", ""),
            profile.get("spending_desc", ""),
            profile.get("devices", ""),
            profile.get("usage_scenes", ""),
            profile.get("core_goal", ""),
            profile.get("short_goal", ""),
            profile.get("long_goal", ""),
            profile.get("pain_points", ""),
            profile.get("constraints", ""),
            profile.get("risk_profile", ""),
            profile.get("interests", ""),
            profile.get("language_style", ""),
            profile.get("sample_dialog", ""),
            profile.get("info_sources", ""),
            profile.get("decision_style", ""),
            profile.get("relation_pace", ""),
            profile.get("scene_pref", ""),
            profile.get("top_expectations", ""),
            profile.get("minefields", ""),
            target_api,
        ]

        if USE_MYSQL:
            # 检查是否有非标量值
            for i, (f, v) in enumerate(zip(fields, values)):
                if isinstance(v, (list, tuple, dict)):
                    print(f"[PERSONA CREATE ERROR] field '{f}' has non-scalar value: {type(v)} = {v}", flush=True)
                    values[i] = str(v) if v else ""
            ph = ", ".join(["%s"] * len(fields))
            sql = f"INSERT INTO personas ({', '.join(fields)}) VALUES ({ph})"
            execute_query(conn, sql, tuple(values))
        else:
            ph = ", ".join(["?"] * len(fields))
            execute_query(conn, f"INSERT INTO personas ({', '.join(fields)}) VALUES ({ph})", tuple(values))

        # 创建成长任务
        cur = execute_query(conn,
            "INSERT INTO growth_tasks (persona_id, speed, status, total_messages) VALUES (?,?,?,?)",
            (persona_id, speed, "pending", len(messages)))
        task_id = get_lastrowid(cur)

        # 创建进度记录
        for idx, msg in enumerate(messages):
            if isinstance(msg, str) and msg.strip():
                execute_query(conn,
                    "INSERT INTO growth_progress (task_id, message_index, user_message, status) VALUES (?,?,?,?)",
                    (task_id, idx, msg.strip(), "pending"))

        conn.commit()
        conn.close()

        results.append({
            "persona_id": persona_id,
            "task_id": task_id,
            "name": name,
            "device_id": device_id,
            "speed": speed,
            "message_count": len(messages),
            "persona": {k: v for k, v in profile.items() if not k.startswith("_")}
        })

        # 启动后台线程
        t = threading.Thread(target=_growth_worker, args=(task_id,), daemon=True)
        t.start()

    return jsonify({
        "created_count": len(results),
        "tasks": results
    })


@app.route("/api/growth/auto_fill", methods=["POST"])
def auto_fill_persona():
    """
    根据缺失字段自动填充用户信息

    请求参数 (JSON):
        persona_id: str - 用户ID
        speed: str - 速度 (fast/normal/slow)
        categories: list - 可选，只填充指定类别 ["basic", "preference", "family"]
        template_id: str - 可选，使用模板预填充 ("young_worker", "student", "new_mom", "senior_worker")

    返回:
        {"task_id": 123, "missing_fields": [...], "total_messages": 15}
    """
    data = request.get_json() or {}
    persona_id = data.get("persona_id")
    speed = data.get("speed", "normal")
    categories = data.get("categories")  # 可选：只填充特定类别
    template_id = data.get("template_id")  # 可选：使用模板

    if not persona_id:
        return jsonify({"error": "persona_id required"}), 400

    # 生成基于缺失字段的消息
    messages = _generate_messages_for_missing_fields(persona_id, categories=categories, template_id=template_id)

    if not messages:
        return jsonify({"error": "no missing fields to fill", "total_messages": 0}), 200

    # 创建成长任务
    conn = get_db_connection()
    cur = execute_query(conn,
        "INSERT INTO growth_tasks (persona_id, speed, status, total_messages) VALUES (?,?,?,?)",
        (persona_id, speed, "pending", len(messages)))
    task_id = get_lastrowid(cur)

    for idx, msg in enumerate(messages):
        execute_query(conn,
            "INSERT INTO growth_progress (task_id, message_index, user_message, status) VALUES (?,?,?,?)",
            (task_id, idx, msg, "pending"))

    conn.commit()
    conn.close()

    # 启动后台线程
    t = threading.Thread(target=_growth_worker, args=(task_id,), daemon=True)
    t.start()

    return jsonify({
        "task_id": task_id,
        "persona_id": persona_id,
        "total_messages": len(messages),
        "status": "started"
    })


@app.route("/api/growth/list", methods=["GET"])
def list_growth_tasks():
    """获取成长任务列表"""
    persona_id = request.args.get("persona_id")

    conn = get_db_connection()
    if persona_id:
        rows = execute_query(conn,
            "SELECT * FROM growth_tasks WHERE persona_id=? ORDER BY created_at DESC LIMIT 50",
            (persona_id,), fetch_all=True)
    else:
        rows = execute_query(conn,
            "SELECT * FROM growth_tasks ORDER BY created_at DESC LIMIT 50",
            fetch_all=True)
    conn.close()

    tasks = [row_to_dict(r) for r in rows]
    for t in tasks:
        total = t["total_messages"] or 1
        completed = t["completed_messages"] or 0
        t["progress"] = round(completed / total * 100, 1)

    return jsonify(tasks)


def _growth_worker(task_id):
    """用户成长后台工作线程"""
    import time as time_module

    try:
        conn = get_db_connection()

        # 原子性抢占任务（避免多 worker 同时恢复导致重复执行）
        if USE_MYSQL:
            execute_query(conn,
                "UPDATE growth_tasks SET status=%s, started_at=NOW() WHERE id=%s AND status='pending'",
                ("running", task_id))
        else:
            execute_query(conn,
                "UPDATE growth_tasks SET status=?, started_at=NOW() WHERE id=? AND status='pending'",
                ("running", task_id))
        conn.commit()

        # 检查是否成功抢占（防止多 worker 重复执行）
        task = execute_query(conn, "SELECT * FROM growth_tasks WHERE id=? AND status='running'", (task_id,), fetch_one=True)
        if not task:
            conn.close()
            print(f"[GROWTH] task {task_id} already claimed by another worker, exiting")
            return
        task = row_to_dict(task)
        persona_id = task["persona_id"]
        speed = task.get("speed", "normal")

        # 获取用户画像
        persona_row = execute_query(conn, "SELECT * FROM personas WHERE id=?", (persona_id,), fetch_one=True)
        if not persona_row:
            execute_query(conn,
                "UPDATE growth_tasks SET status=?, error_message=? WHERE id=?",
                ("failed", "persona not found", task_id))
            conn.commit()
            conn.close()
            return
        persona_data = row_to_dict(persona_row)
        device_id = persona_data.get("device_id", persona_id)
        name = persona_data.get("name", persona_id)

        # 获取待处理的消息
        progress_rows = execute_query(conn,
            "SELECT * FROM growth_progress WHERE task_id=? AND status='pending' ORDER BY message_index",
            (task_id,), fetch_all=True)
        conn.close()

        # 速度配置：每条消息之间的间隔（毫秒）
        delay_map = {"fast": 100, "normal": 500, "slow": 1000}
        delay_ms = delay_map.get(speed, 500)

        completed_count = 0
        facts_count = 0

        for prog in progress_rows:
            prog = row_to_dict(prog)
            prog_id = prog["id"]
            msg_index = prog["message_index"]
            user_message = prog["user_message"]

            try:
                # 1. 保存用户消息到聊天历史
                save_chat_msg(persona_id, "user", name, user_message)

                # 2. 调用玩偶接口
                target_api = persona_data.get("target_api", "pipi")
                api_url, api_key, api_headers = get_api_config_by_code(target_api)
                system_prompt = pipi_api.build_system_prompt(persona_data, device_id)
                api_messages = [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_message},
                ]
                result = pipi_api.call_pipi_stream(api_messages, device_id=device_id, api_url=api_url, api_key=api_key, extra_headers=api_headers)
                reply_text = result.get("full_text", "")

                # 3. 保存玩偶回复
                if reply_text:
                    save_chat_msg(persona_id, "pipi", "秋秋", reply_text)

                # 4. 同步提取事实（准确度优先）
                extracted_facts = []
                try:
                    conn2 = get_db_connection()
                    fact_rows = execute_query(conn2,
                        "SELECT id, category, fact_key, entity_name, fact_value FROM user_facts WHERE persona_id=? AND is_active=1",
                        (persona_id,), fetch_all=True)
                    existing_facts = [row_to_dict(r) for r in fact_rows]
                    history_rows = execute_query(conn2,
                        "SELECT role, text FROM chat_messages WHERE persona_id=? ORDER BY id DESC LIMIT 10",
                        (persona_id,), fetch_all=True)
                    conn2.close()

                    chat_history = []
                    for row in reversed(history_rows):
                        prefix = "用户: " if row["role"] == "user" else "秋秋: "
                        chat_history.append(prefix + row["text"])

                    llm_config = get_llm_config()
                    facts = pipi_api.extract_facts_from_message(
                        user_message, persona_data, existing_facts, chat_history=chat_history, **llm_config["fact_extract"])

                    if facts:
                        for f in facts:
                            related_ids = f.pop("related_to", [])
                            if related_ids:
                                f["related_fact_ids"] = ",".join(str(x) for x in related_ids)
                            f["persona_id"] = persona_id
                            f["confidence"] = "implicit"
                            f["source_session"] = f"growth_task_{task_id}"
                            f["source_text"] = user_message
                            _create_fact(f)
                            extracted_facts.append(f)

                    facts_count += len(extracted_facts)
                except Exception as e:
                    print(f"[GROWTH FACT ERROR] task={task_id} idx={msg_index}: {e}")

                # 5. 更新进度记录
                conn3 = get_db_connection()
                execute_query(conn3,
                    "UPDATE growth_progress SET reply_text=?, facts_json=?, status=?, completed_at=NOW() WHERE id=?",
                    (reply_text, json.dumps(extracted_facts, ensure_ascii=False), "completed", prog_id))
                conn3.commit()
                conn3.close()

                completed_count += 1

                # 6. 更新任务计数
                conn4 = get_db_connection()
                execute_query(conn4,
                    "UPDATE growth_tasks SET completed_messages=?, facts_extracted=? WHERE id=?",
                    (completed_count, facts_count, task_id))
                conn4.commit()
                conn4.close()

                print(f"[GROWTH] task={task_id} completed {msg_index+1}: facts={len(extracted_facts)}")

            except Exception as e:
                # 标记单条消息失败
                conn5 = get_db_connection()
                execute_query(conn5,
                    "UPDATE growth_progress SET status=?, error_message=? WHERE id=?",
                    ("failed", str(e), prog_id))
                conn5.commit()
                conn5.close()
                print(f"[GROWTH ERROR] task={task_id} idx={msg_index}: {e}")

            # 延迟
            if delay_ms > 0:
                time_module.sleep(delay_ms / 1000.0)

        # 任务完成
        conn6 = get_db_connection()
        execute_query(conn6,
            "UPDATE growth_tasks SET status=?, completed_at=NOW(), completed_messages=?, facts_extracted=? WHERE id=?",
            ("completed", completed_count, facts_count, task_id))
        conn6.commit()
        conn6.close()
        print(f"[GROWTH DONE] task={task_id} messages={completed_count} facts={facts_count}")

    except Exception as e:
        import traceback
        print(f"[GROWTH FATAL] task={task_id}: {e}\n{traceback.format_exc()}")
        try:
            conn7 = get_db_connection()
            execute_query(conn7,
                "UPDATE growth_tasks SET status=?, error_message=? WHERE id=?",
                ("failed", str(e), task_id))
            conn7.commit()
            conn7.close()
        except:
            pass


# ─── 测试用例管理 API ─────────────────────────────────────

# ─── 异步任务管理（数据库存储，支持多 worker）─────────────────

def _save_async_task(task_id: str, task_type: str, data: dict):
    """保存任务状态到数据库"""
    conn = get_db_connection()
    config_json = json.dumps({k: v for k, v in data.items() if k in ("persona_id", "dimension_codes", "count_per_dimension", "clear_existing", "case_ids", "dimension_code", "status_filter")}, ensure_ascii=False)
    progress_json = json.dumps(data.get("progress", {}), ensure_ascii=False)
    result_json = json.dumps({k: v for k, v in data.items() if k in ("cases_created", "cases_executed", "cases_evaluated", "errors")}, ensure_ascii=False)

    execute_query(conn, """
        INSERT INTO async_tasks (id, task_type, status, persona_id, config_json, progress_json, result_json, error_message)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
        ON DUPLICATE KEY UPDATE
            status = VALUES(status),
            progress_json = VALUES(progress_json),
            result_json = VALUES(result_json),
            error_message = VALUES(error_message)
    """, (task_id, task_type, data.get("status", "running"), data.get("persona_id", ""),
          config_json, progress_json, result_json, data.get("error_message", "")))
    conn.commit()
    conn.close()


def _load_async_task(task_id: str) -> dict:
    """从数据库加载任务状态"""
    conn = get_db_connection()
    row = execute_query(conn, "SELECT * FROM async_tasks WHERE id = %s", (task_id,), fetch_one=True)
    conn.close()
    if not row:
        return None
    row = row_to_dict(row)
    task = {
        "status": row["status"],
        "persona_id": row.get("persona_id", ""),
        "error_message": row.get("error_message", ""),
    }
    if row.get("config_json"):
        task.update(json.loads(row["config_json"]))
    if row.get("progress_json"):
        task["progress"] = json.loads(row["progress_json"])
    if row.get("result_json"):
        task.update(json.loads(row["result_json"]))
    return task


def _update_async_task(task_id: str, updates: dict):
    """更新任务状态"""
    conn = get_db_connection()
    progress_json = json.dumps(updates.get("progress", {}), ensure_ascii=False) if "progress" in updates else None
    result_json = json.dumps({k: v for k, v in updates.items() if k in ("cases_created", "cases_executed", "cases_evaluated", "errors")}, ensure_ascii=False)

    if progress_json:
        execute_query(conn, """
            UPDATE async_tasks SET status=%s, progress_json=%s, result_json=%s, error_message=%s WHERE id=%s
        """, (updates.get("status", "running"), progress_json, result_json, updates.get("error_message", ""), task_id))
    else:
        execute_query(conn, """
            UPDATE async_tasks SET status=%s, result_json=%s, error_message=%s WHERE id=%s
        """, (updates.get("status", "running"), result_json, updates.get("error_message", ""), task_id))
    conn.commit()
    conn.close()


# 内存缓存（单 worker 内快速访问，跨 worker 从数据库读）
_generate_tasks = {}
_execute_tasks = {}
_evaluate_tasks = {}
_test_tasks = {}  # 新版测试任务缓存


# ═══════════════════════════════════════════════════════════════════════════════
# 测试任务管理 API（新架构）
# ═══════════════════════════════════════════════════════════════════════════════

@app.route("/api/test_tasks", methods=["GET"])
def list_test_tasks():
    """
    获取测试任务列表
    参数:
        status: pending/running/executed/evaluating/completed/failed (可选)
        limit: 返回数量，默认20
    """
    status = request.args.get("status", "")
    limit = int(request.args.get("limit", 20))

    conn = get_db_connection()
    sql = "SELECT * FROM test_tasks WHERE 1=1"
    params = []

    if status:
        sql += " AND status = %s"
        params.append(status)

    sql += " ORDER BY created_at DESC LIMIT %s"
    params.append(limit)

    rows = execute_query(conn, sql, params, fetch_all=True)
    conn.close()

    tasks = []
    for row in rows:
        row = row_to_dict(row)
        task = {
            "id": row["id"],
            "task_id": row["task_id"],
            "name": row.get("name", ""),
            "persona_id": row["persona_id"],
            "device_id": row["device_id"],
            "dimension_codes": json.loads(row["dimension_codes"]) if row.get("dimension_codes") else [],
            "case_ids": json.loads(row["case_ids"]) if row.get("case_ids") else [],
            "status": row["status"],
            "progress_total": row.get("progress_total", 0),
            "progress_done": row.get("progress_done", 0),
            "created_at": str(row.get("created_at", "")),
            "started_at": str(row.get("started_at", "")) if row.get("started_at") else None,
            "completed_at": str(row.get("completed_at", "")) if row.get("completed_at") else None,
            "error_message": row.get("error_message"),
        }
        tasks.append(task)

    return jsonify(tasks)


@app.route("/api/test_tasks", methods=["POST"])
def create_test_task():
    """
    创建测试任务
    请求参数:
        name: 任务名称
        persona_id: 用户ID
        device_id: 设备ID
        dimension_codes: ["A1","B2"] - 选择的维度
        priorities: ["P0","P1"] - 优先级筛选（可选，数组）
    """
    data = request.get_json() or {}
    name = data.get("name", "")
    persona_id = data.get("persona_id")
    device_id = data.get("device_id")
    dimension_codes = data.get("dimension_codes", [])
    priorities = data.get("priorities", [])
    target_api = data.get("target_api", "pipi")

    if not persona_id or not device_id:
        return jsonify({"error": "persona_id and device_id required"}), 400

    conn = get_db_connection()

    # 根据用户、维度和优先级获取用例
    sql = "SELECT id, case_id FROM test_cases WHERE persona_id = %s"
    params = [persona_id]
    if dimension_codes:
        placeholders = ",".join(["%s"] * len(dimension_codes))
        sql += f" AND dimension_code IN ({placeholders})"
        params.extend(dimension_codes)
    if priorities:
        placeholders = ",".join(["%s"] * len(priorities))
        sql += f" AND priority IN ({placeholders})"
        params.extend(priorities)
    sql += " ORDER BY dimension_code, case_id"
    cases = execute_query(conn, sql, params, fetch_all=True)

    cases = [row_to_dict(c) for c in cases]
    case_ids = [c["id"] for c in cases]

    if not case_ids:
        conn.close()
        return jsonify({"error": "no test cases found for selected dimensions"}), 400

    # 生成任务ID: 用户ID-日期时间
    task_id = f"{persona_id}-{datetime.datetime.now().strftime('%Y%m%d%H%M')}"

    if not name:
        name = f"测试任务 {datetime.datetime.now().strftime('%m-%d %H:%M')}"

    # 插入任务记录
    cursor = execute_query(conn,
        """INSERT INTO test_tasks (task_id, name, persona_id, device_id, dimension_codes, case_ids, status, progress_total, target_api)
           VALUES (%s, %s, %s, %s, %s, %s, 'pending', %s, %s)""",
        (task_id, name, persona_id, device_id, json.dumps(dimension_codes), json.dumps(case_ids), len(case_ids), target_api))
    conn.commit()
    task_db_id = get_lastrowid(cursor)

    # 创建 test_results 记录（pending 状态）
    for case_id in case_ids:
        execute_query(conn,
            "INSERT INTO test_results (task_id, case_id, status) VALUES (%s, %s, 'pending')",
            (task_db_id, case_id))
    conn.commit()
    conn.close()

    return jsonify({
        "id": task_db_id,
        "task_id": task_id,
        "name": name,
        "case_count": len(case_ids),
        "status": "pending"
    })


@app.route("/api/test_tasks/<int:task_id>", methods=["GET"])
def get_test_task(task_id):
    """获取测试任务详情"""
    conn = get_db_connection()
    row = execute_query(conn, "SELECT * FROM test_tasks WHERE id = %s", (task_id,), fetch_one=True)
    conn.close()

    if not row:
        return jsonify({"error": "task not found"}), 404

    row = row_to_dict(row)
    return jsonify({
        "id": row["id"],
        "task_id": row["task_id"],
        "name": row.get("name", ""),
        "persona_id": row["persona_id"],
        "device_id": row["device_id"],
        "dimension_codes": json.loads(row["dimension_codes"]) if row.get("dimension_codes") else [],
        "case_ids": json.loads(row["case_ids"]) if row.get("case_ids") else [],
        "status": row["status"],
        "progress_total": row.get("progress_total", 0),
        "progress_done": row.get("progress_done", 0),
        "created_at": str(row.get("created_at", "")),
        "started_at": str(row.get("started_at", "")) if row.get("started_at") else None,
        "completed_at": str(row.get("completed_at", "")) if row.get("completed_at") else None,
        "error_message": row.get("error_message"),
    })


@app.route("/api/test_tasks/<int:task_id>", methods=["DELETE"])
def delete_test_task(task_id):
    """删除测试任务及其结果"""
    conn = get_db_connection()

    # 检查任务是否存在
    row = execute_query(conn, "SELECT status FROM test_tasks WHERE id = %s", (task_id,), fetch_one=True)
    if not row:
        conn.close()
        return jsonify({"error": "task not found"}), 404

    row = row_to_dict(row)
    if row["status"] == "running":
        conn.close()
        return jsonify({"error": "cannot delete running task"}), 400

    # 删除关联的结果
    execute_query(conn, "DELETE FROM test_results WHERE task_id = %s", (task_id,))
    # 删除任务
    execute_query(conn, "DELETE FROM test_tasks WHERE id = %s", (task_id,))
    conn.commit()
    conn.close()

    return jsonify({"success": True})


@app.route("/api/test_tasks/<int:task_id>/terminate", methods=["POST"])
def terminate_test_task(task_id):
    """终止运行中的测试任务"""
    conn = get_db_connection()

    row = execute_query(conn, "SELECT status FROM test_tasks WHERE id = %s", (task_id,), fetch_one=True)
    if not row:
        conn.close()
        return jsonify({"error": "task not found"}), 404

    row = row_to_dict(row)
    if row["status"] not in ("running", "evaluating"):
        conn.close()
        return jsonify({"error": f"task is not running, status: {row['status']}"}), 400

    execute_query(conn, "UPDATE test_tasks SET status = 'cancelled', error_message = 'cancelled by user' WHERE id = %s", (task_id,))
    conn.commit()
    conn.close()

    return jsonify({"success": True, "message": "task cancelled"})


@app.route("/api/test_tasks/<int:task_id>/execute", methods=["POST"])
def execute_test_task(task_id):
    """执行测试任务"""
    conn = get_db_connection()
    row = execute_query(conn, "SELECT * FROM test_tasks WHERE id = %s", (task_id,), fetch_one=True)
    if not row:
        conn.close()
        return jsonify({"error": "task not found"}), 404

    row = row_to_dict(row)
    if row["status"] not in ("pending", "executed", "completed", "failed"):
        conn.close()
        return jsonify({"error": f"task status is {row['status']}, cannot execute"}), 400

    # 更新状态为 running
    execute_query(conn, "UPDATE test_tasks SET status = 'running', started_at = NOW(), progress_done = 0 WHERE id = %s", (task_id,))
    # 重置结果状态
    execute_query(conn, "UPDATE test_results SET status = 'pending', actual_output = NULL, executed_at = NULL WHERE task_id = %s", (task_id,))
    conn.commit()
    conn.close()

    # 启动后台线程
    t = threading.Thread(target=_execute_task_worker, args=(task_id,))
    t.daemon = True
    t.start()

    return jsonify({"status": "running", "task_id": task_id})


def _execute_task_worker(task_id):
    """后台执行测试任务"""
    try:
        conn = get_db_connection()

        # 获取任务信息
        task = execute_query(conn, "SELECT * FROM test_tasks WHERE id = %s", (task_id,), fetch_one=True)
        task = row_to_dict(task)
        persona_id = task["persona_id"]
        device_id = task["device_id"]
        target_api = task.get("target_api", "pipi")

        # 获取待执行的结果记录
        results = execute_query(conn,
            "SELECT r.id, r.case_id, c.case_id as case_code, c.input_text FROM test_results r "
            "JOIN test_cases c ON r.case_id = c.id WHERE r.task_id = %s ORDER BY c.dimension_code, c.case_id",
            (task_id,), fetch_all=True)
        results = [row_to_dict(r) for r in results]

        done = 0
        for result in results:
            case_code = result["case_code"]
            print(f"[TASK-EXEC] {task_id} executing {case_code}...", flush=True)

            try:
                # 解析多轮对话
                rounds = _parse_input_rounds(result.get("input_text", ""))
                if not rounds:
                    execute_query(conn, "UPDATE test_results SET status = 'error' WHERE id = %s", (result["id"],))
                    conn.commit()
                    continue

                # 逐轮发送
                all_replies = []
                has_error = False
                for i, msg in enumerate(rounds):
                    import requests as req
                    resp = req.post(
                        "http://127.0.0.1:8080/api/test/chat",
                        json={"persona_id": persona_id, "device_id": device_id, "message": msg, "extract_facts": True, "target_api": target_api},
                        timeout=120
                    )
                    r = resp.json()
                    if r.get("error"):
                        print(f"[TASK-EXEC ERROR] {case_code} R{i+1}: {r.get('error')}", flush=True)
                        has_error = True
                        break
                    reply = r.get("reply", "")
                    ttfb = r.get("ttfb_ms")
                    all_replies.append(f"【R{i+1}】秋秋：{reply}")
                    print(f"[TASK-EXEC] {case_code} R{i+1}: TTFB={ttfb}ms", flush=True)

                if not has_error and all_replies:
                    actual_output = "\n".join(all_replies)
                    execute_query(conn,
                        "UPDATE test_results SET actual_output = %s, executed_at = NOW(), status = 'executed' WHERE id = %s",
                        (actual_output, result["id"]))
                else:
                    execute_query(conn, "UPDATE test_results SET status = 'error' WHERE id = %s", (result["id"],))

            except Exception as e:
                print(f"[TASK-EXEC ERROR] {case_code}: {e}", flush=True)
                execute_query(conn, "UPDATE test_results SET status = 'error' WHERE id = %s", (result["id"],))

            done += 1
            execute_query(conn, "UPDATE test_tasks SET progress_done = %s WHERE id = %s", (done, task_id))
            conn.commit()

        # 完成
        execute_query(conn, "UPDATE test_tasks SET status = 'executed', completed_at = NOW() WHERE id = %s", (task_id,))
        conn.commit()
        conn.close()
        print(f"[TASK-EXEC] {task_id} completed", flush=True)

    except Exception as e:
        import traceback
        print(f"[TASK-EXEC FATAL] {task_id}: {e}\n{traceback.format_exc()}", flush=True)
        try:
            conn = get_db_connection()
            execute_query(conn, "UPDATE test_tasks SET status = 'failed', error_message = %s WHERE id = %s", (str(e), task_id))
            conn.commit()
            conn.close()
        except:
            pass


@app.route("/api/test_tasks/<int:task_id>/evaluate", methods=["POST"])
def evaluate_test_task(task_id):
    """评测测试任务"""
    conn = get_db_connection()
    row = execute_query(conn, "SELECT * FROM test_tasks WHERE id = %s", (task_id,), fetch_one=True)
    if not row:
        conn.close()
        return jsonify({"error": "task not found"}), 404

    row = row_to_dict(row)
    if row["status"] not in ("executed", "completed", "failed"):
        conn.close()
        return jsonify({"error": f"task status is {row['status']}, need executed status to evaluate"}), 400

    # 统计待评测的数量
    eval_count = execute_query(conn,
        "SELECT COUNT(*) as cnt FROM test_results WHERE task_id = %s AND status = 'executed'",
        (task_id,), fetch_one=True)
    eval_total = eval_count["cnt"] if eval_count else 0

    # 更新状态为 evaluating，设置进度
    execute_query(conn, "UPDATE test_tasks SET status = 'evaluating', progress_done = 0, progress_total = %s WHERE id = %s", (eval_total, task_id))
    conn.commit()
    conn.close()

    # 启动后台线程
    t = threading.Thread(target=_evaluate_task_worker, args=(task_id,))
    t.daemon = True
    t.start()

    return jsonify({"status": "evaluating", "task_id": task_id})


def _load_user_facts(conn, persona_id):
    """加载用户活跃事实（按分类分组），用于评测上下文"""
    facts = execute_query(conn,
        "SELECT id, category, fact_key, entity_name, fact_value FROM user_facts "
        "WHERE persona_id = %s AND is_active = 1",
        (persona_id,), fetch_all=True)
    return [row_to_dict(f) for f in facts] if facts else []


def _evaluate_task_worker(task_id):
    """后台评测测试任务"""
    try:
        conn = get_db_connection()

        # 获取 persona_id
        trow = execute_query(conn, "SELECT persona_id FROM test_tasks WHERE id = %s", (task_id,), fetch_one=True)
        persona_id = trow["persona_id"] if trow else None
        user_facts = _load_user_facts(conn, persona_id) if persona_id else []

        # 获取已执行的结果
        results = execute_query(conn,
            """SELECT r.id, r.actual_output, c.case_id, c.dimension_code, c.title, c.test_point, c.input_text,
                      c.expected_output, c.failure_flags, c.score_2_desc, c.score_6_desc, c.score_10_desc
               FROM test_results r
               JOIN test_cases c ON r.case_id = c.id
               WHERE r.task_id = %s AND r.status = 'executed'
               ORDER BY c.dimension_code, c.case_id""",
            (task_id,), fetch_all=True)
        results = [row_to_dict(r) for r in results]

        done = 0
        passed = 0
        failed = 0

        for result in results:
            case_code = result["case_id"]
            print(f"[TASK-EVAL] {task_id} evaluating {case_code}...", flush=True)

            try:
                # 构建评测用例数据
                case_data = {
                    "case_id": case_code,
                    "dimension_code": result["dimension_code"],
                    "title": result["title"],
                    "test_point": result.get("test_point", ""),
                    "input_text": result["input_text"],
                    "expected_output": result["expected_output"],
                    "actual_output": result["actual_output"],
                    "failure_flags": result["failure_flags"],
                    "score_2_desc": result["score_2_desc"],
                    "score_6_desc": result["score_6_desc"],
                    "score_10_desc": result["score_10_desc"],
                }

                llm_config = get_llm_config()
                eval_result = pipi_api.evaluate_test_case(case_data, **llm_config["eval_case"], user_facts=user_facts)
                score = eval_result.get("score")
                reason = eval_result.get("deduction_reason", "")
                status = eval_result.get("status", "evaluated")

                if score is not None:
                    execute_query(conn,
                        "UPDATE test_results SET score = %s, deduction_reason = %s, status = %s WHERE id = %s",
                        (score, reason, status, result["id"]))
                    if status == "passed":
                        passed += 1
                    else:
                        failed += 1

            except Exception as e:
                print(f"[TASK-EVAL ERROR] {case_code}: {e}", flush=True)

            done += 1
            execute_query(conn, "UPDATE test_tasks SET progress_done = %s WHERE id = %s", (done, task_id))
            conn.commit()

        # 完成
        execute_query(conn, "UPDATE test_tasks SET status = 'completed', completed_at = NOW() WHERE id = %s", (task_id,))
        conn.commit()
        conn.close()
        print(f"[TASK-EVAL] {task_id} completed, passed={passed}, failed={failed}", flush=True)

    except Exception as e:
        import traceback
        print(f"[TASK-EVAL FATAL] {task_id}: {e}\n{traceback.format_exc()}", flush=True)
        try:
            conn = get_db_connection()
            execute_query(conn, "UPDATE test_tasks SET status = 'failed', error_message = %s WHERE id = %s", (str(e), task_id))
            conn.commit()
            conn.close()
        except:
            pass


@app.route("/api/test_tasks/<int:task_id>/progress", methods=["GET"])
def get_test_task_progress(task_id):
    """获取测试任务进度"""
    conn = get_db_connection()
    row = execute_query(conn, "SELECT status, progress_total, progress_done, error_message FROM test_tasks WHERE id = %s", (task_id,), fetch_one=True)
    conn.close()

    if not row:
        return jsonify({"error": "task not found"}), 404

    row = row_to_dict(row)
    return jsonify({
        "status": row["status"],
        "progress_total": row["progress_total"],
        "progress_done": row["progress_done"],
        "error_message": row.get("error_message"),
    })


@app.route("/api/test_results/<int:result_id>/reevaluate", methods=["POST"])
def reevaluate_single_result(result_id):
    """
    重新评测单条测试结果
    用于评测失败/超时的用例
    """
    conn = get_db_connection()

    # 获取结果及关联的用例信息（含 persona_id）
    row = execute_query(conn, """
        SELECT r.id, r.actual_output, r.status, r.task_id,
               c.case_id, c.dimension_code, c.title, c.test_point, c.input_text,
               c.expected_output, c.failure_flags, c.score_2_desc, c.score_6_desc, c.score_10_desc
        FROM test_results r
        JOIN test_cases c ON r.case_id = c.id
        WHERE r.id = %s
    """, (result_id,), fetch_one=True)

    if not row:
        conn.close()
        return jsonify({"error": "result not found"}), 404

    result = row_to_dict(row)

    if not result.get("actual_output"):
        conn.close()
        return jsonify({"error": "no actual_output, need execute first"}), 400

    # 获取用户事实
    trow2 = execute_query(conn, "SELECT persona_id FROM test_tasks WHERE id = %s", (result["task_id"],), fetch_one=True)
    persona_id = trow2["persona_id"] if trow2 else None
    user_facts = _load_user_facts(conn, persona_id) if persona_id else []

    case_code = result["case_id"]
    print(f"[RE-EVAL] Re-evaluating {case_code} (result_id={result_id})...", flush=True)

    try:
        # 构建评测用例数据
        case_data = {
            "case_id": case_code,
            "dimension_code": result["dimension_code"],
            "title": result["title"],
                    "test_point": result.get("test_point", ""),
            "input_text": result["input_text"],
            "expected_output": result["expected_output"],
            "actual_output": result["actual_output"],
            "failure_flags": result["failure_flags"],
            "score_2_desc": result["score_2_desc"],
            "score_6_desc": result["score_6_desc"],
            "score_10_desc": result["score_10_desc"],
        }

        llm_config = get_llm_config()
        eval_result = pipi_api.evaluate_test_case(case_data, **llm_config["eval_case"], user_facts=user_facts)
        score = eval_result.get("score")
        reason = eval_result.get("deduction_reason", "")
        status = eval_result.get("status", "evaluated")

        if score is not None:
            execute_query(conn,
                "UPDATE test_results SET score = %s, deduction_reason = %s, status = %s WHERE id = %s",
                (score, reason, status, result_id))
            conn.commit()
            print(f"[RE-EVAL] {case_code} => score={score}, status={status}", flush=True)
            conn.close()
            return jsonify({
                "success": True,
                "case_id": case_code,
                "score": score,
                "deduction_reason": reason,
                "status": status
            })
        else:
            conn.close()
            return jsonify({
                "success": False,
                "case_id": case_code,
                "error": reason or "评测失败"
            }), 500

    except Exception as e:
        conn.close()
        print(f"[RE-EVAL ERROR] {case_code}: {e}", flush=True)
        return jsonify({"error": str(e)}), 500


@app.route("/api/test_tasks/<int:task_id>/reevaluate_failed", methods=["POST"])
def reevaluate_failed_results(task_id):
    """
    重新评测任务中所有失败的用例（score 为 null 且 status 为 executed 或 pending）
    """
    conn = get_db_connection()

    # 检查任务存在
    task_row = execute_query(conn, "SELECT id FROM test_tasks WHERE id = %s", (task_id,), fetch_one=True)
    if not task_row:
        conn.close()
        return jsonify({"error": "task not found"}), 404

    # 找出需要重新评测的结果
    reval_all = request.args.get("all", "0") == "1"
    if reval_all:
        # 全部重评
        results = execute_query(conn, """
            SELECT r.id, c.case_id
            FROM test_results r
            JOIN test_cases c ON r.case_id = c.id
            WHERE r.task_id = %s AND r.actual_output IS NOT NULL
        """, (task_id,), fetch_all=True)
    else:
        # 仅重评失败/未评的
        results = execute_query(conn, """
            SELECT r.id, c.case_id
            FROM test_results r
            JOIN test_cases c ON r.case_id = c.id
            WHERE r.task_id = %s AND r.actual_output IS NOT NULL
              AND (r.score IS NULL OR r.status IN ('pending', 'executed', 'failed'))
        """, (task_id,), fetch_all=True)
    results = [row_to_dict(r) for r in results]

    if not results:
        conn.close()
        return jsonify({"message": "no results to reevaluate", "count": 0})

    conn.close()

    # 启动后台线程重新评测
    t = threading.Thread(target=_reevaluate_failed_worker, args=(task_id, [r["id"] for r in results]))
    t.daemon = True
    t.start()

    return jsonify({
        "status": "reevaluating",
        "task_id": task_id,
        "count": len(results),
        "case_ids": [r["case_id"] for r in results]
    })


def _reevaluate_failed_worker(task_id, result_ids):
    """后台重新评测失败用例"""
    print(f"[RE-EVAL BATCH] Starting re-evaluation for task {task_id}, {len(result_ids)} results", flush=True)

    conn = get_db_connection()

    # 加载用户事实（所有结果共用）
    trow3 = execute_query(conn, "SELECT persona_id FROM test_tasks WHERE id = %s", (task_id,), fetch_one=True)
    persona_id = trow3["persona_id"] if trow3 else None
    user_facts = _load_user_facts(conn, persona_id) if persona_id else []

    success_count = 0
    fail_count = 0

    for result_id in result_ids:
        row = execute_query(conn, """
            SELECT r.id, r.actual_output, c.case_id, c.dimension_code, c.title, c.test_point, c.input_text,
                   c.expected_output, c.failure_flags, c.score_2_desc, c.score_6_desc, c.score_10_desc
            FROM test_results r
            JOIN test_cases c ON r.case_id = c.id
            WHERE r.id = %s
        """, (result_id,), fetch_one=True)

        if not row:
            continue

        result = row_to_dict(row)
        case_code = result["case_id"]
        print(f"[RE-EVAL BATCH] {task_id} re-evaluating {case_code}...", flush=True)

        try:
            case_data = {
                "case_id": case_code,
                "dimension_code": result["dimension_code"],
                "title": result["title"],
                    "test_point": result.get("test_point", ""),
                "input_text": result["input_text"],
                "expected_output": result["expected_output"],
                "actual_output": result["actual_output"],
                "failure_flags": result["failure_flags"],
                "score_2_desc": result["score_2_desc"],
                "score_6_desc": result["score_6_desc"],
                "score_10_desc": result["score_10_desc"],
            }

            llm_config = get_llm_config()
            eval_result = pipi_api.evaluate_test_case(case_data, **llm_config["eval_case"], user_facts=user_facts)
            score = eval_result.get("score")
            reason = eval_result.get("deduction_reason", "")
            status = eval_result.get("status", "evaluated")

            if score is not None:
                execute_query(conn,
                    "UPDATE test_results SET score = %s, deduction_reason = %s, status = %s WHERE id = %s",
                    (score, reason, status, result_id))
                conn.commit()
                success_count += 1
                print(f"[RE-EVAL BATCH] {case_code} => score={score}", flush=True)
            else:
                fail_count += 1
                print(f"[RE-EVAL BATCH] {case_code} failed: {reason}", flush=True)

        except Exception as e:
            fail_count += 1
            print(f"[RE-EVAL BATCH ERROR] {case_code}: {e}", flush=True)

    conn.close()
    print(f"[RE-EVAL BATCH] Task {task_id} completed: success={success_count}, failed={fail_count}", flush=True)


@app.route("/api/test_results", methods=["GET"])
def get_test_results():
    """
    获取测试结果
    参数:
        task_id: 测试任务ID（必需）
        status: 筛选状态（可选）
        cluster_code: 能力簇筛选（可选）
    """
    task_id = request.args.get("task_id")
    status = request.args.get("status", "")
    cluster_code = request.args.get("cluster_code", "")

    if not task_id:
        return jsonify({"error": "task_id required"}), 400

    conn = get_db_connection()
    ph = "%s" if USE_MYSQL else "?"

    sql = """SELECT r.id, r.task_id, r.case_id, r.actual_output, r.executed_at, r.score, r.deduction_reason, r.status,
                    c.case_id as case_code, c.dimension_code, c.title, c.test_point, c.input_text, c.expected_output
             FROM test_results r
             JOIN test_cases c ON r.case_id = c.id
             WHERE r.task_id = """ + ph
    params = [task_id]

    if cluster_code:
        # 获取该能力簇下的所有维度
        dim_rows = execute_query(conn,
            f"SELECT dimension_code FROM test_dimensions WHERE cluster_code = {ph}",
            (cluster_code,), fetch_all=True)
        dim_codes = [row_to_dict(r)["dimension_code"] for r in dim_rows]
        if dim_codes:
            placeholders = ",".join([ph] * len(dim_codes))
            sql += f" AND c.dimension_code IN ({placeholders})"
            params.extend(dim_codes)
        else:
            sql += " AND 1=0"  # 没有维度则返回空

    if status:
        sql += f" AND r.status = {ph}"
        params.append(status)

    sql += " ORDER BY c.dimension_code, c.case_id"

    rows = execute_query(conn, sql, params, fetch_all=True)
    conn.close()

    results = []
    for row in rows:
        row = row_to_dict(row)
        results.append({
            "id": row["id"],
            "task_id": row["task_id"],
            "case_id": row["case_id"],
            "case_code": row["case_code"],
            "dimension_code": row["dimension_code"],
            "title": row["title"],
            "input_text": row["input_text"],
            "expected_output": row["expected_output"],
            "actual_output": row["actual_output"],
            "executed_at": str(row["executed_at"]) if row.get("executed_at") else None,
            "score": row["score"],
            "deduction_reason": row["deduction_reason"],
            "status": row["status"],
        })

    return jsonify(results)


@app.route("/api/test_results/<int:result_id>", methods=["GET"])
def get_single_test_result(result_id):
    """获取单条测试结果"""
    conn = get_db_connection()
    ph = "%s" if USE_MYSQL else "?"
    row = execute_query(conn, f"""
        SELECT r.id, r.task_id, r.case_id, r.actual_output, r.executed_at, r.score, r.deduction_reason, r.status,
               c.case_id as case_code, c.dimension_code, c.title, c.test_point, c.input_text, c.expected_output
        FROM test_results r
        JOIN test_cases c ON r.case_id = c.id
        WHERE r.id = {ph}
    """, (result_id,), fetch_one=True)
    conn.close()

    if not row:
        return jsonify({"error": "result not found"}), 404

    row = row_to_dict(row)
    return jsonify({
        "id": row["id"],
        "task_id": row["task_id"],
        "case_id": row["case_id"],
        "case_code": row["case_code"],
        "dimension_code": row["dimension_code"],
        "title": row["title"],
        "input_text": row["input_text"],
        "expected_output": row["expected_output"],
        "actual_output": row["actual_output"],
        "executed_at": str(row["executed_at"]) if row.get("executed_at") else None,
        "score": row["score"],
        "deduction_reason": row["deduction_reason"],
        "status": row["status"],
    })


def _generate_report_summary(clusters, failed_cases, pass_rate, avg_score):
    """生成测试报告的描述性总结"""
    # 找出表现最差的维度
    worst_clusters = []
    for code, cluster in clusters.items():
        if cluster["rate"] < 80:
            worst_clusters.append((code, cluster["name"], cluster["rate"], cluster["failed"]))
    worst_clusters.sort(key=lambda x: x[2])

    # 统计常见扣分原因
    deduction_keywords = {}
    for case in failed_cases:
        reason = case.get("deduction_reason", "") or ""
        for keyword in ["情感", "共情", "记忆", "上下文", "敷衍", "生硬", "理解", "回应", "引导", "安慰"]:
            if keyword in reason:
                deduction_keywords[keyword] = deduction_keywords.get(keyword, 0) + 1

    top_issues = sorted(deduction_keywords.items(), key=lambda x: -x[1])[:3]

    # 生成总结文本
    if pass_rate >= 90:
        overall = f"整体表现优秀，通过率 {pass_rate}%，平均分 {avg_score:.1f} 分。"
    elif pass_rate >= 70:
        overall = f"整体表现良好，通过率 {pass_rate}%，平均分 {avg_score:.1f} 分，仍有改进空间。"
    else:
        overall = f"整体表现需改进，通过率 {pass_rate}%，平均分 {avg_score:.1f} 分。"

    weak_points = ""
    if worst_clusters:
        weak_names = "、".join([f"{c[1]}({c[2]}%)" for c in worst_clusters[:3]])
        weak_points = f"薄弱能力簇：{weak_names}。"

    issues = ""
    if top_issues:
        issue_names = "、".join([f"{k}({v}次)" for k, v in top_issues])
        issues = f"主要问题：{issue_names}。"

    return overall + weak_points + issues


@app.route("/api/test_report_v2", methods=["GET"])
def generate_test_report_v2():
    """
    按测试任务生成报告
    参数:
        task_id: 测试任务ID（必需）
        format: json(默认) 或 html
    """
    task_id = request.args.get("task_id")
    output_format = request.args.get("format", "html")  # 默认 HTML
    if not task_id:
        return jsonify({"error": "task_id required"}), 400

    conn = get_db_connection()

    # 获取任务信息
    task = execute_query(conn, "SELECT * FROM test_tasks WHERE id = %s", (task_id,), fetch_one=True)
    if not task:
        conn.close()
        return jsonify({"error": "task not found"}), 404
    task = row_to_dict(task)

    # 获取结果统计
    results = execute_query(conn,
        """SELECT r.*, c.case_id as case_code, c.dimension_code, c.title, c.test_point, c.input_text, c.expected_output
           FROM test_results r
           JOIN test_cases c ON r.case_id = c.id
           WHERE r.task_id = %s
           ORDER BY c.dimension_code, c.case_id""",
        (task_id,), fetch_all=True)
    results = [row_to_dict(r) for r in results]

    # 获取维度信息
    dimensions = execute_query(conn,
        "SELECT dimension_code, dimension_name, cluster_code, cluster_name FROM test_dimensions",
        fetch_all=True)
    dim_map = {row_to_dict(d)["dimension_code"]: row_to_dict(d) for d in dimensions}
    conn.close()

    # 统计
    total = len(results)
    evaluated = [r for r in results if r.get("score") is not None]
    passed = [r for r in evaluated if r.get("status") == "passed"]
    failed = [r for r in evaluated if r.get("status") == "failed"]
    avg_score = sum(r["score"] for r in evaluated) / len(evaluated) if evaluated else 0
    pass_rate = round(len(passed) / len(evaluated) * 100, 1) if evaluated else 0

    # 按维度统计
    by_dim = {}
    for r in results:
        dim = r["dimension_code"]
        if dim not in by_dim:
            by_dim[dim] = {"total": 0, "passed": 0, "failed": 0, "scores": [], "cluster": dim_map.get(dim, {}).get("cluster_name", "")}
        by_dim[dim]["total"] += 1
        if r.get("score") is not None:
            by_dim[dim]["scores"].append(r["score"])
            if r.get("status") == "passed":
                by_dim[dim]["passed"] += 1
            else:
                by_dim[dim]["failed"] += 1

    # JSON 格式输出
    if output_format == "json":
        return jsonify({
            "task": {
                "id": task["id"],
                "task_id": task["task_id"],
                "name": task.get("name", ""),
                "status": task["status"],
                "created_at": str(task.get("created_at", "")),
            },
            "summary": {
                "total": total,
                "evaluated": len(evaluated),
                "passed": len(passed),
                "failed": len(failed),
                "pass_rate": pass_rate,
                "avg_score": round(avg_score, 1),
            },
            "by_dimension": {
                dim: {
                    "dimension_name": dim_map.get(dim, {}).get("dimension_name", dim),
                    "cluster_name": dim_map.get(dim, {}).get("cluster_name", ""),
                    "total": stats["total"],
                    "passed": stats["passed"],
                    "failed": stats["failed"],
                    "pass_rate": round(stats["passed"] / (stats["passed"] + stats["failed"]) * 100, 1) if (stats["passed"] + stats["failed"]) else 0,
                    "avg_score": round(sum(stats["scores"]) / len(stats["scores"]), 1) if stats["scores"] else 0,
                }
                for dim, stats in sorted(by_dim.items())
            },
            "results": results,
        })

    # HTML 格式输出
    import re
    report_time = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    task_created = task.get("created_at", "")
    if hasattr(task_created, "strftime"):
        task_created = task_created.strftime("%Y-%m-%d %H:%M:%S")

    # 按 cluster 分组维度
    clusters = {}
    for dim, stats in sorted(by_dim.items()):
        cluster = dim_map.get(dim, {}).get("cluster_code", "X")
        if cluster not in clusters:
            clusters[cluster] = {
                "name": dim_map.get(dim, {}).get("cluster_name", "未知"),
                "dimensions": []
            }
        d_passed = stats["passed"]
        d_failed = stats["failed"]
        d_evaluated = d_passed + d_failed
        d_rate = round(d_passed / d_evaluated * 100) if d_evaluated > 0 else 0
        d_avg = round(sum(stats["scores"]) / len(stats["scores"]), 1) if stats["scores"] else 0
        clusters[cluster]["dimensions"].append({
            "code": dim,
            "name": dim_map.get(dim, {}).get("dimension_name", dim),
            "total": stats["total"],
            "passed": d_passed,
            "failed": d_failed,
            "rate": d_rate,
            "avg": d_avg
        })

    # 能力簇汇总统计
    cluster_summary = {}
    for cluster_code, cluster_data in clusters.items():
        c_total = sum(d["total"] for d in cluster_data["dimensions"])
        c_passed = sum(d["passed"] for d in cluster_data["dimensions"])
        c_failed = sum(d["failed"] for d in cluster_data["dimensions"])
        c_evaluated = c_passed + c_failed
        c_scores = []
        for dim, stats in by_dim.items():
            if dim_map.get(dim, {}).get("cluster_code") == cluster_code:
                c_scores.extend(stats["scores"])
        cluster_summary[cluster_code] = {
            "name": cluster_data["name"],
            "total": c_total,
            "passed": c_passed,
            "failed": c_failed,
            "rate": round(c_passed / c_evaluated * 100) if c_evaluated > 0 else 0,
            "avg": round(sum(c_scores) / len(c_scores), 1) if c_scores else 0
        }

    # 得分分布统计
    score_dist = {"1-3": 0, "4-5": 0, "6-7": 0, "8-10": 0}
    for r in evaluated:
        s = r.get("score", 0)
        if s <= 3:
            score_dist["1-3"] += 1
        elif s <= 5:
            score_dist["4-5"] += 1
        elif s <= 7:
            score_dist["6-7"] += 1
        else:
            score_dist["8-10"] += 1

    # 生成能力簇汇总 HTML
    cluster_rows = ""
    for cluster_code in sorted(cluster_summary.keys()):
        cs = cluster_summary[cluster_code]
        status_class = "good" if cs["rate"] >= 80 else ("warn" if cs["rate"] >= 60 else "bad")
        cluster_rows += f"""
        <tr>
            <td style="font-weight:600">{cs['name']}</td>
            <td>{cs['total']}</td>
            <td class="num-passed">{cs['passed']}</td>
            <td class="num-failed">{cs['failed']}</td>
            <td class="{status_class}">{cs['rate']}%</td>
            <td>{cs['avg']}</td>
        </tr>
        """

    # 生成得分分布 HTML（横向条形图）
    max_count = max(score_dist.values()) if score_dist.values() else 1
    score_bars = ""
    score_colors = {"1-3": "#ff3b30", "4-5": "#ff9500", "6-7": "#34c759", "8-10": "#007aff"}
    score_labels = {"1-3": "差 (1-3分)", "4-5": "中 (4-5分)", "6-7": "良 (6-7分)", "8-10": "优 (8-10分)"}
    for key in ["8-10", "6-7", "4-5", "1-3"]:
        count = score_dist[key]
        pct = round(count / len(evaluated) * 100) if evaluated else 0
        bar_width = round(count / max_count * 100) if max_count > 0 else 0
        score_bars += f"""
        <div style="display:flex;align-items:center;margin-bottom:8px">
            <div style="width:100px;font-size:13px">{score_labels[key]}</div>
            <div style="flex:1;background:#f5f5f7;border-radius:4px;height:24px;margin:0 12px">
                <div style="width:{bar_width}%;background:{score_colors[key]};height:100%;border-radius:4px"></div>
            </div>
            <div style="width:80px;text-align:right;font-size:13px">{count} ({pct}%)</div>
        </div>
        """

    # 生成维度表格 HTML
    dimension_rows = ""
    for cluster_code in sorted(clusters.keys()):
        cluster = clusters[cluster_code]
        for i, d in enumerate(cluster["dimensions"]):
            status_class = "good" if d["rate"] >= 80 else ("warn" if d["rate"] >= 60 else "bad")
            dimension_rows += f"""
            <tr>
                {"<td rowspan='" + str(len(cluster['dimensions'])) + "' class='cluster-cell'>" + cluster['name'] + "</td>" if i == 0 else ""}
                <td>{d['code']}</td>
                <td>{d['name']}</td>
                <td>{d['total']}</td>
                <td class="num-passed">{d['passed']}</td>
                <td class="num-failed">{d['failed']}</td>
                <td class="{status_class}">{d['rate']}%</td>
                <td>{d['avg']}</td>
            </tr>
            """

    # 生成描述性总结
    summary_text = _generate_report_summary(cluster_summary, failed, pass_rate, avg_score)

    # 生成用例详情 HTML（只显示失败用例）
    case_details = ""
    current_dimension = ""
    failed_by_dim = {}
    for r in results:
        if r.get("status") != "failed":
            continue
        dim_code = r.get("dimension_code", "")
        if dim_code not in failed_by_dim:
            failed_by_dim[dim_code] = []
        failed_by_dim[dim_code].append(r)

    for dim_code in sorted(failed_by_dim.keys()):
        dim_name = dim_map.get(dim_code, {}).get("dimension_name", "")
        case_details += f"""
        <div class="dimension-section">
            <div class="dimension-header" onclick="this.parentElement.classList.toggle('collapsed')">
                <span class="toggle-icon">▼</span>
                {dim_code} - {dim_name} ({len(failed_by_dim[dim_code])}个失败)
            </div>
            <div class="dimension-cases">
        """
        for r in failed_by_dim[dim_code]:
            score = r.get("score")
            score_class = "score-fail"
            actual = r.get("actual_output", "") or ""
            actual_display = re.sub(r'\{"emotion":\s*"[^"]*"\}\s*', '', actual)

            case_details += f"""
            <div class="case-card">
                <div class="case-header">
                    <span class="case-id">{r.get('case_code', '')}</span>
                    <span class="case-title">{r.get('title', '')}</span>
                    <span class='badge badge-fail'>失败</span>
                    <span class="{score_class}">{score if score else '-'}/10</span>
                </div>
                <div class="case-body">
                    <div class="case-section"><b>输入:</b><pre>{r.get('input_text', '')[:500]}</pre></div>
                    <div class="case-section"><b>期望:</b><pre>{r.get('expected_output', '')[:300]}</pre></div>
                    <div class="case-section"><b>实际:</b><pre>{actual_display[:500]}</pre></div>
                    {"<div class='case-section deduction'><b>扣分原因:</b> " + r.get('deduction_reason', '') + "</div>" if r.get('deduction_reason') else ""}
                </div>
            </div>
            """
        case_details += "</div></div>"

    if not failed_by_dim:
        case_details = "<div style='padding:20px;text-align:center;color:#34c759'>🎉 全部用例通过，无失败用例</div>"

    html = f"""<!DOCTYPE html>
<html>
<head>
    <meta charset="UTF-8">
    <title>测试报告 - {task.get('task_id', '')}</title>
    <style>
        * {{ margin: 0; padding: 0; box-sizing: border-box; }}
        body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, 'Helvetica Neue', sans-serif; background: linear-gradient(180deg, #f0f2f5 0%, #e8eaed 100%); padding: 30px; color: #1d1d1f; min-height: 100vh; }}
        .container {{ max-width: 1200px; margin: 0 auto; }}
        .header {{ background: linear-gradient(135deg, #6366f1 0%, #8b5cf6 50%, #a855f7 100%); color: white; padding: 40px; border-radius: 20px; margin-bottom: 24px; box-shadow: 0 10px 40px rgba(99,102,241,0.3); position: relative; overflow: hidden; }}
        .header::before {{ content: ''; position: absolute; top: -50%; right: -50%; width: 100%; height: 200%; background: radial-gradient(circle, rgba(255,255,255,0.1) 0%, transparent 60%); }}
        .header h1 {{ font-size: 28px; margin-bottom: 10px; font-weight: 700; text-shadow: 0 2px 4px rgba(0,0,0,0.1); position: relative; }}
        .header .subtitle {{ opacity: 0.9; font-size: 14px; position: relative; }}
        .summary {{ display: grid; grid-template-columns: repeat(5, 1fr); gap: 16px; margin-bottom: 24px; }}
        .summary-card {{ background: white; padding: 24px 20px; border-radius: 16px; text-align: center; box-shadow: 0 4px 20px rgba(0,0,0,0.08); transition: transform 0.2s, box-shadow 0.2s; }}
        .summary-card:hover {{ transform: translateY(-4px); box-shadow: 0 8px 30px rgba(0,0,0,0.12); }}
        .summary-card .value {{ font-size: 36px; font-weight: 700; background: linear-gradient(135deg, #1d1d1f 0%, #4a4a4a 100%); -webkit-background-clip: text; -webkit-text-fill-color: transparent; }}
        .summary-card .label {{ color: #86868b; font-size: 13px; margin-top: 6px; font-weight: 500; }}
        .value.green {{ background: linear-gradient(135deg, #22c55e 0%, #16a34a 100%); -webkit-background-clip: text; -webkit-text-fill-color: transparent; }}
        .value.red {{ background: linear-gradient(135deg, #ef4444 0%, #dc2626 100%); -webkit-background-clip: text; -webkit-text-fill-color: transparent; }}
        .value.blue {{ background: linear-gradient(135deg, #3b82f6 0%, #2563eb 100%); -webkit-background-clip: text; -webkit-text-fill-color: transparent; }}
        .section {{ background: white; border-radius: 16px; padding: 24px; margin-bottom: 24px; box-shadow: 0 4px 20px rgba(0,0,0,0.08); }}
        .section h2 {{ font-size: 18px; margin-bottom: 20px; padding-bottom: 12px; border-bottom: 2px solid #f0f0f5; color: #1d1d1f; font-weight: 600; }}
        .summary-section {{ background: linear-gradient(135deg, #fefefe 0%, #f8fafc 100%); border-left: 4px solid #6366f1; }}
        .summary-section p {{ font-size: 15px; line-height: 1.9; color: #374151; }}
        table {{ width: 100%; border-collapse: separate; border-spacing: 0; font-size: 13px; }}
        th {{ padding: 14px 16px; text-align: left; background: linear-gradient(135deg, #f8fafc 0%, #f1f5f9 100%); font-weight: 600; color: #475569; border-bottom: 2px solid #e2e8f0; }}
        th:first-child {{ border-radius: 8px 0 0 0; }}
        th:last-child {{ border-radius: 0 8px 0 0; }}
        td {{ padding: 12px 16px; border-bottom: 1px solid #f1f5f9; }}
        tr:hover td {{ background: #fafbfc; }}
        .cluster-cell {{ background: linear-gradient(135deg, #f0f0f5 0%, #e8e8ed 100%); font-weight: 600; vertical-align: middle; }}
        .num-passed {{ color: #22c55e; font-weight: 600; }}
        .num-failed {{ color: #ef4444; font-weight: 600; }}
        .good {{ color: #22c55e; font-weight: 700; }}
        .warn {{ color: #f59e0b; font-weight: 700; }}
        .bad {{ color: #ef4444; font-weight: 700; }}
        .dimension-section {{ margin-bottom: 16px; border: 1px solid #e5e7eb; border-radius: 12px; overflow: hidden; transition: box-shadow 0.2s; }}
        .dimension-section:hover {{ box-shadow: 0 4px 12px rgba(0,0,0,0.08); }}
        .dimension-header {{ background: linear-gradient(135deg, #f8fafc 0%, #f1f5f9 100%); padding: 14px 20px; cursor: pointer; font-weight: 600; display: flex; align-items: center; gap: 10px; color: #374151; }}
        .dimension-header:hover {{ background: linear-gradient(135deg, #f1f5f9 0%, #e5e7eb 100%); }}
        .toggle-icon {{ transition: transform 0.3s ease; color: #6366f1; }}
        .dimension-section.collapsed .toggle-icon {{ transform: rotate(-90deg); }}
        .dimension-section.collapsed .dimension-cases {{ display: none; }}
        .dimension-cases {{ padding: 16px; background: #fafbfc; }}
        .case-card {{ background: white; border-radius: 12px; padding: 16px; margin-bottom: 12px; border: 1px solid #fee2e2; box-shadow: 0 2px 8px rgba(239,68,68,0.08); }}
        .case-header {{ display: flex; align-items: center; gap: 12px; margin-bottom: 12px; flex-wrap: wrap; }}
        .case-id {{ font-family: 'SF Mono', Monaco, monospace; background: linear-gradient(135deg, #fef2f2 0%, #fee2e2 100%); padding: 4px 10px; border-radius: 6px; font-size: 12px; color: #991b1b; font-weight: 500; }}
        .case-title {{ flex: 1; font-weight: 600; color: #1f2937; }}
        .badge {{ padding: 4px 12px; border-radius: 20px; font-size: 11px; font-weight: 600; }}
        .badge-pass {{ background: linear-gradient(135deg, #dcfce7 0%, #bbf7d0 100%); color: #166534; }}
        .badge-fail {{ background: linear-gradient(135deg, #fee2e2 0%, #fecaca 100%); color: #991b1b; }}
        .score-pass {{ color: #22c55e; font-weight: 700; font-size: 14px; }}
        .score-fail {{ color: #ef4444; font-weight: 700; font-size: 14px; }}
        .case-body {{ font-size: 13px; color: #4b5563; }}
        .case-section {{ margin-bottom: 12px; }}
        .case-section b {{ color: #374151; font-weight: 600; }}
        .case-section pre {{ background: linear-gradient(135deg, #f8fafc 0%, #f1f5f9 100%); padding: 12px; border-radius: 8px; white-space: pre-wrap; word-break: break-all; max-height: 120px; overflow-y: auto; margin-top: 6px; font-family: 'SF Mono', Monaco, monospace; font-size: 12px; line-height: 1.6; border: 1px solid #e5e7eb; }}
        .deduction {{ background: linear-gradient(135deg, #fef2f2 0%, #fee2e2 100%); padding: 10px 14px; border-radius: 8px; color: #991b1b; border-left: 3px solid #ef4444; }}
        .no-failures {{ padding: 40px; text-align: center; color: #22c55e; font-size: 18px; font-weight: 600; }}
        @media print {{ body {{ background: white; padding: 10px; }} .section {{ box-shadow: none; border: 1px solid #e5e7eb; }} .dimension-header {{ cursor: default; }} .dimension-section.collapsed .dimension-cases {{ display: block; }} }}
    </style>
</head>
<body>
    <div class="container">
        <div class="header">
            <h1>📊 AI玩偶测试报告</h1>
            <div class="subtitle">📋 {task.get('name', task.get('task_id', ''))} | 🆔 {task.get('task_id', '')} | 📅 {task_created} | ⏰ 报告生成: {report_time}</div>
        </div>

        <div class="summary">
            <div class="summary-card"><div class="value">{total}</div><div class="label">📝 总用例</div></div>
            <div class="summary-card"><div class="value">{len(evaluated)}</div><div class="label">✅ 已评测</div></div>
            <div class="summary-card"><div class="value green">{len(passed)}</div><div class="label">🎯 通过</div></div>
            <div class="summary-card"><div class="value red">{len(failed)}</div><div class="label">⚠️ 失败</div></div>
            <div class="summary-card"><div class="value blue">{pass_rate}%</div><div class="label">📈 通过率</div></div>
        </div>

        <div class="section summary-section">
            <h2>📋 测试总结</h2>
            <p>{summary_text}</p>
        </div>

        <div style="display:grid;grid-template-columns:1fr 1fr;gap:20px;margin-bottom:24px">
            <div class="section" style="margin-bottom:0">
                <h2>🎯 能力簇汇总</h2>
                <table>
                    <tr><th>能力簇</th><th>总数</th><th>通过</th><th>失败</th><th>通过率</th><th>平均分</th></tr>
                    {cluster_rows}
                </table>
            </div>
            <div class="section" style="margin-bottom:0">
                <h2>📊 得分分布</h2>
                <div style="padding:16px 0">
                    {score_bars}
                </div>
            </div>
        </div>

        <div class="section">
            <h2>📈 维度统计</h2>
            <table>
                <tr><th>能力簇</th><th>维度</th><th>名称</th><th>总数</th><th>通过</th><th>失败</th><th>通过率</th><th>平均分</th></tr>
                {dimension_rows}
            </table>
        </div>

        <div class="section">
            <h2>📝 失败用例详情 ({len(failed)})</h2>
            {case_details}
        </div>
    </div>
</body>
</html>"""

    return html, 200, {"Content-Type": "text/html; charset=utf-8"}


@app.route("/api/test_report_excel", methods=["GET"])
def export_test_report_excel():
    """
    导出测试结果为Excel
    参数:
        task_id: 测试任务ID（必需）
        cluster_code: 能力簇筛选（可选）
        status: 状态筛选（可选）
    """
    from io import BytesIO
    from openpyxl import Workbook
    from openpyxl.styles import Font, Alignment, PatternFill, Border, Side
    from openpyxl.utils import get_column_letter

    task_id = request.args.get("task_id")
    cluster_code = request.args.get("cluster_code", "")
    status_filter = request.args.get("status", "")

    if not task_id:
        return jsonify({"error": "task_id required"}), 400

    conn = get_db_connection()

    # 获取任务信息
    task = execute_query(conn, "SELECT * FROM test_tasks WHERE id = %s", (task_id,), fetch_one=True)
    if not task:
        conn.close()
        return jsonify({"error": "task not found"}), 404
    task = row_to_dict(task)

    # 构建查询（支持筛选）
    sql = """SELECT r.*, c.case_id as case_code, c.dimension_code, c.title, c.test_point, c.input_text,
                  c.expected_output, c.priority, td.dimension_name, td.cluster_code, td.cluster_name
           FROM test_results r
           JOIN test_cases c ON r.case_id = c.id
           LEFT JOIN test_dimensions td ON c.dimension_code = td.dimension_code
           WHERE r.task_id = %s"""
    params = [task_id]

    if cluster_code:
        sql += " AND td.cluster_code = %s"
        params.append(cluster_code)
    if status_filter:
        sql += " AND r.status = %s"
        params.append(status_filter)

    sql += " ORDER BY c.dimension_code, c.case_id"

    results = execute_query(conn, sql, params, fetch_all=True)
    results = [row_to_dict(r) for r in results]
    conn.close()

    # 筛选条件描述
    filter_desc = ""
    if cluster_code:
        filter_desc += f" | 能力簇: {cluster_code}"
    if status_filter:
        filter_desc += f" | 状态: {status_filter}"

    # 创建Excel
    wb = Workbook()

    # === Sheet1: 汇总统计 ===
    ws_summary = wb.active
    ws_summary.title = "汇总统计"

    # 样式定义
    header_font = Font(bold=True, color="FFFFFF")
    header_fill = PatternFill(start_color="6366F1", end_color="6366F1", fill_type="solid")
    pass_fill = PatternFill(start_color="D4EDDA", end_color="D4EDDA", fill_type="solid")
    fail_fill = PatternFill(start_color="F8D7DA", end_color="F8D7DA", fill_type="solid")
    thin_border = Border(
        left=Side(style='thin'), right=Side(style='thin'),
        top=Side(style='thin'), bottom=Side(style='thin')
    )

    # 任务信息
    ws_summary["A1"] = "测试任务报告" + filter_desc
    ws_summary["A1"].font = Font(bold=True, size=16)
    ws_summary.merge_cells("A1:D1")

    ws_summary["A3"] = "任务ID:"
    ws_summary["B3"] = task.get("task_id", "")
    ws_summary["A4"] = "任务名称:"
    ws_summary["B4"] = task.get("name", "")
    ws_summary["A5"] = "测试用户:"
    ws_summary["B5"] = task.get("persona_id", "")
    ws_summary["A6"] = "创建时间:"
    ws_summary["B6"] = str(task.get("created_at", ""))

    # 统计数据
    total = len(results)
    evaluated = [r for r in results if r.get("score") is not None]
    passed = [r for r in evaluated if r.get("status") == "passed"]
    failed = [r for r in evaluated if r.get("status") == "failed"]
    avg_score = sum(r["score"] for r in evaluated) / len(evaluated) if evaluated else 0
    pass_rate = round(len(passed) / len(evaluated) * 100, 1) if evaluated else 0

    ws_summary["A8"] = "统计指标"
    ws_summary["A8"].font = Font(bold=True, size=12)
    ws_summary["A9"] = "总用例数"
    ws_summary["B9"] = total
    ws_summary["A10"] = "已评测"
    ws_summary["B10"] = len(evaluated)
    ws_summary["A11"] = "通过"
    ws_summary["B11"] = len(passed)
    ws_summary["A12"] = "失败"
    ws_summary["B12"] = len(failed)
    ws_summary["A13"] = "通过率"
    ws_summary["B13"] = f"{pass_rate}%"
    ws_summary["A14"] = "平均分"
    ws_summary["B14"] = round(avg_score, 2)

    # 按维度统计
    ws_summary["A16"] = "维度统计"
    ws_summary["A16"].font = Font(bold=True, size=12)

    dim_headers = ["维度代码", "维度名称", "能力簇", "总数", "通过", "失败", "通过率", "平均分"]
    for col, header in enumerate(dim_headers, 1):
        cell = ws_summary.cell(row=17, column=col, value=header)
        cell.font = header_font
        cell.fill = header_fill
        cell.border = thin_border

    by_dim = {}
    for r in results:
        dim = r.get("dimension_code", "")
        if dim not in by_dim:
            by_dim[dim] = {"name": r.get("dimension_name", ""), "cluster": r.get("cluster_name", ""),
                           "total": 0, "passed": 0, "failed": 0, "scores": []}
        by_dim[dim]["total"] += 1
        if r.get("status") == "passed":
            by_dim[dim]["passed"] += 1
        elif r.get("status") == "failed":
            by_dim[dim]["failed"] += 1
        if r.get("score") is not None:
            by_dim[dim]["scores"].append(r["score"])

    row = 18
    for dim_code in sorted(by_dim.keys()):
        stats = by_dim[dim_code]
        d_evaluated = stats["passed"] + stats["failed"]
        d_rate = round(stats["passed"] / d_evaluated * 100, 1) if d_evaluated > 0 else 0
        d_avg = round(sum(stats["scores"]) / len(stats["scores"]), 2) if stats["scores"] else 0

        ws_summary.cell(row=row, column=1, value=dim_code).border = thin_border
        ws_summary.cell(row=row, column=2, value=stats["name"]).border = thin_border
        ws_summary.cell(row=row, column=3, value=stats["cluster"]).border = thin_border
        ws_summary.cell(row=row, column=4, value=stats["total"]).border = thin_border
        ws_summary.cell(row=row, column=5, value=stats["passed"]).border = thin_border
        ws_summary.cell(row=row, column=6, value=stats["failed"]).border = thin_border
        ws_summary.cell(row=row, column=7, value=f"{d_rate}%").border = thin_border
        ws_summary.cell(row=row, column=8, value=d_avg).border = thin_border
        row += 1

    # 调整列宽
    ws_summary.column_dimensions['A'].width = 15
    ws_summary.column_dimensions['B'].width = 30
    ws_summary.column_dimensions['C'].width = 20
    ws_summary.column_dimensions['D'].width = 12

    # === Sheet2: 用例详情 ===
    ws_detail = wb.create_sheet("用例详情")

    detail_headers = ["用例ID", "维度", "维度名称", "优先级", "标题", "输入", "期望输出", "实际输出", "得分", "状态", "扣分原因"]
    for col, header in enumerate(detail_headers, 1):
        cell = ws_detail.cell(row=1, column=col, value=header)
        cell.font = header_font
        cell.fill = header_fill
        cell.border = thin_border
        cell.alignment = Alignment(wrap_text=True, vertical="top")

    import re
    for row_idx, r in enumerate(results, 2):
        actual = r.get("actual_output", "") or ""
        actual_clean = re.sub(r'\{"emotion":\s*"[^"]*"\}\s*', '', actual)

        ws_detail.cell(row=row_idx, column=1, value=r.get("case_code", "")).border = thin_border
        ws_detail.cell(row=row_idx, column=2, value=r.get("dimension_code", "")).border = thin_border
        ws_detail.cell(row=row_idx, column=3, value=r.get("dimension_name", "")).border = thin_border
        ws_detail.cell(row=row_idx, column=4, value=r.get("priority", "")).border = thin_border
        ws_detail.cell(row=row_idx, column=5, value=r.get("title", "")).border = thin_border

        input_cell = ws_detail.cell(row=row_idx, column=6, value=r.get("input_text", ""))
        input_cell.border = thin_border
        input_cell.alignment = Alignment(wrap_text=True, vertical="top")

        expected_cell = ws_detail.cell(row=row_idx, column=7, value=r.get("expected_output", ""))
        expected_cell.border = thin_border
        expected_cell.alignment = Alignment(wrap_text=True, vertical="top")

        actual_cell = ws_detail.cell(row=row_idx, column=8, value=actual_clean)
        actual_cell.border = thin_border
        actual_cell.alignment = Alignment(wrap_text=True, vertical="top")

        score_cell = ws_detail.cell(row=row_idx, column=9, value=r.get("score"))
        score_cell.border = thin_border

        status = r.get("status", "")
        status_cell = ws_detail.cell(row=row_idx, column=10, value=status)
        status_cell.border = thin_border
        if status == "passed":
            status_cell.fill = pass_fill
        elif status == "failed":
            status_cell.fill = fail_fill

        ws_detail.cell(row=row_idx, column=11, value=r.get("deduction_reason", "")).border = thin_border

    # 调整列宽
    col_widths = [12, 8, 15, 8, 25, 40, 40, 40, 8, 10, 30]
    for i, width in enumerate(col_widths, 1):
        ws_detail.column_dimensions[get_column_letter(i)].width = width

    # === Sheet3: 失败用例 ===
    ws_failed = wb.create_sheet("失败用例")

    for col, header in enumerate(detail_headers, 1):
        cell = ws_failed.cell(row=1, column=col, value=header)
        cell.font = header_font
        cell.fill = PatternFill(start_color="DC3545", end_color="DC3545", fill_type="solid")
        cell.border = thin_border

    failed_results = [r for r in results if r.get("status") == "failed"]
    for row_idx, r in enumerate(failed_results, 2):
        actual = r.get("actual_output", "") or ""
        actual_clean = re.sub(r'\{"emotion":\s*"[^"]*"\}\s*', '', actual)

        ws_failed.cell(row=row_idx, column=1, value=r.get("case_code", "")).border = thin_border
        ws_failed.cell(row=row_idx, column=2, value=r.get("dimension_code", "")).border = thin_border
        ws_failed.cell(row=row_idx, column=3, value=r.get("dimension_name", "")).border = thin_border
        ws_failed.cell(row=row_idx, column=4, value=r.get("priority", "")).border = thin_border
        ws_failed.cell(row=row_idx, column=5, value=r.get("title", "")).border = thin_border
        ws_failed.cell(row=row_idx, column=6, value=r.get("input_text", "")).border = thin_border
        ws_failed.cell(row=row_idx, column=7, value=r.get("expected_output", "")).border = thin_border
        ws_failed.cell(row=row_idx, column=8, value=actual_clean).border = thin_border
        ws_failed.cell(row=row_idx, column=9, value=r.get("score")).border = thin_border
        ws_failed.cell(row=row_idx, column=10, value=r.get("status", "")).border = thin_border
        ws_failed.cell(row=row_idx, column=11, value=r.get("deduction_reason", "")).border = thin_border

    for i, width in enumerate(col_widths, 1):
        ws_failed.column_dimensions[get_column_letter(i)].width = width

    # 输出文件
    output = BytesIO()
    wb.save(output)
    output.seek(0)

    filename = f"report_{task.get('persona_id', 'unknown')}_{datetime.datetime.now().strftime('%Y%m%d_%H%M')}.xlsx"

    return output.getvalue(), 200, {
        "Content-Type": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        "Content-Disposition": f"attachment; filename={filename}"
    }


@app.route("/api/async_tasks", methods=["GET"])
def list_async_tasks():
    """
    获取异步任务列表
    参数:
        task_type: generate/execute/evaluate (可选)
        persona_id: 用户ID筛选 (可选)
        limit: 返回数量，默认20
    """
    task_type = request.args.get("task_type", "")
    persona_id = request.args.get("persona_id", "")
    limit = int(request.args.get("limit", 20))

    conn = get_db_connection()
    sql = "SELECT * FROM async_tasks WHERE 1=1"
    params = []

    if task_type:
        sql += " AND task_type = %s"
        params.append(task_type)
    if persona_id:
        sql += " AND persona_id = %s"
        params.append(persona_id)

    sql += " ORDER BY created_at DESC LIMIT %s"
    params.append(limit)

    rows = execute_query(conn, sql, params, fetch_all=True)
    conn.close()

    tasks = []
    for row in rows:
        row = row_to_dict(row)
        task = {
            "id": row["id"],
            "task_type": row["task_type"],
            "status": row["status"],
            "persona_id": row.get("persona_id", ""),
            "created_at": str(row.get("created_at", "")),
            "updated_at": str(row.get("updated_at", "")),
        }
        if row.get("progress_json"):
            task["progress"] = json.loads(row["progress_json"])
        if row.get("result_json"):
            task.update(json.loads(row["result_json"]))
        if row.get("config_json"):
            config = json.loads(row["config_json"])
            task["dimension_codes"] = config.get("dimension_codes")
            task["count_per_dimension"] = config.get("count_per_dimension")
        tasks.append(task)

    return jsonify(tasks)


@app.route("/api/async_tasks/<task_id>/cancel", methods=["POST"])
def cancel_async_task(task_id):
    """
    取消/重置卡住的异步任务
    将 running 状态改为 cancelled
    """
    conn = get_db_connection()
    row = execute_query(conn, "SELECT * FROM async_tasks WHERE id = %s", (task_id,), fetch_one=True)
    if not row:
        conn.close()
        return jsonify({"error": "task not found"}), 404

    row = row_to_dict(row)
    if row["status"] != "running":
        conn.close()
        return jsonify({"error": f"task status is {row['status']}, not running"}), 400

    execute_query(conn,
        "UPDATE async_tasks SET status = 'cancelled', updated_at = NOW() WHERE id = %s",
        (task_id,))
    conn.commit()
    conn.close()

    # 清除内存缓存
    task_type = row.get("task_type", "")
    if task_type == "generate" and task_id in _generate_tasks:
        _generate_tasks[task_id]["status"] = "cancelled"
    elif task_type == "execute" and task_id in _execute_tasks:
        _execute_tasks[task_id]["status"] = "cancelled"
    elif task_type == "evaluate" and task_id in _evaluate_tasks:
        _evaluate_tasks[task_id]["status"] = "cancelled"

    return jsonify({"success": True, "message": f"Task {task_id} cancelled"})


@app.route("/api/async_tasks/<task_id>", methods=["DELETE"])
def delete_async_task(task_id):
    """删除异步任务记录"""
    conn = get_db_connection()
    row = execute_query(conn, "SELECT status FROM async_tasks WHERE id = %s", (task_id,), fetch_one=True)
    if not row:
        conn.close()
        return jsonify({"error": "task not found"}), 404

    row = row_to_dict(row)
    if row["status"] == "running":
        conn.close()
        return jsonify({"error": "cannot delete running task, cancel it first"}), 400

    execute_query(conn, "DELETE FROM async_tasks WHERE id = %s", (task_id,))
    conn.commit()
    conn.close()
    return jsonify({"success": True})


@app.route("/api/test_dimensions", methods=["GET"])
def get_test_dimensions():
    """获取测试维度列表"""
    conn = get_db_connection()
    rows = execute_query(conn,
        "SELECT cluster_code, cluster_name, dimension_code, dimension_name, test_points FROM test_dimensions ORDER BY dimension_code",
        fetch_all=True)
    conn.close()
    return jsonify([row_to_dict(r) for r in rows])


@app.route("/api/toy_persona", methods=["GET"])
def get_toy_persona():
    """获取玩偶人设"""
    conn = get_db_connection()
    row = execute_query(conn, "SELECT * FROM toy_persona LIMIT 1", fetch_one=True)
    conn.close()
    if not row:
        return jsonify({"error": "toy_persona not found"}), 404
    data = row_to_dict(row)
    # 解析 JSON 字段
    for key in ["behavior_principles", "personality_traits", "speaking_style", "forbidden_expressions", "emotion_boundaries", "relationship_stages"]:
        if key in data and isinstance(data[key], str):
            try:
                data[key] = json.loads(data[key])
            except:
                pass
    return jsonify(data)


@app.route("/api/test_cases", methods=["GET"])
def list_test_cases():
    """
    获取测试用例列表
    参数: dimension_code, dimension_codes, cluster_code, persona_id, status, priority, priorities, page, limit
    """
    dimension_code = request.args.get("dimension_code", "")
    dimension_codes = request.args.get("dimension_codes", "")  # 逗号分隔
    cluster_code = request.args.get("cluster_code", "")  # 能力簇筛选
    persona_id = request.args.get("persona_id", "")
    status = request.args.get("status", "")
    priority = request.args.get("priority", "")
    priorities = request.args.get("priorities", "")  # 逗号分隔
    page = int(request.args.get("page", 1))
    limit = int(request.args.get("limit", 50))
    offset = (page - 1) * limit

    ph = "%s" if USE_MYSQL else "?"
    conn = get_db_connection()

    # 构建查询条件
    where_clauses = []
    params = []

    # 能力簇筛选：先获取该簇下的所有维度
    if cluster_code:
        dim_rows = execute_query(conn,
            f"SELECT dimension_code FROM test_dimensions WHERE cluster_code = {ph}",
            (cluster_code,), fetch_all=True)
        dim_codes_in_cluster = [row_to_dict(r)["dimension_code"] for r in dim_rows]
        if dim_codes_in_cluster:
            placeholders = ",".join([ph] * len(dim_codes_in_cluster))
            where_clauses.append(f"dimension_code IN ({placeholders})")
            params.extend(dim_codes_in_cluster)
        else:
            where_clauses.append("1=0")  # 没有维度则返回空

    if dimension_code:
        where_clauses.append(f"dimension_code = {ph}")
        params.append(dimension_code)
    if dimension_codes:
        codes = [c.strip() for c in dimension_codes.split(",") if c.strip()]
        if codes:
            placeholders = ",".join([ph] * len(codes))
            where_clauses.append(f"dimension_code IN ({placeholders})")
            params.extend(codes)
    if persona_id:
        where_clauses.append(f"persona_id = {ph}")
        params.append(persona_id)
    if status:
        where_clauses.append(f"status = {ph}")
        params.append(status)
    quality_status_filter = request.args.get("quality_status", "")
    if quality_status_filter:
        where_clauses.append(f"quality_status = {ph}")
        params.append(quality_status_filter)
    if priority:
        where_clauses.append(f"priority = {ph}")
        params.append(priority)
    if priorities:
        plist = [p.strip() for p in priorities.split(",") if p.strip()]
        if plist:
            placeholders = ",".join([ph] * len(plist))
            where_clauses.append(f"priority IN ({placeholders})")
            params.extend(plist)

    where_sql = " AND ".join(where_clauses) if where_clauses else "1=1"

    # 查询总数
    count_row = execute_query(conn, f"SELECT COUNT(*) as cnt FROM test_cases WHERE {where_sql}", params, fetch_one=True)
    total = count_row["cnt"] if count_row else 0

    # 查询列表
    rows = execute_query(conn,
        f"SELECT * FROM test_cases WHERE {where_sql} ORDER BY dimension_code, case_id LIMIT {ph} OFFSET {ph}",
        params + [limit, offset], fetch_all=True)
    conn.close()

    return jsonify({
        "cases": [row_to_dict(r) for r in rows],
        "total": total,
        "page": page,
        "limit": limit
    })


@app.route("/api/test_cases/<int:case_id>", methods=["GET"])
def get_test_case(case_id):
    """获取单条测试用例"""
    conn = get_db_connection()
    ph = "%s" if USE_MYSQL else "?"
    row = execute_query(conn, f"SELECT * FROM test_cases WHERE id = {ph}", (case_id,), fetch_one=True)
    conn.close()
    if not row:
        return jsonify({"error": "test case not found"}), 404
    return jsonify(row_to_dict(row))


def _save_test_case(conn, case_data, persona_id=None, device_id=None, dimension_code=None):
    """
    统一的用例保存函数，手动创建和自动生成共用。

    参数:
        conn: 数据库连接
        case_data: 用例数据字典
        persona_id: 可选，覆盖 case_data 中的值
        device_id: 可选，覆盖 case_data 中的值
        dimension_code: 可选，覆盖 case_data 中的值

    返回:
        新创建的用例 ID
    """
    # 校验必填字段（LLM生成的字段）
    REQUIRED_FIELDS = [
        "case_id", "test_point", "title", "input_text",
        "expected_output", "failure_flags", "evaluation_points",
        "score_2_desc", "score_6_desc", "score_10_desc", "priority"
    ]
    missing_fields = []
    for field in REQUIRED_FIELDS:
        val = case_data.get(field, "")
        if not val or (isinstance(val, str) and not val.strip()):
            missing_fields.append(field)
    
    if missing_fields:
        case_id = case_data.get("case_id", "unknown")
        print(f"[SAVE CASE] 跳过不完整用例 {case_id}，缺少字段: {missing_fields}", flush=True)
        return None

    def clean_value(v):
        if v is None:
            return ""
        return str(v).replace('\x00', '')

    # 优先使用传入参数，否则从 case_data 取
    final_persona_id = persona_id or case_data.get("persona_id", "")
    final_device_id = device_id or case_data.get("device_id", final_persona_id)
    final_dimension_code = dimension_code or case_data.get("dimension_code", "")

    # 规则校验（用例生成时默认 pending，等待 LLM 异步审核）
    validation = validate_case_rules(case_data, final_dimension_code)
    # 如果规则校验有严重问题，直接标记失败；否则设为 pending 等待 LLM 审核
    if validation["issues"] and len(validation["issues"]) > 2:
        quality_status = "failed"
        quality_issues = json.dumps(validation["issues"], ensure_ascii=False)
    else:
        quality_status = "pending"
        quality_issues = None

    cursor = execute_query(conn, """
        INSERT INTO test_cases (case_id, persona_id, device_id, dimension_code, test_point, title, priority,
            input_text, expected_output, evaluation_points, failure_flags, score_2_desc, score_6_desc, score_10_desc, status, quality_status, quality_issues)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'pending', %s, %s)
    """ if USE_MYSQL else """
        INSERT INTO test_cases (case_id, persona_id, device_id, dimension_code, test_point, title, priority,
            input_text, expected_output, evaluation_points, failure_flags, score_2_desc, score_6_desc, score_10_desc, status, quality_status, quality_issues)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?)
    """, (
        clean_value(case_data.get("case_id")),
        clean_value(final_persona_id),
        clean_value(final_device_id),
        clean_value(final_dimension_code),
        clean_value(case_data.get("test_point", "")),
        clean_value(case_data.get("title", "")),
        clean_value(case_data.get("priority", "P1")),
        clean_value(case_data.get("input_text", "")),
        clean_value(case_data.get("expected_output", "")),
        clean_value(case_data.get("evaluation_points", "")),
        clean_value(case_data.get("failure_flags", "")),
        clean_value(case_data.get("score_2_desc", "")),
        clean_value(case_data.get("score_6_desc", "")),
        clean_value(case_data.get("score_10_desc", "")),
        quality_status,
        quality_issues
    ))
    return get_lastrowid(cursor)


@app.route("/api/test_cases", methods=["POST"])
def create_test_case():
    """创建测试用例"""
    data = request.get_json() or {}
    required = ["case_id", "dimension_code", "title", "input_text"]
    for field in required:
        if not data.get(field):
            return jsonify({"error": f"{field} is required"}), 400

    conn = get_db_connection()
    new_id = _save_test_case(conn, data)
    conn.commit()
    conn.close()

    return jsonify({"id": new_id, "case_id": data.get("case_id")})


@app.route("/api/test_cases/<int:case_id>", methods=["PUT"])
def update_test_case(case_id):
    """更新测试用例"""
    data = request.get_json() or {}
    conn = get_db_connection()

    # 检查是否存在
    row = execute_query(conn, "SELECT id FROM test_cases WHERE id = ?", (case_id,), fetch_one=True)
    if not row:
        conn.close()
        return jsonify({"error": "test case not found"}), 404

    # 构建更新语句
    updatable = ["case_id", "persona_id", "device_id", "dimension_code", "title", "priority",
                 "input_text", "expected_output", "evaluation_points", "failure_flags",
                 "score_2_desc", "score_6_desc", "score_10_desc",
                 "actual_output", "score", "deduction_reason", "status"]
    set_clauses = []
    params = []
    for field in updatable:
        if field in data:
            set_clauses.append(f"{field} = ?")
            params.append(data[field])

    if not set_clauses:
        conn.close()
        return jsonify({"error": "no fields to update"}), 400

    params.append(case_id)
    execute_query(conn, f"UPDATE test_cases SET {', '.join(set_clauses)} WHERE id = ?", params)
    conn.commit()
    conn.close()

    return jsonify({"ok": True, "id": case_id})


@app.route("/api/test_cases/<int:case_id>", methods=["DELETE"])
def delete_test_case(case_id):
    """删除测试用例"""
    conn = get_db_connection()
    execute_query(conn, "DELETE FROM test_cases WHERE id = ?", (case_id,))
    conn.commit()
    conn.close()
    return jsonify({"ok": True})


@app.route("/api/test_cases/generate", methods=["POST"])
def generate_test_cases():
    """
    生成测试用例（异步任务）

    请求:
        dimension_codes: ["A1", "A2"] 或 null (全部22个)
        persona_id: "xiaojuzi"
        count_per_dimension: 5
        clear_existing: false
    """
    data = request.get_json() or {}
    persona_id = data.get("persona_id", "").strip()
    dimension_codes = data.get("dimension_codes")  # null 表示全部
    count_per_dim = int(data.get("count_per_dimension", 5))
    clear_existing = data.get("clear_existing", False)

    if not persona_id:
        return jsonify({"error": "persona_id is required"}), 400

    # 生成任务 ID
    import uuid
    task_id = str(uuid.uuid4())[:8]

    # 初始化任务状态
    task_data = {
        "status": "running",
        "persona_id": persona_id,
        "dimension_codes": dimension_codes,
        "count_per_dimension": count_per_dim,
        "clear_existing": clear_existing,
        "progress": {"total": 0, "done": 0, "current": None},
        "cases_created": 0,
        "errors": [],
    }
    _generate_tasks[task_id] = task_data
    _save_async_task(task_id, "generate", task_data)

    # 启动后台线程
    t = threading.Thread(target=_generate_cases_worker, args=(task_id,), daemon=True)
    t.start()

    return jsonify({"task_id": task_id, "status": "running"})


def import_datetime_now():
    from datetime import datetime
    return datetime.now()


@app.route("/api/test_cases/generate/<task_id>", methods=["GET"])
def get_generate_status(task_id):
    """获取生成任务状态"""
    # 先查内存缓存，再查数据库
    task = _generate_tasks.get(task_id) or _load_async_task(task_id)
    if not task:
        return jsonify({"error": "task not found"}), 404
    return jsonify(task)


def _generate_cases_worker(task_id):
    """后台生成用例的 worker"""
    task = _generate_tasks.get(task_id) or _load_async_task(task_id)
    if not task:
        return

    try:
        conn = get_db_connection()

        # 获取测试维度
        if task["dimension_codes"]:
            placeholders = ",".join(["?" for _ in task["dimension_codes"]])
            dims = execute_query(conn,
                f"SELECT * FROM test_dimensions WHERE dimension_code IN ({placeholders}) ORDER BY dimension_code",
                task["dimension_codes"], fetch_all=True)
        else:
            dims = execute_query(conn,
                "SELECT * FROM test_dimensions ORDER BY dimension_code",
                fetch_all=True)
        dims = [row_to_dict(d) for d in dims]

        task["progress"]["total"] = len(dims)

        # 获取玩偶人设
        toy_row = execute_query(conn, "SELECT * FROM toy_persona LIMIT 1", fetch_one=True)
        toy_persona = row_to_dict(toy_row) if toy_row else {}

        # 获取用户角色
        persona_row = execute_query(conn, "SELECT * FROM personas WHERE id = ?", (task["persona_id"],), fetch_one=True)
        persona = row_to_dict(persona_row) if persona_row else {}

        # 获取用户已知事实
        facts_rows = execute_query(conn,
            "SELECT category, fact_key, fact_value FROM user_facts WHERE persona_id = ? AND is_active = 1",
            (task["persona_id"],), fetch_all=True)
        user_facts = [row_to_dict(f) for f in facts_rows]

        conn.close()

        # 逐维度生成
        for dim in dims:
            dim_code = dim["dimension_code"]
            task["progress"]["current"] = dim_code
            print(f"[CASE GEN] {task_id} generating {dim_code}...", flush=True)

            try:
                # 检查该维度已有多少用例
                conn2 = get_db_connection()
                existing_count_row = execute_query(conn2,
                    "SELECT COUNT(*) as cnt FROM test_cases WHERE dimension_code = %s AND persona_id = %s" if USE_MYSQL else
                    "SELECT COUNT(*) as cnt FROM test_cases WHERE dimension_code = ? AND persona_id = ?",
                    (dim_code, task["persona_id"]), fetch_one=True)
                existing_count = existing_count_row["cnt"] if existing_count_row else 0

                # 如果需要清空已有
                if task["clear_existing"]:
                    execute_query(conn2,
                        "DELETE FROM test_cases WHERE dimension_code = %s AND persona_id = %s" if USE_MYSQL else
                        "DELETE FROM test_cases WHERE dimension_code = ? AND persona_id = ?",
                        (dim_code, task["persona_id"]))
                    conn2.commit()
                    existing_count = 0
                conn2.close()

                # 计算需要生成的数量（补足到指定数量）
                target_count = task["count_per_dimension"]
                need_count = max(0, target_count - existing_count)

                if need_count == 0:
                    print(f"[CASE GEN] {task_id} {dim_code} already has {existing_count} cases, skip", flush=True)
                    task["progress"]["done"] += 1
                    _save_async_task(task_id, "generate", task)
                    continue

                print(f"[CASE GEN] {task_id} {dim_code} has {existing_count}, need {need_count} more", flush=True)

                # 调用 LLM 生成
                llm_config = get_llm_config()
                cases = pipi_api.generate_test_cases(
                    dimension=dim,
                    toy_persona=toy_persona,
                    persona=persona,
                    user_facts=user_facts,
                    count=need_count,
                    **llm_config["case_gen"]
                )

                # 保存到数据库（带重试）
                if cases:
                    db_retry_count = 0
                    db_max_retries = 2
                    db_success = False

                    while db_retry_count <= db_max_retries and not db_success:
                        try:
                            conn3 = get_db_connection()
                            created_this_round = 0
                            for case in cases:
                                # 生成唯一 case_id（检查是否已存在）
                                base_case_id = case.get("case_id", f"{dim_code}-01")
                                final_case_id = _get_unique_case_id(conn3, base_case_id)
                                case["case_id"] = final_case_id

                                # 调用统一的保存函数
                                new_case_id = _save_test_case(
                                    conn3, case,
                                    persona_id=task["persona_id"],
                                    device_id=task["persona_id"],
                                    dimension_code=dim_code
                                )

                                # 记录创建的用例ID
                                if "created_case_ids" not in task:
                                    task["created_case_ids"] = []
                                if "_current_dim_case_ids" not in task:
                                    task["_current_dim_case_ids"] = []
                                if new_case_id:
                                    task["created_case_ids"].append(new_case_id)
                                    task["_current_dim_case_ids"].append(new_case_id)
                                if new_case_id:
                                    created_this_round += 1
                            conn3.commit()
                            conn3.close()
                            task["cases_created"] += created_this_round
                            db_success = True
                            print(f"[CASE GEN] {task_id} {dim_code} done, created {created_this_round}/{len(cases)} valid cases", flush=True)

                            # 用例完整性重试：如果保存成功数 < 期望数，删除已保存的并重新生成覆盖
                            if created_this_round < need_count:
                                for retry_attempt in range(2):  # 最多重试2次
                                    print(f"[CASE GEN] {task_id} {dim_code} 完整用例 {created_this_round}/{need_count}，重试 {retry_attempt+1}/2", flush=True)
                                    # 删除本轮已保存的不完整批次
                                    conn4 = get_db_connection()
                                    if task.get("created_case_ids"):
                                        for cid in task["created_case_ids"]:
                                            if cid:
                                                execute_query(conn4, "DELETE FROM test_cases WHERE id = %s" if USE_MYSQL else "DELETE FROM test_cases WHERE id = ?", (cid,))
                                        task["cases_created"] -= created_this_round
                                    conn4.commit()
                                    conn4.close()
                                    task["created_case_ids"] = []
                                    # 重新生成整批
                                    retry_cases = pipi_api.generate_test_cases(
                                        dimension=dim, toy_persona=toy_persona, persona=persona,
                                        user_facts=user_facts, count=need_count, **llm_config["case_gen"]
                                    )
                                    if not retry_cases:
                                        continue
                                    conn5 = get_db_connection()
                                    created_this_round = 0
                                    for rc in retry_cases:
                                        rc["case_id"] = _get_unique_case_id(conn5, rc.get("case_id", f"{dim_code}-01"))
                                        rid = _save_test_case(conn5, rc, persona_id=task["persona_id"], device_id=task["persona_id"], dimension_code=dim_code)
                                        if rid:
                                            task["created_case_ids"].append(rid)
                                            task["_current_dim_case_ids"].append(rid)
                                            created_this_round += 1
                                    conn5.commit()
                                    conn5.close()
                                    task["cases_created"] += created_this_round
                                    print(f"[CASE GEN] {task_id} {dim_code} 重试后 {created_this_round}/{need_count} 条", flush=True)
                                    if created_this_round >= need_count:
                                        break
                        except Exception as db_err:
                            db_retry_count += 1
                            print(f"[CASE GEN DB ERROR] {task_id} {dim_code} attempt {db_retry_count}: {db_err}", flush=True)
                            if db_retry_count <= db_max_retries:
                                print(f"[CASE GEN] {task_id} {dim_code} retrying LLM generation...", flush=True)
                                import time
                                time.sleep(2)
                                # 重新调用 LLM 生成
                                cases = pipi_api.generate_test_cases(
                                    dimension=dim,
                                    toy_persona=toy_persona,
                                    persona=persona,
                                    user_facts=user_facts,
                                    count=task["count_per_dimension"],
                                    **llm_config["case_gen"]
                                )
                                if not cases:
                                    print(f"[CASE GEN] {task_id} {dim_code} retry LLM returned empty", flush=True)
                                    break
                            else:
                                raise db_err

                    if not db_success:
                        task["errors"].append({"dimension": dim_code, "error": f"数据库插入失败（重试{db_max_retries}次）"})
                else:
                    # LLM 返回空或解析失败，worker 层面再重试1次
                    print(f"[CASE GEN] {task_id} {dim_code} LLM returned empty (after 3 attempts), worker retry...", flush=True)
                    time.sleep(5)
                    cases = pipi_api.generate_test_cases(
                        dimension=dim, toy_persona=toy_persona, persona=persona,
                        user_facts=user_facts, count=need_count, **llm_config["case_gen"]
                    )
                    if cases:
                        print(f"[CASE GEN] {task_id} {dim_code} worker retry succeeded, got {len(cases)} cases", flush=True)
                        conn3 = get_db_connection()
                        created_this_round = 0
                        for case in cases:
                            base_case_id = case.get("case_id", f"{dim_code}-01")
                            final_case_id = _get_unique_case_id(conn3, base_case_id)
                            case["case_id"] = final_case_id
                            new_case_id = _save_test_case(
                                conn3, case, persona_id=task["persona_id"],
                                device_id=task["persona_id"], dimension_code=dim_code
                            )
                            if "created_case_ids" not in task:
                                task["created_case_ids"] = []
                            if "_current_dim_case_ids" not in task:
                                task["_current_dim_case_ids"] = []
                            if new_case_id:
                                task["created_case_ids"].append(new_case_id)
                                task["_current_dim_case_ids"].append(new_case_id)
                                created_this_round += 1
                        conn3.commit()
                        conn3.close()
                        task["cases_created"] += created_this_round
                        print(f"[CASE GEN] {task_id} {dim_code} worker retry done, created {created_this_round}/{len(cases)} cases", flush=True)
                    else:
                        print(f"[CASE GEN] {task_id} {dim_code} worker retry also empty, giving up", flush=True)
                        task["errors"].append({"dimension": dim_code, "error": "LLM 返回空或解析失败（已重试）"})

            except Exception as e:
                print(f"[CASE GEN ERROR] {task_id} {dim_code}: {e}", flush=True)
                task["errors"].append({"dimension": dim_code, "error": str(e)})

            task["progress"]["done"] += 1
            _update_async_task(task_id, task)

            # 每个维度完成后立即触发异步审核（与后续生成并行）
            dim_case_ids = task.get("_current_dim_case_ids", [])
            if dim_case_ids:
                print(f"[CASE GEN] {task_id} {dim_code} triggering async review for {len(dim_case_ids)} cases", flush=True)
                async_review_cases(dim_case_ids)
            task["_current_dim_case_ids"] = []  # 清空当前维度ID列表

        task["status"] = "completed"
        task["progress"]["current"] = None
        _update_async_task(task_id, task)
        print(f"[CASE GEN] {task_id} completed, total {task['cases_created']} cases", flush=True)

    except Exception as e:
        import traceback
        print(f"[CASE GEN FATAL] {task_id}: {e}\n{traceback.format_exc()}", flush=True)
        task["status"] = "failed"
        task["errors"].append({"dimension": "global", "error": str(e)})
        _update_async_task(task_id, task)


# ─── 测试用例执行 ─────────────────────────────────────

@app.route("/api/test_cases/execute", methods=["POST"])
def execute_test_cases():
    """
    执行测试用例
    请求参数:
        case_ids: [1,2,3] - 指定用例ID列表
        或按条件筛选:
        persona_id: str - 用户ID
        dimension_code: str - 维度代码（可选）
        status: str - 状态筛选（可选，默认 pending）
    """
    data = request.get_json() or {}
    case_ids = data.get("case_ids", [])
    persona_id = data.get("persona_id", "")
    dimension_code = data.get("dimension_code", "")
    status_filter = data.get("status", "pending")

    conn = get_db_connection()

    # 获取要执行的用例
    if case_ids:
        placeholders = ",".join(["%s" if USE_MYSQL else "?"] * len(case_ids))
        cases = execute_query(conn,
            f"SELECT * FROM test_cases WHERE id IN ({placeholders})",
            case_ids, fetch_all=True)
    else:
        sql = "SELECT * FROM test_cases WHERE 1=1"
        params = []
        if persona_id:
            sql += " AND persona_id = " + ("%s" if USE_MYSQL else "?")
            params.append(persona_id)
        if dimension_code:
            sql += " AND dimension_code = " + ("%s" if USE_MYSQL else "?")
            params.append(dimension_code)
        if status_filter:
            sql += " AND status = " + ("%s" if USE_MYSQL else "?")
            params.append(status_filter)
        sql += " ORDER BY id"
        cases = execute_query(conn, sql, params, fetch_all=True)

    conn.close()
    cases = [row_to_dict(c) for c in cases]

    if not cases:
        return jsonify({"error": "no cases to execute"}), 400

    # 创建执行任务
    import uuid
    task_id = "exec_" + uuid.uuid4().hex[:8]
    task = {
        "task_id": task_id,
        "status": "running",
        "persona_id": persona_id,
        "progress": {"total": len(cases), "done": 0, "current_case_id": ""},
        "cases_executed": 0,
        "errors": [],
        "case_ids": [c["id"] for c in cases],
    }
    _execute_tasks[task_id] = task
    _save_async_task(task_id, "execute", task)

    # 启动后台线程执行
    import threading
    t = threading.Thread(target=_execute_cases_worker, args=(task_id,))
    t.daemon = True
    t.start()

    return jsonify({"task_id": task_id, "status": "running", "total": len(cases)})


@app.route("/api/test_cases/execute/<task_id>", methods=["GET"])
def get_execute_status(task_id):
    """获取执行任务状态"""
    task = _execute_tasks.get(task_id) or _load_async_task(task_id)
    if not task:
        return jsonify({"error": "task not found"}), 404
    return jsonify(task)


def _parse_input_rounds(input_text):
    """
    解析 input_text 中的多轮对话
    格式: 【R1】用户：xxx\n【R2】用户：yyy
    返回: ["xxx", "yyy"]
    """
    import re
    # 匹配 【R数字】用户：内容
    pattern = r'【R\d+】\s*用户[：:]\s*(.+?)(?=【R\d+】|$)'
    matches = re.findall(pattern, input_text, re.DOTALL)
    if matches:
        return [m.strip() for m in matches if m.strip()]
    # 如果没有匹配到格式，直接返回整个文本作为单轮
    return [input_text.strip()] if input_text.strip() else []


def _execute_cases_worker(task_id):
    """后台执行用例的 worker"""
    task = _execute_tasks.get(task_id) or _load_async_task(task_id)
    if not task:
        return
    _execute_tasks[task_id] = task  # 确保本地缓存有

    try:
        conn = get_db_connection()
        case_ids = task["case_ids"]

        for case_id in case_ids:
            # 获取用例详情
            case = execute_query(conn,
                "SELECT * FROM test_cases WHERE id = " + ("%s" if USE_MYSQL else "?"),
                (case_id,), fetch_one=True)
            if not case:
                continue
            case = row_to_dict(case)

            task["progress"]["current_case_id"] = case.get("case_id", str(case_id))
            print(f"[EXEC] {task_id} executing {case['case_id']}...", flush=True)

            try:
                # 解析多轮对话
                rounds = _parse_input_rounds(case.get("input_text", ""))
                if not rounds:
                    task["errors"].append({"case_id": case["case_id"], "error": "empty input"})
                    continue

                # 逐轮发送，保存所有轮次回复
                persona_id = case.get("persona_id") or case.get("device_id", "")
                all_replies = []
                has_error = False

                for i, msg in enumerate(rounds):
                    # 调用 /api/test/chat
                    import requests as req
                    import time as _time
                    _t0 = _time.time()
                    resp = req.post(
                        "http://127.0.0.1:8080/api/test/chat",
                        json={"persona_id": persona_id, "message": msg, "extract_facts": True},
                        timeout=180
                    )
                    _elapsed = _time.time() - _t0
                    result = resp.json()

                    if result.get("error"):
                        task["errors"].append({
                            "case_id": case["case_id"],
                            "round": i + 1,
                            "error": result["error"]
                        })
                        has_error = True
                        break

                    reply = result.get("reply", "")
                    all_replies.append(f"【R{i+1}】秋秋：{reply}")
                    print(f"[EXEC] {case['case_id']} R{i+1}: {_elapsed:.1f}s reply_len={len(reply)}", flush=True)

                # 合并所有轮次回复
                actual_output = "\n".join(all_replies) if all_replies else ""

                # 更新数据库
                if actual_output:
                    execute_query(conn,
                        "UPDATE test_cases SET actual_output = " + ("%s" if USE_MYSQL else "?") +
                        ", executed_at = NOW(), status = 'executed' WHERE id = " + ("%s" if USE_MYSQL else "?"),
                        (actual_output, case_id))
                    conn.commit()
                    task["executed_count"] += 1

            except Exception as e:
                print(f"[EXEC ERROR] {case['case_id']}: {e}", flush=True)
                task["errors"].append({"case_id": case["case_id"], "error": str(e)})

            task["progress"]["done"] += 1
            task["cases_executed"] = task.get("cases_executed", 0) + (1 if actual_output else 0)
            _update_async_task(task_id, task)

        conn.close()
        task["status"] = "completed"
        _update_async_task(task_id, task)
        print(f"[EXEC] {task_id} completed, executed {task.get('cases_executed', 0)} cases", flush=True)

    except Exception as e:
        import traceback
        print(f"[EXEC FATAL] {task_id}: {e}\n{traceback.format_exc()}", flush=True)
        task["status"] = "failed"
        task["errors"].append({"case_id": "global", "error": str(e)})
        _update_async_task(task_id, task)


def _get_unique_case_id(conn, base_id):
    """获取唯一的 case_id，如果已存在则递增序号"""
    import re
    match = re.match(r'^([A-Z]\d+)-(\d+)$', base_id)
    if match:
        prefix = match.group(1)
        num = int(match.group(2))
    else:
        prefix = base_id
        num = 1

    # 查找该前缀的最大序号（使用数字排序而非字符串排序）
    if USE_MYSQL:
        row = execute_query(conn,
            "SELECT case_id FROM test_cases WHERE case_id LIKE %s ORDER BY CAST(SUBSTRING_INDEX(case_id, '-', -1) AS UNSIGNED) DESC LIMIT 1",
            (f"{prefix}-%",), fetch_one=True)
    else:
        row = execute_query(conn,
            "SELECT case_id FROM test_cases WHERE case_id LIKE ? ORDER BY CAST(SUBSTR(case_id, INSTR(case_id, '-') + 1) AS INTEGER) DESC LIMIT 1",
            (f"{prefix}-%",), fetch_one=True)
    if row:
        existing_match = re.match(r'^[A-Z]\d+-(\d+)$', row["case_id"])
        if existing_match:
            num = int(existing_match.group(1)) + 1

    return f"{prefix}-{num:02d}"


# ─── 测试用例评测 ─────────────────────────────────────

@app.route("/api/test_cases/evaluate", methods=["POST"])
def evaluate_test_cases():
    """
    评测测试用例
    请求参数:
        case_ids: [1,2,3] - 指定用例ID列表
        或按条件筛选:
        persona_id: str - 用户ID
        dimension_code: str - 维度代码（可选）
        status: str - 状态筛选（默认 executed）
    """
    data = request.get_json() or {}
    case_ids = data.get("case_ids", [])
    persona_id = data.get("persona_id", "")
    dimension_code = data.get("dimension_code", "")
    status_filter = data.get("status", "executed")

    conn = get_db_connection()

    # 获取要评测的用例
    if case_ids:
        placeholders = ",".join(["%s" if USE_MYSQL else "?"] * len(case_ids))
        cases = execute_query(conn,
            f"SELECT * FROM test_cases WHERE id IN ({placeholders})",
            case_ids, fetch_all=True)
    else:
        sql = "SELECT * FROM test_cases WHERE 1=1"
        params = []
        if persona_id:
            sql += " AND persona_id = " + ("%s" if USE_MYSQL else "?")
            params.append(persona_id)
        if dimension_code:
            sql += " AND dimension_code = " + ("%s" if USE_MYSQL else "?")
            params.append(dimension_code)
        if status_filter:
            sql += " AND status = " + ("%s" if USE_MYSQL else "?")
            params.append(status_filter)
        sql += " ORDER BY id"
        cases = execute_query(conn, sql, params, fetch_all=True)

    conn.close()
    cases = [row_to_dict(c) for c in cases]

    if not cases:
        return jsonify({"error": "no cases to evaluate"}), 400

    # 创建评测任务
    import uuid
    task_id = "eval_" + uuid.uuid4().hex[:8]
    task = {
        "task_id": task_id,
        "status": "running",
        "persona_id": persona_id,
        "progress": {"total": len(cases), "done": 0, "current_case_id": ""},
        "cases_evaluated": 0,
        "passed_count": 0,
        "failed_count": 0,
        "errors": [],
        "case_ids": [c["id"] for c in cases],
    }
    _evaluate_tasks[task_id] = task
    _save_async_task(task_id, "evaluate", task)

    # 启动后台线程评测
    t = threading.Thread(target=_evaluate_cases_worker, args=(task_id,))
    t.daemon = True
    t.start()

    return jsonify({"task_id": task_id, "status": "running", "total": len(cases)})


@app.route("/api/test_cases/evaluate/<task_id>", methods=["GET"])
def get_evaluate_status(task_id):
    """获取评测任务状态"""
    task = _evaluate_tasks.get(task_id) or _load_async_task(task_id)
    if not task:
        return jsonify({"error": "task not found"}), 404
    return jsonify(task)


def _evaluate_cases_worker(task_id):
    """后台评测用例的 worker"""
    task = _evaluate_tasks.get(task_id) or _load_async_task(task_id)
    if not task:
        return
    _evaluate_tasks[task_id] = task  # 确保本地缓存有

    try:
        conn = get_db_connection()
        case_ids = task["case_ids"]

        for case_id in case_ids:
            # 获取用例详情
            case = execute_query(conn,
                "SELECT * FROM test_cases WHERE id = " + ("%s" if USE_MYSQL else "?"),
                (case_id,), fetch_one=True)
            if not case:
                continue
            case = row_to_dict(case)

            task["progress"]["current_case_id"] = case.get("case_id", str(case_id))
            print(f"[EVAL] {task_id} evaluating {case['case_id']}...", flush=True)

            # 检查是否有实际输出
            if not case.get("actual_output"):
                task["errors"].append({"case_id": case["case_id"], "error": "no actual_output"})
                task["progress"]["done"] += 1
                continue

            try:
                # 加载用户事实
                user_facts = _load_user_facts(conn, case.get("persona_id")) if case.get("persona_id") else []

                # 调用评测函数
                llm_config = get_llm_config()
                result = pipi_api.evaluate_test_case(case, **llm_config["eval_case"], user_facts=user_facts)

                score = result.get("score")
                reason = result.get("deduction_reason", "")
                status = result.get("status", "pending")

                if score is not None:
                    # 更新数据库
                    execute_query(conn,
                        "UPDATE test_cases SET score = " + ("%s" if USE_MYSQL else "?") +
                        ", deduction_reason = " + ("%s" if USE_MYSQL else "?") +
                        ", status = " + ("%s" if USE_MYSQL else "?") +
                        " WHERE id = " + ("%s" if USE_MYSQL else "?"),
                        (score, reason, status, case_id))
                    conn.commit()
                    task["evaluated_count"] += 1
                    if status == "passed":
                        task["passed_count"] += 1
                    else:
                        task["failed_count"] += 1
                else:
                    task["errors"].append({"case_id": case["case_id"], "error": reason})

            except Exception as e:
                print(f"[EVAL ERROR] {case['case_id']}: {e}", flush=True)
                task["errors"].append({"case_id": case["case_id"], "error": str(e)})

            task["progress"]["done"] += 1
            task["cases_evaluated"] = task.get("cases_evaluated", 0) + (1 if score is not None else 0)
            _update_async_task(task_id, task)

        conn.close()
        task["status"] = "completed"
        _update_async_task(task_id, task)
        print(f"[EVAL] {task_id} completed, evaluated {task.get('cases_evaluated', 0)} cases, passed {task['passed_count']}, failed {task['failed_count']}", flush=True)

    except Exception as e:
        import traceback
        print(f"[EVAL FATAL] {task_id}: {e}\n{traceback.format_exc()}", flush=True)
        task["status"] = "failed"
        task["errors"].append({"case_id": "global", "error": str(e)})
        _update_async_task(task_id, task)


# ─── 测试报告生成 ─────────────────────────────────

@app.route("/api/test_report", methods=["GET"])
def generate_test_report():
    """
    生成测试报告 HTML
    参数:
        task_id: 评测任务ID（优先）- 按任务维度生成报告
        persona_id: 用户ID（备选）- 按角色维度生成报告
    """
    task_id = request.args.get("task_id", "")
    persona_id = request.args.get("persona_id", "")

    conn = get_db_connection()

    # 如果指定了 task_id，从任务中获取 case_ids
    case_ids = []
    task_info = None
    if task_id:
        task = execute_query(conn, "SELECT * FROM async_tasks WHERE id = %s", (task_id,), fetch_one=True)
        if task:
            task_info = row_to_dict(task)
            config = json.loads(task_info.get("config_json", "{}"))
            case_ids = config.get("case_ids", [])
            if not persona_id:
                persona_id = task_info.get("persona_id", "")

    # 构建 WHERE 子句
    where_clause = ""
    params = []
    if case_ids:
        placeholders = ",".join(["%s"] * len(case_ids))
        where_clause = f" WHERE id IN ({placeholders})"
        params = case_ids
    elif persona_id:
        where_clause = " WHERE persona_id = %s"
        params = [persona_id]

    # 总用例数
    total = execute_query(conn, f"SELECT COUNT(*) as cnt FROM test_cases{where_clause}", params, fetch_one=True)
    total_count = total["cnt"] if total else 0

    # 各状态统计
    stats_sql = f"""
        SELECT
            COUNT(*) as total,
            SUM(CASE WHEN actual_output IS NOT NULL AND actual_output != '' THEN 1 ELSE 0 END) as executed,
            SUM(CASE WHEN score IS NOT NULL THEN 1 ELSE 0 END) as evaluated,
            SUM(CASE WHEN status = 'passed' THEN 1 ELSE 0 END) as passed,
            SUM(CASE WHEN status = 'failed' THEN 1 ELSE 0 END) as failed,
            AVG(CASE WHEN score IS NOT NULL THEN score ELSE NULL END) as avg_score
        FROM test_cases{where_clause}
    """
    overview = execute_query(conn, stats_sql, params, fetch_one=True)

    # 2. 按维度统计
    if case_ids:
        placeholders = ",".join(["%s"] * len(case_ids))
        dimension_where = f"WHERE tc.id IN ({placeholders})"
    elif persona_id:
        dimension_where = "WHERE tc.persona_id = %s"
    else:
        dimension_where = ""

    dimension_sql = f"""
        SELECT
            tc.dimension_code,
            td.dimension_name,
            td.cluster_code,
            td.cluster_name,
            COUNT(*) as total,
            SUM(CASE WHEN tc.status = 'passed' THEN 1 ELSE 0 END) as passed,
            SUM(CASE WHEN tc.status = 'failed' THEN 1 ELSE 0 END) as failed,
            AVG(CASE WHEN tc.score IS NOT NULL THEN tc.score ELSE NULL END) as avg_score
        FROM test_cases tc
        LEFT JOIN test_dimensions td ON tc.dimension_code = td.dimension_code
        {dimension_where}
        GROUP BY tc.dimension_code, td.dimension_name, td.cluster_code, td.cluster_name
        ORDER BY tc.dimension_code
    """
    dimensions = execute_query(conn, dimension_sql, params, fetch_all=True)
    dimensions = [row_to_dict(d) for d in dimensions] if dimensions else []

    # 3. 获取已评测的用例详情
    if case_ids:
        placeholders = ",".join(["%s"] * len(case_ids))
        cases_sql = f"""
            SELECT tc.*, td.dimension_name, td.cluster_name
            FROM test_cases tc
            LEFT JOIN test_dimensions td ON tc.dimension_code = td.dimension_code
            WHERE tc.id IN ({placeholders}) AND tc.score IS NOT NULL
            ORDER BY tc.dimension_code, tc.case_id
        """
        cases_params = case_ids
    elif persona_id:
        cases_sql = """
            SELECT tc.*, td.dimension_name, td.cluster_name
            FROM test_cases tc
            LEFT JOIN test_dimensions td ON tc.dimension_code = td.dimension_code
            WHERE tc.score IS NOT NULL AND tc.persona_id = %s
            ORDER BY tc.dimension_code, tc.case_id
        """
        cases_params = [persona_id]
    else:
        cases_sql = """
            SELECT tc.*, td.dimension_name, td.cluster_name
            FROM test_cases tc
            LEFT JOIN test_dimensions td ON tc.dimension_code = td.dimension_code
            WHERE tc.score IS NOT NULL
            ORDER BY tc.dimension_code, tc.case_id
        """
        cases_params = []

    cases = execute_query(conn, cases_sql, cases_params, fetch_all=True)
    cases = [row_to_dict(c) for c in cases] if cases else []

    conn.close()

    # 4. 生成 HTML 报告
    overview_dict = row_to_dict(overview) if overview else {}
    avg_score = overview_dict.get("avg_score")
    avg_score_str = f"{float(avg_score):.1f}" if avg_score else "N/A"
    evaluated = overview_dict.get("evaluated") or 0
    passed = overview_dict.get("passed") or 0
    failed = overview_dict.get("failed") or 0
    pass_rate = round(passed / evaluated * 100, 1) if evaluated > 0 else 0

    report_time = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    # 报告副标题：显示任务信息或角色信息
    if task_info:
        task_created = task_info.get("created_at", "")
        if hasattr(task_created, "strftime"):
            task_created = task_created.strftime("%Y-%m-%d %H:%M:%S")
        report_subtitle = f"评测任务: {task_id} | 角色: {persona_id} | 任务创建: {task_created} | 报告生成: {report_time}"
    elif persona_id:
        report_subtitle = f"角色: {persona_id} | 生成时间: {report_time}"
    else:
        report_subtitle = f"生成时间: {report_time}"

    # 按 cluster 分组维度
    clusters = {}
    for d in dimensions:
        cluster = d.get("cluster_code", "X")
        if cluster not in clusters:
            clusters[cluster] = {
                "name": d.get("cluster_name", "未知"),
                "dimensions": []
            }
        clusters[cluster]["dimensions"].append(d)

    # 生成维度表格 HTML
    dimension_rows = ""
    for cluster_code in sorted(clusters.keys()):
        cluster = clusters[cluster_code]
        for i, d in enumerate(cluster["dimensions"]):
            d_passed = d.get("passed") or 0
            d_failed = d.get("failed") or 0
            d_evaluated = d_passed + d_failed
            d_rate = round(d_passed / d_evaluated * 100) if d_evaluated > 0 else 0
            d_avg = f"{float(d['avg_score']):.1f}" if d.get("avg_score") else "-"
            status_class = "good" if d_rate >= 80 else ("warn" if d_rate >= 60 else "bad")

            dimension_rows += f"""
            <tr>
                {"<td rowspan='" + str(len(cluster['dimensions'])) + "' class='cluster-cell'>" + cluster['name'] + "</td>" if i == 0 else ""}
                <td>{d.get('dimension_code', '')}</td>
                <td>{d.get('dimension_name', '')}</td>
                <td>{d.get('total', 0)}</td>
                <td class="num-passed">{d_passed}</td>
                <td class="num-failed">{d_failed}</td>
                <td class="{status_class}">{d_rate}%</td>
                <td>{d_avg}</td>
            </tr>
            """

    # 生成用例详情 HTML
    case_details = ""
    current_dimension = ""
    for c in cases:
        if c.get("dimension_code") != current_dimension:
            if current_dimension:
                case_details += "</div></div>"
            current_dimension = c.get("dimension_code", "")
            dim_name = c.get("dimension_name", "")
            case_details += f"""
            <div class="dimension-section">
                <div class="dimension-header" onclick="this.parentElement.classList.toggle('collapsed')">
                    <span class="toggle-icon">▼</span>
                    {current_dimension} - {dim_name}
                </div>
                <div class="dimension-cases">
            """

        score = c.get("score")
        score_class = "score-pass" if score and score >= 6 else "score-fail"
        status_badge = f"<span class='badge badge-pass'>通过</span>" if c.get("status") == "passed" else "<span class='badge badge-fail'>失败</span>"

        # 清理 actual_output 中的 emotion 标签用于显示
        import re
        actual = c.get("actual_output", "") or ""
        actual_display = re.sub(r'\{"emotion":\s*"[^"]*"\}\s*', '', actual)

        case_details += f"""
        <div class="case-item">
            <div class="case-header">
                <span class="case-id">{c.get('case_id', '')}</span>
                <span class="case-title">{c.get('title', '')}</span>
                {status_badge}
                <span class="{score_class}">{score}分</span>
            </div>
            <div class="case-body">
                <div class="case-row">
                    <div class="case-label">用户输入</div>
                    <div class="case-content">{c.get('input_text', '').replace(chr(10), '<br>')}</div>
                </div>
                <div class="case-row">
                    <div class="case-label">期望输出</div>
                    <div class="case-content">{c.get('expected_output', '').replace(chr(10), '<br>')}</div>
                </div>
                <div class="case-row">
                    <div class="case-label">实际回复</div>
                    <div class="case-content actual">{actual_display.replace(chr(10), '<br>')}</div>
                </div>
                <div class="case-row">
                    <div class="case-label">扣分原因</div>
                    <div class="case-content deduction">{c.get('deduction_reason', '') or '无'}</div>
                </div>
            </div>
        </div>
        """

    if current_dimension:
        case_details += "</div></div>"

    html = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>AI玩偶测试报告</title>
    <style>
        * {{ margin: 0; padding: 0; box-sizing: border-box; }}
        body {{
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, 'Helvetica Neue', Arial, sans-serif;
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            min-height: 100vh;
            padding: 40px 20px;
        }}
        .container {{
            max-width: 1200px;
            margin: 0 auto;
        }}
        .report-header {{
            background: white;
            border-radius: 16px;
            padding: 32px;
            margin-bottom: 24px;
            box-shadow: 0 10px 40px rgba(0,0,0,0.1);
        }}
        .report-title {{
            font-size: 28px;
            font-weight: 700;
            color: #1d1d1f;
            margin-bottom: 8px;
        }}
        .report-subtitle {{
            font-size: 14px;
            color: #86868b;
        }}
        .overview-cards {{
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
            gap: 16px;
            margin-bottom: 24px;
        }}
        .overview-card {{
            background: white;
            border-radius: 12px;
            padding: 24px;
            box-shadow: 0 4px 20px rgba(0,0,0,0.08);
            text-align: center;
        }}
        .overview-card .value {{
            font-size: 36px;
            font-weight: 700;
            margin-bottom: 4px;
        }}
        .overview-card .label {{
            font-size: 13px;
            color: #86868b;
        }}
        .overview-card.primary .value {{ color: #007aff; }}
        .overview-card.success .value {{ color: #34c759; }}
        .overview-card.danger .value {{ color: #ff3b30; }}
        .overview-card.warning .value {{ color: #ff9500; }}

        .section {{
            background: white;
            border-radius: 16px;
            padding: 24px;
            margin-bottom: 24px;
            box-shadow: 0 10px 40px rgba(0,0,0,0.1);
        }}
        .section-title {{
            font-size: 18px;
            font-weight: 600;
            color: #1d1d1f;
            margin-bottom: 16px;
            padding-bottom: 12px;
            border-bottom: 1px solid #e5e5ea;
        }}

        table {{
            width: 100%;
            border-collapse: collapse;
            font-size: 13px;
        }}
        th, td {{
            padding: 12px;
            text-align: left;
            border-bottom: 1px solid #e5e5ea;
        }}
        th {{
            background: #f5f5f7;
            font-weight: 600;
            color: #1d1d1f;
        }}
        .cluster-cell {{
            background: #f0f0f5;
            font-weight: 600;
            vertical-align: middle;
        }}
        .num-passed {{ color: #34c759; font-weight: 600; }}
        .num-failed {{ color: #ff3b30; font-weight: 600; }}
        .good {{ color: #34c759; font-weight: 600; }}
        .warn {{ color: #ff9500; font-weight: 600; }}
        .bad {{ color: #ff3b30; font-weight: 600; }}

        .dimension-section {{
            margin-bottom: 16px;
            border: 1px solid #e5e5ea;
            border-radius: 8px;
            overflow: hidden;
        }}
        .dimension-section.collapsed .dimension-cases {{
            display: none;
        }}
        .dimension-section.collapsed .toggle-icon {{
            transform: rotate(-90deg);
        }}
        .dimension-header {{
            background: #f5f5f7;
            padding: 12px 16px;
            font-weight: 600;
            cursor: pointer;
            display: flex;
            align-items: center;
            gap: 8px;
        }}
        .dimension-header:hover {{
            background: #e5e5ea;
        }}
        .toggle-icon {{
            font-size: 12px;
            transition: transform 0.2s;
        }}
        .dimension-cases {{
            padding: 12px;
        }}

        .case-item {{
            background: #fafafa;
            border-radius: 8px;
            margin-bottom: 12px;
            overflow: hidden;
        }}
        .case-header {{
            padding: 12px 16px;
            display: flex;
            align-items: center;
            gap: 12px;
            border-bottom: 1px solid #e5e5ea;
        }}
        .case-id {{
            font-family: monospace;
            font-weight: 600;
            color: #007aff;
        }}
        .case-title {{
            flex: 1;
            color: #1d1d1f;
        }}
        .badge {{
            padding: 2px 8px;
            border-radius: 4px;
            font-size: 11px;
            font-weight: 600;
        }}
        .badge-pass {{ background: #d1f2d9; color: #1b7a3a; }}
        .badge-fail {{ background: #fdd; color: #c41e3a; }}
        .score-pass {{ font-weight: 700; color: #34c759; }}
        .score-fail {{ font-weight: 700; color: #ff3b30; }}

        .case-body {{
            padding: 16px;
        }}
        .case-row {{
            margin-bottom: 12px;
        }}
        .case-row:last-child {{
            margin-bottom: 0;
        }}
        .case-label {{
            font-size: 11px;
            font-weight: 600;
            color: #86868b;
            text-transform: uppercase;
            margin-bottom: 4px;
        }}
        .case-content {{
            font-size: 13px;
            color: #1d1d1f;
            line-height: 1.6;
            white-space: pre-wrap;
        }}
        .case-content.actual {{
            background: #e8f4fd;
            padding: 8px 12px;
            border-radius: 6px;
            border-left: 3px solid #007aff;
        }}
        .case-content.deduction {{
            color: #ff3b30;
            font-style: italic;
        }}

        .footer {{
            text-align: center;
            color: rgba(255,255,255,0.7);
            font-size: 12px;
            margin-top: 24px;
        }}

        @media print {{
            body {{ background: white; padding: 20px; }}
            .section, .report-header, .overview-card {{
                box-shadow: none;
                border: 1px solid #e5e5ea;
            }}
        }}
    </style>
</head>
<body>
    <div class="container">
        <div class="report-header">
            <h1 class="report-title">🧸 AI玩偶测试报告</h1>
            <p class="report-subtitle">{report_subtitle}</p>
        </div>

        <div class="overview-cards">
            <div class="overview-card primary">
                <div class="value">{total_count}</div>
                <div class="label">总用例数</div>
            </div>
            <div class="overview-card">
                <div class="value">{evaluated}</div>
                <div class="label">已评测</div>
            </div>
            <div class="overview-card success">
                <div class="value">{passed}</div>
                <div class="label">通过</div>
            </div>
            <div class="overview-card danger">
                <div class="value">{failed}</div>
                <div class="label">失败</div>
            </div>
            <div class="overview-card warning">
                <div class="value">{pass_rate}%</div>
                <div class="label">通过率</div>
            </div>
            <div class="overview-card">
                <div class="value">{avg_score_str}</div>
                <div class="label">平均分</div>
            </div>
        </div>

        <div class="section">
            <h2 class="section-title">📊 维度统计</h2>
            <table>
                <thead>
                    <tr>
                        <th>能力群</th>
                        <th>维度</th>
                        <th>名称</th>
                        <th>用例数</th>
                        <th>通过</th>
                        <th>失败</th>
                        <th>通过率</th>
                        <th>平均分</th>
                    </tr>
                </thead>
                <tbody>
                    {dimension_rows if dimension_rows else "<tr><td colspan='8' style='text-align:center;color:#86868b'>暂无数据</td></tr>"}
                </tbody>
            </table>
        </div>

        <div class="section">
            <h2 class="section-title">📝 用例详情（点击维度可折叠）</h2>
            {case_details if case_details else "<p style='color:#86868b;text-align:center'>暂无已评测的用例</p>"}
        </div>

        <div class="footer">
            AI玩偶自动化测试系统 · Powered by Claude
        </div>
    </div>
</body>
</html>
"""

    return html, 200, {"Content-Type": "text/html; charset=utf-8"}


# ─── 预约任务 API ─────────────────────────────────────

@app.route("/api/scheduled_tasks", methods=["GET"])
def list_scheduled_tasks():
    """获取预约任务列表"""
    status = request.args.get("status", "")
    limit = int(request.args.get("limit", 20))

    conn = get_db_connection()
    sql = "SELECT * FROM scheduled_tasks WHERE 1=1"
    params = []
    if status:
        sql += " AND status = %s" if USE_MYSQL else " AND status = ?"
        params.append(status)
    sql += " ORDER BY scheduled_at DESC LIMIT %s" if USE_MYSQL else " ORDER BY scheduled_at DESC LIMIT ?"
    params.append(limit)

    rows = execute_query(conn, sql, params, fetch_all=True)
    conn.close()

    result = []
    for r in rows:
        item = row_to_dict(r)
        if item.get("config_json"):
            try:
                item["config"] = json.loads(item["config_json"])
            except:
                item["config"] = {}
        item["scheduled_at"] = str(item["scheduled_at"]) if item.get("scheduled_at") else None
        item["created_at"] = str(item["created_at"]) if item.get("created_at") else None
        item["started_at"] = str(item["started_at"]) if item.get("started_at") else None
        item["completed_at"] = str(item["completed_at"]) if item.get("completed_at") else None
        result.append(item)

    return jsonify(result)


@app.route("/api/scheduled_tasks", methods=["POST"])
def create_scheduled_task():
    """创建预约任务
    请求参数:
        task_type: generate/execute/evaluate/full_flow
        scheduled_at: "2026-05-21 09:00:00" 预约时间
        config: {
            # generate
            persona_id, dimension_codes, count_per_dimension, clear_existing
            # execute
            persona_id, dimension_codes
            # evaluate
            task_id 或 persona_id
            # full_flow
            以上全部
        }
    """
    data = request.get_json() or {}
    task_type = data.get("task_type", "").strip()
    scheduled_at = data.get("scheduled_at", "").strip()
    config = data.get("config", {})

    if task_type not in ("generate", "execute", "evaluate", "full_flow"):
        return jsonify({"error": "task_type must be generate/execute/evaluate/full_flow"}), 400

    if not scheduled_at:
        return jsonify({"error": "scheduled_at is required"}), 400

    # 验证时间格式
    try:
        from datetime import datetime, timedelta
        scheduled_dt = datetime.strptime(scheduled_at, "%Y-%m-%d %H:%M:%S")
        # 允许当前时间前5分钟内的预约（避免时区/时钟偏差问题）
        if scheduled_dt < datetime.now() - timedelta(minutes=5):
            return jsonify({"error": "scheduled_at must be in the future"}), 400
    except ValueError:
        return jsonify({"error": "scheduled_at format: YYYY-MM-DD HH:MM:SS"}), 400

    # 验证配置
    if task_type == "generate":
        if not config.get("persona_id"):
            return jsonify({"error": "config.persona_id is required for generate"}), 400
    elif task_type == "execute":
        if not config.get("persona_id"):
            return jsonify({"error": "config.persona_id is required for execute"}), 400
    elif task_type == "evaluate":
        if not config.get("task_id") and not config.get("persona_id"):
            return jsonify({"error": "config.task_id or config.persona_id is required for evaluate"}), 400
    elif task_type == "full_flow":
        if not config.get("persona_id"):
            return jsonify({"error": "config.persona_id is required for full_flow"}), 400

    conn = get_db_connection()
    execute_query(conn, """
        INSERT INTO scheduled_tasks (task_type, scheduled_at, config_json, status)
        VALUES (%s, %s, %s, 'pending')
    """ if USE_MYSQL else """
        INSERT INTO scheduled_tasks (task_type, scheduled_at, config_json, status)
        VALUES (?, ?, ?, 'pending')
    """, (task_type, scheduled_at, json.dumps(config, ensure_ascii=False)))
    conn.commit()

    # 获取新创建的ID
    row = execute_query(conn, "SELECT LAST_INSERT_ID() as id" if USE_MYSQL else "SELECT last_insert_rowid() as id", fetch_one=True)
    task_id = row["id"] if row else None
    conn.close()

    return jsonify({"ok": True, "id": task_id, "scheduled_at": scheduled_at})


@app.route("/api/scheduled_tasks/<int:task_id>", methods=["DELETE"])
def cancel_scheduled_task(task_id):
    """取消预约任务"""
    conn = get_db_connection()
    row = execute_query(conn, "SELECT status FROM scheduled_tasks WHERE id = %s" if USE_MYSQL else "SELECT status FROM scheduled_tasks WHERE id = ?", (task_id,), fetch_one=True)

    if not row:
        conn.close()
        return jsonify({"error": "task not found"}), 404

    if row["status"] != "pending":
        conn.close()
        return jsonify({"error": f"cannot cancel task with status: {row['status']}"}), 400

    execute_query(conn, "UPDATE scheduled_tasks SET status = 'cancelled' WHERE id = %s" if USE_MYSQL else "UPDATE scheduled_tasks SET status = 'cancelled' WHERE id = ?", (task_id,))
    conn.commit()
    conn.close()

    return jsonify({"ok": True})


# ─── 预约任务执行器 ─────────────────────────────────────

_scheduler_running = False

def _run_scheduled_task(task):
    """执行预约任务（状态已在调度器中更新为 running）"""
    from datetime import datetime

    task_id = task["id"]
    task_type = task["task_type"]
    config = json.loads(task["config_json"]) if task["config_json"] else {}

    print(f"[SCHEDULER] Starting scheduled task {task_id}: {task_type}", flush=True)

    try:
        result_ids = []

        generated_case_ids = []  # 记录本次生成的用例ID

        if task_type == "generate" or task_type == "full_flow":
            # 生成用例
            import uuid
            gen_task_id = str(uuid.uuid4())[:8]
            gen_task_data = {
                "status": "running",
                "persona_id": config.get("persona_id"),
                "dimension_codes": config.get("dimension_codes"),
                "count_per_dimension": config.get("count_per_dimension", 5),
                "clear_existing": config.get("clear_existing", False),
                "progress": {"total": 0, "done": 0, "current": None},
                "cases_created": 0,
                "errors": [],
                "created_case_ids": [],  # 收集生成的用例ID
            }
            _generate_tasks[gen_task_id] = gen_task_data
            _save_async_task(gen_task_id, "generate", gen_task_data)

            # 同步执行
            _generate_cases_worker(gen_task_id)
            result_ids.append(f"gen:{gen_task_id}")

            # 获取本次生成的用例ID
            generated_case_ids = _generate_tasks.get(gen_task_id, {}).get("created_case_ids", [])

            # full_flow 模式：等待用例审核完成且无不合格
            if task_type == "full_flow" and generated_case_ids:
                print(f"[FULL FLOW] Waiting for quality review of {len(generated_case_ids)} cases...", flush=True)
                generated_case_ids = _wait_for_quality_review(
                    persona_id=config.get("persona_id"),
                    max_wait_seconds=1800,  # 最多等待30分钟
                    check_interval=10  # 每10秒检查一次
                )
                print(f"[FULL FLOW] Quality review completed, {len(generated_case_ids)} cases ready for execution", flush=True)

        if task_type == "execute" or task_type == "full_flow":
            # 执行用例 - 使用 test_tasks 流程
            import uuid

            conn = get_db_connection()

            # full_flow 模式下，只执行刚刚生成的用例
            if task_type == "full_flow" and generated_case_ids:
                case_ids = generated_case_ids
            else:
                # 单独执行模式，按配置查询用例
                sql = "SELECT id FROM test_cases WHERE persona_id = %s" if USE_MYSQL else "SELECT id FROM test_cases WHERE persona_id = ?"
                params = [config.get("persona_id")]
                if config.get("dimension_codes"):
                    placeholders = ",".join(["%s" if USE_MYSQL else "?"] * len(config["dimension_codes"]))
                    sql += f" AND dimension_code IN ({placeholders})"
                    params.extend(config["dimension_codes"])
                cases = execute_query(conn, sql, params, fetch_all=True)
                case_ids = [c["id"] for c in cases]

            if case_ids:
                # 创建 test_task 记录
                task_code = f"{config.get('persona_id', 'unknown')}-{datetime.now().strftime('%Y%m%d%H%M')}"
                task_name = config.get("name") or f"预约执行 {datetime.now().strftime('%m-%d %H:%M')}"
                # 从 personas 表获取真正的 device_id
                persona_row = execute_query(conn, "SELECT device_id FROM personas WHERE id = %s" if USE_MYSQL else "SELECT device_id FROM personas WHERE id = ?", (config.get("persona_id"),), fetch_one=True)
                device_id = (persona_row["device_id"] if persona_row else None) or config.get("device_id") or config.get("persona_id")
                dimension_codes = config.get("dimension_codes") or []

                target_api = config.get("target_api", "pipi")
                cursor = execute_query(conn,
                    """INSERT INTO test_tasks (task_id, name, persona_id, device_id, dimension_codes, case_ids, status, progress_total, target_api)
                       VALUES (%s, %s, %s, %s, %s, %s, 'running', %s, %s)""" if USE_MYSQL else
                    """INSERT INTO test_tasks (task_id, name, persona_id, device_id, dimension_codes, case_ids, status, progress_total, target_api)
                       VALUES (?, ?, ?, ?, ?, ?, 'running', ?, ?)""",
                    (task_code, task_name, config.get("persona_id"), device_id, json.dumps(dimension_codes), json.dumps(case_ids), len(case_ids), target_api))
                conn.commit()
                test_task_id = get_lastrowid(cursor)

                # 创建 test_results 记录
                for case_id in case_ids:
                    execute_query(conn,
                        "INSERT INTO test_results (task_id, case_id, status) VALUES (%s, %s, 'pending')" if USE_MYSQL else
                        "INSERT INTO test_results (task_id, case_id, status) VALUES (?, ?, 'pending')",
                        (test_task_id, case_id))
                conn.commit()
                conn.close()

                # 同步执行（复用 _execute_task_worker）
                _execute_task_worker(test_task_id)
                result_ids.append(f"task:{test_task_id}")
            else:
                conn.close()

        if task_type == "evaluate" or task_type == "full_flow":
            # 评测用例 - 使用 test_tasks 流程
            # 如果是 full_flow，评测刚创建的 test_task
            # 如果是单独的 evaluate，需要找到指定的 test_task
            test_task_id = config.get("test_task_id")

            if not test_task_id and result_ids:
                # full_flow 模式下，使用刚创建的 task
                for rid in result_ids:
                    if rid.startswith("task:"):
                        test_task_id = int(rid.split(":")[1])
                        break

            if test_task_id:
                conn = get_db_connection()
                # 统计待评测数量
                eval_count = execute_query(conn,
                    "SELECT COUNT(*) as cnt FROM test_results WHERE task_id = %s AND status = 'executed'" if USE_MYSQL else
                    "SELECT COUNT(*) as cnt FROM test_results WHERE task_id = ? AND status = 'executed'",
                    (test_task_id,), fetch_one=True)
                eval_total = eval_count["cnt"] if eval_count else 0

                if eval_total > 0:
                    # 更新状态为 evaluating
                    execute_query(conn,
                        "UPDATE test_tasks SET status = 'evaluating', progress_done = 0, progress_total = %s WHERE id = %s" if USE_MYSQL else
                        "UPDATE test_tasks SET status = 'evaluating', progress_done = 0, progress_total = ? WHERE id = ?",
                        (eval_total, test_task_id))
                    conn.commit()
                    conn.close()

                    # 同步评测
                    _evaluate_task_worker(test_task_id)
                    result_ids.append(f"eval:{test_task_id}")
                else:
                    conn.close()
            else:
                print(f"[SCHEDULER] No test_task_id for evaluate", flush=True)

        # 更新完成状态
        conn = get_db_connection()
        execute_query(conn, """
            UPDATE scheduled_tasks SET status = 'completed', completed_at = %s, result_task_id = %s WHERE id = %s
        """ if USE_MYSQL else """
            UPDATE scheduled_tasks SET status = 'completed', completed_at = ?, result_task_id = ? WHERE id = ?
        """, (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), ",".join(result_ids), task_id))
        conn.commit()
        conn.close()

        print(f"[SCHEDULER] Task {task_id} completed: {result_ids}", flush=True)

    except Exception as e:
        import traceback
        print(f"[SCHEDULER] Task {task_id} failed: {e}\n{traceback.format_exc()}", flush=True)

        conn = get_db_connection()
        execute_query(conn, """
            UPDATE scheduled_tasks SET status = 'failed', completed_at = %s, error_message = %s WHERE id = %s
        """ if USE_MYSQL else """
            UPDATE scheduled_tasks SET status = 'failed', completed_at = ?, error_message = ? WHERE id = ?
        """, (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), str(e), task_id))
        conn.commit()
        conn.close()


def _scheduler_loop():
    """后台调度循环，每 60 秒检查一次"""
    global _scheduler_running
    from datetime import datetime
    import time
    from datetime import timedelta

    print("[SCHEDULER] Starting scheduler loop...", flush=True)

    # 启动时检查并恢复卡住的 running 任务（超过30分钟视为卡住）
    try:
        conn = get_db_connection()
        stale_time = (datetime.now() - timedelta(minutes=30)).strftime("%Y-%m-%d %H:%M:%S")
        # 将超时的 running 任务重置为 pending
        execute_query(conn,
            "UPDATE scheduled_tasks SET status = 'pending', started_at = NULL WHERE status = 'running' AND started_at < %s" if USE_MYSQL else
            "UPDATE scheduled_tasks SET status = 'pending', started_at = NULL WHERE status = 'running' AND started_at < ?",
            (stale_time,))
        conn.commit()
        conn.close()
        print("[SCHEDULER] Recovered stale running tasks", flush=True)
    except Exception as e:
        print(f"[SCHEDULER] Error recovering stale tasks: {e}", flush=True)

    while _scheduler_running:
        try:
            conn = get_db_connection()
            now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

            # 查找到期的任务
            tasks = execute_query(conn, """
                SELECT * FROM scheduled_tasks WHERE status = 'pending' AND scheduled_at <= %s
            """ if USE_MYSQL else """
                SELECT * FROM scheduled_tasks WHERE status = 'pending' AND scheduled_at <= ?
            """, (now,), fetch_all=True)
            conn.close()

            for task in tasks:
                task = row_to_dict(task)
                task_id = task['id']
                print(f"[SCHEDULER] Found due task: {task_id} ({task['task_type']})", flush=True)

                # 原子操作：只有 pending 状态才能改为 running，防止重复执行
                conn2 = get_db_connection()
                cursor = conn2.cursor()
                if USE_MYSQL:
                    cursor.execute(
                        "UPDATE scheduled_tasks SET status = 'running', started_at = %s WHERE id = %s AND status = 'pending'",
                        (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), task_id))
                else:
                    cursor.execute(
                        "UPDATE scheduled_tasks SET status = 'running', started_at = ? WHERE id = ? AND status = 'pending'",
                        (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), task_id))
                affected = cursor.rowcount
                conn2.commit()
                conn2.close()

                if affected == 0:
                    print(f"[SCHEDULER] Task {task_id} already taken by another worker, skipping", flush=True)
                    continue

                # 成功获取锁，启动线程执行
                t = threading.Thread(target=_run_scheduled_task, args=(task,), daemon=True)
                t.start()

        except Exception as e:
            print(f"[SCHEDULER] Error in scheduler loop: {e}", flush=True)

        time.sleep(60)

    print("[SCHEDULER] Scheduler loop stopped", flush=True)


_scheduler_lock_file = None

def start_scheduler():
    """启动调度器（使用文件锁确保只有一个 worker 运行）"""
    global _scheduler_running, _scheduler_lock_file
    if _scheduler_running:
        return

    # 尝试获取文件锁，只有一个 worker 能拿到
    import fcntl
    lock_path = "/tmp/pipi_scheduler.lock"
    try:
        _scheduler_lock_file = open(lock_path, 'w')
        fcntl.flock(_scheduler_lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except (IOError, OSError):
        # 其他 worker 已持有锁，跳过
        print("[SCHEDULER] Another worker holds scheduler lock, skipping", flush=True)
        return

    _scheduler_running = True
    t = threading.Thread(target=_scheduler_loop, daemon=True)
    t.start()
    print("[SCHEDULER] Scheduler started (holding lock)", flush=True)


# 应用启动时自动启动调度器
start_scheduler()


# ─── API 接口配置管理 ─────────────────────────────────────

@app.route("/api/endpoints", methods=["GET"])
def get_endpoints():
    """获取所有接口配置"""
    conn = get_db_connection()
    rows = execute_query(conn, "SELECT * FROM api_endpoints ORDER BY id", fetch_all=True)
    conn.close()
    return jsonify([row_to_dict(r) for r in rows])


@app.route("/api/endpoints/<int:eid>", methods=["GET"])
def get_endpoint(eid):
    """获取单个接口配置"""
    conn = get_db_connection()
    ph = "%s" if USE_MYSQL else "?"
    row = execute_query(conn, f"SELECT * FROM api_endpoints WHERE id = {ph}", (eid,), fetch_one=True)
    conn.close()
    if not row:
        return jsonify({"error": "not found"}), 404
    return jsonify(row_to_dict(row))


@app.route("/api/endpoints", methods=["POST"])
def create_endpoint():
    """创建接口配置"""
    data = request.get_json() or {}
    name = data.get("name", "").strip()
    code = data.get("code", "").strip()
    base_url = data.get("base_url", "").strip()

    if not name or not code or not base_url:
        return jsonify({"error": "name, code, base_url are required"}), 400

    conn = get_db_connection()
    ph = "%s" if USE_MYSQL else "?"
    try:
        # 合并 headers 到 auth_config
        auth_config = data.get("auth_config", {})
        if isinstance(auth_config, str):
            auth_config = json.loads(auth_config)
        headers = data.get("headers")
        if headers and isinstance(headers, dict):
            auth_config["headers"] = headers

        execute_query(conn, f"""
            INSERT INTO api_endpoints (name, code, base_url, auth_type, auth_config, request_template, timeout_sec)
            VALUES ({ph}, {ph}, {ph}, {ph}, {ph}, {ph}, {ph})
        """, (name, code, base_url,
              data.get("auth_type", "none"),
              json.dumps(auth_config, ensure_ascii=False) if auth_config else None,
              json.dumps(data.get("request_template", {}), ensure_ascii=False) if data.get("request_template") else None,
              data.get("timeout_sec", 30)))
        conn.commit()
        new_id = conn.cursor().lastrowid if USE_MYSQL else conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    except Exception as e:
        conn.close()
        return jsonify({"error": str(e)}), 400
    conn.close()
    return jsonify({"ok": True, "id": new_id})


@app.route("/api/endpoints/<int:eid>", methods=["PUT"])
def update_endpoint(eid):
    """更新接口配置"""
    data = request.get_json() or {}
    conn = get_db_connection()
    ph = "%s" if USE_MYSQL else "?"

    updates = []
    params = []
    for field in ["name", "code", "base_url", "auth_type", "timeout_sec", "is_active"]:
        if field in data:
            updates.append(f"{field} = {ph}")
            params.append(data[field])
    # headers 需合并到 auth_config
    headers = data.get("headers")
    if headers is not None:
        existing = execute_query(conn, f"SELECT auth_config FROM api_endpoints WHERE id = {ph}", (eid,), fetch_one=True)
        auth_config = {}
        if existing and existing[0]:
            try:
                auth_config = json.loads(existing[0]) if isinstance(existing[0], str) else existing[0]
            except:
                pass
        if isinstance(headers, dict):
            auth_config["headers"] = headers
        updates.append(f"auth_config = {ph}")
        params.append(json.dumps(auth_config, ensure_ascii=False) if auth_config else None)
    elif "auth_config" in data:
        updates.append(f"auth_config = {ph}")
        params.append(json.dumps(data["auth_config"], ensure_ascii=False) if data["auth_config"] else None)
    if "request_template" in data:
        updates.append(f"request_template = {ph}")
        params.append(json.dumps(data["request_template"], ensure_ascii=False) if data["request_template"] else None)

    if not updates:
        conn.close()
        return jsonify({"error": "no fields to update"}), 400

    params.append(eid)
    execute_query(conn, f"UPDATE api_endpoints SET {', '.join(updates)} WHERE id = {ph}", params)
    conn.commit()
    conn.close()
    return jsonify({"ok": True})


@app.route("/api/endpoints/<int:eid>", methods=["DELETE"])
def delete_endpoint(eid):
    """删除接口配置"""
    conn = get_db_connection()
    ph = "%s" if USE_MYSQL else "?"
    execute_query(conn, f"DELETE FROM api_endpoints WHERE id = {ph}", (eid,))
    conn.commit()
    conn.close()
    return jsonify({"ok": True})


@app.route("/api/endpoints/<int:eid>/test", methods=["POST"])
def test_endpoint(eid):
    """测试接口连通性"""
    conn = get_db_connection()
    ph = "%s" if USE_MYSQL else "?"
    row = execute_query(conn, f"SELECT * FROM api_endpoints WHERE id = {ph}", (eid,), fetch_one=True)
    conn.close()

    if not row:
        return jsonify({"ok": False, "error": "接口配置不存在"})

    endpoint = row_to_dict(row)
    base_url = endpoint.get("base_url", "")
    timeout_sec = endpoint.get("timeout_sec", 30)
    # api_key 和自定义请求头从 auth_config JSON 中读取
    api_key = None
    api_headers = {}
    auth_config = endpoint.get("auth_config")
    if auth_config:
        try:
            config = json.loads(auth_config) if isinstance(auth_config, str) else auth_config
            api_key = config.get("api_key")
            api_headers = config.get("headers", {})
        except:
            pass

    if not base_url:
        return jsonify({"ok": False, "error": "接口地址为空"})

    try:
        # 构造测试消息
        test_messages = [{"role": "user", "content": "你好"}]

        # 调用接口
        result = pipi_api.call_pipi_stream(
            test_messages,
            device_id="test_device_001",
            api_url=base_url,
            api_key=api_key,
            extra_headers=api_headers
        )

        # result 是 dict: {"full_text": "...", "response_time_ms": ..., "error": ...}
        if result.get("error"):
            return jsonify({"ok": False, "error": f"接口返回错误: {result['error']}"})

        full_text = result.get("full_text", "")
        response_time = result.get("response_time_ms", 0)

        if full_text and full_text.strip():
            return jsonify({"ok": True, "message": f"接口响应正常 ({response_time}ms)，回复: {full_text[:100]}..."})
        else:
            return jsonify({"ok": False, "error": "接口返回空响应"})

    except Exception as e:
        return jsonify({"ok": False, "error": f"接口调用失败: {str(e)}"})


# ─── 启动 ─────────────────────────────────────────

if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8080
    print(f"Flask dev server on {port}")
    app.run(host="0.0.0.0", port=port, threaded=True)


# ─── 用例质量校验 ───────────────────────────────────

def validate_case_rules(case_data, dimension_code):
    """
    规则校验（保存前同步执行）
    返回: {"passed": bool, "issues": ["问题1", "问题2"]}
    """
    issues = []
    input_text = case_data.get("input_text", "")
    
    # 1. input_text 不应包含 AI 回复
    if "秋秋：" in input_text or "秋秋:" in input_text:
        issues.append("input_text 包含 AI 回复（应只有用户输入）")
    
    # 2. 多轮格式检查
    if "【R" in input_text:
        import re
        rounds = re.findall(r'【R(\d+)】', input_text)
        if rounds:
            nums = [int(r) for r in rounds]
            expected = list(range(1, max(nums) + 1))
            if sorted(nums) != expected:
                issues.append(f"多轮编号不连续: 找到 {nums}，期望 {expected}")
    
    # 3. C1-C5 维度需要多轮
    memory_dims = ["C1", "C2", "C3", "C4", "C5"]
    if dimension_code in memory_dims:
        round_count = input_text.count("【R")
        if round_count < 3:
            issues.append(f"记忆维度 {dimension_code} 需要至少3轮对话，当前仅 {round_count} 轮")
    
    # 4. 评分描述递进检查
    score_2 = case_data.get("score_2_desc", "")
    score_6 = case_data.get("score_6_desc", "")
    score_10 = case_data.get("score_10_desc", "")
    if score_2 and score_6 and score_10:
        if len(score_2) > len(score_10):
            issues.append("评分描述长度异常：2分描述不应比10分长")
    
    # 5. expected_output 不应为空或过短
    expected = case_data.get("expected_output", "")
    if len(expected) < 10:
        issues.append("expected_output 过短，可能不完整")

    # 6. expected_output 检测行为列表格式（应为具体回复文本）
    if expected and re.match(r'^\s*\d+[\.、）)]', expected.strip()):
        issues.append("expected_output 疑似行为原则列表，应为具体回复文本")

    # 7. evaluation_points 不应为空
    eval_points = case_data.get("evaluation_points", "")
    if not eval_points or (isinstance(eval_points, str) and not eval_points.strip()):
        issues.append("缺少 evaluation_points（关键评估点）")
    
    return {
        "passed": len(issues) == 0,
        "issues": issues,
        "status": "passed" if len(issues) == 0 else ("warning" if len(issues) <= 2 else "failed")
    }



def _wait_for_quality_review(persona_id, max_wait_seconds=1800, check_interval=10):
    """
    等待用例审核完成且无不合格用例
    返回：审核通过的用例ID列表（passed + warning）
    """
    import time
    start_time = time.time()

    while True:
        elapsed = time.time() - start_time
        if elapsed > max_wait_seconds:
            print(f"[FULL FLOW] Quality review timeout after {max_wait_seconds}s", flush=True)
            break

        conn = get_db_connection()

        # 查询该用户所有用例的审核状态
        rows = execute_query(conn,
            "SELECT id, quality_status FROM test_cases WHERE persona_id = %s" if USE_MYSQL else
            "SELECT id, quality_status FROM test_cases WHERE persona_id = ?",
            (persona_id,), fetch_all=True)
        conn.close()

        if not rows:
            print(f"[FULL FLOW] No cases found for {persona_id}", flush=True)
            return []

        # 统计各状态数量
        status_count = {"pending": 0, "passed": 0, "warning": 0, "failed": 0, "needs_manual_review": 0}
        for r in rows:
            status = r["quality_status"] if isinstance(r, dict) else r[1]
            status = status or "pending"
            status_count[status] = status_count.get(status, 0) + 1

        total = len(rows)
        pending = status_count.get("pending", 0)
        failed = status_count.get("failed", 0)
        needs_manual = status_count.get("needs_manual_review", 0)
        passed = status_count.get("passed", 0)
        warning = status_count.get("warning", 0)

        print(f"[FULL FLOW] Review status: {passed} passed, {warning} warning, {failed} failed, {pending} pending, {needs_manual} needs_manual ({int(elapsed)}s elapsed)", flush=True)

        # 检查是否完成
        if pending == 0 and failed == 0:
            # 全部审核完成且无不合格（needs_manual_review 也算完成，只是需要人工处理）
            print(f"[FULL FLOW] All cases reviewed, {passed + warning} ready for execution", flush=True)

            # 返回通过的用例ID（passed + warning）
            conn = get_db_connection()
            passed_rows = execute_query(conn,
                "SELECT id FROM test_cases WHERE persona_id = %s AND quality_status IN ('passed', 'warning')" if USE_MYSQL else
                "SELECT id FROM test_cases WHERE persona_id = ? AND quality_status IN ('passed', 'warning')",
                (persona_id,), fetch_all=True)
            conn.close()

            return [r["id"] if isinstance(r, dict) else r[0] for r in passed_rows] if passed_rows else []

        # 还有 pending 或 failed（等待自动重生成），继续等待
        time.sleep(check_interval)

    # 超时后返回当前已通过的用例
    conn = get_db_connection()
    passed_rows = execute_query(conn,
        "SELECT id FROM test_cases WHERE persona_id = %s AND quality_status IN ('passed', 'warning')" if USE_MYSQL else
        "SELECT id FROM test_cases WHERE persona_id = ? AND quality_status IN ('passed', 'warning')",
        (persona_id,), fetch_all=True)
    conn.close()

    return [r["id"] if isinstance(r, dict) else r[0] for r in passed_rows] if passed_rows else []


# 记录每个 (persona_id, dimension_code) 的重试次数
_dimension_retry_count = {}

def async_review_cases(case_ids, auto_regenerate=True):
    """异步 LLM 复核用例质量，不合格自动重生成（最多2次）"""
    import threading
    def _review_worker():
        try:
            conn = get_db_connection()
            reviewed_cases = []  # 记录审核结果

            for case_id in case_ids:
                if not case_id:
                    continue
                # 获取用例数据
                row = execute_query(conn,
                    "SELECT * FROM test_cases WHERE id = %s" if USE_MYSQL else "SELECT * FROM test_cases WHERE id = ?",
                    (case_id,), fetch_one=True)
                if not row:
                    continue
                case = row_to_dict(row)

                # 获取维度信息
                dim_row = execute_query(conn,
                    "SELECT * FROM test_dimensions WHERE dimension_code = %s" if USE_MYSQL else "SELECT * FROM test_dimensions WHERE dimension_code = ?",
                    (case.get("dimension_code"),), fetch_one=True)
                dim_info = row_to_dict(dim_row) if dim_row else {}

                # 获取用户事实
                persona_id = case.get("persona_id") or case.get("device_id")
                user_facts = []
                if persona_id:
                    fact_rows = execute_query(conn,
                        "SELECT category, fact_key, fact_value FROM user_facts WHERE persona_id = %s AND is_active = 1 ORDER BY id DESC" if USE_MYSQL else
                        "SELECT category, fact_key, fact_value FROM user_facts WHERE persona_id = ? AND is_active = 1 ORDER BY id DESC",
                        (persona_id,), fetch_all=True)
                    if fact_rows:
                        user_facts = [row_to_dict(r) for r in fact_rows]

                # LLM 复核
                llm_config = get_llm_config()
                result = pipi_api.review_case_quality(case, dim_info, user_facts, **llm_config["case_review"])

                # 更新数据库
                execute_query(conn,
                    "UPDATE test_cases SET quality_status = %s, quality_score = %s, quality_issues = %s WHERE id = %s" if USE_MYSQL else
                    "UPDATE test_cases SET quality_status = ?, quality_score = ?, quality_issues = ? WHERE id = ?",
                    (result["status"], result.get("score"), json.dumps(result.get("issues", []), ensure_ascii=False), case_id))
                conn.commit()
                print(f"[QUALITY REVIEW] case {case_id}: score={result.get('score')} status={result['status']}", flush=True)

                # 记录审核结果
                reviewed_cases.append({
                    "id": case_id,
                    "case_id": case.get("case_id"),
                    "persona_id": persona_id,
                    "dimension_code": case.get("dimension_code"),
                    "status": result["status"],
                    "score": result.get("score"),
                    "issues": result.get("issues", [])
                })

            conn.close()

            # 审核完成后，检查不合格用例并自动重生成
            if auto_regenerate:
                _auto_regenerate_failed_cases(reviewed_cases)

        except Exception as e:
            print(f"[QUALITY REVIEW ERROR] {e}", flush=True)
            import traceback
            traceback.print_exc()

    t = threading.Thread(target=_review_worker, daemon=True)
    t.start()
    return t


def _auto_regenerate_failed_cases(reviewed_cases):
    """自动重生成不合格用例（最多重试2次）"""
    # 筛选不合格用例（failed 状态）
    failed_cases = [c for c in reviewed_cases if c["status"] == "failed"]
    if not failed_cases:
        return

    # 按 (persona_id, dimension_code) 分组
    from collections import defaultdict
    grouped = defaultdict(list)
    for c in failed_cases:
        key = (c["persona_id"], c["dimension_code"])
        grouped[key].append(c)

    for (persona_id, dim_code), cases in grouped.items():
        retry_key = f"{persona_id}:{dim_code}"
        current_retry = _dimension_retry_count.get(retry_key, 0)

        if current_retry >= 2:
            # 超过重试次数，标记为需人工处理
            print(f"[AUTO REGEN] {retry_key} exceeded max retries (2), marking as needs_manual_review", flush=True)
            conn = get_db_connection()
            for c in cases:
                execute_query(conn,
                    "UPDATE test_cases SET quality_status = %s WHERE id = %s" if USE_MYSQL else
                    "UPDATE test_cases SET quality_status = ? WHERE id = ?",
                    ("needs_manual_review", c["id"]))
            conn.commit()
            conn.close()
            continue

        # 收集问题作为反馈
        issues_feedback = []
        for c in cases:
            if c["issues"]:
                issues_feedback.append(f"- {c['case_id']}: {'; '.join(c['issues'][:2])}")

        print(f"[AUTO REGEN] {retry_key} retry {current_retry + 1}/2, regenerating {len(cases)} failed cases", flush=True)

        # 删除不合格用例
        conn = get_db_connection()
        for c in cases:
            execute_query(conn,
                "DELETE FROM test_cases WHERE id = %s" if USE_MYSQL else "DELETE FROM test_cases WHERE id = ?",
                (c["id"],))
        conn.commit()

        # 获取维度信息
        dim_row = execute_query(conn,
            "SELECT * FROM test_dimensions WHERE dimension_code = %s" if USE_MYSQL else "SELECT * FROM test_dimensions WHERE dimension_code = ?",
            (dim_code,), fetch_one=True)
        dim_info = row_to_dict(dim_row) if dim_row else {}

        # 获取用户事实
        user_facts = []
        fact_rows = execute_query(conn,
            "SELECT category, fact_key, fact_value FROM user_facts WHERE persona_id = %s AND is_active = 1 ORDER BY id DESC LIMIT 30" if USE_MYSQL else
            "SELECT category, fact_key, fact_value FROM user_facts WHERE persona_id = ? AND is_active = 1 ORDER BY id DESC LIMIT 30",
            (persona_id,), fetch_all=True)
        if fact_rows:
            user_facts = [row_to_dict(r) for r in fact_rows]

        # 获取玩偶人设（从 toy_personas 表）
        toy_persona = None
        toy_row = execute_query(conn,
            "SELECT * FROM toy_persona LIMIT 1",  # 当前只有一个玩偶
            fetch_one=True)
        if toy_row:
            toy_persona = row_to_dict(toy_row)

        # 获取用户角色信息（从 personas 表）
        persona = None
        persona_row = execute_query(conn,
            "SELECT * FROM personas WHERE id = %s" if USE_MYSQL else "SELECT * FROM personas WHERE id = ?",
            (persona_id,), fetch_one=True)
        if persona_row:
            persona = row_to_dict(persona_row)

        conn.close()

        # 增加重试计数
        _dimension_retry_count[retry_key] = current_retry + 1

        # 调用带反馈的重生成
        _regenerate_dimension_with_feedback(
            persona_id=persona_id,
            dimension=dim_info,
            toy_persona=toy_persona,
            persona=persona,
            user_facts=user_facts,
            count=len(cases),
            issues_feedback=issues_feedback
        )


def _regenerate_dimension_with_feedback(persona_id, dimension, toy_persona, persona, user_facts, count, issues_feedback):
    """带反馈重新生成维度用例"""
    import threading

    def _regen_worker():
        try:
            # 调用 LLM 生成（带问题反馈）
            llm_config = get_llm_config()
            new_cases = pipi_api.generate_test_cases_with_feedback(
                dimension=dimension,
                toy_persona=toy_persona,
                persona=persona,
                user_facts=user_facts,
                count=count,
                issues_feedback=issues_feedback,
                **llm_config["case_regenerate"]
            )

            if not new_cases:
                print(f"[AUTO REGEN] {persona_id} {dimension.get('dimension_code')} LLM returned empty", flush=True)
                return

            # 保存新用例
            conn = get_db_connection()
            new_case_ids = []
            dim_code = dimension.get("dimension_code", "")

            for case in new_cases:
                base_case_id = case.get("case_id", f"{dim_code}-01")
                final_case_id = _get_unique_case_id(conn, base_case_id)
                case["case_id"] = final_case_id

                new_id = _save_test_case(conn, case, persona_id=persona_id, device_id=persona_id, dimension_code=dim_code)
                if new_id:
                    new_case_ids.append(new_id)

            conn.commit()
            conn.close()

            print(f"[AUTO REGEN] {persona_id} {dim_code} regenerated {len(new_case_ids)} cases, triggering review", flush=True)

            # 触发新用例的审核（继续自动重生成流程）
            if new_case_ids:
                async_review_cases(new_case_ids, auto_regenerate=True)

        except Exception as e:
            print(f"[AUTO REGEN ERROR] {e}", flush=True)
            import traceback
            traceback.print_exc()

    t = threading.Thread(target=_regen_worker, daemon=True)
    t.start()


@app.route("/api/test_cases/review", methods=["POST"])
def trigger_case_review():
    """手动触发用例质量复核"""
    data = request.get_json() or {}
    case_ids = data.get("case_ids", [])
    persona_id = data.get("persona_id")

    if not case_ids and persona_id:
        # 根据 persona_id 获取所有用例
        conn = get_db_connection()
        rows = execute_query(conn,
            "SELECT id FROM test_cases WHERE persona_id = %s" if USE_MYSQL else "SELECT id FROM test_cases WHERE persona_id = ?",
            (persona_id,), fetch_all=True)
        case_ids = [r["id"] if isinstance(r, dict) else r[0] for r in rows] if rows else []
        conn.close()

    if not case_ids:
        return jsonify({"error": "请提供 case_ids 或 persona_id"}), 400

    async_review_cases(case_ids)
    return jsonify({"message": f"已触发 {len(case_ids)} 条用例的异步复核", "case_count": len(case_ids)})


# ─── Jira 集成 ─────────────────────────────────────

def _get_jira_config():
    """获取 Jira 配置"""
    conn = get_db_connection()
    rows = execute_query(conn, "SELECT config_key, config_value FROM jira_config", fetch_all=True)
    conn.close()
    config = {}
    for row in rows or []:
        row = row_to_dict(row) if not isinstance(row, dict) else row
        config[row["config_key"]] = row["config_value"]
    return config


@app.route("/api/jira/projects", methods=["GET"])
def get_jira_projects():
    """获取 Jira 项目列表"""
    import requests
    config = _get_jira_config()
    jira_url = config.get("jira_url", "").rstrip("/")
    jira_token = config.get("jira_token", "")

    if not jira_url or not jira_token:
        return jsonify({"error": "Jira 配置不完整，请先配置 jira_url 和 jira_token"}), 400

    try:
        resp = requests.get(
            f"{jira_url}/rest/api/2/project",
            headers={"Authorization": f"Bearer {jira_token}"},
            timeout=10
        )
        if resp.status_code != 200:
            return jsonify({"error": f"Jira API 错误: {resp.status_code} {resp.text[:200]}"}), resp.status_code

        projects = resp.json()
        result = [{"key": p["key"], "name": p["name"], "id": p["id"]} for p in projects]
        return jsonify({"projects": result})
    except requests.RequestException as e:
        return jsonify({"error": f"连接 Jira 失败: {str(e)}"}), 500


@app.route("/api/jira/project_meta", methods=["GET"])
def get_jira_project_meta():
    """获取指定项目的版本和工单类型列表"""
    import requests
    project_key = request.args.get("project_key", "")
    if not project_key:
        return jsonify({"error": "缺少 project_key 参数"}), 400

    config = _get_jira_config()
    jira_url = config.get("jira_url", "").rstrip("/")
    jira_token = config.get("jira_token", "")

    if not jira_url or not jira_token:
        return jsonify({"error": "Jira 配置不完整"}), 400

    headers = {"Authorization": f"Bearer {jira_token}"}
    result = {"versions": [], "issuetypes": []}

    try:
        # 获取版本
        resp = requests.get(f"{jira_url}/rest/api/2/project/{project_key}/versions", headers=headers, timeout=10)
        if resp.status_code == 200:
            versions = resp.json()
            result["versions"] = [{"id": v["id"], "name": v["name"]} for v in versions if not v.get("archived")]

        # 获取工单类型
        resp = requests.get(f"{jira_url}/rest/api/2/issue/createmeta?projectKeys={project_key}&expand=projects.issuetypes", headers=headers, timeout=10)
        if resp.status_code == 200:
            data = resp.json()
            projects = data.get("projects", [])
            if projects:
                issuetypes = projects[0].get("issuetypes", [])
                result["issuetypes"] = [{"id": t["id"], "name": t["name"]} for t in issuetypes if not t.get("subtask")]

        return jsonify(result)
    except requests.RequestException as e:
        return jsonify({"error": f"连接 Jira 失败: {str(e)}"}), 500


@app.route("/api/jira/create_issue", methods=["POST"])
def create_jira_issue():
    """创建 Jira 工单"""
    import requests
    data = request.get_json() or {}
    project_key = data.get("project_key")
    result_id = data.get("result_id")

    if not project_key or not result_id:
        return jsonify({"error": "缺少 project_key 或 result_id"}), 400

    config = _get_jira_config()
    jira_url = config.get("jira_url", "").rstrip("/")
    jira_token = config.get("jira_token", "")

    if not jira_url or not jira_token:
        return jsonify({"error": "Jira 配置不完整"}), 400

    conn = get_db_connection()
    result = execute_query(conn, """
        SELECT tr.*, tc.case_id as case_code, tc.title, tc.input_text, tc.expected_output,
               tc.dimension_code, td.dimension_name, tt.task_id, tt.device_id
        FROM test_results tr
        LEFT JOIN test_cases tc ON tr.case_id = tc.id
        LEFT JOIN test_dimensions td ON tc.dimension_code = td.dimension_code
        LEFT JOIN test_tasks tt ON tr.task_id = tt.id
        WHERE tr.id = %s
    """ if USE_MYSQL else """
        SELECT tr.*, tc.case_id as case_code, tc.title, tc.input_text, tc.expected_output,
               tc.dimension_code, td.dimension_name, tt.task_id, tt.device_id
        FROM test_results tr
        LEFT JOIN test_cases tc ON tr.case_id = tc.id
        LEFT JOIN test_dimensions td ON tc.dimension_code = td.dimension_code
        LEFT JOIN test_tasks tt ON tr.task_id = tt.id
        WHERE tr.id = ?
    """, (result_id,), fetch_one=True)
    conn.close()

    if not result:
        return jsonify({"error": f"找不到评测结果 ID={result_id}"}), 404

    result = row_to_dict(result)
    case_code = result.get("case_code", "")
    title = result.get("title", "")
    dimension_name = result.get("dimension_name", "")
    input_text = result.get("input_text", "")
    expected_output = result.get("expected_output", "")
    actual_output = result.get("actual_output", "")
    score = result.get("score", 0)
    deduction_reason = result.get("deduction_reason", "")
    task_id = result.get("task_id", "")
    device_id = result.get("device_id", "")
    executed_at = result.get("executed_at", "")

    summary = f"[评测失败] {case_code} - {title[:50]}" if title else f"[评测失败] {case_code}"
    description = f"""*用例信息*
- 用例ID: {case_code}
- 执行时间: {executed_at}
- 维度: {dimension_name}
- 标题: {title}

*测试输入*
{{{input_text}}}

*期望输出*
{{{expected_output}}}

*实际输出*
{{{actual_output}}}

*评测结果*
- 得分: {score}/10
- 扣分原因: {deduction_reason}

*来源*
- 任务ID: {task_id}
- DeviceID: {device_id}
"""

    version = data.get("version", "").strip()

    issue_data = {
        "fields": {
            "project": {"key": project_key},
            "summary": summary[:255],
            "description": description,
            "issuetype": {"name": data.get("issuetype", "Bug")}
        }
    }
    if version:
        issue_data["fields"]["versions"] = [{"name": version}]

    try:
        resp = requests.post(
            f"{jira_url}/rest/api/2/issue",
            headers={
                "Authorization": f"Bearer {jira_token}",
                "Content-Type": "application/json"
            },
            json=issue_data,
            timeout=15
        )
        if resp.status_code in (200, 201):
            issue = resp.json()
            issue_key = issue.get("key", "")
            issue_url = f"{jira_url}/browse/{issue_key}"
            return jsonify({
                "success": True,
                "issue_key": issue_key,
                "issue_url": issue_url
            })
        else:
            return jsonify({"error": f"创建失败: {resp.status_code} {resp.text[:300]}"}), resp.status_code
    except requests.RequestException as e:
        return jsonify({"error": f"连接 Jira 失败: {str(e)}"}), 500

