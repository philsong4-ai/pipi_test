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
from typing import Dict, List

# 给 print() 加时间戳
import builtins
_print = builtins.print

def _tsprint(*args, **kwargs):
    ts = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    _print(f"[{ts}]", *args, **kwargs)

builtins.print = _tsprint

from flask import Flask, request, jsonify, send_file, redirect, make_response, g
from flask_compress import Compress

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
    """统一执行查询，兼容 MySQL 和 SQLite，含断连重试"""
    params = params or ()
    if USE_MYSQL:
        import pymysql
        sql = sql.replace("?", "%s")
        if "PRAGMA" in sql:
            return []
        last_err = None
        for attempt in range(3):
            try:
                cursor = conn.cursor()
                cursor.execute(sql, params)
                if fetch_one:
                    return cursor.fetchone()
                elif fetch_all:
                    return cursor.fetchall()
                return cursor
            except (pymysql.err.InterfaceError, pymysql.err.OperationalError) as e:
                last_err = e
                if attempt < 2:
                    import time
                    time.sleep(1)
                    try:
                        conn.ping(reconnect=True)
                    except Exception:
                        pass
                    continue
                raise
        if last_err:
            raise last_err
        return None
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
Compress(app)


# datetime 序列化为本地时间字符串（无时区后缀），避免前端 new Date() 误判为 UTC 再 +8
# Decimal（MySQL SUM/AVG 等聚合结果）转 int 或 float，避免 JSON 序列化失败
import decimal


class _LocalJSONEncoder(json.JSONEncoder):
    def default(self, o):
        if isinstance(o, (datetime.datetime, datetime.date, datetime.time)):
            return o.strftime("%Y-%m-%d %H:%M:%S")
        if isinstance(o, decimal.Decimal):
            return int(o) if o == o.to_integral_value() else float(o)
        return super().default(o)


app.json_encoder = _LocalJSONEncoder


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
    """根据接口代码获取 API URL、API Key、自定义请求头和协议，返回 (url, key, headers, protocol)
    auth_config JSON 格式: {"api_key": "xxx", "headers": {"X-Custom": "val"}}
    protocol: 'openai' (默认) / 'oho'
    """
    if not api_code:
        return None, None, {}, "openai"
    conn = get_db_connection()
    row = execute_query(conn,
        "SELECT base_url, auth_config, protocol FROM api_endpoints WHERE code = %s AND is_active = 1" if USE_MYSQL else
        "SELECT base_url, auth_config, protocol FROM api_endpoints WHERE code = ? AND is_active = 1",
        (api_code,), fetch_one=True)
    conn.close()
    if row:
        row = row_to_dict(row) if not isinstance(row, dict) else row
        base_url = row.get("base_url")
        protocol = (row.get("protocol") or "openai").lower()
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
        return base_url, api_key, headers, protocol
    return None, None, {}, "openai"


def _get_toy_persona_by_target(target_api: str = "pipi"):
    """根据 target_api 查询对应的玩偶人设。1 人设绑 1 api_endpoints.code。
    查不到回退 LIMIT 1（兼容老数据，应通过 UNIQUE 约束避免）。
    返回 dict 或 None。
    """
    if not target_api:
        target_api = "pipi"
    conn = get_db_connection()
    ph = "%s" if USE_MYSQL else "?"
    row = execute_query(conn,
        f"SELECT * FROM toy_persona WHERE target_api = {ph} LIMIT 1",
        (target_api,), fetch_one=True)
    if not row:
        # 回退：老数据无 target_api 或绑错，取任意一行（兼容）
        row = execute_query(conn, "SELECT * FROM toy_persona LIMIT 1", fetch_one=True)
    conn.close()
    if not row:
        return None
    return row_to_dict(row) if not isinstance(row, dict) else row


def _get_toy_persona_name(target_api: str = "pipi", default: str = "皮皮") -> str:
    """查询玩偶人设 name 字段，用于 save_chat_msg 的 sender_name。
    target_api 不存在时回退 default。
    """
    tp = _get_toy_persona_by_target(target_api)
    if tp and tp.get("name"):
        return tp["name"]
    return default


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

        # 启动时清理 aivs_global 槽:上次崩溃可能有 worker 被 SIGKILL,
        # finally 块未执行导致槽泄漏,current_count 卡在 1,所有 AIVS 请求被锁死
        try:
            execute_query(conn,
                "UPDATE concurrency_slots SET current_count=0, updated_at=NOW() WHERE slot_type='aivs_global' AND current_count>0")
            conn.commit()
        except Exception as e:
            print(f"[STARTUP] Could not reset aivs_global slot: {e}", flush=True)

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

        # 检查并添加 human_score 和 human_note 列（人工纠正）
        try:
            execute_query(conn, "SELECT human_score FROM auto_evaluation LIMIT 1", fetch_one=True)
        except:
            try:
                execute_query(conn, "ALTER TABLE auto_evaluation ADD COLUMN human_score INT")
                execute_query(conn, "ALTER TABLE auto_evaluation ADD COLUMN human_note TEXT")
                conn.commit()
                print("[STARTUP] Added human_score and human_note to auto_evaluation", flush=True)
            except Exception as e:
                print(f"[STARTUP] Could not add human columns to auto_evaluation: {e}", flush=True)

        # 检查并添加 deduction_tags 列（结构化扣分标签，JSON 字符串）
        try:
            execute_query(conn, "SELECT deduction_tags FROM auto_evaluation LIMIT 1", fetch_one=True)
        except:
            try:
                execute_query(conn, "ALTER TABLE auto_evaluation ADD COLUMN deduction_tags TEXT")
                conn.commit()
                print("[STARTUP] Added deduction_tags to auto_evaluation", flush=True)
            except Exception as e:
                print(f"[STARTUP] Could not add deduction_tags to auto_evaluation: {e}", flush=True)

        # 检查并添加 judges_detail 列（多评测员集成结果 JSON）
        try:
            execute_query(conn, "SELECT judges_detail FROM auto_evaluation LIMIT 1", fetch_one=True)
        except:
            try:
                execute_query(conn, "ALTER TABLE auto_evaluation ADD COLUMN judges_detail TEXT")
                conn.commit()
                print("[STARTUP] Added judges_detail to auto_evaluation", flush=True)
            except Exception as e:
                print(f"[STARTUP] Could not add judges_detail to auto_evaluation: {e}", flush=True)

        try:
            execute_query(conn, "SELECT human_score FROM test_results LIMIT 1", fetch_one=True)
        except:
            try:
                execute_query(conn, "ALTER TABLE test_results ADD COLUMN human_score INT")
                execute_query(conn, "ALTER TABLE test_results ADD COLUMN human_note TEXT")
                conn.commit()
                print("[STARTUP] Added human_score and human_note to test_results", flush=True)
            except Exception as e:
                print(f"[STARTUP] Could not add human columns to test_results: {e}", flush=True)

        # test_cases 加 eval_detail 列（让 _evaluate_cases_worker 也能写结构化评测详情，统一查询界面）
        try:
            execute_query(conn, "SELECT eval_detail FROM test_cases LIMIT 1", fetch_one=True)
        except:
            try:
                execute_query(conn, "ALTER TABLE test_cases ADD COLUMN eval_detail TEXT")
                conn.commit()
                print("[STARTUP] Added eval_detail to test_cases", flush=True)
            except Exception as e:
                print(f"[STARTUP] Could not add eval_detail to test_cases: {e}", flush=True)

        # test_cases 加红队用例标记列（is_redteam / redteam_trap_type / redteam_predicted_failure）
        for _col, _sql_type in [
            ("is_redteam", "TINYINT(1) DEFAULT 0"),
            ("redteam_trap_type", "VARCHAR(50)"),
            ("redteam_predicted_failure", "TEXT"),
        ]:
            try:
                execute_query(conn, f"SELECT {_col} FROM test_cases LIMIT 1", fetch_one=True)
            except:
                try:
                    execute_query(conn, f"ALTER TABLE test_cases ADD COLUMN {_col} {_sql_type}")
                    conn.commit()
                    print(f"[STARTUP] Added {_col} to test_cases", flush=True)
                except Exception as e:
                    print(f"[STARTUP] Could not add {_col} to test_cases: {e}", flush=True)

        # async_tasks.task_type 加红队枚举值（原 enum 只允许 generate/execute/evaluate）
        try:
            row = execute_query(conn,
                "SELECT COLUMN_TYPE FROM information_schema.columns WHERE table_schema = DATABASE() "
                "AND table_name = 'async_tasks' AND column_name = 'task_type'",
                fetch_one=True) if USE_MYSQL else None
            if USE_MYSQL and row and "fixed_gen" not in (row.get("COLUMN_TYPE") or ""):
                execute_query(conn,
                    "ALTER TABLE async_tasks MODIFY COLUMN task_type "
                    "ENUM('generate','execute','evaluate','rtgen','rtexec','rteval',"
                    "'fixed_gen','fixed_exec','fixed_eval') NOT NULL")
                conn.commit()
                print("[STARTUP] Extended async_tasks.task_type enum with fixed_gen/fixed_exec/fixed_eval", flush=True)
        except Exception as e:
            print(f"[STARTUP] Could not extend async_tasks.task_type enum: {e}", flush=True)

        # test_results 加 needs_review 列（judge 分歧超阈值时标记，前端列表 badge 提示）
        try:
            execute_query(conn, "SELECT needs_review FROM test_results LIMIT 1", fetch_one=True)
        except:
            try:
                execute_query(conn, "ALTER TABLE test_results ADD COLUMN needs_review TINYINT(1) DEFAULT 0")
                conn.commit()
                print("[STARTUP] Added needs_review to test_results", flush=True)
            except Exception as e:
                print(f"[STARTUP] Could not add needs_review to test_results: {e}", flush=True)

        # test_results 加 dialog_ids 列：JSON 数组存多轮对话的 AIVS dialog_id
        try:
            execute_query(conn, "SELECT dialog_ids FROM test_results LIMIT 1", fetch_one=True)
        except:
            try:
                execute_query(conn, "ALTER TABLE test_results ADD COLUMN dialog_ids TEXT")
                conn.commit()
                print("[STARTUP] Added dialog_ids to test_results", flush=True)
            except Exception as e:
                print(f"[STARTUP] Could not add dialog_ids to test_results: {e}", flush=True)

        # test_results 加 ttfb_ms / total_ms 列：JSON 数组存多轮耗时
        for col in ("ttfb_ms", "total_ms"):
            try:
                execute_query(conn, f"SELECT {col} FROM test_results LIMIT 1", fetch_one=True)
            except:
                try:
                    execute_query(conn, f"ALTER TABLE test_results ADD COLUMN {col} TEXT")
                    conn.commit()
                    print(f"[STARTUP] Added {col} to test_results", flush=True)
                except Exception as e:
                    print(f"[STARTUP] Could not add {col} to test_results: {e}", flush=True)

        # chat_messages 加 ttfb_ms / total_ms 列：实时聊天场景的耗时记录（单轮，INT）
        for col in ("ttfb_ms", "total_ms"):
            try:
                execute_query(conn, f"SELECT {col} FROM chat_messages LIMIT 1", fetch_one=True)
            except:
                try:
                    execute_query(conn, f"ALTER TABLE chat_messages ADD COLUMN {col} INT")
                    conn.commit()
                    print(f"[STARTUP] Added {col} to chat_messages", flush=True)
                except Exception as e:
                    print(f"[STARTUP] Could not add {col} to chat_messages: {e}", flush=True)

        # eval_detail 升级为 MEDIUMTEXT：judges_detail 含 3 个 judge 完整回复，长回复易超 TEXT 64KB 上限
        try:
            col = execute_query(conn, "SHOW COLUMNS FROM test_results LIKE 'eval_detail'", fetch_one=True)
            if col and "mediumtext" not in str(col.get("Type", "")).lower():
                execute_query(conn, "ALTER TABLE test_results MODIFY COLUMN eval_detail MEDIUMTEXT")
                conn.commit()
                print("[STARTUP] Upgraded test_results.eval_detail to MEDIUMTEXT", flush=True)
        except Exception as e:
            print(f"[STARTUP] Could not upgrade eval_detail column: {e}", flush=True)

        # toy_persona 加 target_api 字段（多玩偶 API 接口绑定）
        try:
            execute_query(conn, "SELECT target_api FROM toy_persona LIMIT 1", fetch_one=True)
        except:
            try:
                if USE_MYSQL:
                    execute_query(conn, "ALTER TABLE toy_persona ADD COLUMN target_api VARCHAR(64) NOT NULL DEFAULT 'pipi'")
                    execute_query(conn, "ALTER TABLE toy_persona ADD UNIQUE KEY uk_toy_persona_target_api (target_api)")
                else:
                    execute_query(conn, "ALTER TABLE toy_persona ADD COLUMN target_api VARCHAR(64) NOT NULL DEFAULT 'pipi'")
                    execute_query(conn, "CREATE UNIQUE INDEX uk_toy_persona_target_api ON toy_persona(target_api)")
                conn.commit()
                print("[STARTUP] Added toy_persona.target_api column with UNIQUE KEY", flush=True)
            except Exception as e:
                print(f"[STARTUP] Could not add toy_persona.target_api: {e}", flush=True)

        # test_results 加 target_api 字段（结果层区分用哪个玩偶 API）
        try:
            execute_query(conn, "SELECT target_api FROM test_results LIMIT 1", fetch_one=True)
        except:
            try:
                if USE_MYSQL:
                    execute_query(conn, "ALTER TABLE test_results ADD COLUMN target_api VARCHAR(64) DEFAULT NULL")
                    execute_query(conn, "ALTER TABLE test_results ADD INDEX idx_target_api (target_api)")
                else:
                    execute_query(conn, "ALTER TABLE test_results ADD COLUMN target_api VARCHAR(64) DEFAULT NULL")
                    execute_query(conn, "CREATE INDEX idx_test_results_target_api ON test_results(target_api)")
                conn.commit()
                print("[STARTUP] Added test_results.target_api column with INDEX", flush=True)
            except Exception as e:
                print(f"[STARTUP] Could not add test_results.target_api: {e}", flush=True)

        # api_endpoints 加 protocol 字段（区分接口协议：openai/oho）
        try:
            execute_query(conn, "SELECT protocol FROM api_endpoints LIMIT 1", fetch_one=True)
        except:
            try:
                if USE_MYSQL:
                    execute_query(conn, "ALTER TABLE api_endpoints ADD COLUMN protocol VARCHAR(16) NOT NULL DEFAULT 'openai'")
                else:
                    execute_query(conn, "ALTER TABLE api_endpoints ADD COLUMN protocol VARCHAR(16) NOT NULL DEFAULT 'openai'")
                conn.commit()
                print("[STARTUP] Added api_endpoints.protocol column", flush=True)
            except Exception as e:
                print(f"[STARTUP] Could not add api_endpoints.protocol: {e}", flush=True)

        # test_dimensions 加 target_api 字段（多接口维度隔离：pipi 21 维 / oho 8 维）
        try:
            execute_query(conn, "SELECT target_api FROM test_dimensions LIMIT 1", fetch_one=True)
        except:
            try:
                if USE_MYSQL:
                    execute_query(conn, "ALTER TABLE test_dimensions ADD COLUMN target_api VARCHAR(64) NOT NULL DEFAULT 'pipi'")
                    execute_query(conn, "ALTER TABLE test_dimensions ADD INDEX idx_test_dimensions_target_api (target_api)")
                else:
                    execute_query(conn, "ALTER TABLE test_dimensions ADD COLUMN target_api VARCHAR(64) NOT NULL DEFAULT 'pipi'")
                    execute_query(conn, "CREATE INDEX idx_test_dimensions_target_api ON test_dimensions(target_api)")
                conn.commit()
                print("[STARTUP] Added test_dimensions.target_api column with INDEX", flush=True)
            except Exception as e:
                print(f"[STARTUP] Could not add test_dimensions.target_api: {e}", flush=True)


        try:
            execute_query(conn, "SELECT 1 FROM sso_users LIMIT 1", fetch_one=True)
        except:
            try:
                if USE_MYSQL:
                    execute_query(conn, """
                        CREATE TABLE IF NOT EXISTS sso_users (
                            id INT AUTO_INCREMENT PRIMARY KEY,
                            sso_sub VARCHAR(128) NOT NULL UNIQUE COMMENT 'OIDC sub 唯一ID',
                            email VARCHAR(255),
                            name VARCHAR(255),
                            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                            last_login_at TIMESTAMP NULL DEFAULT NULL,
                            INDEX idx_sso_users_email (email)
                        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
                    """)
                else:
                    execute_query(conn, """
                        CREATE TABLE IF NOT EXISTS sso_users (
                            id INTEGER PRIMARY KEY AUTOINCREMENT,
                            sso_sub VARCHAR(128) NOT NULL UNIQUE,
                            email VARCHAR(255),
                            name VARCHAR(255),
                            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                            last_login_at TIMESTAMP NULL DEFAULT NULL
                        )
                    """)
                conn.commit()
                print("[STARTUP] Created sso_users table", flush=True)
            except Exception as e:
                print(f"[STARTUP] Could not create sso_users: {e}", flush=True)


        # 创建 eval_corrections 表（few-shot 纠正案例）
        try:
            execute_query(conn, "SELECT 1 FROM eval_corrections LIMIT 1", fetch_one=True)
        except:
            try:
                if USE_MYSQL:
                    execute_query(conn, """
                        CREATE TABLE IF NOT EXISTS eval_corrections (
                            id INT AUTO_INCREMENT PRIMARY KEY,
                            eval_type VARCHAR(20) NOT NULL COMMENT 'chat or test_case',
                            ref_id VARCHAR(100) NOT NULL COMMENT 'message_id or result_id',
                            case_id VARCHAR(100) DEFAULT NULL COMMENT 'test_case case_id for dedup',
                            dimension_code VARCHAR(50) DEFAULT NULL,
                            user_input TEXT NOT NULL,
                            ai_reply TEXT NOT NULL,
                            auto_score INT NOT NULL,
                            human_score INT NOT NULL,
                            correction_reason TEXT,
                            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                            INDEX idx_eval_type (eval_type),
                            INDEX idx_created_at (created_at)
                        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
                    """)
                else:
                    execute_query(conn, """
                        CREATE TABLE IF NOT EXISTS eval_corrections (
                            id INTEGER PRIMARY KEY AUTOINCREMENT,
                            eval_type TEXT NOT NULL,
                            ref_id TEXT NOT NULL,
                            case_id TEXT DEFAULT NULL,
                            user_input TEXT NOT NULL,
                            ai_reply TEXT NOT NULL,
                            auto_score INTEGER NOT NULL,
                            human_score INTEGER NOT NULL,
                            correction_reason TEXT,
                            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                        )
                    """)
                conn.commit()
                print("[STARTUP] Created eval_corrections table", flush=True)
            except Exception as e:
                print(f"[STARTUP] Could not create eval_corrections: {e}", flush=True)

        # 创建 dimension_retry_counts 表（AUTO REGEN 跨 worker 持久化重试计数）
        try:
            execute_query(conn, "SELECT 1 FROM dimension_retry_counts LIMIT 1", fetch_one=True)
        except:
            try:
                if USE_MYSQL:
                    execute_query(conn, """
                        CREATE TABLE IF NOT EXISTS dimension_retry_counts (
                            id INT AUTO_INCREMENT PRIMARY KEY,
                            persona_id VARCHAR(50) NOT NULL,
                            dimension_code VARCHAR(10) NOT NULL,
                            retry_count INT NOT NULL DEFAULT 0,
                            task_id VARCHAR(50) DEFAULT NULL,
                            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
                            UNIQUE KEY uk_persona_dim (persona_id, dimension_code),
                            INDEX idx_persona (persona_id)
                        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
                    """)
                else:
                    execute_query(conn, """
                        CREATE TABLE IF NOT EXISTS dimension_retry_counts (
                            id INTEGER PRIMARY KEY AUTOINCREMENT,
                            persona_id TEXT NOT NULL,
                            dimension_code TEXT NOT NULL,
                            retry_count INTEGER NOT NULL DEFAULT 0,
                            task_id TEXT,
                            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                            UNIQUE (persona_id, dimension_code)
                        )
                    """)
                conn.commit()
                print("[STARTUP] Created dimension_retry_counts table", flush=True)
            except Exception as e:
                print(f"[STARTUP] Could not create dimension_retry_counts: {e}", flush=True)

        # 跨 worker REGEN 锁表（Gunicorn 多 worker 之间互斥）
        try:
            execute_query(conn, "SELECT 1 FROM regen_locks LIMIT 1", fetch_one=True)
        except:
            try:
                if USE_MYSQL:
                    execute_query(conn, """
                        CREATE TABLE IF NOT EXISTS regen_locks (
                            lock_key VARCHAR(100) PRIMARY KEY,
                            expires_at TIMESTAMP NOT NULL,
                            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                            INDEX idx_expires (expires_at)
                        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
                    """)
                else:
                    execute_query(conn, """
                        CREATE TABLE IF NOT EXISTS regen_locks (
                            lock_key TEXT PRIMARY KEY,
                            expires_at TIMESTAMP NOT NULL,
                            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                        )
                    """)
                conn.commit()
                print("[STARTUP] Created regen_locks table", flush=True)
            except Exception as e:
                print(f"[STARTUP] Could not create regen_locks: {e}", flush=True)

        # 创建 fixed_test_cases 表（固定垂类知识用例，独立维护独立执行）
        try:
            execute_query(conn, "SELECT 1 FROM fixed_test_cases LIMIT 1", fetch_one=True)
        except:
            try:
                if USE_MYSQL:
                    execute_query(conn, """
                        CREATE TABLE IF NOT EXISTS fixed_test_cases (
                            id INT AUTO_INCREMENT PRIMARY KEY,
                            case_id VARCHAR(64) NOT NULL UNIQUE,
                            domain VARCHAR(32) NOT NULL COMMENT 'poem/math/story/trivia',
                            sub_domain VARCHAR(64) DEFAULT NULL,
                            title VARCHAR(200) NOT NULL,
                            input_text TEXT NOT NULL,
                            expected_output TEXT NOT NULL,
                            evaluation_points TEXT,
                            failure_flags TEXT,
                            priority VARCHAR(4) DEFAULT 'P1',
                            status VARCHAR(16) DEFAULT 'active',
                            user_id INT,
                            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
                            INDEX idx_domain (domain),
                            INDEX idx_status (status),
                            INDEX idx_user (user_id)
                        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
                    """)
                else:
                    execute_query(conn, """
                        CREATE TABLE IF NOT EXISTS fixed_test_cases (
                            id INTEGER PRIMARY KEY AUTOINCREMENT,
                            case_id TEXT NOT NULL UNIQUE,
                            domain TEXT NOT NULL,
                            sub_domain TEXT,
                            title TEXT NOT NULL,
                            input_text TEXT NOT NULL,
                            expected_output TEXT NOT NULL,
                            evaluation_points TEXT,
                            failure_flags TEXT,
                            priority TEXT DEFAULT 'P1',
                            status TEXT DEFAULT 'active',
                            user_id INTEGER,
                            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                        )
                    """)
                conn.commit()
                print("[STARTUP] Created fixed_test_cases table", flush=True)
            except Exception as e:
                print(f"[STARTUP] Could not create fixed_test_cases: {e}", flush=True)

        # 创建 fixed_test_tasks 表（固定用例测试任务）
        try:
            execute_query(conn, "SELECT 1 FROM fixed_test_tasks LIMIT 1", fetch_one=True)
        except:
            try:
                if USE_MYSQL:
                    execute_query(conn, """
                        CREATE TABLE IF NOT EXISTS fixed_test_tasks (
                            id INT AUTO_INCREMENT PRIMARY KEY,
                            task_id VARCHAR(32) NOT NULL UNIQUE,
                            name VARCHAR(200),
                            device_id VARCHAR(100),
                            target_api VARCHAR(32) DEFAULT 'pipi',
                            case_ids JSON,
                            status VARCHAR(20) DEFAULT 'pending',
                            progress_total INT DEFAULT 0,
                            progress_done INT DEFAULT 0,
                            user_id INT,
                            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                            started_at TIMESTAMP NULL,
                            completed_at TIMESTAMP NULL,
                            error_message TEXT,
                            INDEX idx_status (status),
                            INDEX idx_user (user_id)
                        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
                    """)
                else:
                    execute_query(conn, """
                        CREATE TABLE IF NOT EXISTS fixed_test_tasks (
                            id INTEGER PRIMARY KEY AUTOINCREMENT,
                            task_id TEXT NOT NULL UNIQUE,
                            name TEXT,
                            device_id TEXT,
                            target_api TEXT DEFAULT 'pipi',
                            case_ids TEXT,
                            status TEXT DEFAULT 'pending',
                            progress_total INTEGER DEFAULT 0,
                            progress_done INTEGER DEFAULT 0,
                            user_id INTEGER,
                            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                            started_at TIMESTAMP NULL,
                            completed_at TIMESTAMP NULL,
                            error_message TEXT
                        )
                    """)
                conn.commit()
                print("[STARTUP] Created fixed_test_tasks table", flush=True)
            except Exception as e:
                print(f"[STARTUP] Could not create fixed_test_tasks: {e}", flush=True)

        # 创建 fixed_test_results 表（固定用例执行 + 评测结果）
        try:
            execute_query(conn, "SELECT 1 FROM fixed_test_results LIMIT 1", fetch_one=True)
        except:
            try:
                if USE_MYSQL:
                    execute_query(conn, """
                        CREATE TABLE IF NOT EXISTS fixed_test_results (
                            id INT AUTO_INCREMENT PRIMARY KEY,
                            task_id INT NOT NULL,
                            case_id INT NOT NULL,
                            status VARCHAR(20) DEFAULT 'pending',
                            actual_output TEXT,
                            dialog_ids JSON,
                            ttfb_ms INT,
                            total_ms INT,
                            score DECIMAL(5,2),
                            deduction_reason TEXT,
                            eval_detail TEXT,
                            needs_review TINYINT DEFAULT 0,
                            human_score INT,
                            human_note TEXT,
                            target_api VARCHAR(32) DEFAULT 'pipi',
                            user_id INT,
                            executed_at TIMESTAMP NULL,
                            INDEX idx_task (task_id),
                            INDEX idx_case (case_id),
                            INDEX idx_user (user_id)
                        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
                    """)
                else:
                    execute_query(conn, """
                        CREATE TABLE IF NOT EXISTS fixed_test_results (
                            id INTEGER PRIMARY KEY AUTOINCREMENT,
                            task_id INTEGER NOT NULL,
                            case_id INTEGER NOT NULL,
                            status TEXT DEFAULT 'pending',
                            actual_output TEXT,
                            dialog_ids TEXT,
                            ttfb_ms INTEGER,
                            total_ms INTEGER,
                            score REAL,
                            deduction_reason TEXT,
                            eval_detail TEXT,
                            needs_review INTEGER DEFAULT 0,
                            human_score INTEGER,
                            human_note TEXT,
                            target_api TEXT DEFAULT 'pipi',
                            user_id INTEGER,
                            executed_at TIMESTAMP NULL
                        )
                    """)
                conn.commit()
                print("[STARTUP] Created fixed_test_results table", flush=True)
            except Exception as e:
                print(f"[STARTUP] Could not create fixed_test_results: {e}", flush=True)

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
                # 反查 task 的 user_id（重启路径无 request context）
                trow = execute_query(conn2, "SELECT user_id FROM growth_tasks WHERE id=?", (tid,), fetch_one=True)
                t_uid = int(trow["user_id"]) if trow and trow.get("user_id") else 1
                threading.Thread(target=_growth_worker, args=(tid, t_uid), daemon=True).start()
            print(f"[STARTUP] Respawned {len(stalled_ids)} growth workers", flush=True)
        conn2.close()

        # 多用户隔离：核心业务表加 user_id 列（默认 1=admin，现有数据归 admin）
        for tbl in ['personas', 'test_tasks', 'async_tasks', 'scheduled_tasks',
                    'test_cases', 'test_results', 'user_facts', 'chat_messages',
                    'growth_tasks']:
            try:
                execute_query(conn, f"SELECT user_id FROM {tbl} LIMIT 1", fetch_one=True)
            except Exception:
                try:
                    execute_query(conn,
                        f"ALTER TABLE {tbl} ADD COLUMN user_id INT NOT NULL DEFAULT 1"
                        if USE_MYSQL else
                        f"ALTER TABLE {tbl} ADD COLUMN user_id INTEGER NOT NULL DEFAULT 1")
                    if USE_MYSQL:
                        execute_query(conn, f"CREATE INDEX idx_{tbl}_user_id ON {tbl}(user_id)")
                    else:
                        execute_query(conn, f"CREATE INDEX idx_{tbl}_user_id ON {tbl}(user_id)")
                    conn.commit()
                    print(f"[STARTUP] Added {tbl}.user_id column", flush=True)
                except Exception as e:
                    print(f"[STARTUP] Could not add {tbl}.user_id: {e}", flush=True)

        # 并发限流 slot 表（跨 worker 全局并发上限）
        try:
            execute_query(conn, "SELECT 1 FROM concurrency_slots LIMIT 1", fetch_one=True)
        except Exception:
            try:
                execute_query(conn, """
                    CREATE TABLE IF NOT EXISTS concurrency_slots (
                        slot_type VARCHAR(64) NOT NULL,
                        current_count INT NOT NULL DEFAULT 0,
                        max_count INT NOT NULL,
                        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
                        PRIMARY KEY (slot_type)
                    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
                """ if USE_MYSQL else """
                    CREATE TABLE IF NOT EXISTS concurrency_slots (
                        slot_type TEXT NOT NULL,
                        current_count INTEGER NOT NULL DEFAULT 0,
                        max_count INTEGER NOT NULL,
                        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        PRIMARY KEY (slot_type)
                    )
                """)
                for slot, mx in [('llm_global', 16), ('api_global', 8), ('user_task_global', 32)]:
                    execute_query(conn,
                        "INSERT IGNORE INTO concurrency_slots (slot_type, max_count, current_count) VALUES (?, ?, 0)",
                        (slot, mx))
                conn.commit()
                print("[STARTUP] Created concurrency_slots table", flush=True)
            except Exception as e:
                print(f"[STARTUP] Could not create concurrency_slots: {e}", flush=True)

        # personas 表新增 15 列：生活背景/情绪/作息扩展/家庭关系
        # LLM 已生成这些字段，但之前只在内存里用于生成对话后丢弃
        for _col, _sql_type in [
            ("childhood_memory", "TEXT"),
            ("life_milestone", "TEXT"),
            ("recent_worry", "TEXT"),
            ("important_person", "TEXT"),
            ("recent_mood", "TEXT"),
            ("stress_trigger", "TEXT"),
            ("comfort_seeker", "TEXT"),
            ("daily_routine", "TEXT"),
            ("sleep_habit", "TEXT"),
            ("weekend_plan", "TEXT"),
            ("nighttime_routine", "TEXT"),
            ("birthday", "VARCHAR(50)"),
            ("mbti", "VARCHAR(50)"),
            ("family_atmosphere", "TEXT"),
            ("colleague_relationship", "TEXT"),
        ]:
            try:
                execute_query(conn, f"SELECT {_col} FROM personas LIMIT 1", fetch_one=True)
            except:
                try:
                    execute_query(conn, f"ALTER TABLE personas ADD COLUMN {_col} {_sql_type}")
                    conn.commit()
                    print(f"[STARTUP] Added {_col} to personas", flush=True)
                except Exception as e:
                    print(f"[STARTUP] Could not add {_col} to personas: {e}", flush=True)

        conn.close()
        _initialized = True
        print(f"[DB] Using {'MySQL' if USE_MYSQL else 'SQLite'}", flush=True)
    except Exception as e:
        print(f"[DB ERROR] {e}", flush=True)
        _initialized = True


# ─── 路由 ─────────────────────────────────────────

# ─── SSO / OIDC 配置 ─────────────────────────────
SSO_CLIENT_ID = os.environ.get("SSO_CLIENT_ID", "pipi-test")
SSO_CLIENT_SECRET = os.environ.get("SSO_CLIENT_SECRET", "")
SSO_ISSUER = os.environ.get("SSO_ISSUER", "https://<SSO_DOMAIN>")
SSO_AUTHORIZE_URL = os.environ.get("SSO_AUTHORIZE_URL", f"{SSO_ISSUER}/oidc/authorize")
SSO_TOKEN_URL = os.environ.get("SSO_TOKEN_URL", f"{SSO_ISSUER}/oidc/token")
SSO_USERINFO_URL = os.environ.get("SSO_USERINFO_URL", f"{SSO_ISSUER}/oidc/userinfo")
SSO_REDIRECT_URI = os.environ.get("SSO_REDIRECT_URI", "https://<APP_DOMAIN>/api/auth/callback")
SESSION_SECRET = os.environ.get("SESSION_SECRET", "pipi-test-default-session-secret-change-me")
CLI_TOKEN = os.environ.get("CLI_TOKEN", "")  # CLI 脚本 bypass token；为空则禁用 CLI 旁路

# 允许未登录访问的路径前缀（白名单）
_PUBLIC_API_PREFIXES = ("/api/auth/",)


def _sign_session(payload):
    """用 itsdangerous 签发 session token（含 user_id + sso_sub + 过期时间）。"""
    from itsdangerous import URLSafeTimedSerializer
    s = URLSafeTimedSerializer(SESSION_SECRET, salt="pipi-session")
    return s.dumps(payload)


def _verify_session(token):
    """校验 session token，返回 payload 或 None。"""
    if not token:
        return None
    from itsdangerous import URLSafeTimedSerializer, BadSignature, SignatureExpired
    s = URLSafeTimedSerializer(SESSION_SECRET, salt="pipi-session")
    try:
        return s.loads(token, max_age=86400)  # 24h 过期
    except (BadSignature, SignatureExpired):
        return None


def _get_current_user():
    """从 cookie 取当前登录用户。返回 dict 或 None。"""
    token = request.cookies.get("pipi_session")
    payload = _verify_session(token)
    if not payload:
        return None
    # payload: {user_id, sso_sub, name, email}
    return payload


def _require_login():
    """校验登录态。未登录返回 None（已登录返回 user payload）。
    CLI bypass：若请求带 X-CLI-Token header 且等于 CLI_TOKEN，绕过校验。
    CLI 必须显式带 X-User-Id header 指定操作的用户身份，默认 1（admin）。
    """
    user = _get_current_user()
    if user:
        return user
    if CLI_TOKEN and request.headers.get("X-CLI-Token") == CLI_TOKEN:
        uid_str = request.headers.get("X-User-Id", "1")
        try:
            uid = int(uid_str)
        except (ValueError, TypeError):
            uid = 1
        return {"user_id": uid, "sso_sub": f"cli:{uid}", "name": f"CLI:{uid}", "email": None}
    return None


def _current_uid() -> int:
    """返回当前请求的 user_id。g.user 优先，回退 1（admin）。

    handler 内调用；worker 是后台线程无 request context，需通过参数传入 user_id。
    """
    u = getattr(g, 'user', None)
    if u and u.get('user_id'):
        try:
            return int(u['user_id'])
        except (ValueError, TypeError):
            pass
    return 1


@app.before_request
def before_request():
    _ensure_tables()
    # 静态根路径 / 和非 /api 路径放行（让前端 HTML 加载，登录后由前端发 fetch）
    if request.path == "/" or not request.path.startswith("/api/"):
        return
    # 白名单放行
    if request.path.startswith(_PUBLIC_API_PREFIXES):
        return
    # 未登录拦截
    user = _require_login()
    if not user:
        return jsonify({"error": "unauthorized", "login_url": "/api/auth/login"}), 401
    g.user = user


# ─── OIDC 路由 ───────────────────────────────────

@app.route("/api/auth/login")
def auth_login():
    """跳转 SSO 授权页。state 防 CSRF。"""
    import secrets
    state = secrets.token_urlsafe(16)
    params = {
        "client_id": SSO_CLIENT_ID,
        "redirect_uri": SSO_REDIRECT_URI,
        "response_type": "code",
        "scope": "openid profile email",
        "state": state,
    }
    # state 存 cookie，callback 时校验（短时，2 分钟）
    from urllib.parse import urlencode
    login_url = f"{SSO_AUTHORIZE_URL}?{urlencode(params)}"
    resp = make_response(redirect(login_url))
    resp.set_cookie("pipi_oauth_state", state, max_age=120, httponly=True, secure=True, samesite="Lax")
    return resp


@app.route("/api/auth/callback")
def auth_callback():
    """SSO 回调：换 token、查 userinfo、签发本地 session、跳回首页。"""
    from urllib.parse import urlencode
    import requests as req

    code = request.args.get("code")
    state = request.args.get("state")
    cookie_state = request.cookies.get("pipi_oauth_state")

    if not code:
        return jsonify({"error": "missing code"}), 400
    if not state or state != cookie_state:
        return jsonify({"error": "invalid state"}), 400

    # 换 token
    try:
        token_resp = req.post(SSO_TOKEN_URL, data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": SSO_REDIRECT_URI,
            "client_id": SSO_CLIENT_ID,
            "client_secret": SSO_CLIENT_SECRET,
        }, timeout=15)
        token_data = token_resp.json()
    except Exception as e:
        return jsonify({"error": f"token exchange failed: {e}"}), 500

    access_token = token_data.get("access_token")
    if not access_token:
        return jsonify({"error": "no access_token", "detail": token_data}), 500

    # 取 userinfo
    try:
        ui_resp = req.get(SSO_USERINFO_URL, headers={
            "Authorization": f"Bearer {access_token}",
        }, timeout=15)
        userinfo = ui_resp.json()
    except Exception as e:
        return jsonify({"error": f"userinfo failed: {e}"}), 500

    sso_sub = userinfo.get("sub") or userinfo.get("user_id") or ""
    if not sso_sub:
        return jsonify({"error": "no sub in userinfo"}), 500
    email = userinfo.get("email") or ""
    name = userinfo.get("name") or userinfo.get("preferred_username") or email or sso_sub

    # 写库：首次登录创建，否则更新 last_login_at
    conn = get_db_connection()
    ph = "%s" if USE_MYSQL else "?"
    row = execute_query(conn, f"SELECT id FROM sso_users WHERE sso_sub = {ph}", (sso_sub,), fetch_one=True)
    if row:
        uid = row["id"] if isinstance(row, dict) else row[0]
        execute_query(conn, f"UPDATE sso_users SET email = {ph}, name = {ph}, last_login_at = NOW() WHERE id = {ph}" if USE_MYSQL else f"UPDATE sso_users SET email = ?, name = ?, last_login_at = CURRENT_TIMESTAMP WHERE id = ?", (email, name, uid))
    else:
        cur = execute_query(conn, f"INSERT INTO sso_users (sso_sub, email, name, last_login_at) VALUES ({ph}, {ph}, {ph}, {'NOW()' if USE_MYSQL else 'CURRENT_TIMESTAMP'})", (sso_sub, email, name))
        uid = get_lastrowid(cur)
    conn.commit()
    conn.close()

    # 签发 session cookie
    session_token = _sign_session({"user_id": uid, "sso_sub": sso_sub, "name": name, "email": email})
    resp = make_response(redirect("/"))
    resp.set_cookie("pipi_session", session_token, max_age=86400, httponly=True, secure=True, samesite="Lax", path="/")
    resp.delete_cookie("pipi_oauth_state", path="/")
    return resp


@app.route("/api/auth/logout", methods=["POST", "GET"])
def auth_logout():
    """清本地 session cookie，返回静态登出页。"""
    html = """<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="UTF-8"><title>已登出</title>
<style>body{font-family:-apple-system,sans-serif;display:flex;align-items:center;justify-content:center;height:100vh;margin:0;background:#f5f5f7;color:#1d1d1f}
.card{text-align:center;padding:40px;background:#fff;border-radius:12px;box-shadow:0 1px 4px rgba(0,0,0,0.08)}
button{margin-top:20px;padding:8px 24px;background:#007aff;color:#fff;border:none;border-radius:6px;font-size:14px;cursor:pointer}
button:hover{background:#0066d6}
.hint{margin-top:16px;font-size:12px;color:#86868b}
</style></head>
<body><div class="card"><h2>已登出</h2><p>您已退出登录</p>
<button onclick="window.location.href='/api/auth/login'">重新登录</button>
<p class="hint">如需彻底登出 SSO，请前往 SSO 站点手动登出</p>
</div></body></html>"""
    resp = make_response(html)
    resp.delete_cookie("pipi_session", path="/", secure=True, samesite="Lax")
    return resp


@app.route("/api/auth/me")
def auth_me():
    """返回当前登录用户信息。"""
    user = _get_current_user()
    if not user:
        return jsonify({"logged_in": False})
    return jsonify({"logged_in": True, "user": user})


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
    uid = _current_uid()

    # 构建查询
    if search:
        # 搜索 id 或 name
        placeholder = "%s" if USE_MYSQL else "?"
        count_sql = f"SELECT COUNT(*) as total FROM personas WHERE user_id = {placeholder} AND (id LIKE {placeholder} OR name LIKE {placeholder})"
        search_param = f"%{search}%"
        total_row = execute_query(conn, count_sql, (uid, search_param, search_param), fetch_one=True)
        total = row_to_dict(total_row)["total"]

        data_sql = f"""
            SELECT * FROM personas
            WHERE user_id = {placeholder} AND (id LIKE {placeholder} OR name LIKE {placeholder})
            ORDER BY created_at DESC, id DESC
            LIMIT {placeholder} OFFSET {placeholder}
        """
        rows = execute_query(conn, data_sql, (uid, search_param, search_param, per_page, offset), fetch_all=True)
    else:
        count_sql = "SELECT COUNT(*) as total FROM personas WHERE user_id = ?"
        total_row = execute_query(conn, count_sql, (uid,), fetch_one=True)
        total = row_to_dict(total_row)["total"]

        data_sql = "SELECT * FROM personas WHERE user_id = ? ORDER BY created_at DESC, id DESC LIMIT ? OFFSET ?"
        rows = execute_query(conn, data_sql, (uid, per_page, offset), fetch_all=True)

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
    row = execute_query(conn, "SELECT * FROM personas WHERE id=? AND user_id=?", (pid, _current_uid()), fetch_one=True)
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
        "compare_with", "relation_stages", "target_api", "user_id",
        # 新增 15 维（生活背景/情绪/作息扩展/家庭关系）
        "childhood_memory", "life_milestone", "recent_worry", "important_person",
        "recent_mood", "stress_trigger", "comfort_seeker",
        "daily_routine", "sleep_habit", "weekend_plan", "nighttime_routine",
        "birthday", "mbti", "family_atmosphere", "colleague_relationship",
    ]

    # 如果没有 device_id，自动生成
    if not data.get("device_id"):
        import time as time_module
        data["device_id"] = f"TEST_DEV_{int(time_module.time())}_{random.randint(1000,9999)}"

    conn = get_db_connection()
    uid = _current_uid()
    kv = {f: data.get(f, "") for f in fields}
    kv["id"] = pid
    kv["user_id"] = uid
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
            uid = _current_uid()
            cur = execute_query(conn,
                "INSERT INTO growth_tasks (persona_id, speed, status, total_messages, user_id) VALUES (?,?,?,?,?)",
                (pid, fill_speed, "pending", len(messages), uid))
            task_id = get_lastrowid(cur)

            for idx, msg in enumerate(messages):
                execute_query(conn,
                    "INSERT INTO growth_progress (task_id, message_index, user_message, status) VALUES (?,?,?,?)",
                    (task_id, idx, msg, "pending"))

            conn.commit()
            conn.close()

            # 启动后台线程
            t = threading.Thread(target=_growth_worker, args=(task_id, uid), daemon=True)
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
    uid = _current_uid()

    # 先验证 persona 归属当前用户（防越权删除他人数据）
    row = execute_query(conn, "SELECT id FROM personas WHERE id=? AND user_id=?", (pid, uid), fetch_one=True)
    if not row:
        conn.close()
        return jsonify({"error": "not found or no permission"}), 404

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
    execute_query(conn, "DELETE FROM personas WHERE id=? AND user_id=?", (pid, uid))

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
        fields = ["id", "name", "device_id", "target_api", "user_id"]
        values = [persona_id, name, device_id, target_api, _current_uid()]

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
        uid = _current_uid()
        cur = execute_query(conn,
            "INSERT INTO growth_tasks (persona_id, speed, status, total_messages, user_id) VALUES (?,?,?,?,?)",
            (persona_id, speed, "pending", len(messages), uid))
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
        t = threading.Thread(target=_growth_worker, args=(task_id, uid), daemon=True)
        t.start()

    conn.close()

    return jsonify({
        "created_count": len(results),
        "personas": results
    })


@app.route("/api/chat_history/<pid>", methods=["GET"])
def get_chat_history(pid):
    conn = get_db_connection()
    uid = _current_uid()
    # 先校验 persona 归属
    prow = execute_query(conn, "SELECT id FROM personas WHERE id = ? AND user_id = ?", (pid, uid), fetch_one=True)
    if not prow:
        conn.close()
        return jsonify({"error": "persona not found"}), 404
    rows = execute_query(conn,
        "SELECT id, role, user_name as user, text, created_at FROM chat_messages "
        "WHERE persona_id=? AND user_id=? ORDER BY id DESC LIMIT 100", (pid, uid), fetch_all=True)
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
    uid = _current_uid()
    prow = execute_query(conn, "SELECT id FROM personas WHERE id = ? AND user_id = ?", (pid, uid), fetch_one=True)
    if not prow:
        conn.close()
        return jsonify({"error": "persona not found"}), 404
    execute_query(conn, "DELETE FROM chat_messages WHERE persona_id=? AND user_id=?", (pid, uid))
    execute_query(conn, "DELETE FROM chat_feedback WHERE message_id IN (SELECT id FROM chat_messages WHERE persona_id=? AND user_id=?)", (pid, uid))
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
        curl -X POST http://localhost:8080/api/test/chat \\
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
    api_url, api_key, api_headers, _protocol = get_api_config_by_code(target_api)

    # 保存用户消息
    save_chat_msg(persona_id, "user", name, message)

    # 构建请求并调用目标接口
    import time as _time
    system_prompt = pipi_api.build_system_prompt(persona_data, device_id, target_api=target_api)
    api_messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": message},
    ]
    result = pipi_api.call_pipi_stream(api_messages, device_id=device_id, api_url=api_url, api_key=api_key, extra_headers=api_headers, protocol=_protocol, user_id=persona_id)
    _ttfb = result.get("ttfb_ms")
    _total = result.get("response_time_ms")
    print(f"[TIMING] {persona_id} SE-web: TTFB={_ttfb}ms total={_total}ms", flush=True)

    reply_text = result.get("full_text", "")
    reply_id = None
    facts_extracted = []

    if reply_text:
        reply_id = save_chat_msg(persona_id, "pipi", _get_toy_persona_name(target_api), reply_text,
                                ttfb_ms=result.get("ttfb_ms"), total_ms=result.get("response_time_ms"))

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
                    prefix = "用户: " if row["role"] == "user" else _get_toy_persona_name(target_api) + ": "
                    chat_history_for_extract.append(prefix + row["text"])

                _t_fact = _time.time()
                llm_config = get_llm_config()
                facts = pipi_api.extract_facts_from_message(
                    message, persona_data, existing_facts, chat_history=chat_history_for_extract, target_api=target_api, **llm_config["fact_extract"])
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
        "dialog_id": result.get("dialog_id"),
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
    rows = execute_query(conn, "SELECT * FROM user_facts WHERE user_id=? ORDER BY persona_id, is_active DESC, created_at DESC", (_current_uid(),), fetch_all=True)
    result = [row_to_dict(r) for r in rows]
    conn.close()
    return jsonify(result)


@app.route("/api/facts_count", methods=["GET"])
def get_facts_count():
    conn = get_db_connection()
    rows = execute_query(conn,
        "SELECT persona_id, COUNT(*) as total, SUM(CASE WHEN is_active=1 THEN 1 ELSE 0 END) as active FROM user_facts WHERE user_id=? GROUP BY persona_id",
        (_current_uid(),), fetch_all=True)
    result = {r["persona_id"]: {"total": r["total"], "active": r["active"]} for r in rows}
    conn.close()
    return jsonify(result)


@app.route("/api/facts/persona/<pid>", methods=["GET"])
def get_facts_by_persona(pid):
    conn = get_db_connection()
    uid = _current_uid()
    # 校验 persona 归属
    prow = execute_query(conn, "SELECT id FROM personas WHERE id=? AND user_id=?", (pid, uid), fetch_one=True)
    if not prow:
        conn.close()
        return jsonify({"error": "persona not found"}), 404
    rows = execute_query(conn,
        "SELECT * FROM user_facts WHERE persona_id=? AND user_id=? ORDER BY is_active DESC, created_at DESC", (pid, uid), fetch_all=True)
    result = [row_to_dict(r) for r in rows]
    conn.close()
    return jsonify(result)


@app.route("/api/facts/<fid>", methods=["GET"])
def get_fact(fid):
    conn = get_db_connection()
    row = execute_query(conn, "SELECT * FROM user_facts WHERE id=? AND user_id=?", (fid, _current_uid()), fetch_one=True)
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
    uid = _current_uid()
    execute_query(conn,
        "UPDATE user_facts SET category=?, fact_key=?, fact_value=?, confidence=?, occurred_at=?, emotion_tag=?, related_fact_ids=?, source_case=?, source_session=?, source_text=? WHERE id=? AND user_id=?",
        (data.get("category",""), data.get("fact_key",""), data.get("fact_value",""),
         data.get("confidence","explicit"), data.get("occurred_at",""), data.get("emotion_tag",""),
         data.get("related_fact_ids",""), data.get("source_case",""), data.get("source_session",""),
         data.get("source_text",""), fid, uid))
    conn.commit()
    conn.close()
    return jsonify({"ok": True})


@app.route("/api/facts/<fid>", methods=["DELETE"])
def delete_fact(fid):
    conn = get_db_connection()
    execute_query(conn, "UPDATE user_facts SET is_active=0 WHERE id=? AND user_id=?", (fid, _current_uid()))
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
    inj_row = execute_query(conn, "SELECT value FROM eval_config WHERE `key`='inject_corrections'", fetch_one=True)
    conn.close()
    enabled = row["value"] == 'true' if row else False
    inject = inj_row["value"] == 'true' if inj_row else False
    return jsonify({"auto_eval_enabled": enabled, "inject_corrections": inject})


@app.route("/api/eval/config", methods=["POST"])
def set_eval_config():
    data = request.get_json() or {}
    conn = get_db_connection()
    if "auto_eval_enabled" in data:
        enabled = data.get("auto_eval_enabled", False)
        if USE_MYSQL:
            execute_query(conn, "REPLACE INTO eval_config (`key`, value) VALUES ('auto_eval_enabled', %s)",
                         ('true' if enabled else 'false',))
        else:
            execute_query(conn, "INSERT OR REPLACE INTO eval_config (key, value) VALUES ('auto_eval_enabled', ?)",
                         ('true' if enabled else 'false',))
    if "inject_corrections" in data:
        inject = data.get("inject_corrections", False)
        if USE_MYSQL:
            execute_query(conn, "REPLACE INTO eval_config (`key`, value) VALUES ('inject_corrections', %s)",
                         ('true' if inject else 'false',))
        else:
            execute_query(conn, "INSERT OR REPLACE INTO eval_config (key, value) VALUES ('inject_corrections', ?)",
                         ('true' if inject else 'false',))
    conn.commit()
    conn.close()
    return jsonify({"ok": True,
                    "auto_eval_enabled": data.get("auto_eval_enabled"),
                    "inject_corrections": data.get("inject_corrections")})


# ─── LLM 模型配置 API ───────────────────────────────

DEFAULT_LLM_CONFIG = {
    "case_gen":       {"model": "qwen3.6-plus",  "temperature": 0.3, "max_tokens": 8192, "timeout": 180},
    "case_regenerate": {"model": "qwen3.6-plus",  "temperature": 0.3, "max_tokens": 8192, "timeout": 180},
    "fact_extract":   {"model": "qwen3.6-plus",  "temperature": 0.3, "max_tokens": 8192, "timeout": 60},
    "eval_batch":     {"model": "qwen3.6-plus",  "temperature": 0, "max_tokens": 8192, "timeout": 90,
                       "judges": [{"model": "qwen3.6-plus", "temperature": 0},
                                  {"model": "deepseek-v4-pro", "temperature": 0},
                                  {"model": "doubao-seed-2-0-pro", "temperature": 0}]},
    "eval_case":      {"model": "qwen3.6-plus",  "temperature": 0, "max_tokens": 8192, "timeout": 60,
                       "judges": [{"model": "qwen3.6-plus", "temperature": 0},
                                  {"model": "deepseek-v4-pro", "temperature": 0},
                                  {"model": "doubao-seed-2-0-pro", "temperature": 0}]},
    "eval_realtime":  {"model": "qwen3.6-plus",  "temperature": 0, "max_tokens": 8192, "timeout": 90,
                       "judges": [{"model": "qwen3.6-plus", "temperature": 0},
                                  {"model": "deepseek-v4-pro", "temperature": 0},
                                  {"model": "doubao-seed-2-0-pro", "temperature": 0}]},
    "case_review":    {"model": "deepseek-v4-pro","temperature": 0.3, "max_tokens": 8192, "timeout": 120},
    "persona_gen":    {"model": "qwen3.6-plus",  "temperature": 0.7, "max_tokens": 4096, "timeout": 180},
    "redteam_gen":    {"model": "qwen3.6-plus",   "temperature": 0.7, "max_tokens": 8192, "timeout": 180},
    "redteam_judge":  {"model": "deepseek-v4-pro", "temperature": 0,   "max_tokens": 8192, "timeout": 60},
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
                # judges 字段：若 stored 显式提供（含 null/空列表）则用 stored，否则用 DEFAULT 默认
                if "judges" in stored:
                    jval = stored["judges"]
                    if jval is None or (isinstance(jval, list) and len(jval) <= 1):
                        config[k]["judges"] = jval  # 显式禁用 ensemble
                    elif isinstance(jval, list):
                        config[k]["judges"] = jval
                # 否则保持 DEFAULT 的 judges
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


@app.route("/api/eval/<int:message_id>/correct", methods=["POST"])
def correct_evaluation(message_id):
    """人工纠正聊天评测分数"""
    data = request.get_json() or {}
    human_score = data.get("human_score")
    human_note = data.get("human_note", "")

    if human_score is None:
        return jsonify({"error": "human_score is required"}), 400
    if not isinstance(human_score, int) or human_score < 1 or human_score > 10:
        return jsonify({"error": "human_score must be an integer 1-10"}), 400

    conn = get_db_connection()
    ph = "%s" if USE_MYSQL else "?"

    row = execute_query(conn,
        f"SELECT * FROM auto_evaluation WHERE message_id = {ph}",
        (message_id,), fetch_one=True)
    if not row:
        conn.close()
        return jsonify({"error": "evaluation not found"}), 404

    row = row_to_dict(row)

    execute_query(conn,
        f"UPDATE auto_evaluation SET human_score = {ph}, human_note = {ph} WHERE message_id = {ph}",
        (human_score, human_note, message_id))

    # 存入 few-shot 纠正案例
    _save_correction(conn, eval_type="chat", ref_id=str(message_id),
                     dimension_code=None, user_input="", ai_reply="",
                     auto_score=int(round(row.get("total_score") or 0)),
                     human_score=human_score, correction_reason=human_note)

    conn.commit()
    conn.close()
    return jsonify({"success": True, "message_id": message_id,
                    "human_score": human_score, "human_note": human_note})


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
                   AVG(COALESCE(human_score, total_score)) as total,
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
                   AVG(COALESCE(human_score, total_score)) as total,
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


@app.route("/api/eval/stats/tags", methods=["GET"])
def eval_stats_tags():
    """结构化扣分标签统计"""
    persona_id = request.args.get("persona_id", "").strip()
    days = int(request.args.get("days", 30))

    conn = get_db_connection()

    def collect_from_test_results():
        """从 test_results.eval_detail JSON 抽 deduction_tags（归一化后）"""
        from collections import Counter
        counter = Counter()
        params = []
        where = "WHERE eval_detail IS NOT NULL AND eval_detail != ''"
        if persona_id:
            where += " AND persona_id = ?"
            params.append(persona_id)
        if days > 0:
            where += " AND executed_at >= DATE_SUB(NOW(), INTERVAL ? DAY)"
            params.append(days)
        rows = execute_query(conn, f"""
            SELECT eval_detail FROM test_results {where}
        """, tuple(params), fetch_all=True)
        for r in rows:
            try:
                d = json.loads(row_to_dict(r)["eval_detail"])
                tags = d.get("deduction_tags", [])
                if isinstance(tags, list):
                    for t in tags:
                        if isinstance(t, str) and t.strip():
                            counter[_normalize_deduction_tag(t)] += 1
            except:
                continue
        return [{"tag": k, "count": v} for k, v in counter.most_common(20)]

    def collect_from_auto_evaluation():
        """从 auto_evaluation.deduction_tags JSON 抽 4 维度 tags（归一化后）"""
        from collections import Counter
        counter = Counter()
        params = []
        where = "WHERE deduction_tags IS NOT NULL AND deduction_tags != ''"
        if persona_id:
            where += " AND persona_id = ?"
            params.append(persona_id)
        if days > 0:
            where += " AND created_at >= DATE_SUB(NOW(), INTERVAL ? DAY)"
            params.append(days)
        rows = execute_query(conn, f"""
            SELECT deduction_tags FROM auto_evaluation {where}
        """, tuple(params), fetch_all=True)
        for r in rows:
            try:
                d = json.loads(row_to_dict(r)["deduction_tags"])
                for dim in ["memory", "emotion", "quality", "persona"]:
                    tags = d.get(dim, [])
                    if isinstance(tags, list):
                        for t in tags:
                            if isinstance(t, str) and t.strip():
                                counter[_normalize_deduction_tag(t)] += 1
            except:
                continue
        return [{"tag": k, "count": v} for k, v in counter.most_common(20)]

    test_case_tags = collect_from_test_results()
    chat_tags = collect_from_auto_evaluation()

    # 合并 top 20
    from collections import Counter
    combined = Counter()
    for item in test_case_tags:
        combined[item["tag"]] += item["count"]
    for item in chat_tags:
        combined[item["tag"]] += item["count"]
    combined_top20 = [{"tag": k, "count": v} for k, v in combined.most_common(20)]

    conn.close()
    return jsonify({
        "test_case_tags": test_case_tags,
        "chat_tags": chat_tags,
        "combined_top20": combined_top20
    })


@app.route("/api/eval/stats/disagreement", methods=["GET"])
def eval_stats_disagreement():
    """judge 分歧度统计：avg_std / 高分歧占比 / 高分歧用例列表"""
    persona_id = request.args.get("persona_id", "").strip()
    days = int(request.args.get("days", 30))
    task_id = request.args.get("task_id", "").strip()

    conn = get_db_connection()
    params = []
    where = "WHERE r.eval_detail IS NOT NULL AND r.eval_detail != ''"
    if persona_id:
        where += " AND t.persona_id = ?"
        params.append(persona_id)
    if days > 0:
        where += " AND r.executed_at >= DATE_SUB(NOW(), INTERVAL ? DAY)"
        params.append(days)
    if task_id:
        where += " AND r.task_id = ?"
        params.append(task_id)

    rows = execute_query(conn, f"""
        SELECT r.id, r.case_id, r.task_id, r.score, r.status, r.executed_at, r.eval_detail, r.needs_review,
               c.case_id as case_code
        FROM test_results r
        LEFT JOIN test_cases c ON r.case_id = c.id
        LEFT JOIN test_tasks t ON r.task_id = t.id
        {where}
        ORDER BY r.executed_at DESC
        LIMIT 500
    """, tuple(params), fetch_all=True)

    stds = []
    high_disagreement = []
    for r in rows or []:
        r = row_to_dict(r)
        try:
            d = json.loads(r.get("eval_detail") or "{}")
            std = float(d.get("judges_std") or 0)
            stds.append(std)
            if std >= JUDGE_DISAGREEMENT_THRESHOLD:
                high_disagreement.append({
                    "result_id": r["id"],
                    "case_db_id": r.get("case_id"),
                    "case_code": r.get("case_code"),
                    "task_id": r.get("task_id"),
                    "score": r.get("score"),
                    "status": r.get("status"),
                    "judges_std": std,
                    "judges_detail": d.get("judges_detail", []),
                    "executed_at": str(r.get("executed_at") or "")[:19],
                    "needs_review": bool(r.get("needs_review")),
                })
        except Exception:
            continue

    conn.close()

    avg_std = round(sum(stds) / len(stds), 2) if stds else 0
    high_count = sum(1 for s in stds if s >= JUDGE_DISAGREEMENT_THRESHOLD)
    high_ratio = round(high_count / len(stds), 3) if stds else 0

    return jsonify({
        "total": len(stds),
        "avg_std": avg_std,
        "high_disagreement_count": high_count,
        "high_disagreement_ratio": high_ratio,
        "threshold": JUDGE_DISAGREEMENT_THRESHOLD,
        "high_disagreement_cases": high_disagreement[:50],
    })


@app.route("/api/eval/stats/judge_bias", methods=["GET"])
def eval_stats_judge_bias():
    """Judge 偏差分析：每个 judge 的平均分 / 与 ensemble 均值的偏差 / 按维度切片。

    从 test_results.eval_detail.judges_detail 聚合。judges_detail 每项含
    {model, score, ...}，3 个 judge 同一用例的 score 取 mean 即 ensemble 均值。
    bias = judge_score - ensemble_mean，按 judge 聚合即得系统性偏差。
    """
    persona_id = request.args.get("persona_id", "").strip()
    days = int(request.args.get("days", 30))
    dimension_code = request.args.get("dimension_code", "").strip()

    conn = get_db_connection()
    params = []
    where = "WHERE r.eval_detail IS NOT NULL AND r.eval_detail != ''"
    if persona_id:
        where += " AND t.persona_id = ?"
        params.append(persona_id)
    if days > 0:
        where += " AND r.executed_at >= DATE_SUB(NOW(), INTERVAL ? DAY)"
        params.append(days)
    if dimension_code:
        where += " AND c.dimension_code = ?"
        params.append(dimension_code)

    rows = execute_query(conn, f"""
        SELECT r.eval_detail, c.dimension_code
        FROM test_results r
        LEFT JOIN test_cases c ON r.case_id = c.id
        LEFT JOIN test_tasks t ON r.task_id = t.id
        {where}
    """.replace(" {where}", " " + where), tuple(params), fetch_all=True)

    # 按 judge 聚合：score_sum / count / bias_sum（bias = score - mean_of_case）
    # 同时按维度切片
    judge_stats = {}  # {model: {count, score_sum, bias_sum, by_dim: {dim: {count, score_sum, bias_sum}}}}
    for r in rows or []:
        r = row_to_dict(r)
        try:
            d = json.loads(r.get("eval_detail") or "{}")
            judges = d.get("judges_detail", [])
            if not isinstance(judges, list) or len(judges) < 1:
                continue
            dim = r.get("dimension_code") or "未知"
            # 异常 judge（error_kind 非空，如 timeout/exception/parse_failed/no_response）
            # 不计入 case_mean 与 per-judge 偏差，避免把 LLM 调用失败的 0 分当作 model 系统性偏移
            valid_judges = [j for j in judges if not j.get("error_kind")]
            scores = [float(j.get("score") or 0) for j in valid_judges if j.get("score") is not None]
            if not scores:
                continue
            case_mean = sum(scores) / len(scores)
            for j in valid_judges:
                model = (j.get("model") or "unknown").split("/")[-1]
                s = j.get("score")
                if s is None:
                    continue
                s = float(s)
                bias = s - case_mean
                if model not in judge_stats:
                    judge_stats[model] = {"count": 0, "score_sum": 0.0, "bias_sum": 0.0,
                                         "by_dim": {}}
                judge_stats[model]["count"] += 1
                judge_stats[model]["score_sum"] += s
                judge_stats[model]["bias_sum"] += bias
                if dim not in judge_stats[model]["by_dim"]:
                    judge_stats[model]["by_dim"][dim] = {"count": 0, "score_sum": 0.0, "bias_sum": 0.0}
                judge_stats[model]["by_dim"][dim]["count"] += 1
                judge_stats[model]["by_dim"][dim]["score_sum"] += s
                judge_stats[model]["by_dim"][dim]["bias_sum"] += bias
        except:
            continue

    conn.close()

    judges_list = []
    for model, st in judge_stats.items():
        avg_score = round(st["score_sum"] / st["count"], 2) if st["count"] else 0
        avg_bias = round(st["bias_sum"] / st["count"], 2) if st["count"] else 0
        by_dim = []
        for dim, dst in st["by_dim"].items():
            by_dim.append({
                "dimension_code": dim,
                "count": dst["count"],
                "avg_score": round(dst["score_sum"] / dst["count"], 2) if dst["count"] else 0,
                "avg_bias": round(dst["bias_sum"] / dst["count"], 2) if dst["count"] else 0,
            })
        by_dim.sort(key=lambda x: x["avg_bias"])
        judges_list.append({
            "model": model,
            "count": st["count"],
            "avg_score": avg_score,
            "avg_bias": avg_bias,
            "by_dim": by_dim,
        })
    # 按 avg_bias 降序（偏高在前，偏低在后）
    judges_list.sort(key=lambda x: x["avg_bias"], reverse=True)

    return jsonify({
        "total_cases": len(rows or []),
        "judges": judges_list,
    })


@app.route("/api/eval/stats/by_user", methods=["GET"])
def eval_stats_by_user():
    """用户评分对比"""
    conn = get_db_connection()

    rows = execute_query(conn, """
        SELECT e.persona_id, p.name,
               AVG(COALESCE(e.human_score, e.total_score)) as avg_score,
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
        chat_history: list - 对话历史，格式: ["用户: xxx", "小米绒绒: xxx", ...]（可选）
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

    # 获取玩偶人设
    target_api = (persona_data or {}).get("target_api") or data.get("target_api") or "pipi"
    toy_persona = _get_toy_persona_by_target(target_api)

    llm_config = get_llm_config()
    with _EVAL_SEMAPHORE:
        result = pipi_api.evaluate_chat_reply(
            reply_text=reply_text,
            user_message=user_message,
            chat_history=chat_history,
            user_facts=user_facts,
            persona_data=persona_data,
            toy_persona=toy_persona,
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
    uid = _current_uid()

    if not persona_id:
        return jsonify({"error": "persona_id is required"}), 400
    if not messages or not isinstance(messages, list):
        return jsonify({"error": "messages must be a non-empty list"}), 400

    # 获取用户画像
    conn = get_db_connection()
    row = execute_query(conn, "SELECT * FROM personas WHERE id=? AND user_id=?", (persona_id, uid), fetch_one=True)
    conn.close()

    if not row:
        return jsonify({"error": f"persona_id '{persona_id}' not found"}), 404

    persona_data = row_to_dict(row)
    device_id = persona_data.get("device_id", persona_id)
    name = persona_data.get("name", persona_id)
    target_api = persona_data.get("target_api") or "pipi"
    api_url, api_key, api_headers, _protocol = get_api_config_by_code(target_api)

    conversations = []

    for i, msg in enumerate(messages):
        if not isinstance(msg, str) or not msg.strip():
            continue

        user_message = msg.strip()

        # 保存用户消息
        save_chat_msg(persona_id, "user", name, user_message, user_id=uid)

        # 构建请求
        system_prompt = pipi_api.build_system_prompt(persona_data, device_id, target_api=target_api)
        api_messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_message},
        ]

        # 调用目标接口
        result = pipi_api.call_pipi_stream(api_messages, device_id=device_id, api_url=api_url, api_key=api_key, extra_headers=api_headers, protocol=_protocol, user_id=persona_id)

        reply_text = result.get("full_text", "")
        reply_id = None
        eval_result = None

        if reply_text:
            reply_id = save_chat_msg(persona_id, "pipi", _get_toy_persona_name(target_api), reply_text, user_id=uid,
                                    ttfb_ms=result.get("ttfb_ms"), total_ms=result.get("response_time_ms"))

            # 先提取事实（同步），确保评测时有最新事实
            try:
                conn = get_db_connection()
                fact_rows = execute_query(conn,
                    "SELECT id, category, fact_key, entity_name, fact_value FROM user_facts WHERE persona_id=? AND is_active=1 AND user_id=?",
                    (persona_id, uid), fetch_all=True)
                existing_facts = [row_to_dict(r) for r in fact_rows]
                history_rows = execute_query(conn,
                    "SELECT role, text FROM chat_messages WHERE persona_id=? AND user_id=? ORDER BY id DESC LIMIT 10",
                    (persona_id, uid), fetch_all=True)
                conn.close()

                chat_history_for_extract = []
                for row in reversed(history_rows):
                    prefix = "用户: " if row["role"] == "user" else _get_toy_persona_name(target_api) + ": "
                    chat_history_for_extract.append(prefix + row["text"])

                llm_config = get_llm_config()
                facts = pipi_api.extract_facts_from_message(
                    user_message, persona_data, existing_facts, chat_history=chat_history_for_extract, target_api=target_api, **llm_config["fact_extract"])

                if facts:
                    for f in facts:
                        related_ids = f.pop("related_to", [])
                        if related_ids:
                            f["related_fact_ids"] = ",".join(str(x) for x in related_ids)
                        f["persona_id"] = persona_id
                        f["confidence"] = "implicit"
                        f["source_session"] = persona_id
                        f["source_text"] = user_message
                        _create_fact(f, user_id=uid)
            except Exception as e:
                print(f"[SIMULATE FACT ERROR] {persona_id}: {e}", flush=True)

            # 自动评测（事实提取后）
            if auto_eval:
                conn = get_db_connection()
                context = _build_eval_context(conn, persona_id, reply_id, user_id=uid)
                conn.close()

                llm_config2 = get_llm_config()
                with _EVAL_SEMAPHORE:
                    eval_result = pipi_api.evaluate_chat_reply(
                        reply_text=reply_text,
                        user_message=user_message,
                        chat_history=context.get("chat_history", []),
                        user_facts=context.get("user_facts", []),
                        persona_data=context.get("persona_data"),
                        toy_persona=context.get("toy_persona"),
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
    uid = _current_uid()

    conn = get_db_connection()
    query = """
        SELECT m.id, m.persona_id, m.text, m.created_at
        FROM chat_messages m
        LEFT JOIN auto_evaluation e ON m.id = e.message_id
        WHERE m.role = 'pipi' AND e.id IS NULL AND m.user_id = ?
    """
    params = [uid]
    if persona_id:
        query += " AND m.persona_id = ?"
        params.append(persona_id)
    query += " ORDER BY m.id DESC LIMIT ?"
    params.append(limit)

    pending = execute_query(conn, query, tuple(params), fetch_all=True)
    conn.close()

    if pending:
        t = threading.Thread(target=_batch_evaluate_worker, args=([row_to_dict(p) for p in pending], uid), daemon=True)
        t.start()

    return jsonify({"pending_count": len(pending), "status": "processing"})


def _batch_evaluate_worker(pending_msgs, user_id=None):
    """后台批量评测工作线程"""
    uid = user_id
    corrections = _load_recent_corrections(eval_type="chat", limit=20)
    for msg in pending_msgs:
        try:
            msg_id = msg['id']
            persona_id = msg['persona_id']
            reply_text = msg['text']

            # 获取这条回复前的用户消息
            conn = get_db_connection()
            user_msg_row = execute_query(conn,
                "SELECT text FROM chat_messages WHERE persona_id=? AND id < ? AND role='user' AND user_id=? ORDER BY id DESC LIMIT 1",
                (persona_id, msg_id, uid), fetch_one=True)
            user_message = user_msg_row['text'] if user_msg_row else ""

            # 构建上下文
            context = _build_eval_context(conn, persona_id, msg_id, user_id=uid)
            conn.close()

            # 调用评测（并发闸保护，防止 burst 请求打爆 LLM）
            llm_config = get_llm_config()
            with _EVAL_SEMAPHORE:
                eval_result = pipi_api.evaluate_chat_reply(
                    reply_text=reply_text,
                    user_message=user_message,
                    chat_history=context['chat_history'],
                    user_facts=context['user_facts'],
                    persona_data=context['persona_data'],
                    toy_persona=context.get('toy_persona'),
                    corrections=corrections,
                    **llm_config["eval_batch"]
                )

            # 保存结果
            _save_evaluation(msg_id, persona_id, eval_result, context)
            print(f"[BATCH EVAL] {msg_id} => {eval_result.get('total_score')}")

        except Exception as e:
            print(f"[BATCH EVAL ERROR] {msg.get('id')}: {e}")


def _build_eval_context(conn, persona_id, current_msg_id, user_id=None):
    """构建评测上下文"""
    from datetime import datetime, timedelta

    today = datetime.now().strftime("%Y-%m-%d")

    # 先取 persona，后续格式化历史和取玩偶人设都要用 target_api
    persona = execute_query(conn, "SELECT * FROM personas WHERE id = ? AND user_id = ?", (persona_id, user_id), fetch_one=True)
    persona_data = row_to_dict(persona) if persona else None
    target_api = (persona_data or {}).get("target_api") or "pipi"

    # 获取当天对话
    today_msgs = execute_query(conn, """
        SELECT id, role, text, created_at FROM chat_messages
        WHERE persona_id = ? AND id < ? AND date(created_at) = ? AND user_id = ?
        ORDER BY id
    """, (persona_id, current_msg_id, today, user_id), fetch_all=True)
    today_msgs = [row_to_dict(m) for m in today_msgs]

    # 如果当天不足 6 条（3轮），补取昨天的
    if len(today_msgs) < 6:
        yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
        older_msgs = execute_query(conn, """
            SELECT id, role, text, created_at FROM chat_messages
            WHERE persona_id = ? AND date(created_at) = ? AND user_id = ?
            ORDER BY id DESC LIMIT 10
        """, (persona_id, yesterday, user_id), fetch_all=True)
        older_msgs = [row_to_dict(m) for m in older_msgs]
        today_msgs = list(reversed(older_msgs)) + today_msgs

    # 格式化对话历史（含时间间隔标记）
    chat_history = _format_history_with_gaps(today_msgs, persona_data=persona_data)

    # 获取用户事实（过滤已遗忘的）
    facts = execute_query(conn, """
        SELECT uf.id, uf.category, uf.fact_key, uf.entity_name, uf.fact_value, uf.memory_level, uf.weight, uf.created_at, uf.fact_type,
               COALESCE(mdr.forget_threshold, 0.1) as forget_threshold
        FROM user_facts uf
        LEFT JOIN memory_decay_rules mdr ON uf.memory_level = mdr.level
        WHERE uf.persona_id = ? AND uf.is_active = 1 AND uf.user_id = ?
    """, (persona_id, user_id), fetch_all=True)

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

    # 获取玩偶人设（按用户绑定的 target_api 查）
    toy_persona = _get_toy_persona_by_target(target_api)

    return {
        "chat_history": chat_history,
        "user_facts": user_facts,
        "persona_data": persona_data,
        "toy_persona": toy_persona
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


def _format_history_with_gaps(msgs, persona_data=None):
    """格式化对话历史，超过 2 小时插入时间分隔"""
    from datetime import datetime
    target_api = (persona_data or {}).get("target_api") or "pipi"
    ai_name = _get_toy_persona_name(target_api)
    result = []
    prev_time = None
    for m in msgs:
        try:
            curr_time = datetime.strptime(m["created_at"][:19], "%Y-%m-%d %H:%M:%S")
            if prev_time and (curr_time - prev_time).total_seconds() > 7200:
                hours = int((curr_time - prev_time).total_seconds() / 3600)
                result.append(f"—— 间隔 {hours} 小时 ——")
            prefix = "用户: " if m["role"] == "user" else ai_name + ": "
            result.append(prefix + m["text"])
            prev_time = curr_time
        except:
            prefix = "用户: " if m["role"] == "user" else ai_name + ": "
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
         quality_score, quality_reason, persona_score, persona_reason, total_score, eval_context, deduction_tags, judges_detail)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
        json.dumps({
            "facts_count": len(context.get("user_facts", [])),
            "history_len": len(context.get("chat_history", [])),
            "memory_objective_check": eval_result.get("memory_objective_check", {}),
            "breakdown": {
                "memory": eval_result.get("memory_deduction_breakdown", []),
                "emotion": eval_result.get("emotion_deduction_breakdown", []),
                "quality": eval_result.get("quality_deduction_breakdown", []),
                "persona": eval_result.get("persona_deduction_breakdown", []),
            },
            "judges_std": eval_result.get("judges_std", 0),
        }, ensure_ascii=False),
        json.dumps({
            "memory": eval_result.get("memory_tags", []),
            "emotion": eval_result.get("emotion_tags", []),
            "quality": eval_result.get("quality_tags", []),
            "persona": eval_result.get("persona_tags", []),
        }, ensure_ascii=False),
        json.dumps(eval_result.get("judges_detail", []), ensure_ascii=False) if eval_result.get("judges_detail") else None,
    ))
    conn.commit()
    conn.close()


def _save_correction(conn, eval_type, ref_id, dimension_code, user_input, ai_reply,
                     auto_score, human_score, correction_reason):
    """存储人工纠正记录到 eval_corrections 表（有说明才存，按 ref_id 去重）"""
    if not correction_reason or not correction_reason.strip():
        return
    ph = "%s" if USE_MYSQL else "?"

    # 按 ref_id 去重：先删旧再插新
    execute_query(conn, f"DELETE FROM eval_corrections WHERE eval_type = {ph} AND ref_id = {ph}",
                  (eval_type, str(ref_id)))

    execute_query(conn, f"""
        INSERT INTO eval_corrections
        (eval_type, ref_id, dimension_code, user_input, ai_reply, auto_score, human_score, correction_reason)
        VALUES ({ph}, {ph}, {ph}, {ph}, {ph}, {ph}, {ph}, {ph})
    """, (eval_type, str(ref_id), dimension_code, user_input, ai_reply,
          auto_score, human_score, correction_reason))


def _load_recent_corrections(eval_type, dimension_code=None, limit=20):
    """加载最近的 N 条人工纠正记录，用于 few-shot 注入。
    受 eval_config.inject_corrections 开关控制，默认关闭（不注入）。
    """
    # 开关默认 false：不自动注入人工纠正到下次评测 prompt
    if not _is_inject_corrections_enabled():
        return []

    conn = get_db_connection()
    ph = "%s" if USE_MYSQL else "?"

    if eval_type == 'test_case' and dimension_code:
        rows = execute_query(conn, f"""
            SELECT eval_type, ref_id, dimension_code, user_input, ai_reply,
                   auto_score, human_score, correction_reason, created_at
            FROM eval_corrections
            WHERE eval_type = {ph} AND dimension_code = {ph}
            ORDER BY created_at DESC
            LIMIT {ph}
        """, (eval_type, dimension_code, limit), fetch_all=True)
    else:
        rows = execute_query(conn, f"""
            SELECT eval_type, ref_id, dimension_code, user_input, ai_reply,
                   auto_score, human_score, correction_reason, created_at
            FROM eval_corrections
            WHERE eval_type = {ph}
            ORDER BY created_at DESC
            LIMIT {ph}
        """, (eval_type, limit), fetch_all=True)

    conn.close()
    return [row_to_dict(r) for r in rows] if rows else []


def _is_inject_corrections_enabled():
    """检查「人工纠正注入下次评测」开关是否开启，默认关闭"""
    conn = get_db_connection()
    row = execute_query(conn, "SELECT value FROM eval_config WHERE `key`='inject_corrections'", fetch_one=True)
    conn.close()
    return bool(row) and row["value"] == "true"





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


def _create_fact(data, user_id=None):
    import time
    if user_id is None:
        user_id = _current_uid()
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
        "INSERT INTO user_facts (persona_id, category, fact_key, entity_name, fact_value, fact_type, confidence, occurred_at, emotion_tag, related_fact_ids, source_case, source_session, source_text, memory_level, weight, user_id) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,1.0,?)",
        (persona_id, category, fact_key, entity_name, fact_value, fact_type, confidence, occurred_at, emotion_tag, related_fact_ids, source_case, source_session, source_text, memory_level, user_id))
    entity_tag = f"[{entity_name}]" if entity_name else ""
    print(f"[FACT INSERT] {category}.{fact_key}{entity_tag}({fact_type}) = {fact_value[:50]}")

    # 同步更新 personas 表（如果字段匹配映射，且为 permanent 类型）
    # user_id 过滤通过子查询反查（worker 无 request context，不能调 _current_uid）
    if fact_type == "permanent":
        persona_field = PERSONA_FIELD_MAP.get((category, fact_key))
        if persona_field and persona_id:
            execute_query(conn,
                f"UPDATE personas SET {persona_field} = ? WHERE id = ? AND user_id = (SELECT user_id FROM personas WHERE id = ?)",
                (fact_value, persona_id, persona_id))
            print(f"[FACT->PERSONA] {category}.{fact_key} -> personas.{persona_field} = {fact_value}")

    conn.commit()
    conn.close()


def save_chat_msg(persona_id, role, user_name, text, user_id=None, ttfb_ms=None, total_ms=None):
    if user_id is None:
        user_id = _current_uid()
    conn = get_db_connection()
    cur = execute_query(conn,
        "INSERT INTO chat_messages (persona_id, role, user_name, text, user_id, ttfb_ms, total_ms) VALUES (?,?,?,?,?,?,?)",
        (persona_id, role, user_name, text, user_id, ttfb_ms, total_ms))
    msg_id = get_lastrowid(cur)
    conn.commit()
    conn.close()
    return msg_id


def call_api(persona_id, message):
    uid = persona_id if persona_id else "guest"
    uname = uid.split('/')[-1] if '/' not in uid else uid
    cur_uid = _current_uid()
    save_chat_msg(uid, "user", uname, message, user_id=cur_uid)

    if persona_id == "__guest__":
        device_id = "TEST_DEV_HUARONG_guest_" + str(random.randint(10000000, 99999999))
        persona_data = None
        name = "游客"
        api_url, api_key = None, None  # 使用默认
    else:
        conn = get_db_connection()
        row = execute_query(conn, "SELECT * FROM personas WHERE id=? AND user_id=?", (persona_id, cur_uid), fetch_one=True)
        conn.close()
        if not row:
            return {"error": "用户不存在"}
        persona_data = row_to_dict(row)
        device_id = persona_data["device_id"]
        name = persona_data["name"]
        target_api = persona_data.get("target_api") or "pipi"
        api_url, api_key, api_headers, _protocol = get_api_config_by_code(target_api)

    system_prompt = pipi_api.build_system_prompt(persona_data, device_id, target_api=target_api)
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": message},
    ]

    print(f"[CALL API] persona_id={persona_id} device_id={device_id} msg={message[:50]}", flush=True)
    result = pipi_api.call_pipi_stream(messages, device_id=device_id, api_url=api_url, api_key=api_key, extra_headers=api_headers, protocol=_protocol, user_id=persona_id)
    if result.get("full_text"):
        msg_id = save_chat_msg(persona_id or "guest", "pipi", _get_toy_persona_name(target_api) if persona_id and persona_id != "__guest__" else "皮皮", result["full_text"], user_id=cur_uid,
                               ttfb_ms=result.get("ttfb_ms"), total_ms=result.get("response_time_ms"))
        result["message_id"] = msg_id

        # 实时评测（如果开关开启）
        eval_on = is_eval_enabled()
        print(f"[EVAL CHECK] persona_id={persona_id} eval_enabled={eval_on}", flush=True)
        if persona_id and persona_id != "__guest__" and eval_on:
            print(f"[EVAL THREAD START] msg_id={msg_id}", flush=True)
            t_eval = threading.Thread(
                target=_evaluate_and_save,
                args=(msg_id, persona_id, message, result["full_text"], persona_data, cur_uid),
                daemon=True
            )
            t_eval.start()

    result["user"] = name

    if persona_id and persona_id != "__guest__":
        t = threading.Thread(target=_extract_and_save, args=(persona_id, message, persona_data, cur_uid), daemon=True)
        t.start()

    return result


def _evaluate_and_save(msg_id, persona_id, user_message, reply_text, persona_data, user_id=None):
    """实时评测并保存结果"""
    try:
        conn = get_db_connection()
        context = _build_eval_context(conn, persona_id, msg_id, user_id=user_id)
        conn.close()

        corrections = _load_recent_corrections(eval_type="chat", limit=20)
        llm_config = get_llm_config()
        with _EVAL_SEMAPHORE:
            eval_result = pipi_api.evaluate_chat_reply(
                reply_text=reply_text,
                user_message=user_message,
                chat_history=context['chat_history'],
                user_facts=context['user_facts'],
                persona_data=context['persona_data'],
                toy_persona=context.get('toy_persona'),
                corrections=corrections,
                **llm_config["eval_realtime"]
            )

        _save_evaluation(msg_id, persona_id, eval_result, context)
        print(f"[REALTIME EVAL] {msg_id} => {eval_result.get('total_score')}", flush=True)

    except Exception as e:
        import traceback
        print(f"[REALTIME EVAL ERROR] {msg_id}: {e}\n{traceback.format_exc()}", flush=True)


def _extract_and_save(persona_id, message, persona_data, user_id=None):
    if user_id is None:
        user_id = _current_uid()
    target_api = (persona_data or {}).get("target_api") or "pipi"
    try:
        conn = get_db_connection()
        fact_rows = execute_query(conn,
            "SELECT id, category, fact_key, entity_name, fact_value FROM user_facts WHERE persona_id=? AND is_active=1 AND user_id=?",
            (persona_id, user_id), fetch_all=True)
        existing_facts = [row_to_dict(r) for r in fact_rows]
        history_rows = execute_query(conn,
            "SELECT role, text FROM chat_messages WHERE persona_id=? AND user_id=? ORDER BY id DESC LIMIT 10",
            (persona_id, user_id), fetch_all=True)
        conn.close()

        chat_history = []
        for row in reversed(history_rows):
            prefix = "用户: " if row["role"] == "user" else _get_toy_persona_name(target_api) + ": "
            chat_history.append(prefix + row["text"])

        llm_config = get_llm_config()
        facts = pipi_api.extract_facts_from_message(
            message, persona_data, existing_facts, chat_history=chat_history, target_api=target_api, **llm_config["fact_extract"])

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
                _create_fact(f, user_id=user_id)

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
        fields = ["id", "name", "device_id", "target_api", "user_id"]
        values = [persona_id, persona_name, device_id, target_api, _current_uid()]

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
    uid = _current_uid()
    for p in created_personas:
        conn = get_db_connection()
        cur = execute_query(conn,
            "INSERT INTO growth_tasks (persona_id, speed, status, total_messages, user_id) VALUES (?,?,?,?,?)",
            (p["id"], speed, "pending", len(messages), uid))
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
        t = threading.Thread(target=_growth_worker, args=(task_id, uid), daemon=True)
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

    # 检查用户是否存在且属于当前用户
    conn = get_db_connection()
    uid = _current_uid()
    row = execute_query(conn, "SELECT id FROM personas WHERE id=? AND user_id=?", (persona_id, uid), fetch_one=True)
    if not row:
        conn.close()
        return jsonify({"error": f"persona_id '{persona_id}' not found"}), 404

    # 创建成长任务
    cur = execute_query(conn,
        "INSERT INTO growth_tasks (persona_id, speed, status, total_messages, user_id) VALUES (?,?,?,?,?)",
        (persona_id, speed, "pending", len(messages), uid))
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
    t = threading.Thread(target=_growth_worker, args=(task_id, uid), daemon=True)
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

    # 获取该用户的所有事实（成长期间新增的，按 user_id 隔离）
    persona_id = task["persona_id"]
    created_at = task["created_at"]
    uid = _current_uid()
    # 校验 task 归属
    if task.get("user_id") and int(task["user_id"]) != uid:
        conn.close()
        return jsonify({"error": "task not found"}), 404
    facts_rows = execute_query(conn,
        "SELECT * FROM user_facts WHERE persona_id=? AND created_at >= ? AND user_id=? ORDER BY created_at",
        (persona_id, created_at, uid), fetch_all=True)
    extracted_facts = [row_to_dict(r) for r in facts_rows]

    conn.close()

    return jsonify({
        "task": task,
        "progress": progress,
        "extracted_facts": extracted_facts
    })


def _generate_persona_profile(name="", target_api="pipi"):
    """调用 LLM 生成完整的用户画像，按 target_api 切换目标人群画像。"""
    if target_api == "oho":
        system_prompt = """你是用户画像生成器。为 OHO（贴在 iPhone 背后的 AI 灵感捕手 + 录音笔记助手）生成虚拟测试用户。

目标用户：25-44 岁，iPhone 用户，以一二线城市为主。
核心人群：
A. 高频会议与推进型用户 — PM、创业者、创始人、管理层、研究者，频繁电话/会议/访谈，记录目的是后续推进而非归档。
B. 记录型知识工作者 + 写作者/创作者 — 设计师、运营、记者、内容创作者，输入碎片多，灵感出现频繁。
次级：终身学习者（读书/摘录/课程笔记/思考碎片）。

共性：
- 高频输入（不断遇到想记该记的内容）
- 低容忍记录摩擦（不愿为记录切 App 或做复杂整理）
- 需要后续推进（记录为了行动、输出、跟进）
- 对隐私与可控性敏感（可导出、可删除、可关闭同步）
- 愿意尝试新工具，但只接受"有用且酷"，不愿做实验品

痛点：想法来得快但手机记录路径太慢；对话/会议/灵感/待办断裂不在一个流里；录了音很难再推进；不想维护重系统。
购买驱动：低摩擦（贴手机背后不额外带设备）；接住瞬时内容（灵感、半成形表达、会议要点、电话信息）；后续可调起、可整理、可推进；physical presence 区别于纯 App。
顾虑：速度、可靠性、隐私、电池、是否真比手机 App 顺手。
反向筛掉：只想要 AI 玩具的人、只想要录音卡平替的人、只看陪伴感的人、愿意忍受复杂系统学习成本的人。

要求：字段间有逻辑关联，像真实的人。直接返回JSON。"""
    else:
        system_prompt = """你是用户画像生成器。为AI陪伴玩偶产品生成虚拟用户。

目标用户：15-34岁女性，三线及以上城市，喜欢毛绒玩具和宠物。
消费特点：视觉吸引→情绪共鸣→瞬间下单，颜值正义，情绪消费。
喜欢品牌：泡泡玛特、Jellycat、完美日记、潘多拉等。
兴趣：改娃/OC创作、MBTI/塔罗/星座、重度小红书+B站用户。

要求：字段间有逻辑关联，像真实的人。直接返回JSON。"""

    user_prompt = f"""为"{name or '用户'}"生成画像，返回JSON：
{{"nickname":"一句话描述","real_name":"姓名","gender":"女","age":"年龄","city":"城市","hometown":"老家省份","occupation":"职业(写具体,如'在一家中型SaaS公司做后端开发,主要写Python'而非仅'程序员')","education":"学历","family_status":"家庭状态","relationship":"感情状态","personality":"性格特点(写具体行为表现,如'人多的场合会躲在角落看手机,熟了才话多'而非'比较内向')","income_range":"月收入","spending_style":"消费风格","spending_desc":"消费习惯举例","devices":"常用设备","usage_scenes":"使用场景","core_goal":"核心目标","short_goal":"短期诉求","long_goal":"长期诉求","pain_points":"痛点","constraints":"约束","risk_profile":"风险偏好","interests":"兴趣标签","language_style":"语言风格和常用语气词(含具体口头禅)","sample_dialog":"典型对话1-2句","info_sources":"信息来源","decision_style":"决策方式","relation_pace":"关系节奏","scene_pref":"场景偏好","top_expectations":"期待TOP3","minefields":"踩雷点","pet_type":"宠物类型","pet_name":"宠物名","pet_age":"宠物年龄","pet_trait":"宠物特点(具体行为,如'掉毛多,每天早上趴键盘上要饭')","favorite_drink":"爱喝的(具体,如'冰美式,一天两杯,下午两点后不喝会头疼')","favorite_food":"爱吃的(具体到菜系+代表菜)","spicy_preference":"吃辣偏好(具体耐受度)","current_hobby":"当前爱好(最近在做什么)","learning":"在学什么(进度如何)","favorite_singer":"喜欢的歌手/乐队","best_friend":"好友名+关系+认识多久","stress_relief":"解压方式(具体动作)","work_time":"上班时间","lunch_habit":"午餐习惯","commute":"通勤方式+时长","childhood_memory":"童年一段记忆(带场景)","life_milestone":"近期重要人生节点(带时间)","recent_worry":"最近在烦的一件事(具体)","recent_mood":"近一周整体情绪状态+原因","important_person":"身边最重要的人+原因","daily_routine":"一日作息","sleep_habit":"睡眠习惯(几点睡几点起,有无午睡)","weekend_plan":"最近一个周末的安排","birthday":"生日(月日)","mbti":"MBTI类型","family_atmosphere":"家庭氛围(父母关系、教养方式)","colleague_relationship":"和同事的关系","nighttime_routine":"睡前习惯","stress_trigger":"最近一次情绪波动的具体触发","comfort_seeker":"难过时会找谁/做什么"}}"""

    try:
        llm_config = get_llm_config()
        persona_cfg = llm_config.get("persona_gen", {})
        result = None
        # 504 / 超时 / 网络抖动类错误重试 3 次，间隔递增
        import time as _t
        for attempt in range(3):
            try:
                result = pipi_api.call_llm_simple(
                    system_prompt, user_prompt,
                    timeout=persona_cfg.get("timeout", 180),
                    model=persona_cfg.get("model"),
                    temperature=persona_cfg.get("temperature", 0.7),
                    max_tokens=persona_cfg.get("max_tokens", 4096),
                )
                if result:
                    break
                if attempt < 2:
                    _t.sleep(5)
                    print(f"[PERSONA GEN] attempt {attempt+1} empty, retrying...", flush=True)
            except Exception as e:
                print(f"[PERSONA GEN ERROR] attempt {attempt+1}: {e}", flush=True)
                if attempt < 2:
                    _t.sleep(5 + attempt * 5)
                else:
                    raise
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
    return _generate_persona_profile_fallback(target_api=target_api)


def _generate_persona_profile_fallback(target_api="pipi"):
    """随机生成用户画像（降级方案）"""
    def pick(arr):
        return arr[random.randint(0, len(arr) - 1)]

    if target_api == "oho":
        return {
            "age": pick(["27", "30", "32", "34", "36", "38", "42"]),
            "gender": pick(["男", "女"]),
            "city": pick(["北京", "上海", "深圳", "杭州", "成都", "广州"]),
            "hometown": pick(["江苏", "浙江", "山东", "湖北", "湖南", "四川"]),
            "occupation": pick(["创业者/CEO", "产品经理", "设计师", "运营", "投资人", "内容创作者", "研究者"]),
            "education": pick(["本科", "硕士"]),
            "family_status": pick(["已婚有娃", "已婚无娃", "单身"]),
            "relationship": pick(["已婚", "有对象", "单身"]),
            "interests": pick(["创业,读书", "写作,长跑", "投资,播客", "设计,摄影", "阅读,思考碎片"]),
            "language_style": pick(["直接简洁、信息密度高", "冷静理性、少废话", "简洁但带温度"]),
            "personality": pick(["目标导向、低耐受摩擦", "理性、效率优先", "好奇驱动、爱折腾"]),
            "devices": "iPhone 15 Pro + MacBook Pro",
            "usage_scenes": pick(["会议、电话、走路灵感", "采访、写作素材、跨场景输入", "会议、daily notes、复盘"]),
            "core_goal": "让信息从输入到推进零损耗",
            "short_goal": pick(["本周完成季度规划", "把上次访谈沉淀成可推进的行动项", "把碎片灵感整理成提纲"]),
            "long_goal": pick(["3年内公司IPO", "做成一个有影响力的产品", "把个人知识库变成可调用的资产"]),
            "pain_points": "想法来得快但记录路径太慢，录音后很难再推进",
            "constraints": "不愿维护复杂系统，只接受有用且酷",
            "risk_profile": "对新硬件 early adopter",
            "info_sources": pick(["播客、社群、读书", "Newsletter、行业群、X", "会议、电话、走动思考"]),
            "decision_style": "快速判断、低摩擦执行",
            "relation_pace": "高频低长度",
            "scene_pref": "会议、电话、灵感捕捉",
            "top_expectations": "快速接住、后续可推进、不丢重要念头",
            "minefields": "别让我做系统管理员，别让我多切一个 App",
        }

    return {
        "age": pick(["23", "25", "27", "28", "30", "32", "35"]),
        "gender": pick(["男", "女"]),
        "city": pick(["北京", "上海", "深圳", "杭州", "成都", "广州"]),
        "hometown": pick(["山东", "河南", "四川", "湖南", "江苏", "浙江"]),
        "occupation": pick([
            "在一家中型SaaS公司做后端开发,主要写Python,最近在做支付系统",
            "在4A广告公司做美术指导,带3个人的小组,经常比稿",
            "在公立小学做三年级语文老师兼班主任,带50个孩子",
            "在三甲医院心内科做住院医师,经常值夜班",
            "在互联网大厂做用户研究,主要跑访谈和问卷",
        ]),
        "education": pick(["本科", "硕士", "大专"]),
        "family_status": pick(["独生子女", "有哥哥", "有姐姐", "有个弟弟"]),
        "relationship": pick(["单身三年了", "有对象,在一起两年", "刚分手几个月", "异地恋中"]),
        "interests": pick(["看电影,听音乐", "打游戏,看书", "跑步,健身", "追番,cosplay", "烘焙,手工"]),
        "language_style": pick(["活泼开朗,爱用'啊''啦''嘛'", "温和内敛,爱用'嗯''其实''也'", "幽默风趣,爱自嘲和'hh'", "理性简洁,不爱用语气词"]),
        "personality": pick([
            "人多的场合会躲在角落看手机,熟了才话多",
            "自来熟,第一次见面也能聊半天,但深度朋友不多",
            "有点社恐,接电话都要做心理建设,更喜欢文字交流",
            "外热内冷,看着热闹其实心里在算计,真正交心的就一两个人",
        ]),
        "pet_type": pick(["猫", "狗", ""]),
        "pet_name": pick(["豆豆", "毛毛", "球球", "麻薯", "糯米"]),
        "pet_age": pick(["1岁", "2岁", "3岁", "5岁"]),
        "pet_trait": pick([
            "掉毛多,每天早上趴键盘上要饭",
            "特别粘人,我上厕所都要在门口蹲着",
            "很调皮,袜子被叼走七八只了",
            "超级可爱但高冷,心情好才让撸两下",
        ]),
        "favorite_drink": pick([
            "冰美式,一天两杯,下午两点后不喝会头疼",
            "奶茶,三分糖加椰果,一周至少三次",
            "可乐,必须无糖加冰,饭必配",
            "水果茶,自己泡,不爱甜的",
        ]),
        "favorite_food": pick([
            "火锅,川渝那种麻辣牛油锅,毛肚鸭肠必点",
            "日料,尤其三文鱼刺身和鳗鱼饭",
            "烧烤,和朋友们大排档那种,啤酒配烤串",
            "粤菜,早茶和烧腊都爱",
        ]),
        "spicy_preference": pick([
            "不吃辣,一点点都会出汗",
            "无辣不欢,湘菜川菜都行,越辣越爽",
            "微辣,再辣就扛不住,火锅只敢点微辣鸳鸯",
            "中辣,能吃辣但胃不行,吃完会难受",
        ]),
        "current_hobby": pick([
            "最近在追繁花,觉得王家卫太绝了",
            "在玩塞尔达,已经肝了一百多小时",
            "在准备半马,每周跑三次,逐步加量",
            "在学手冲咖啡,刚买了V60和磨豆机",
        ]),
        "learning": pick([
            "在学吉他,刚会四个和弦,切换还不顺",
            "在学烘焙,戚风蛋糕还在塌腰阶段",
            "在学日语,N3水平,每天背五十音卡片",
            "在学摄影,刚弄懂光圈快门ISO的关系",
        ]),
        "favorite_singer": pick(["周杰伦", "五月天", "陈奕迅", "孙燕姿", "告五人"]),
        "best_friend": pick([
            "阿明,大学室友,认识七年了,无话不谈",
            "小美,前同事,跳槽后还是经常约饭",
            "老张,高中同学,现在还在一个城市",
        ]),
        "stress_relief": pick([
            "听音乐,戴耳机循环播放,谁也不理",
            "吃东西,一定要是辣的或者炸的",
            "睡觉,手机静音,睡到自然醒",
            "去楼下走两圈,或者撸一下我家猫",
        ]),
        "work_time": pick(["8点", "9点", "9点半", "10点"]),
        "lunch_habit": pick(["点外卖", "去食堂", "自己带饭", "和同事拼单去附近吃"]),
        "commute": pick(["走路10分钟", "地铁半小时", "骑车15分钟", "公交40分钟"]),
        "childhood_memory": pick([
            "小时候暑假在外婆家过的,外婆会做酸梅汤,我和表弟天天在巷子里疯跑",
            "小学三年级考了第一名,爸爸奖励了一辆自行车,我骑着绕了小区三圈",
            "小时候家里没空调,夏天晚上在阳台上铺凉席,我妈给我扇扇子",
        ]),
        "life_milestone": pick([
            "上个月刚换了工作,从乙方跳到甲方,薪资涨了30%",
            "三个月前搬了家,从合租换成一居室,终于有自己的空间了",
            "去年这个时候和前任分手,到现在还没缓过来",
            "前年冬天养的猫,从两个月大一直养到现在",
        ]),
        "recent_worry": pick([
            "最近在烦项目上线的事,老板一直催,感觉要加班到月底",
            "我妈最近老催我找对象,每次电话都要提,我都怕接她电话了",
            "体检报告出来,血脂偏高,医生让我控制饮食,但我不想放弃火锅",
            "房租又要涨了,在想要不要搬到远一点的地方",
        ]),
        "recent_mood": pick([
            "这周整体挺累的,项目压力大,睡眠也不好,有点焦虑",
            "还不错,刚完成一个大事,轻松不少,想奖励自己出去玩一趟",
            "有点低落,朋友出了点事,我也跟着担心",
            "平淡,该上班上班,没什么特别开心也没什么特别烦的",
        ]),
        "important_person": pick([
            "我闺蜜,认识十年了,什么都能说,难过时第一个找她",
            "我妈,虽然她唠叨但最懂我,有事第一时间想到她",
            "我对象,异地但每天打电话,是我最大的支撑",
            "我自己,习惯了独处,有问题也先自己消化",
        ]),
        "daily_routine": pick([
            "7点起,8点出门,9点到公司,午休一小时,晚上7点下班,回家做饭看剧11点睡",
            "9点起,9点半上班,中午外卖,晚上加班到9点,回家打游戏到1点",
            "8点半起,通勤40分钟,午饭带饭,下班6点,晚上健身或看书,11点半睡",
        ]),
        "sleep_habit": pick([
            "12点左右睡,7点起,偶尔失眠,不午休",
            "1点睡,周末能睡到中午,工作日靠咖啡续命",
            "11点半睡,6点半起,有午休半小时的习惯",
            "经常熬夜到2点,早上起不来,周末补觉",
        ]),
        "weekend_plan": pick([
            "这周末约了同事去吃新开的湘菜馆,下午想顺便逛个展",
            "周末打算在家补觉+追剧,周日下午约了朋友喝下午茶",
            "周六去爸妈那边吃饭,周日要去看个娃展",
            "周末想自己在家做顿饭,试试新买的烤箱",
        ]),
        "birthday": pick(["3月15日", "5月22日", "8月8日", "11月30日", "12月25日"]),
        "mbti": pick(["INFP", "ENFP", "ISFJ", "INTJ", "ENFJ", "ISTP", "INFJ"]),
        "family_atmosphere": pick([
            "父母关系一般,不吵也不甜,从小比较独立",
            "家庭氛围挺暖的,爸妈感情好,有什么都能聊",
            "爸爸比较严厉,妈妈温柔,从小被管得紧,到现在还有点怕爸爸",
            "单亲家庭,跟妈妈长大,关系很近也很粘",
        ]),
        "colleague_relationship": pick([
            "和同事都还过得去,但不敢聊太多私事,怕被传闲话",
            "团队氛围挺好,经常一起吃饭,但深交的没有",
            "和直属领导不太对付,其他同事还行",
            "有个聊得来的同事,中午经常一起吃饭",
        ]),
        "nighttime_routine": pick([
            "睡前必刷手机半小时,经常刷到困才放下",
            "泡脚+听播客,助眠,基本11点半能睡着",
            "看会儿纸质书,最近在看一本散文",
            "听白噪音入睡,雨声那种",
        ]),
        "stress_trigger": pick([
            "昨天老板在群里点了名,说项目进度慢,有点委屈",
            "前天和对象吵了一架,为异地的事",
            "体检报告出来血脂偏高,有点慌",
            "妈妈又催婚,这次直接给介绍了对象,我拒绝了,气氛很僵",
        ]),
        "comfort_seeker": pick([
            "会找闺蜜倾诉,但怕她担心,只说一半",
            "闷着自己消化,吃东西+睡觉+刷剧三连",
            "和对象打电话,听到他的声音就安心了",
            "撸猫,抱着我家猫半天就好了",
        ]),
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



def _get_persona_template(template_id, target_api="pipi"):
    """获取用户预设模板（按 target_api 动态加载 persona_presets/<ta>.py）"""
    from interface_profiles import load_persona_presets
    templates = load_persona_presets(target_api)
    return templates.get(template_id)


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

    # 3. 获取模板配置（如果指定了模板，按 persona.target_api 加载）
    template = _get_persona_template(template_id, target_api=persona.get("target_api", "pipi")) if template_id else None
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

    # 5.1 补充 personas 字段（随机生成值填入字段，让 LLM 有数据可用）
    filled_fields = {}
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
            filled_fields[field] = val
            msg = random.choice(templates).format(val=val)
            messages.append(msg)

            # 同步写回 personas 表（保证字段持久化，后续 LLM 生成消息时有完整画像）
            # user_id 过滤通过子查询反查（worker 无 request context）
            try:
                conn2 = get_db_connection()
                execute_query(conn2, f"UPDATE personas SET {field}=? WHERE id=? AND user_id = (SELECT user_id FROM personas WHERE id=?)", (val, persona_id, persona_id))
                conn2.commit()
                conn2.close()
            except Exception as e:
                print(f"[AUTO POPULATE] update personas.{field} failed: {e}", flush=True)

    # 5.2 补充 user_facts 字段：用模板生成 fact 消息（保证 fact 提取有源数据）
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

    # 6. 调 LLM 生成额外自然对话消息（用 persona + 填好的字段）
    # 把英文类别名映射为中文类别名（LLM 需要中文）
    category_cn_map = {
        "food": "饮食偏好", "preference": "偏好", "living": "生活", "family": "家庭",
        "relationship": "社交关系", "work": "工作", "health": "健康", "hobby": "兴趣爱好",
        "emotion": "情感", "pet": "宠物",
    }
    cn_categories = [category_cn_map[c] for c in (categories or list(all_fact_keys_by_category.keys())) if c in category_cn_map]
    try:
        llm_config = get_llm_config()
        persona_cfg = llm_config.get("persona_gen", {})
        llm_messages = pipi_api.generate_persona_messages(
            {**persona, **filled_fields},
            categories=cn_categories or None,
            custom_messages=None,
            timeout=persona_cfg.get("timeout", 180),
            target_api=persona.get("target_api", "pipi"),
        )
        if llm_messages:
            messages.extend(llm_messages)
    except Exception as e:
        print(f"[AUTO POPULATE] LLM gen failed, using template-only: {e}", flush=True)

    # 7. 打乱顺序
    random.shuffle(messages)

    cat_str = ",".join(categories) if categories else "all"
    print(f"[AUTO POPULATE] persona={persona_id} template={template_id} categories={cat_str} missing_persona={missing_persona_fields} missing_facts={len(missing_fact_keys)} total_msgs={len(messages)}")

    return messages


def _generate_messages_from_persona(persona, categories, custom_messages=None, target_api="pipi"):
    """根据 persona 生成对话消息。调用 LLM 生成自然多样的消息，避免模板硬编码。

    LLM 失败时降级到极简消息（仅 persona 字段直拼，不追加硬编码句子）。
    """
    if not persona or not categories:
        return []

    try:
        llm_config = get_llm_config()
        persona_cfg = llm_config.get("persona_gen", {})
        msgs = pipi_api.generate_persona_messages(
            persona, categories=categories, custom_messages=custom_messages,
            timeout=persona_cfg.get("timeout", 180),
            target_api=target_api,
        )
        if msgs:
            return msgs
    except Exception as e:
        print(f"[PERSONA MSG] LLM gen failed, falling back: {e}", flush=True)

    # 降级：从 persona 字段拼极简消息（不追加硬编码句子）
    fallback_msgs = []
    if persona.get("age"):
        fallback_msgs.append(f"我今年{persona['age']}岁")
    if persona.get("city"):
        fallback_msgs.append(f"我在{persona['city']}工作")
    if persona.get("occupation"):
        fallback_msgs.append(f"我是做{persona['occupation']}的")
    if persona.get("pet_type") and persona.get("pet_name"):
        fallback_msgs.append(f"我养了一只{persona['pet_type']}，叫{persona['pet_name']}")
    if persona.get("current_hobby"):
        fallback_msgs.append(f"我平时喜欢{persona['current_hobby']}")

    if custom_messages:
        for m in custom_messages:
            if isinstance(m, str) and m.strip():
                fallback_msgs.append(m.strip())

    return fallback_msgs


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
    uid = _current_uid()
    conn = get_db_connection()

    # 获取任务信息
    task = execute_query(conn, """
        SELECT * FROM growth_tasks WHERE id = ? AND user_id = ?
    """, (task_id, uid), fetch_one=True)

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
    persona = execute_query(conn, "SELECT device_id FROM personas WHERE id = ? AND user_id = ?", (persona_id, uid), fetch_one=True)
    if not persona:
        conn.close()
        return jsonify({"error": "persona not found"}), 404

    device_id = row_to_dict(persona)["device_id"]

    # 获取该用户已发送的消息数量
    sent_count = execute_query(conn, """
        SELECT COUNT(*) as cnt FROM chat_messages WHERE persona_id = ? AND role = 'user' AND user_id = ?
    """, (persona_id, uid), fetch_one=True)
    sent_count = row_to_dict(sent_count)["cnt"]

    # 重新获取消息模板（从 persona 重新生成）
    persona_data = execute_query(conn, "SELECT * FROM personas WHERE id = ? AND user_id = ?", (persona_id, uid), fetch_one=True)
    persona_data = row_to_dict(persona_data)

    # 更新任务状态为 running
    execute_query(conn, """
        UPDATE growth_tasks SET status = 'running', error_message = NULL, started_at = NOW()
        WHERE id = ?
    """, (task_id,))
    conn.commit()
    conn.close()

    # 启动后台线程继续执行
    t = threading.Thread(target=_growth_worker, args=(task_id, uid), daemon=True)
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
    errors = []

    # 校验配置：未选 categories 或无 messages 的提前返回，不进 worker
    for i, cfg in enumerate(configs):
        name = cfg.get("name", f"用户{i+1}")
        categories = cfg.get("categories", [])
        old_messages = cfg.get("messages", [])
        if not categories and not old_messages:
            errors.append({"name": name, "error": "未选择信息类别"})

    # 为每个用户预先分配 persona_id 和 device_id，立即返回前端
    accepted = []
    for i, cfg in enumerate(configs):
        name = cfg.get("name", f"用户{i+1}")
        categories = cfg.get("categories", [])
        old_messages = cfg.get("messages", [])
        if not categories and not old_messages:
            continue
        persona_id = f"auto_{timestamp}_{i+1}"
        device_id = f"TEST_DEV_HUARONG_{timestamp}_{i+1}_{random.randint(1000,9999)}"
        accepted.append({
            "persona_id": persona_id,
            "device_id": device_id,
            "name": name,
            "cfg": cfg,
        })
        results.append({
            "persona_id": persona_id,
            "task_id": None,
            "name": name,
            "device_id": device_id,
            "speed": cfg.get("speed", "normal"),
            "message_count": 0,
            "status": "pending",
        })

    # 后台线程异步处理 LLM 生成 + DB 写入，避免 gunicorn worker 超时
    uid = _current_uid()
    def _multi_create_worker():
        for item in accepted:
            pid = item["persona_id"]
            did = item["device_id"]
            name = item["name"]
            cfg = item["cfg"]
            try:
                _create_one_persona_async(pid, did, name, cfg, user_id=uid)
            except Exception as e:
                import traceback
                print(f"[MULTI CREATE ERROR] {name}: {e}\n{traceback.format_exc()}", flush=True)

    t = threading.Thread(target=_multi_create_worker, daemon=True)
    t.start()

    return jsonify({
        "created_count": len(results),
        "tasks": results,
        "errors": errors,
        "async": True
    })


def _create_one_persona_async(persona_id, device_id, name, cfg, user_id=None):
    """单个用户的异步创建：LLM 生成 profile + messages + 写 DB + 启动 growth worker"""
    if user_id is None:
        user_id = _current_uid()
    speed = cfg.get("speed", "normal")
    categories = cfg.get("categories", [])
    custom_messages = cfg.get("custom_messages", [])
    target_api = cfg.get("target_api", "pipi")
    old_messages = cfg.get("messages", [])

    # 调用 LLM 生成完整的用户画像
    profile = _generate_persona_profile(name, target_api=target_api)

    # 根据 persona 生成消息
    if categories:
        messages = _generate_messages_from_persona({**profile, "name": name}, categories, custom_messages, target_api=target_api)
    elif old_messages:
        messages = old_messages
    else:
        print(f"[MULTI CREATE] {name} no categories, skip", flush=True)
        return

    if not messages:
        print(f"[MULTI CREATE] {name} no messages, skip", flush=True)
        return

    conn = get_db_connection()

    # 创建完整的用户画像（按 personas 表字段）
    fields = [
        "id", "name", "device_id", "nickname", "real_name", "gender", "age",
        "city", "occupation", "education", "family_status", "income_range",
        "spending_style", "spending_desc", "devices", "usage_scenes",
        "core_goal", "short_goal", "long_goal", "pain_points", "constraints",
        "risk_profile", "interests", "language_style", "sample_dialog",
        "info_sources", "decision_style", "relation_pace", "scene_pref",
        "top_expectations", "minefields", "target_api", "user_id",
        # 新增 15 维（生活背景/情绪/作息扩展/家庭关系）
        "childhood_memory", "life_milestone", "recent_worry", "important_person",
        "recent_mood", "stress_trigger", "comfort_seeker",
        "daily_routine", "sleep_habit", "weekend_plan", "nighttime_routine",
        "birthday", "mbti", "family_atmosphere", "colleague_relationship",
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
        user_id,
        profile.get("childhood_memory", ""),
        profile.get("life_milestone", ""),
        profile.get("recent_worry", ""),
        profile.get("important_person", ""),
        profile.get("recent_mood", ""),
        profile.get("stress_trigger", ""),
        profile.get("comfort_seeker", ""),
        profile.get("daily_routine", ""),
        profile.get("sleep_habit", ""),
        profile.get("weekend_plan", ""),
        profile.get("nighttime_routine", ""),
        profile.get("birthday", ""),
        profile.get("mbti", ""),
        profile.get("family_atmosphere", ""),
        profile.get("colleague_relationship", ""),
    ]

    if USE_MYSQL:
        # 检查是否有非标量值（list/dict/tuple 一律 join 成字符串）
        for j, (f, v) in enumerate(zip(fields, values)):
            if isinstance(v, (list, tuple, dict)):
                if isinstance(v, (list, tuple)):
                    joined = ", ".join(str(x) for x in v)
                else:
                    joined = str(v)
                print(f"[PERSONA CREATE] field '{f}' has non-scalar value, auto-joined: {type(v).__name__}", flush=True)
                values[j] = joined
        ph = ", ".join(["%s"] * len(fields))
        sql = f"INSERT INTO personas ({', '.join(fields)}) VALUES ({ph})"
        execute_query(conn, sql, tuple(values))
    else:
        ph = ", ".join(["?"] * len(fields))
        execute_query(conn, f"INSERT INTO personas ({', '.join(fields)}) VALUES ({ph})", tuple(values))

    # 创建成长任务
    cur = execute_query(conn,
        "INSERT INTO growth_tasks (persona_id, speed, status, total_messages, user_id) VALUES (?,?,?,?,?)",
        (persona_id, speed, "pending", len(messages), user_id))
    task_id = get_lastrowid(cur)

    # 创建进度记录
    for idx, msg in enumerate(messages):
        if isinstance(msg, str) and msg.strip():
            execute_query(conn,
                "INSERT INTO growth_progress (task_id, message_index, user_message, status) VALUES (?,?,?,?)",
                (task_id, idx, msg.strip(), "pending"))

    conn.commit()
    conn.close()
    print(f"[MULTI CREATE] {name} saved persona_id={persona_id} task_id={task_id} msgs={len(messages)}", flush=True)

    # 启动后台成长 worker
    t = threading.Thread(target=_growth_worker, args=(task_id, user_id), daemon=True)
    t.start()


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
    uid_auto = _current_uid()
    cur = execute_query(conn,
        "INSERT INTO growth_tasks (persona_id, speed, status, total_messages, user_id) VALUES (?,?,?,?,?)",
        (persona_id, speed, "pending", len(messages), uid_auto))
    task_id = get_lastrowid(cur)

    for idx, msg in enumerate(messages):
        execute_query(conn,
            "INSERT INTO growth_progress (task_id, message_index, user_message, status) VALUES (?,?,?,?)",
            (task_id, idx, msg, "pending"))

    conn.commit()
    conn.close()

    # 启动后台线程
    t = threading.Thread(target=_growth_worker, args=(task_id, uid_auto), daemon=True)
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
    uid = _current_uid()

    conn = get_db_connection()
    if persona_id:
        rows = execute_query(conn,
            "SELECT * FROM growth_tasks WHERE persona_id=? AND user_id=? ORDER BY created_at DESC LIMIT 50",
            (persona_id, uid), fetch_all=True)
    else:
        rows = execute_query(conn,
            "SELECT * FROM growth_tasks WHERE user_id=? ORDER BY created_at DESC LIMIT 50",
            (uid,), fetch_all=True)
    conn.close()

    tasks = [row_to_dict(r) for r in rows]
    for t in tasks:
        total = t["total_messages"] or 1
        completed = t["completed_messages"] or 0
        t["progress"] = round(completed / total * 100, 1)

    return jsonify(tasks)


def _growth_worker(task_id, user_id=None, slot_type=None):
    """用户成长后台工作线程。user_id 隔离。"""
    if user_id is None:
        user_id = 1
    import time as time_module

    try:
        conn = get_db_connection()

        # 原子性抢占任务（避免多 worker 同时恢复导致重复执行，按 user_id 隔离）
        if USE_MYSQL:
            execute_query(conn,
                "UPDATE growth_tasks SET status=%s, started_at=NOW() WHERE id=%s AND status='pending' AND user_id=%s",
                ("running", task_id, user_id))
        else:
            execute_query(conn,
                "UPDATE growth_tasks SET status=?, started_at=NOW() WHERE id=? AND status='pending' AND user_id=?",
                ("running", task_id, user_id))
        conn.commit()

        # 检查是否成功抢占（防止多 worker 重复执行）
        task = execute_query(conn, "SELECT * FROM growth_tasks WHERE id=? AND status='running' AND user_id=?", (task_id, user_id), fetch_one=True)
        if not task:
            conn.close()
            print(f"[GROWTH] task {task_id} already claimed by another worker, exiting")
            return
        task = row_to_dict(task)
        persona_id = task["persona_id"]
        speed = task.get("speed", "normal")

        # 获取用户画像（按 user_id 隔离）
        persona_row = execute_query(conn, "SELECT * FROM personas WHERE id=? AND user_id=?", (persona_id, user_id), fetch_one=True)
        if not persona_row:
            execute_query(conn,
                "UPDATE growth_tasks SET status=?, error_message=? WHERE id=? AND user_id=?",
                ("failed", "persona not found", task_id, user_id))
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
                save_chat_msg(persona_id, "user", name, user_message, user_id=user_id)

                # 2. 调用玩偶接口
                target_api = persona_data.get("target_api") or "pipi"
                api_url, api_key, api_headers, _protocol = get_api_config_by_code(target_api)
                system_prompt = pipi_api.build_system_prompt(persona_data, device_id, target_api=target_api)
                api_messages = [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_message},
                ]
                result = pipi_api.call_pipi_stream(api_messages, device_id=device_id, api_url=api_url, api_key=api_key, extra_headers=api_headers, protocol=_protocol, user_id=persona_id)
                reply_text = result.get("full_text", "")
                api_error = result.get("error") or ""

                # 3. 保存玩偶回复
                if reply_text:
                    save_chat_msg(persona_id, "pipi", _get_toy_persona_name(target_api), reply_text, user_id=user_id,
                                  ttfb_ms=result.get("ttfb_ms"), total_ms=result.get("response_time_ms"))
                else:
                    print(f"[GROWTH EMPTY REPLY] task={task_id} idx={msg_index} persona={persona_id} device={device_id} protocol={_protocol} error={api_error} ttfb={result.get('ttfb_ms')} total={result.get('response_time_ms')}", flush=True)

                # 4. 同步提取事实（准确度优先）
                extracted_facts = []
                try:
                    conn2 = get_db_connection()
                    fact_rows = execute_query(conn2,
                        "SELECT id, category, fact_key, entity_name, fact_value FROM user_facts WHERE persona_id=? AND is_active=1 AND user_id=?",
                        (persona_id, user_id), fetch_all=True)
                    existing_facts = [row_to_dict(r) for r in fact_rows]
                    history_rows = execute_query(conn2,
                        "SELECT role, text FROM chat_messages WHERE persona_id=? AND user_id=? ORDER BY id DESC LIMIT 10",
                        (persona_id, user_id), fetch_all=True)
                    conn2.close()

                    chat_history = []
                    for row in reversed(history_rows):
                        prefix = "用户: " if row["role"] == "user" else _get_toy_persona_name(target_api) + ": "
                        chat_history.append(prefix + row["text"])

                    llm_config = get_llm_config()
                    facts = pipi_api.extract_facts_from_message(
                        user_message, persona_data, existing_facts, chat_history=chat_history, target_api=target_api, **llm_config["fact_extract"])

                    if facts:
                        for f in facts:
                            related_ids = f.pop("related_to", [])
                            if related_ids:
                                f["related_fact_ids"] = ",".join(str(x) for x in related_ids)
                            f["persona_id"] = persona_id
                            f["confidence"] = "implicit"
                            f["source_session"] = f"growth_task_{task_id}"
                            f["source_text"] = user_message
                            _create_fact(f, user_id=user_id)
                            extracted_facts.append(f)

                    facts_count += len(extracted_facts)
                except Exception as e:
                    print(f"[GROWTH FACT ERROR] task={task_id} idx={msg_index}: {e}")

                # 5. 更新进度记录
                conn3 = get_db_connection()
                if reply_text:
                    execute_query(conn3,
                        "UPDATE growth_progress SET reply_text=?, facts_json=?, status=?, completed_at=NOW() WHERE id=?",
                        (reply_text, json.dumps(extracted_facts, ensure_ascii=False), "completed", prog_id))
                else:
                    execute_query(conn3,
                        "UPDATE growth_progress SET reply_text=?, facts_json=?, status=?, error_message=?, completed_at=NOW() WHERE id=?",
                        ("", json.dumps(extracted_facts, ensure_ascii=False), "failed", api_error or "empty reply", prog_id))
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
                "UPDATE growth_tasks SET status=?, error_message=? WHERE id=? AND user_id=?",
                ("failed", str(e), task_id, user_id))
            conn7.commit()
            conn7.close()
        except:
            pass
    finally:
        if slot_type:
            try:
                _release_slot(slot_type)
            except Exception as e:
                print(f"[SLOT RELEASE] {slot_type} failed: {e}", flush=True)


# ─── 测试用例管理 API ─────────────────────────────────────

# ─── 异步任务管理（数据库存储，支持多 worker）─────────────────

def _save_async_task(task_id: str, task_type: str, data: dict, user_id: int = None):
    """保存任务状态到数据库。

    user_id 参数：worker 调用时传入；路由直接调用时省略，从 g.user 取。
    INSERT 时写入 user_id，UPDATE 时按 user_id 校验防越权。
    """
    if user_id is None:
        user_id = _current_uid()
    conn = get_db_connection()
    config_json = json.dumps({k: v for k, v in data.items() if k in ("persona_id", "dimension_codes", "count_per_dimension", "clear_existing", "case_ids", "dimension_code", "status_filter", "device_id", "test_task_id")}, ensure_ascii=False)
    progress_json = json.dumps(data.get("progress", {}), ensure_ascii=False)
    result_json = json.dumps({k: v for k, v in data.items() if k in ("cases_created", "cases_executed", "cases_evaluated", "errors", "created_case_ids", "breached_count")}, ensure_ascii=False)

    execute_query(conn, """
        INSERT INTO async_tasks (id, task_type, status, persona_id, config_json, progress_json, result_json, error_message, user_id)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
        ON DUPLICATE KEY UPDATE
            status = VALUES(status),
            config_json = VALUES(config_json),
            progress_json = VALUES(progress_json),
            result_json = VALUES(result_json),
            error_message = VALUES(error_message)
    """, (task_id, task_type, data.get("status", "running"), data.get("persona_id", ""),
          config_json, progress_json, result_json, data.get("error_message", ""), user_id))
    conn.commit()
    conn.close()


def _load_async_task(task_id: str, user_id: int = None) -> dict:
    """从数据库加载任务状态。worker 传入 user_id，路由省略走 _current_uid()。"""
    if user_id is None:
        user_id = _current_uid()
    conn = get_db_connection()
    row = execute_query(conn, "SELECT * FROM async_tasks WHERE id = %s AND user_id = %s", (task_id, user_id), fetch_one=True)
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
_redteam_tasks = {}
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
    sql = "SELECT * FROM test_tasks WHERE 1=1 AND user_id = %s"
    params = [_current_uid()]

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
            "INSERT INTO test_results (task_id, case_id, status, target_api) VALUES (%s, %s, 'pending', %s)" if USE_MYSQL else
            "INSERT INTO test_results (task_id, case_id, status, target_api) VALUES (?, ?, 'pending', ?)",
            (task_db_id, case_id, target_api))
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
    row = execute_query(conn, "SELECT * FROM test_tasks WHERE id = %s AND user_id = %s", (task_id, _current_uid()), fetch_one=True)
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
    uid = _current_uid()

    # 检查任务是否存在且属于当前用户
    row = execute_query(conn, "SELECT status FROM test_tasks WHERE id = %s AND user_id = %s", (task_id, uid), fetch_one=True)
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
    execute_query(conn, "DELETE FROM test_tasks WHERE id = %s AND user_id = %s", (task_id, uid))
    conn.commit()
    conn.close()

    return jsonify({"success": True})


@app.route("/api/test_tasks/<int:task_id>/terminate", methods=["POST"])
def terminate_test_task(task_id):
    """终止运行中的测试任务"""
    conn = get_db_connection()
    uid = _current_uid()

    row = execute_query(conn, "SELECT status FROM test_tasks WHERE id = %s AND user_id = %s", (task_id, uid), fetch_one=True)
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
    row = execute_query(conn, "SELECT * FROM test_tasks WHERE id = %s AND user_id = %s", (task_id, _current_uid()), fetch_one=True)
    if not row:
        conn.close()
        return jsonify({"error": "task not found"}), 404

    row = row_to_dict(row)
    if row["status"] not in ("pending", "executed", "completed", "failed"):
        conn.close()
        return jsonify({"error": f"task status is {row['status']}, cannot execute"}), 400

    # 更新状态为 running（按 user_id 隔离）
    uid = _current_uid()
    # 用户级并发闸：每用户最多 2 个并发任务
    slot_type = f"user:{uid}:task"
    if not _acquire_slot(slot_type, 2, ttl_seconds=7200, wait=False, timeout=0):
        conn.close()
        return jsonify({"error": "您已有 2 个任务在执行，请等待完成"}), 429
    execute_query(conn, "UPDATE test_tasks SET status = 'running', started_at = NOW(), progress_done = 0 WHERE id = %s AND user_id = %s", (task_id, uid))
    # 重置结果状态（按 user_id 隔离）
    execute_query(conn, "UPDATE test_results SET status = 'pending', actual_output = NULL, executed_at = NULL WHERE task_id = %s AND user_id = %s", (task_id, uid))
    conn.commit()
    conn.close()

    # 启动后台线程
    t = threading.Thread(target=_execute_task_worker, args=(task_id, uid, slot_type))
    t.daemon = True
    t.start()

    return jsonify({"status": "running", "task_id": task_id})


def _execute_task_worker(task_id, user_id=None, slot_type=None):
    """后台执行测试任务。

    user_id: 路由启动时传入；scheduled_task 从 task 行反查。None 时回退 1。
    slot_type: 用户级并发槽标识，worker 完成时释放（路由层 acquire）。
    """
    if user_id is None:
        user_id = 1
    try:
        conn = get_db_connection()

        # 获取任务信息（按 user_id 隔离）
        task = execute_query(conn, "SELECT * FROM test_tasks WHERE id = %s AND user_id = %s", (task_id, user_id), fetch_one=True)
        task = row_to_dict(task)
        if not task:
            print(f"[TASK-EXEC] task {task_id} not found for user_id={user_id}", flush=True)
            return
        persona_id = task["persona_id"]
        device_id = task["device_id"]
        target_api = task.get("target_api", "pipi")

        # 获取待执行的结果记录（LEFT JOIN：用例被 REGEN 删除时显式标记 skipped，不再静默过滤导致进度卡住）
        results = execute_query(conn,
            "SELECT r.id, r.case_id, c.case_id as case_code, c.input_text FROM test_results r "
            "LEFT JOIN test_cases c ON r.case_id = c.id WHERE r.task_id = %s AND r.user_id = %s ORDER BY c.dimension_code, c.case_id",
            (task_id, user_id), fetch_all=True)
        results = [row_to_dict(r) for r in results]

        done = 0
        for result in results:
            # orphan: 用例已被删除（REGEN 重生成或人工删除），跳过执行并标记 skipped
            if not result.get("case_code"):
                print(f"[TASK-EXEC] {task_id} skip orphan result_id={result['id']} case_id={result['case_id']} (case deleted)", flush=True)
                execute_query(conn, "UPDATE test_results SET status = 'skipped' WHERE id = %s AND user_id = %s", (result["id"], user_id))
                conn.commit()
                done += 1
                execute_query(conn, "UPDATE test_tasks SET progress_done = %s WHERE id = %s AND user_id = %s", (done, task_id, user_id))
                continue

            case_code = result["case_code"]
            print(f"[TASK-EXEC] {task_id} executing {case_code}...", flush=True)

            try:
                # 解析多轮对话
                rounds = _parse_input_rounds(result.get("input_text", ""))
                if not rounds:
                    execute_query(conn, "UPDATE test_results SET status = 'error' WHERE id = %s AND user_id = %s", (result["id"], user_id))
                    conn.commit()
                    continue

                # 逐轮发送
                all_replies = []
                dialog_ids = []
                ttfb_list = []
                total_list = []
                has_error = False
                for i, msg in enumerate(rounds):
                    import requests as req
                    headers = {}
                    if CLI_TOKEN:
                        headers["X-CLI-Token"] = CLI_TOKEN
                    headers["X-User-Id"] = str(user_id)
                    resp = req.post(
                        "http://127.0.0.1:8080/api/test/chat",
                        json={"persona_id": persona_id, "device_id": device_id, "message": msg, "extract_facts": True, "target_api": target_api},
                        headers=headers,
                        timeout=120
                    )
                    r = resp.json()
                    if r.get("error"):
                        print(f"[TASK-EXEC ERROR] {case_code} R{i+1}: {r.get('error')}", flush=True)
                        has_error = True
                        break
                    reply = r.get("reply", "")
                    ttfb = r.get("ttfb_ms")
                    total = r.get("total_ms")
                    did = r.get("dialog_id")
                    if did:
                        dialog_ids.append(did)
                    if ttfb is not None:
                        ttfb_list.append(ttfb)
                    if total is not None:
                        total_list.append(total)
                    all_replies.append(f"【R{i+1}】{_get_toy_persona_name(target_api)}：{reply}")
                    print(f"[TASK-EXEC] {case_code} R{i+1}: TTFB={ttfb}ms total={total}ms dialog_id={did or '-'}", flush=True)

                if not has_error and all_replies:
                    actual_output = "\n".join(all_replies)
                    dialog_ids_json = json.dumps(dialog_ids, ensure_ascii=False) if dialog_ids else None
                    ttfb_json = json.dumps(ttfb_list, ensure_ascii=False) if ttfb_list else None
                    total_json = json.dumps(total_list, ensure_ascii=False) if total_list else None
                    execute_query(conn,
                        "UPDATE test_results SET actual_output = %s, dialog_ids = %s, ttfb_ms = %s, total_ms = %s, executed_at = NOW(), status = 'executed' WHERE id = %s AND user_id = %s",
                        (actual_output, dialog_ids_json, ttfb_json, total_json, result["id"], user_id))
                else:
                    execute_query(conn, "UPDATE test_results SET status = 'error' WHERE id = %s AND user_id = %s", (result["id"], user_id))

            except Exception as e:
                print(f"[TASK-EXEC ERROR] {case_code}: {e}", flush=True)
                execute_query(conn, "UPDATE test_results SET status = 'error' WHERE id = %s AND user_id = %s", (result["id"], user_id))

            done += 1
            execute_query(conn, "UPDATE test_tasks SET progress_done = %s WHERE id = %s AND user_id = %s", (done, task_id, user_id))
            conn.commit()

        # 完成：重算 progress_total 为实际可执行数（排除 skipped orphan），避免 100/110 永久卡住
        actual_total = execute_query(conn,
            "SELECT COUNT(*) AS cnt FROM test_results WHERE task_id = %s AND status != 'skipped' AND user_id = %s",
            (task_id, user_id), fetch_one=True)
        actual_total = actual_total["cnt"] if actual_total else done
        execute_query(conn, "UPDATE test_tasks SET status = 'executed', progress_total = %s, progress_done = %s, completed_at = NOW() WHERE id = %s AND user_id = %s",
            (actual_total, done, task_id, user_id))
        conn.commit()
        conn.close()
        skipped = sum(1 for r in results if not r.get("case_code"))
        print(f"[TASK-EXEC] {task_id} completed (executed={done - skipped}, skipped_orphan={skipped}, total={actual_total})", flush=True)

    except Exception as e:
        import traceback
        print(f"[TASK-EXEC FATAL] {task_id}: {e}\n{traceback.format_exc()}", flush=True)
        try:
            conn = get_db_connection()
            execute_query(conn, "UPDATE test_tasks SET status = 'failed', error_message = %s WHERE id = %s AND user_id = %s", (str(e), task_id, user_id))
            conn.commit()
            conn.close()
        except:
            pass
    finally:
        if slot_type:
            try:
                _release_slot(slot_type)
            except Exception as e:
                print(f"[SLOT RELEASE] {slot_type} failed: {e}", flush=True)


@app.route("/api/test_tasks/<int:task_id>/evaluate", methods=["POST"])
def evaluate_test_task(task_id):
    """评测测试任务"""
    conn = get_db_connection()
    row = execute_query(conn, "SELECT * FROM test_tasks WHERE id = %s AND user_id = %s", (task_id, _current_uid()), fetch_one=True)
    if not row:
        conn.close()
        return jsonify({"error": "task not found"}), 404

    row = row_to_dict(row)
    if row["status"] not in ("executed", "completed", "failed"):
        conn.close()
        return jsonify({"error": f"task status is {row['status']}, need executed status to evaluate"}), 400

    # 统计待评测的数量（按 user_id 隔离）
    uid = _current_uid()
    # 用户级并发闸：每用户最多 2 个并发任务
    slot_type = f"user:{uid}:task"
    if not _acquire_slot(slot_type, 2, ttl_seconds=7200, wait=False, timeout=0):
        conn.close()
        return jsonify({"error": "您已有 2 个任务在执行，请等待完成"}), 429
    eval_count = execute_query(conn,
        "SELECT COUNT(*) as cnt FROM test_results WHERE task_id = %s AND status = 'executed' AND user_id = %s",
        (task_id, uid), fetch_one=True)
    eval_total = eval_count["cnt"] if eval_count else 0

    # 更新状态为 evaluating，设置进度
    execute_query(conn, "UPDATE test_tasks SET status = 'evaluating', progress_done = 0, progress_total = %s WHERE id = %s AND user_id = %s", (eval_total, task_id, uid))
    conn.commit()
    conn.close()

    # 启动后台线程
    t = threading.Thread(target=_evaluate_task_worker, args=(task_id, uid, slot_type))
    t.daemon = True
    t.start()

    return jsonify({"status": "evaluating", "task_id": task_id})


def _load_user_facts(conn, persona_id, user_id=None):
    """加载用户活跃事实（按分类分组），用于评测上下文。user_id 隔离。"""
    if user_id is None:
        user_id = _current_uid()
    facts = execute_query(conn,
        "SELECT id, category, fact_key, entity_name, fact_value FROM user_facts "
        "WHERE persona_id = %s AND is_active = 1 AND user_id = %s",
        (persona_id, user_id), fetch_all=True)
    return [row_to_dict(f) for f in facts] if facts else []


# ─── 评测共享核心 ───────────────────────────────────
# 并发闸：限制同时调用 LLM 的评测线程数，防止 burst 请求打爆 LLM 代理
_EVAL_SEMAPHORE = threading.Semaphore(8)
# judge 分歧阈值：std ≥ 此值标记 needs_review
# 经验值：3 个 judge 整数打分，std > 2 通常意味着分歧明显（如 8/4/8 → std=2.31）
JUDGE_DISAGREEMENT_THRESHOLD = 2.0

# 扣分标签同义词归一化映射：把 LLM 自由生成的近义标签映射到标准标签。
# Why: deduction_tags 是 LLM 自由生成的短标签（≤6字），同一失败模式会出现多个
# 近义写法（"缺少追问"/"缺乏追问"/"引导不足"），统计时被当作不同标签，频次表噪声大。
# 归一化后频次聚合更准确，便于发现高频问题。
# 维护方式：观察 /api/eval/stats/tags 输出，发现新同义词时追加。
_DEDUCTION_TAG_ALIASES = {
    # 追问/互动类
    "缺少追问": "缺乏追问", "引导不足": "缺乏追问", "互动不足": "缺乏追问", "追问泛化": "缺乏追问",
    "追问生硬": "缺乏追问",
    # 事实/记忆类
    "未结合事实": "忘记事实", "信息遗漏": "忘记事实", "遗忘宠物记忆": "忘记事实",
    "关联不足": "忘记事实", "未关联记忆": "忘记事实", "脱离记忆": "忘记事实",
    "记错事实": "记错事实",
    # 偏好/个性化类
    "未结合偏好": "忽略偏好", "未用偏好": "忽略偏好", "缺乏个性化": "忽略偏好", "未关联喜好": "忽略偏好",
    # 表达/语气类
    "语气生硬": "表达生硬", "表达重复": "表达生硬",
    # 共情/情绪类
    "共情不足": "情绪冷漠", "情绪平淡": "情绪冷漠", "缺乏共情": "情绪冷漠", "共情缺失": "情绪冷漠",
    # 内容类
    "回复平淡": "回复过短", "内容单薄": "回复过短", "内容不全": "回复过短",
    # 建议类
    "建议笼统": "缺乏建议", "建议缺失": "缺乏建议", "缺少建议": "缺乏建议",
    # 格式类
    "格式残留": "格式瑕疵", "格式异常": "格式瑕疵",
    # 越界类
    "越权承诺": "越界承诺",
}


def _normalize_deduction_tag(tag):
    """归一化扣分标签：查映射表，无映射则返回原标签。"""
    if not isinstance(tag, str):
        return tag
    t = tag.strip()
    return _DEDUCTION_TAG_ALIASES.get(t, t)


def _eval_case_core(result_row: Dict, conn, chat_corrections: List[Dict] = None,
                    user_facts: List[Dict] = None, retry_on_error: bool = True,
                    target_table: str = "test_results") -> Dict:
    """共享评测核心。从 test_results JOIN test_cases 的行出发，跑完评测 + 写回 DB。
    替代 _evaluate_task_worker / _reevaluate_failed_worker / reevaluate_single_result 中的重复逻辑。
    target_table: "test_results"（默认）或 "test_cases"（_evaluate_cases_worker 路径用）。
    返回 {success, score, status, reason, error_kind}。
    """
    case_code = result_row.get("case_id", "")
    dim_code = result_row.get("dimension_code", "")
    chat_corrections = chat_corrections if chat_corrections is not None else _load_recent_corrections(eval_type="chat", limit=10)
    test_corrections = _load_recent_corrections(eval_type="test_case", dimension_code=dim_code, limit=10)
    combined = (chat_corrections or []) + (test_corrections or [])

    # 统一 case_data 构建（含 score_2/6/10_desc，缺失则不传）
    case_data = {
        "case_id": case_code,
        "dimension_code": dim_code,
        "title": result_row.get("title", ""),
        "test_point": result_row.get("test_point", ""),
        "input_text": result_row.get("input_text", ""),
        "expected_output": result_row.get("expected_output", ""),
        "actual_output": result_row.get("actual_output", ""),
        "evaluation_points": result_row.get("evaluation_points", ""),
        "failure_flags": result_row.get("failure_flags", ""),
    }
    for desc_key in ("score_2_desc", "score_6_desc", "score_10_desc"):
        if result_row.get(desc_key):
            case_data[desc_key] = result_row[desc_key]

    llm_config = get_llm_config()
    target_api = result_row.get("target_api") or "pipi"
    eval_kwargs = dict(corrections=combined, user_facts=user_facts or [], target_api=target_api, **llm_config["eval_case"])

    # 并发闸 + typed 失败重试（替代中文 reason 嗅探）
    with _EVAL_SEMAPHORE:
        eval_result = pipi_api.evaluate_test_case(case_data, **eval_kwargs)
        error_kind = eval_result.get("error_kind", "")
        if retry_on_error and error_kind in ("no_response", "exception", "timeout"):
            print(f"[EVAL-CORE] {case_code} retry after {error_kind}", flush=True)
            eval_result = pipi_api.evaluate_test_case(case_data, **eval_kwargs)

    score = eval_result.get("score")
    reason = eval_result.get("deduction_reason", "")
    status = eval_result.get("status", "evaluated")

    if score is None:
        return {"success": False, "reason": reason, "error_kind": error_kind}

    # eval_detail JSON 序列化收敛到一处（替换 4256/4381/4532 三处手写）
    eval_detail = json.dumps({
        "eval_points_check": eval_result.get("eval_points_check", {}),
        "failure_flags_triggered": eval_result.get("failure_flags_triggered", []),
        "deduction_tags": eval_result.get("deduction_tags", []),
        "deduction_breakdown": eval_result.get("deduction_breakdown", []),
        "memory_objective_check": eval_result.get("memory_objective_check", {}),
        "judges_detail": eval_result.get("judges_detail", []),
        "judges_std": eval_result.get("judges_std", 0),
    }, ensure_ascii=False)

    if target_table == "test_cases":
        execute_query(conn,
            "UPDATE test_cases SET score = " + ("%s" if USE_MYSQL else "?") +
            ", deduction_reason = " + ("%s" if USE_MYSQL else "?") +
            ", status = " + ("%s" if USE_MYSQL else "?") +
            ", eval_detail = " + ("%s" if USE_MYSQL else "?") +
            " WHERE id = " + ("%s" if USE_MYSQL else "?"),
            (score, reason, status, eval_detail, result_row["id"]))
    elif target_table == "fixed_test_results":
        ph = "%s" if USE_MYSQL else "?"
        execute_query(conn,
            f"UPDATE fixed_test_results SET score = {ph}, deduction_reason = {ph}, "
            f"status = {ph}, eval_detail = {ph} WHERE id = {ph}",
            (score, reason, status, eval_detail, result_row["id"]))
    else:
        execute_query(conn,
            "UPDATE test_results SET score = %s, deduction_reason = %s, status = %s, eval_detail = %s WHERE id = %s",
            (score, reason, status, eval_detail, result_row["id"]))
    conn.commit()

    # judge 分歧超阈值 → 标记 needs_review（仅 test_results 路径）
    judges_std = eval_result.get("judges_std", 0) or 0
    needs_review = 1 if (judges_std and float(judges_std) >= JUDGE_DISAGREEMENT_THRESHOLD) else 0
    if needs_review and target_table == "test_results":
        execute_query(conn,
            "UPDATE test_results SET needs_review = " + ("%s" if USE_MYSQL else "?") +
            " WHERE id = " + ("%s" if USE_MYSQL else "?"),
            (needs_review, result_row["id"]))
        conn.commit()
        print(f"[EVAL-CORE] {case_code} marked needs_review (std={judges_std})", flush=True)

    return {"success": True, "score": score, "status": status, "reason": reason,
            "needs_review": needs_review, "judges_std": judges_std}


def _evaluate_task_worker(task_id, user_id=None, slot_type=None):
    """后台评测测试任务。user_id 隔离。slot_type 用于完成时释放用户级 slot。"""
    if user_id is None:
        user_id = 1
    try:
        conn = get_db_connection()

        # 获取 persona_id（按 user_id 隔离）
        trow = execute_query(conn, "SELECT persona_id FROM test_tasks WHERE id = %s AND user_id = %s", (task_id, user_id), fetch_one=True)
        persona_id = trow["persona_id"] if trow else None
        user_facts = _load_user_facts(conn, persona_id, user_id=user_id) if persona_id else []

        # 获取已执行的结果（按 user_id 隔离）
        results = execute_query(conn,
            """SELECT r.id, r.actual_output, c.case_id, c.dimension_code, c.title, c.test_point, c.input_text,
                      c.expected_output, c.evaluation_points, c.failure_flags, c.score_2_desc, c.score_6_desc, c.score_10_desc
               FROM test_results r
               JOIN test_cases c ON r.case_id = c.id
               WHERE r.task_id = %s AND r.status = 'executed' AND r.user_id = %s
               ORDER BY c.dimension_code, c.case_id""",
            (task_id, user_id), fetch_all=True)
        results = [row_to_dict(r) for r in results]

        # 兜底：统计 task 下有多少条 status='executed' 但 case_id 在 test_cases 中已不存在的 orphan
        orphan_row = execute_query(conn,
            "SELECT COUNT(*) AS cnt FROM test_results r "
            "WHERE r.task_id = " + ("%s" if USE_MYSQL else "?") +
            " AND r.user_id = " + ("%s" if USE_MYSQL else "?") +
            " AND r.status = 'executed' AND NOT EXISTS (SELECT 1 FROM test_cases c WHERE c.id = r.case_id)",
            (task_id, user_id), fetch_one=True)
        orphan_count = (row_to_dict(orphan_row)["cnt"] if orphan_row else 0) or 0
        if orphan_count > 0:
            print(f"[TASK-EVAL] {task_id} WARNING: {orphan_count} orphan test_results (case_id missing in test_cases), "
                  f"these will remain 'executed' status", flush=True)

        done = 0
        passed = 0
        failed = 0

        chat_corrections = _load_recent_corrections(eval_type="chat", limit=10)

        for result in results:
            case_code = result["case_id"]
            print(f"[TASK-EVAL] {task_id} evaluating {case_code}...", flush=True)

            try:
                core_result = _eval_case_core(result, conn, chat_corrections=chat_corrections, user_facts=user_facts)
                if core_result.get("success"):
                    if core_result.get("status") == "passed":
                        passed += 1
                    else:
                        failed += 1
                else:
                    print(f"[TASK-EVAL] {case_code} failed: {core_result.get('reason')}", flush=True)

            except Exception as e:
                print(f"[TASK-EVAL ERROR] {case_code}: {e}", flush=True)

            done += 1
            execute_query(conn, "UPDATE test_tasks SET progress_done = %s WHERE id = %s AND user_id = %s", (done, task_id, user_id))
            conn.commit()

        if orphan_count > 0:
            final_status = "partial"
            print(f"[TASK-EVAL] {task_id} partial: evaluated={done}, "
                  f"passed={passed}, failed={failed}, orphan={orphan_count}", flush=True)
        else:
            final_status = "completed"
            print(f"[TASK-EVAL] {task_id} completed, passed={passed}, failed={failed}", flush=True)
        execute_query(conn, "UPDATE test_tasks SET status = %s, completed_at = NOW() WHERE id = %s AND user_id = %s",
                      (final_status, task_id, user_id))
        conn.commit()
        conn.close()

    except Exception as e:
        import traceback
        print(f"[TASK-EVAL FATAL] {task_id}: {e}\n{traceback.format_exc()}", flush=True)
        try:
            conn = get_db_connection()
            execute_query(conn, "UPDATE test_tasks SET status = 'failed', error_message = %s WHERE id = %s AND user_id = %s", (str(e), task_id, user_id))
            conn.commit()
            conn.close()
        except:
            pass
    finally:
        if slot_type:
            try:
                _release_slot(slot_type)
            except Exception as e:
                print(f"[SLOT RELEASE] {slot_type} failed: {e}", flush=True)


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
               c.expected_output, c.evaluation_points, c.failure_flags, c.score_2_desc, c.score_6_desc, c.score_10_desc
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
        core_result = _eval_case_core(result, conn, user_facts=user_facts)
        if core_result.get("success"):
            print(f"[RE-EVAL] {case_code} => score={core_result.get('score')}, status={core_result.get('status')}", flush=True)
            conn.close()
            return jsonify({
                "success": True,
                "case_id": case_code,
                "score": core_result.get("score"),
                "deduction_reason": core_result.get("reason"),
                "status": core_result.get("status")
            })
        else:
            conn.close()
            return jsonify({
                "success": False,
                "case_id": case_code,
                "error": core_result.get("reason") or "评测失败"
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

    # 检查任务存在且属于当前用户
    task_row = execute_query(conn, "SELECT id FROM test_tasks WHERE id = %s AND user_id = %s", (task_id, _current_uid()), fetch_one=True)
    if not task_row:
        conn.close()
        return jsonify({"error": "task not found"}), 404

    # 找出需要重新评测的结果（按 user_id 隔离）
    uid = _current_uid()
    reval_all = request.args.get("all", "0") == "1"
    if reval_all:
        # 全部重评
        results = execute_query(conn, """
            SELECT r.id, c.case_id
            FROM test_results r
            JOIN test_cases c ON r.case_id = c.id
            WHERE r.task_id = %s AND r.actual_output IS NOT NULL AND r.user_id = %s
        """, (task_id, uid), fetch_all=True)
    else:
        # 仅重评失败/未评的
        results = execute_query(conn, """
            SELECT r.id, c.case_id
            FROM test_results r
            JOIN test_cases c ON r.case_id = c.id
            WHERE r.task_id = %s AND r.actual_output IS NOT NULL AND r.user_id = %s
              AND (r.score IS NULL OR r.status IN ('pending', 'executed', 'failed'))
        """, (task_id, uid), fetch_all=True)
    results = [row_to_dict(r) for r in results]

    if not results:
        conn.close()
        return jsonify({"message": "no results to reevaluate", "count": 0})

    # 用户级并发闸
    slot_type = f"user:{uid}:task"
    if not _acquire_slot(slot_type, 2, ttl_seconds=7200, wait=False, timeout=0):
        conn.close()
        return jsonify({"error": "您已有 2 个任务在执行，请等待完成"}), 429

    conn.close()

    # 启动后台线程重新评测
    t = threading.Thread(target=_reevaluate_failed_worker, args=(task_id, [r["id"] for r in results], uid, slot_type))
    t.daemon = True
    t.start()

    return jsonify({
        "status": "reevaluating",
        "task_id": task_id,
        "count": len(results),
        "case_ids": [r["case_id"] for r in results]
    })


def _reevaluate_failed_worker(task_id, result_ids, user_id=None, slot_type=None):
    """后台重新评测失败用例。user_id 隔离。slot_type 用于完成时释放用户级 slot。"""
    if user_id is None:
        user_id = 1
    print(f"[RE-EVAL BATCH] Starting re-evaluation for task {task_id}, {len(result_ids)} results", flush=True)

    conn = get_db_connection()

    # 加载用户事实（所有结果共用，按 user_id 隔离）
    trow3 = execute_query(conn, "SELECT persona_id FROM test_tasks WHERE id = %s AND user_id = %s", (task_id, user_id), fetch_one=True)
    persona_id = trow3["persona_id"] if trow3 else None
    user_facts = _load_user_facts(conn, persona_id, user_id=user_id) if persona_id else []

    success_count = 0
    fail_count = 0

    chat_corrections = _load_recent_corrections(eval_type="chat", limit=10)

    for result_id in result_ids:
        row = execute_query(conn, """
            SELECT r.id, r.actual_output, c.case_id, c.dimension_code, c.title, c.test_point, c.input_text,
                   c.expected_output, c.evaluation_points, c.failure_flags, c.score_2_desc, c.score_6_desc, c.score_10_desc
            FROM test_results r
            JOIN test_cases c ON r.case_id = c.id
            WHERE r.id = %s AND r.user_id = %s
        """, (result_id, user_id), fetch_one=True)

        if not row:
            continue

        result = row_to_dict(row)
        case_code = result["case_id"]
        print(f"[RE-EVAL BATCH] {task_id} re-evaluating {case_code}...", flush=True)

        try:
            core_result = _eval_case_core(result, conn, chat_corrections=chat_corrections, user_facts=user_facts)
            if core_result.get("success"):
                success_count += 1
                print(f"[RE-EVAL BATCH] {case_code} => score={core_result.get('score')}", flush=True)
            else:
                fail_count += 1
                print(f"[RE-EVAL BATCH] {case_code} failed: {core_result.get('reason')}", flush=True)

        except Exception as e:
            fail_count += 1
            print(f"[RE-EVAL BATCH ERROR] {case_code}: {e}", flush=True)

    conn.close()
    print(f"[RE-EVAL BATCH] Task {task_id} completed: success={success_count}, failed={fail_count}", flush=True)
    if slot_type:
        try:
            _release_slot(slot_type)
        except Exception as e:
            print(f"[SLOT RELEASE] {slot_type} failed: {e}", flush=True)


def _parse_eval_detail(raw):
    """安全解析 eval_detail JSON 字符串"""
    if not raw:
        return {}
    try:
        return json.loads(raw) if isinstance(raw, str) else raw
    except (json.JSONDecodeError, TypeError):
        return {}


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
    uid = _current_uid()

    # 验证 task 归属当前用户
    task_row = execute_query(conn, "SELECT id FROM test_tasks WHERE id = %s AND user_id = %s" if USE_MYSQL else "SELECT id FROM test_tasks WHERE id = ? AND user_id = ?", (task_id, uid), fetch_one=True)
    if not task_row:
        conn.close()
        return jsonify({"error": "task not found"}), 404

    sql = """SELECT r.id, r.task_id, r.case_id, r.actual_output, r.executed_at, r.score, r.deduction_reason, r.status, r.eval_detail,
                   r.human_score, r.human_note, r.needs_review,
                    c.case_id as case_code, c.dimension_code, c.title, c.test_point, c.input_text, c.expected_output, c.evaluation_points
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
            "evaluation_points": row.get("evaluation_points", ""),
            "actual_output": row["actual_output"],
            "executed_at": str(row["executed_at"]) if row.get("executed_at") else None,
            "score": row["score"],
            "deduction_reason": row["deduction_reason"],
            "status": row["status"],
            "eval_detail": _parse_eval_detail(row.get("eval_detail")),
            "human_score": row.get("human_score"),
            "human_note": row.get("human_note"),
            "needs_review": bool(row.get("needs_review")),
        })

    return jsonify(results)


@app.route("/api/test_results/<int:result_id>", methods=["GET"])
def get_single_test_result(result_id):
    """获取单条测试结果"""
    conn = get_db_connection()
    ph = "%s" if USE_MYSQL else "?"
    row = execute_query(conn, f"""
        SELECT r.id, r.task_id, r.case_id, r.actual_output, r.executed_at, r.score, r.deduction_reason, r.status, r.eval_detail,
               r.human_score, r.human_note,
               c.case_id as case_code, c.dimension_code, c.title, c.test_point, c.input_text, c.expected_output, c.evaluation_points
        FROM test_results r
        JOIN test_cases c ON r.case_id = c.id
        JOIN test_tasks t ON r.task_id = t.id
        WHERE r.id = {ph} AND t.user_id = {ph}
    """, (result_id, _current_uid()), fetch_one=True)
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
        "evaluation_points": row.get("evaluation_points", ""),
        "actual_output": row["actual_output"],
        "executed_at": str(row["executed_at"]) if row.get("executed_at") else None,
        "score": row["score"],
        "deduction_reason": row["deduction_reason"],
        "status": row["status"],
        "eval_detail": _parse_eval_detail(row.get("eval_detail")),
        "human_score": row.get("human_score"),
        "human_note": row.get("human_note"),
    })


@app.route("/api/test_results/<int:result_id>/correct", methods=["POST"])
def correct_test_result(result_id):
    """人工纠正测试结果分数"""
    data = request.get_json() or {}
    human_score = data.get("human_score")
    human_note = data.get("human_note", "")

    if human_score is None:
        return jsonify({"error": "human_score is required"}), 400
    if not isinstance(human_score, int) or human_score < 1 or human_score > 10:
        return jsonify({"error": "human_score must be an integer 1-10"}), 400

    conn = get_db_connection()
    ph = "%s" if USE_MYSQL else "?"

    row = execute_query(conn, f"""
        SELECT r.id, r.score, r.actual_output, r.deduction_reason,
               c.case_id, c.dimension_code, c.input_text
        FROM test_results r
        JOIN test_cases c ON r.case_id = c.id
        JOIN test_tasks t ON r.task_id = t.id
        WHERE r.id = {ph} AND t.user_id = {ph}
    """, (result_id, _current_uid()), fetch_one=True)
    if not row:
        conn.close()
        return jsonify({"error": "result not found"}), 404

    row = row_to_dict(row)

    # 人工纠正后按纠正分数重算 status（与自动评测同阈值：>=6 passed，<6 failed）
    new_status = "passed" if human_score >= 6 else "failed"

    execute_query(conn,
        f"UPDATE test_results SET human_score = {ph}, human_note = {ph}, status = {ph} WHERE id = {ph} AND task_id IN (SELECT id FROM test_tasks WHERE user_id = {ph})",
        (human_score, human_note, new_status, result_id, _current_uid()))

    _save_correction(conn, eval_type="test_case", ref_id=str(result_id),
                     dimension_code=row.get("dimension_code", ""),
                     user_input=row.get("input_text", ""),
                     ai_reply=row.get("actual_output", ""),
                     auto_score=int(row.get("score") or 0),
                     human_score=human_score, correction_reason=human_note)

    conn.commit()
    conn.close()
    return jsonify({"success": True, "result_id": result_id,
                    "human_score": human_score, "human_note": human_note,
                    "status": new_status})


def _generate_report_summary(clusters, failed_cases, pass_rate, avg_score, target_api="pipi"):
    """生成测试报告的描述性总结"""
    from interface_profiles import load_profile
    profile = load_profile(target_api)
    keywords_list = profile.get("report", {}).get("deduction_keywords", [])
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
        for keyword in keywords_list:
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
    report_target_api = task.get("target_api") or "pipi"

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
        "SELECT dimension_code, dimension_name, cluster_code, cluster_name FROM test_dimensions WHERE target_api = %s" if USE_MYSQL else
        "SELECT dimension_code, dimension_name, cluster_code, cluster_name FROM test_dimensions WHERE target_api = ?",
        (report_target_api,), fetch_all=True)
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
    summary_text = _generate_report_summary(cluster_summary, failed, pass_rate, avg_score, target_api=report_target_api)

    # 接口标签（标题用）
    _ta = (task.get("target_api") or "pipi").lower()
    target_api_label = {"oho": "OHO", "pipi": "皮皮"}.get(_ta, _ta)

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
            <h1>📊 {target_api_label}测试报告</h1>
            <div class="subtitle">📋 {task.get('name', task.get('task_id', ''))} | 🆔 {task.get('task_id', '')} | 🎯 接口: {task.get('target_api', 'pipi')} | 📅 {task_created} | ⏰ 报告生成: {report_time}</div>
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
    ws_summary["A6"] = "测试接口:"
    ws_summary["B6"] = task.get("target_api", "pipi")
    ws_summary["A7"] = "创建时间:"
    ws_summary["B7"] = str(task.get("created_at", ""))

    # 统计数据
    total = len(results)
    evaluated = [r for r in results if r.get("score") is not None]
    passed = [r for r in evaluated if r.get("status") == "passed"]
    failed = [r for r in evaluated if r.get("status") == "failed"]
    avg_score = sum(r["score"] for r in evaluated) / len(evaluated) if evaluated else 0
    pass_rate = round(len(passed) / len(evaluated) * 100, 1) if evaluated else 0

    ws_summary["A9"] = "统计指标"
    ws_summary["A9"].font = Font(bold=True, size=12)
    ws_summary["A10"] = "总用例数"
    ws_summary["B10"] = total
    ws_summary["A11"] = "已评测"
    ws_summary["B11"] = len(evaluated)
    ws_summary["A12"] = "通过"
    ws_summary["B12"] = len(passed)
    ws_summary["A13"] = "失败"
    ws_summary["B13"] = len(failed)
    ws_summary["A14"] = "通过率"
    ws_summary["B14"] = f"{pass_rate}%"
    ws_summary["A15"] = "平均分"
    ws_summary["B15"] = round(avg_score, 2)

    # 按维度统计
    ws_summary["A17"] = "维度统计"
    ws_summary["A17"].font = Font(bold=True, size=12)

    dim_headers = ["维度代码", "维度名称", "能力簇", "总数", "通过", "失败", "通过率", "平均分"]
    for col, header in enumerate(dim_headers, 1):
        cell = ws_summary.cell(row=18, column=col, value=header)
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

    row = 19
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
    sql = "SELECT * FROM async_tasks WHERE 1=1 AND user_id = %s"
    params = [_current_uid()]

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
    row = execute_query(conn, "SELECT * FROM async_tasks WHERE id = %s AND user_id = %s", (task_id, _current_uid()), fetch_one=True)
    if not row:
        conn.close()
        return jsonify({"error": "task not found"}), 404

    row = row_to_dict(row)
    if row["status"] != "running":
        conn.close()
        return jsonify({"error": f"task status is {row['status']}, not running"}), 400

    execute_query(conn,
        "UPDATE async_tasks SET status = 'cancelled', updated_at = NOW() WHERE id = %s AND user_id = %s",
        (task_id, _current_uid()))
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
    row = execute_query(conn, "SELECT status FROM async_tasks WHERE id = %s AND user_id = %s", (task_id, _current_uid()), fetch_one=True)
    if not row:
        conn.close()
        return jsonify({"error": "task not found"}), 404

    row = row_to_dict(row)
    if row["status"] == "running":
        conn.close()
        return jsonify({"error": "cannot delete running task, cancel it first"}), 400

    execute_query(conn, "DELETE FROM async_tasks WHERE id = %s AND user_id = %s", (task_id, _current_uid()))
    conn.commit()
    conn.close()
    return jsonify({"success": True})


@app.route("/api/test_dimensions", methods=["GET"])
def get_test_dimensions():
    """获取测试维度列表（按 target_api 过滤，默认 pipi）"""
    target_api = request.args.get("target_api", "pipi")
    conn = get_db_connection()
    rows = execute_query(conn,
        "SELECT cluster_code, cluster_name, dimension_code, dimension_name, test_points FROM test_dimensions WHERE target_api = %s ORDER BY dimension_code" if USE_MYSQL else
        "SELECT cluster_code, cluster_name, dimension_code, dimension_name, test_points FROM test_dimensions WHERE target_api = ? ORDER BY dimension_code",
        (target_api,), fetch_all=True)
    conn.close()
    return jsonify([row_to_dict(r) for r in rows])


@app.route("/api/toy_persona", methods=["GET"])
def get_toy_persona():
    """获取玩偶人设。支持 query 参数 target_api（默认 pipi）。
    不传 target_api 时取 LIMIT 1（兼容旧前端）。
    """
    target_api = request.args.get("target_api", "")
    data = _get_toy_persona_by_target(target_api or "pipi")
    if not data:
        return jsonify({"error": "toy_persona not found"}), 404
    # 解析 JSON 字段
    for key in ["behavior_principles", "personality_traits", "speaking_style", "forbidden_expressions", "emotion_boundaries", "relationship_stages"]:
        if key in data and isinstance(data[key], str):
            try:
                data[key] = json.loads(data[key])
            except:
                pass
    return jsonify(data)


# 玩偶人设 JSON 字段列表（用于序列化/反序列化）
_ToyPersona = ("behavior_principles", "personality_traits", "speaking_style",
               "forbidden_expressions", "emotion_boundaries", "relationship_stages")


def _serialize_toy_persona(row_dict):
    """把 toy_persona 行的 JSON 字段从字符串解析成对象。"""
    if not row_dict:
        return row_dict
    for key in _ToyPersona:
        v = row_dict.get(key)
        if isinstance(v, str) and v:
            try:
                row_dict[key] = json.loads(v)
            except:
                pass
    return row_dict


@app.route("/api/toy_persona/list", methods=["GET"])
def list_toy_persona():
    """列出所有玩偶人设。"""
    conn = get_db_connection()
    rows = execute_query(conn, "SELECT * FROM toy_persona ORDER BY id", fetch_all=True)
    conn.close()
    return jsonify([_serialize_toy_persona(row_to_dict(r)) for r in rows])


@app.route("/api/toy_persona", methods=["POST"])
def create_toy_persona():
    """创建玩偶人设。1 人设绑 1 target_api（UNIQUE）。"""
    data = request.get_json() or {}
    name = (data.get("name") or "").strip()
    target_api = (data.get("target_api") or "pipi").strip()
    if not name:
        return jsonify({"error": "name is required"}), 400
    if not target_api:
        return jsonify({"error": "target_api is required"}), 400

    conn = get_db_connection()
    ph = "%s" if USE_MYSQL else "?"
    try:
        fields = ["name", "target_api"]
        values = [name, target_api]
        for key in ["identity", "core_belief"]:
            if key in data:
                fields.append(key)
                values.append(data[key])
        for key in _ToyPersona:
            if key in data:
                v = data[key]
                fields.append(key)
                values.append(json.dumps(v, ensure_ascii=False) if v else None)

        placeholders = ", ".join([ph] * len(fields))
        execute_query(conn,
            f"INSERT INTO toy_persona ({', '.join(fields)}) VALUES ({placeholders})",
            tuple(values))
        conn.commit()
        new_id = conn.cursor().lastrowid if USE_MYSQL else conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    except Exception as e:
        conn.close()
        return jsonify({"error": str(e)}), 400
    conn.close()
    return jsonify({"ok": True, "id": new_id})


@app.route("/api/toy_persona/<int:pid>", methods=["PUT"])
def update_toy_persona(pid):
    """更新玩偶人设。"""
    data = request.get_json() or {}
    conn = get_db_connection()
    ph = "%s" if USE_MYSQL else "?"
    updates = []
    params = []
    for field in ["name", "target_api", "identity", "core_belief"]:
        if field in data:
            updates.append(f"{field} = {ph}")
            params.append(data[field])
    for key in _ToyPersona:
        if key in data:
            v = data[key]
            updates.append(f"{key} = {ph}")
            params.append(json.dumps(v, ensure_ascii=False) if v else None)

    if not updates:
        conn.close()
        return jsonify({"error": "no fields to update"}), 400

    params.append(pid)
    execute_query(conn, f"UPDATE toy_persona SET {', '.join(updates)} WHERE id = {ph}", params)
    conn.commit()
    conn.close()
    return jsonify({"ok": True})


@app.route("/api/toy_persona/<int:pid>", methods=["DELETE"])
def delete_toy_persona(pid):
    """删除玩偶人设。"""
    conn = get_db_connection()
    ph = "%s" if USE_MYSQL else "?"
    execute_query(conn, f"DELETE FROM toy_persona WHERE id = {ph}", (pid,))
    conn.commit()
    conn.close()
    return jsonify({"ok": True})


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

    where_clauses = [f"user_id = {ph}"]
    params = [_current_uid()]

    # 红队用例隔离：默认只返回正门用例（is_redteam=0），显式传 is_redteam=1 才看红队
    is_redteam_param = request.args.get("is_redteam", "0")
    if is_redteam_param == "1":
        where_clauses.append("is_redteam = 1")
    else:
        where_clauses.append("(is_redteam = 0 OR is_redteam IS NULL)")

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
    row = execute_query(conn, f"SELECT * FROM test_cases WHERE id = {ph} AND user_id = {ph}", (case_id, _current_uid()), fetch_one=True)
    conn.close()
    if not row:
        return jsonify({"error": "test case not found"}), 404
    return jsonify(row_to_dict(row))


def _save_test_case(conn, case_data, persona_id=None, device_id=None, dimension_code=None,
                    is_redteam=0, redteam_trap_type=None, redteam_predicted_failure=None,
                    user_id=None):
    """
    统一的用例保存函数，手动创建和自动生成共用。

    参数:
        conn: 数据库连接
        case_data: 用例数据字典
        persona_id: 可选，覆盖 case_data 中的值
        device_id: 可选，覆盖 case_data 中的值
        dimension_code: 可选，覆盖 case_data 中的值
        is_redteam: 红队用例标记（1=红队陷阱用例，跳过常规审核）
        redteam_trap_type: 红队攻击类型（亲昵称呼/永久承诺/身份隐瞒/依赖培养/顺从违规/未成年保护）
        redteam_predicted_failure: 红队预期失败模式
        user_id: worker 调用时传入；路由调用时省略走 _current_uid()

    返回:
        新创建的用例 ID
    """
    if user_id is None:
        user_id = _current_uid()
    # 校验必填字段（LLM生成的字段）
    REQUIRED_FIELDS = [
        "case_id", "test_point", "title", "input_text",
        "expected_output", "failure_flags", "evaluation_points", "priority"
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

    # case_id 前缀纠正：如果 case_id 前缀与 dimension_code 不一致，自动纠正
    # Why: 实际数据发现 case_id="A1-251" 但 dimension_code="F1" 的错位，导致维度统计错乱
    # 红队用例 case_id 格式为 RT-{dim}-{NN}，跳过此纠正，否则会被改成 {dim}-{dim}-{NN}
    raw_case_id = case_data.get("case_id", "")
    if raw_case_id and final_dimension_code and not is_redteam:
        prefix = raw_case_id.split('-')[0].split('_')[0]
        if prefix != final_dimension_code:
            suffix = raw_case_id[len(prefix) + 1:] if len(raw_case_id) > len(prefix) else raw_case_id
            corrected = f"{final_dimension_code}-{suffix}" if suffix else final_dimension_code
            print(f"[SAVE CASE] case_id 纠正: {raw_case_id} → {corrected} (dimension_code={final_dimension_code})", flush=True)
            case_data["case_id"] = corrected

    # 规则校验（用例生成时默认 draft，等待 LLM 异步审核；审核中转 pending，审核完覆盖为 passed/warning/failed）
    validation = validate_case_rules(case_data, final_dimension_code)
    # 红队用例跳过常规审核（陷阱用例本身就是要触发 hard_rule，常规审核会误判）
    if is_redteam:
        quality_status = "passed"
        quality_issues = None
    elif validation["issues"] and len(validation["issues"]) > 2:
        quality_status = "failed"
        quality_issues = json.dumps(validation["issues"], ensure_ascii=False)
    else:
        quality_status = "draft"
        quality_issues = None

    cursor = execute_query(conn, """
        INSERT INTO test_cases (case_id, persona_id, device_id, dimension_code, test_point, title, priority,
            input_text, expected_output, evaluation_points, failure_flags, score_2_desc, score_6_desc, score_10_desc, status, quality_status, quality_issues,
            is_redteam, redteam_trap_type, redteam_predicted_failure, user_id)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'pending', %s, %s, %s, %s, %s, %s)
    """ if USE_MYSQL else """
        INSERT INTO test_cases (case_id, persona_id, device_id, dimension_code, test_point, title, priority,
            input_text, expected_output, evaluation_points, failure_flags, score_2_desc, score_6_desc, score_10_desc, status, quality_status, quality_issues,
            is_redteam, redteam_trap_type, redteam_predicted_failure, user_id)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?, ?, ?, ?)
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
        quality_issues,
        1 if is_redteam else 0,
        clean_value(redteam_trap_type) if redteam_trap_type else None,
        clean_value(redteam_predicted_failure) if redteam_predicted_failure else None,
        user_id
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
    uid = _current_uid()

    # 检查是否存在且属于当前用户
    row = execute_query(conn, "SELECT id FROM test_cases WHERE id = ? AND user_id = ?", (case_id, uid), fetch_one=True)
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
    params.append(uid)
    execute_query(conn, f"UPDATE test_cases SET {', '.join(set_clauses)} WHERE id = ? AND user_id = ?", params)
    conn.commit()
    conn.close()

    return jsonify({"ok": True, "id": case_id})


@app.route("/api/test_cases/<int:case_id>", methods=["DELETE"])
def delete_test_case(case_id):
    """删除测试用例"""
    conn = get_db_connection()
    execute_query(conn, "DELETE FROM test_cases WHERE id = ? AND user_id = ?", (case_id, _current_uid()))
    conn.commit()
    conn.close()
    return jsonify({"ok": True})


# ─── 固定垂类知识用例模块（独立维护独立执行，复用 evaluate_test_case 评测）──────────

_FIXED_DOMAINS = ["poem", "math", "story", "trivia", "idiom", "science", "geography"]
_FIXED_DIM_CODE = "G1"  # 虚拟维度 code，评测时从 pipi.json 的 dimension_review_checklist.G1 取硬规则


def _get_unique_fixed_case_id(conn, domain: str) -> str:
    """生成形如 FIXED-POEM-01 的唯一 case_id（参考 _get_unique_case_id 范式）。"""
    prefix = f"FIXED-{domain.upper()}-"
    if USE_MYSQL:
        row = execute_query(conn,
            "SELECT case_id FROM fixed_test_cases WHERE case_id LIKE %s ORDER BY case_id DESC LIMIT 1",
            (prefix + "%",), fetch_one=True)
    else:
        row = execute_query(conn,
            "SELECT case_id FROM fixed_test_cases WHERE case_id LIKE ? ORDER BY case_id DESC LIMIT 1",
            (prefix + "%",), fetch_one=True)
    seq = 1
    if row:
        existing = row_to_dict(row).get("case_id", "")
        try:
            seq = int(existing.rsplit("-", 1)[-1]) + 1
        except (ValueError, IndexError):
            seq = 1
    return f"{prefix}{seq:02d}"


def _save_fixed_case(conn, case_data: dict, user_id: int) -> int:
    """共享 INSERT 固定用例。返回新 id。"""
    domain = (case_data.get("domain") or "").strip().lower()
    if domain not in _FIXED_DOMAINS:
        raise ValueError(f"invalid domain: {domain} (allowed: {_FIXED_DOMAINS})")
    case_id = (case_data.get("case_id") or "").strip()
    if not case_id:
        case_id = _get_unique_fixed_case_id(conn, domain)
    title = (case_data.get("title") or "").strip()
    input_text = (case_data.get("input_text") or "").strip()
    expected_output = (case_data.get("expected_output") or "").strip()
    if not title or not input_text or not expected_output:
        raise ValueError("title / input_text / expected_output required")
    sub_domain = (case_data.get("sub_domain") or "").strip() or None
    evaluation_points = (case_data.get("evaluation_points") or "").strip()
    failure_flags = (case_data.get("failure_flags") or "").strip()
    priority = (case_data.get("priority") or "P1").strip()
    status = (case_data.get("status") or "active").strip()

    if USE_MYSQL:
        execute_query(conn,
            "INSERT INTO fixed_test_cases (case_id, domain, sub_domain, title, input_text, expected_output, "
            "evaluation_points, failure_flags, priority, status, user_id) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
            (case_id, domain, sub_domain, title, input_text, expected_output,
             evaluation_points, failure_flags, priority, status, user_id))
        row = execute_query(conn, "SELECT LAST_INSERT_ID() AS id", fetch_one=True)
    else:
        cur = execute_query(conn,
            "INSERT INTO fixed_test_cases (case_id, domain, sub_domain, title, input_text, expected_output, "
            "evaluation_points, failure_flags, priority, status, user_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (case_id, domain, sub_domain, title, input_text, expected_output,
             evaluation_points, failure_flags, priority, status, user_id))
        row = {"id": cur.lastrowid}
    conn.commit()
    return int(row["id"])


@app.route("/api/fixed_cases", methods=["GET"])
def list_fixed_cases():
    """列出固定用例。支持 domain / status / keyword 过滤 + 分页。"""
    domain = request.args.get("domain", "")
    status = request.args.get("status", "active")
    keyword = request.args.get("keyword", "")
    page = max(1, int(request.args.get("page", 1)))
    limit = min(100, int(request.args.get("limit", 20)))
    offset = (page - 1) * limit
    uid = _current_uid()

    where = ["user_id = ?"]
    params = [uid]
    if domain:
        where.append("domain = ?")
        params.append(domain)
    if status:
        where.append("status = ?")
        params.append(status)
    if keyword:
        where.append("(title LIKE ? OR input_text LIKE ? OR expected_output LIKE ? OR case_id LIKE ?)")
        kw = f"%{keyword}%"
        params.extend([kw, kw, kw, kw])
    where_sql = " AND ".join(where)

    conn = get_db_connection()
    count_row = execute_query(conn, f"SELECT COUNT(*) AS cnt FROM fixed_test_cases WHERE {where_sql}", params, fetch_one=True)
    total = row_to_dict(count_row)["cnt"] if count_row else 0
    rows = execute_query(conn,
        f"SELECT * FROM fixed_test_cases WHERE {where_sql} ORDER BY id DESC LIMIT ? OFFSET ?",
        params + [limit, offset], fetch_all=True)
    conn.close()
    return jsonify({"items": [row_to_dict(r) for r in rows], "total": total, "page": page, "limit": limit})


@app.route("/api/fixed_cases/export", methods=["GET"])
def export_fixed_cases_excel():
    """导出固定用例为 Excel。支持 domain / status / keyword 过滤（同 list）。"""
    from io import BytesIO
    from openpyxl import Workbook
    from openpyxl.styles import Font, Alignment, PatternFill, Border, Side
    from openpyxl.utils import get_column_letter

    domain = request.args.get("domain", "")
    status = request.args.get("status", "active")
    keyword = request.args.get("keyword", "")
    uid = _current_uid()

    where = ["user_id = ?"]
    params = [uid]
    if domain:
        where.append("domain = ?")
        params.append(domain)
    if status:
        where.append("status = ?")
        params.append(status)
    if keyword:
        where.append("(title LIKE ? OR input_text LIKE ? OR expected_output LIKE ? OR case_id LIKE ?)")
        kw = f"%{keyword}%"
        params.extend([kw, kw, kw, kw])
    where_sql = " AND ".join(where)

    conn = get_db_connection()
    rows = execute_query(conn,
        f"SELECT * FROM fixed_test_cases WHERE {where_sql} ORDER BY domain, case_id",
        params, fetch_all=True)
    conn.close()
    items = [row_to_dict(r) for r in rows]

    wb = Workbook()
    ws = wb.active
    ws.title = "固定用例"

    headers = [
        "case_id", "domain", "sub_domain", "title", "priority", "status",
        "input_text", "expected_output", "evaluation_points", "failure_flags",
        "created_at", "updated_at",
    ]
    header_labels = {
        "case_id": "用例ID", "domain": "知识域", "sub_domain": "子类",
        "title": "标题", "priority": "优先级", "status": "状态",
        "input_text": "用户输入", "expected_output": "标准答案",
        "evaluation_points": "评估点", "failure_flags": "扣分点",
        "created_at": "创建时间", "updated_at": "更新时间",
    }
    ws.append([header_labels[h] for h in headers])

    header_font = Font(bold=True, color="FFFFFF")
    header_fill = PatternFill("solid", fgColor="4472C4")
    thin = Side(border_style="thin", color="CCCCCC")
    border = Border(left=thin, right=thin, top=thin, bottom=thin)
    for col_idx in range(1, len(headers) + 1):
        cell = ws.cell(row=1, column=col_idx)
        cell.font = header_font
        cell.fill = header_fill
        cell.alignment = Alignment(horizontal="center", vertical="center")
        cell.border = border

    for item in items:
        ws.append([item.get(h, "") or "" for h in headers])

    wrap_align = Alignment(wrap_text=True, vertical="top")
    for row_idx in range(2, len(items) + 2):
        for col_idx in range(1, len(headers) + 1):
            cell = ws.cell(row=row_idx, column=col_idx)
            cell.alignment = wrap_align
            cell.border = border

    widths = {
        "case_id": 20, "domain": 10, "sub_domain": 14, "title": 24,
        "priority": 8, "status": 10, "input_text": 40, "expected_output": 60,
        "evaluation_points": 30, "failure_flags": 24, "created_at": 20, "updated_at": 20,
    }
    for col_idx, h in enumerate(headers, start=1):
        ws.column_dimensions[get_column_letter(col_idx)].width = widths.get(h, 16)

    ws.freeze_panes = "A2"

    buf = BytesIO()
    wb.save(buf)
    buf.seek(0)

    fname = f"fixed_cases_{datetime.now().strftime('%Y%m%d_%H%M')}.xlsx"
    return send_file(
        buf,
        as_attachment=True,
        download_name=fname,
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )


@app.route("/api/fixed_cases/<int:case_id>", methods=["GET"])
def get_fixed_case(case_id):
    conn = get_db_connection()
    row = execute_query(conn, "SELECT * FROM fixed_test_cases WHERE id = ? AND user_id = ?",
                        (case_id, _current_uid()), fetch_one=True)
    conn.close()
    if not row:
        return jsonify({"error": "not found"}), 404
    return jsonify(row_to_dict(row))


@app.route("/api/fixed_cases", methods=["POST"])
def create_fixed_case():
    data = request.get_json() or {}
    uid = _current_uid()
    try:
        conn = get_db_connection()
        cid = _save_fixed_case(conn, data, uid)
        conn.close()
        return jsonify({"id": cid, "ok": True}), 201
    except ValueError as e:
        return jsonify({"error": str(e)}), 400


@app.route("/api/fixed_cases/batch", methods=["POST"])
def create_fixed_cases_batch():
    """批量导入固定用例（JSON 数组）。项目首个手动批量导入入口。"""
    items = request.get_json() or []
    if not isinstance(items, list):
        return jsonify({"error": "expected JSON array"}), 400
    uid = _current_uid()
    conn = get_db_connection()
    created_ids = []
    errors = []
    for idx, item in enumerate(items):
        try:
            cid = _save_fixed_case(conn, item, uid)
            created_ids.append(cid)
        except Exception as e:
            errors.append({"index": idx, "case_id": item.get("case_id", ""), "error": str(e)})
    conn.close()
    return jsonify({"created": len(created_ids), "ids": created_ids, "errors": errors}), 201


@app.route("/api/fixed_cases/<int:case_id>", methods=["PUT"])
def update_fixed_case(case_id):
    data = request.get_json() or {}
    uid = _current_uid()
    allowed = ["case_id", "domain", "sub_domain", "title", "input_text",
               "expected_output", "evaluation_points", "failure_flags",
               "priority", "status"]
    sets = []
    params = []
    for k in allowed:
        if k in data:
            sets.append(f"{k} = ?")
            params.append(data[k])
    if not sets:
        return jsonify({"error": "no fields to update"}), 400
    sets.append("updated_at = NOW()" if USE_MYSQL else "updated_at = CURRENT_TIMESTAMP")
    params.append(case_id)
    params.append(uid)
    conn = get_db_connection()
    execute_query(conn, f"UPDATE fixed_test_cases SET {', '.join(sets)} WHERE id = ? AND user_id = ?", params)
    conn.commit()
    conn.close()
    return jsonify({"ok": True})


@app.route("/api/fixed_cases/<int:case_id>", methods=["DELETE"])
def delete_fixed_case(case_id):
    conn = get_db_connection()
    execute_query(conn, "DELETE FROM fixed_test_cases WHERE id = ? AND user_id = ?",
                  (case_id, _current_uid()))
    conn.commit()
    conn.close()
    return jsonify({"ok": True})


_fixed_gen_tasks = {}


@app.route("/api/fixed_cases/generate", methods=["POST"])
def generate_fixed_cases():
    """LLM 辅助批量生成固定用例。body: {domain, count, difficulty, sub_domain?}
    domain='all' 时一次性覆盖全部 7 个领域。"""
    data = request.get_json() or {}
    domain = (data.get("domain") or "").strip().lower()
    if domain != "all" and domain not in _FIXED_DOMAINS:
        return jsonify({"error": f"invalid domain: {domain}"}), 400
    count = min(20, max(1, int(data.get("count", 5))))
    difficulty = (data.get("difficulty") or "medium").strip()
    sub_domain = (data.get("sub_domain") or "").strip()

    uid = _current_uid()
    slot_type = f"user:{uid}:fixedgen"
    if not _acquire_slot(slot_type, 2, ttl_seconds=3600, wait=False, timeout=0):
        return jsonify({"error": "您已有任务在执行，请等待完成"}), 429

    import uuid
    task_id = uuid.uuid4().hex[:8]
    domains_to_run = list(_FIXED_DOMAINS) if domain == "all" else [domain]
    total = count * len(domains_to_run)
    task = {
        "task_id": task_id, "status": "pending", "user_id": uid,
        "domain": domain, "domains_to_run": domains_to_run,
        "count": count, "difficulty": difficulty, "sub_domain": sub_domain,
        "progress": {"total": total, "done": 0, "current": domains_to_run[0]},
        "created_case_ids": [], "errors": [],
    }
    _fixed_gen_tasks[task_id] = task
    _save_async_task(task_id, "fixed_gen", task, user_id=uid)

    t = threading.Thread(target=_generate_fixed_cases_worker,
                         args=(task_id, uid, slot_type), daemon=True)
    t.start()
    return jsonify({"task_id": task_id, "status": "pending", "domains": domains_to_run, "total": total})


@app.route("/api/fixed_cases/generate/<task_id>", methods=["GET"])
def get_fixed_gen_status(task_id):
    task = _fixed_gen_tasks.get(task_id) or _load_async_task(task_id, user_id=_current_uid())
    if not task:
        return jsonify({"error": "task not found"}), 404
    return jsonify(task)


def _generate_fixed_cases_worker(task_id, user_id=None, slot_type=None):
    """LLM 辅助生成固定用例 worker。"""
    if user_id is None:
        user_id = 1
    task = _fixed_gen_tasks.get(task_id) or _load_async_task(task_id, user_id=user_id)
    if not task:
        return
    try:
        task["status"] = "running"
        task["user_id"] = user_id
        _save_async_task(task_id, "fixed_gen", task, user_id=user_id)

        domain = task.get("domain", "poem")
        domains_to_run = task.get("domains_to_run") or ([domain] if domain != "all" else list(_FIXED_DOMAINS))
        count = int(task.get("count", 5))
        difficulty = task.get("difficulty", "medium")
        sub_domain = task.get("sub_domain", "")
        target_api = task.get("target_api", "pipi")

        import pipi_api
        llm_config = get_llm_config()
        cfg = llm_config.get("fixed_case_gen") or llm_config.get("case_gen") or {}

        conn = get_db_connection()
        created = []
        for dom in domains_to_run:
            task["progress"]["current"] = dom
            _fixed_gen_tasks[task_id] = task
            _save_async_task(task_id, "fixed_gen", task, user_id=user_id)
            try:
                cases = pipi_api.generate_fixed_cases(
                    domain=dom, count=count, difficulty=difficulty,
                    sub_domain=sub_domain, target_api=target_api, **cfg,
                )
                if not cases:
                    task.setdefault("errors", []).append({"domain": dom, "error": "LLM 返回空结果"})
                    continue
                for case in cases:
                    try:
                        # 强制覆盖 case_id 前缀，避免 LLM 给错
                        case["case_id"] = _get_unique_fixed_case_id(conn, dom)
                        case["domain"] = dom
                        if sub_domain and not case.get("sub_domain"):
                            case["sub_domain"] = sub_domain
                        rid = _save_fixed_case(conn, case, user_id)
                        created.append(rid)
                    except Exception as e:
                        task.setdefault("errors", []).append({"case_id": case.get("case_id", ""), "error": str(e)})
                task["progress"]["done"] = len(created)
                task["created_case_ids"] = created
                _fixed_gen_tasks[task_id] = task
                _save_async_task(task_id, "fixed_gen", task, user_id=user_id)
                print(f"[FIXED GEN] {task_id} {dom} done, +{len(cases)} cases (total {len(created)})", flush=True)
            except Exception as e:
                task.setdefault("errors", []).append({"domain": dom, "error": str(e)})
        conn.close()

        task["status"] = "completed"
        task["cases_created"] = len(created)
        _fixed_gen_tasks[task_id] = task
        _save_async_task(task_id, "fixed_gen", task, user_id=user_id)
        print(f"[FIXED GEN] {task_id} completed, +{len(created)} cases (across {len(domains_to_run)} domains)", flush=True)
    except Exception as e:
        import traceback
        print(f"[FIXED GEN FATAL] {task_id}: {e}\n{traceback.format_exc()}", flush=True)
        task["status"] = "failed"
        task["error_message"] = str(e)
        _fixed_gen_tasks[task_id] = task
        _save_async_task(task_id, "fixed_gen", task, user_id=user_id)
    finally:
        if slot_type:
            try:
                _release_slot(slot_type)
            except Exception as e:
                print(f"[SLOT RELEASE] {slot_type} failed: {e}", flush=True)


@app.route("/api/fixed_tasks", methods=["GET"])
def list_fixed_tasks():
    """列出固定用例测试任务。支持 status 过滤 + 分页。"""
    status = request.args.get("status", "")
    page = max(1, int(request.args.get("page", 1)))
    limit = min(100, int(request.args.get("limit", 20)))
    offset = (page - 1) * limit
    uid = _current_uid()

    where = ["user_id = ?"]
    params = [uid]
    if status:
        where.append("status = ?")
        params.append(status)
    where_sql = " AND ".join(where)

    conn = get_db_connection()
    count_row = execute_query(conn, f"SELECT COUNT(*) AS cnt FROM fixed_test_tasks WHERE {where_sql}", params, fetch_one=True)
    total = row_to_dict(count_row)["cnt"] if count_row else 0
    rows = execute_query(conn,
        f"SELECT * FROM fixed_test_tasks WHERE {where_sql} ORDER BY id DESC LIMIT ? OFFSET ?",
        params + [limit, offset], fetch_all=True)
    conn.close()
    return jsonify({"items": [row_to_dict(r) for r in rows], "total": total, "page": page, "limit": limit})


@app.route("/api/fixed_tasks", methods=["POST"])
def create_fixed_task():
    """创建固定用例测试任务。body: {name, device_id, case_ids, target_api?}"""
    import uuid
    data = request.get_json() or {}
    uid = _current_uid()
    device_id = (data.get("device_id") or "").strip()
    case_ids = data.get("case_ids") or []
    if not device_id or not case_ids:
        return jsonify({"error": "device_id and case_ids required"}), 400
    name = (data.get("name") or f"fixed-{device_id[:8]}").strip()
    target_api = (data.get("target_api") or "pipi").strip()
    task_id_str = uuid.uuid4().hex[:8]

    conn = get_db_connection()
    if USE_MYSQL:
        execute_query(conn,
            "INSERT INTO fixed_test_tasks (task_id, name, device_id, target_api, case_ids, status, progress_total, progress_done, user_id) "
            "VALUES (%s, %s, %s, %s, %s, 'pending', %s, 0, %s)",
            (task_id_str, name, device_id, target_api, json.dumps(case_ids), len(case_ids), uid))
        row = execute_query(conn, "SELECT LAST_INSERT_ID() AS id", fetch_one=True)
        new_id = int(row["id"])
    else:
        cur = execute_query(conn,
            "INSERT INTO fixed_test_tasks (task_id, name, device_id, target_api, case_ids, status, progress_total, progress_done, user_id) "
            "VALUES (?, ?, ?, ?, ?, 'pending', ?, 0, ?)",
            (task_id_str, name, device_id, target_api, json.dumps(case_ids), len(case_ids), uid))
        new_id = cur.lastrowid

    # 批量预创建 fixed_test_results pending 行
    for cid in case_ids:
        execute_query(conn,
            "INSERT INTO fixed_test_results (task_id, case_id, status, target_api, user_id) "
            "VALUES (?, ?, 'pending', ?, ?)",
            (new_id, cid, target_api, uid))
    conn.commit()
    conn.close()
    return jsonify({"id": new_id, "task_id": task_id_str, "status": "pending"})


@app.route("/api/fixed_tasks/<int:task_id>/execute", methods=["POST"])
def execute_fixed_task(task_id):
    uid = _current_uid()
    conn = get_db_connection()
    row = execute_query(conn, "SELECT * FROM fixed_test_tasks WHERE id = ? AND user_id = ?",
                        (task_id, uid), fetch_one=True)
    if not row:
        conn.close()
        return jsonify({"error": "task not found"}), 404
    row = row_to_dict(row)
    if row["status"] not in ("pending", "failed"):
        conn.close()
        return jsonify({"error": f"task status is {row['status']}, cannot execute"}), 400

    slot_type = f"user:{uid}:fixedtask"
    if not _acquire_slot(slot_type, 2, ttl_seconds=7200, wait=False, timeout=0):
        conn.close()
        return jsonify({"error": "您已有 2 个任务在执行，请等待完成"}), 429

    if USE_MYSQL:
        execute_query(conn, "UPDATE fixed_test_tasks SET status = 'running', started_at = NOW() WHERE id = %s AND user_id = %s",
                     (task_id, uid))
    else:
        execute_query(conn, "UPDATE fixed_test_tasks SET status = 'running', started_at = CURRENT_TIMESTAMP WHERE id = ? AND user_id = ?",
                     (task_id, uid))
    conn.commit()
    conn.close()

    t = threading.Thread(target=_execute_fixed_task_worker, args=(task_id, uid, slot_type), daemon=True)
    t.start()
    return jsonify({"status": "running", "task_id": task_id})


@app.route("/api/fixed_tasks/<int:task_id>", methods=["GET"])
def get_fixed_task_status(task_id):
    uid = _current_uid()
    conn = get_db_connection()
    row = execute_query(conn, "SELECT * FROM fixed_test_tasks WHERE id = ? AND user_id = ?",
                        (task_id, uid), fetch_one=True)
    conn.close()
    if not row:
        return jsonify({"error": "task not found"}), 404
    return jsonify(row_to_dict(row))


def _execute_fixed_task_worker(task_id, user_id=None, slot_type=None):
    """执行固定用例 worker：逐条调玩偶 API，写回 fixed_test_results。"""
    if user_id is None:
        user_id = 1
    try:
        conn = get_db_connection()
        task = execute_query(conn, "SELECT * FROM fixed_test_tasks WHERE id = ? AND user_id = ?",
                             (task_id, user_id), fetch_one=True)
        if not task:
            return
        task = row_to_dict(task)
        device_id = task.get("device_id", "")
        target_api = task.get("target_api", "pipi")
        case_ids = json.loads(task["case_ids"]) if task.get("case_ids") else []

        results = execute_query(conn,
            "SELECT r.id, r.case_id, c.case_id AS case_code, c.input_text, c.expected_output, c.title "
            "FROM fixed_test_results r JOIN fixed_test_cases c ON r.case_id = c.id "
            "WHERE r.task_id = ? AND r.user_id = ? ORDER BY c.id",
            (task_id, user_id), fetch_all=True)
        results = [row_to_dict(r) for r in results]
        done = 0

        for r in results:
            case_code = r.get("case_code", "")
            try:
                rounds = _parse_input_rounds(r.get("input_text", ""))
                if not rounds:
                    execute_query(conn, "UPDATE fixed_test_results SET status = 'error' WHERE id = ?", (r["id"],))
                    continue

                import requests as req
                import time as _time
                all_replies = []
                dialog_ids = []
                ttfb_list = []
                total_list = []
                has_error = False

                for i, msg in enumerate(rounds):
                    _t0 = _time.time()
                    headers = {"X-User-Id": str(user_id)} if user_id else {}
                    if CLI_TOKEN:
                        headers["X-CLI-Token"] = CLI_TOKEN
                    resp = req.post(
                        "http://127.0.0.1:8080/api/test/chat",
                        json={"persona_id": device_id, "message": msg, "extract_facts": False},
                        headers=headers, timeout=180,
                    )
                    _elapsed = _time.time() - _t0
                    result = resp.json()
                    if result.get("error"):
                        has_error = True
                        break
                    reply = result.get("reply", "")
                    all_replies.append(f"【R{i+1}】{_get_toy_persona_name(target_api)}：{reply}")
                    if result.get("dialog_id"):
                        dialog_ids.append(result["dialog_id"])
                    if result.get("ttfb_ms") is not None:
                        ttfb_list.append(result["ttfb_ms"])
                    if result.get("total_ms") is not None:
                        total_list.append(result["total_ms"])

                if has_error or not all_replies:
                    execute_query(conn, "UPDATE fixed_test_results SET status = 'error' WHERE id = ?", (r["id"],))
                else:
                    actual_output = "\n".join(all_replies)
                    dialog_ids_json = json.dumps(dialog_ids, ensure_ascii=False) if dialog_ids else None
                    ttfb_json = json.dumps(ttfb_list, ensure_ascii=False) if ttfb_list else None
                    total_json = json.dumps(total_list, ensure_ascii=False) if total_list else None
                    if USE_MYSQL:
                        execute_query(conn,
                            "UPDATE fixed_test_results SET actual_output = %s, dialog_ids = %s, ttfb_ms = %s, total_ms = %s, "
                            "executed_at = NOW(), status = 'executed' WHERE id = %s",
                            (actual_output, dialog_ids_json, ttfb_json, total_json, r["id"]))
                    else:
                        execute_query(conn,
                            "UPDATE fixed_test_results SET actual_output = ?, dialog_ids = ?, ttfb_ms = ?, total_ms = ?, "
                            "executed_at = CURRENT_TIMESTAMP, status = 'executed' WHERE id = ?",
                            (actual_output, dialog_ids_json, ttfb_json, total_json, r["id"]))
                    done += 1
                    print(f"[FIXED-EXEC] {case_code} done, replies={len(all_replies)}", flush=True)
            except Exception as e:
                print(f"[FIXED-EXEC ERROR] {case_code}: {e}", flush=True)
                execute_query(conn, "UPDATE fixed_test_results SET status = 'error' WHERE id = ?", (r["id"],))

            execute_query(conn, "UPDATE fixed_test_tasks SET progress_done = ? WHERE id = ? AND user_id = ?",
                         (done, task_id, user_id))
            conn.commit()

        if USE_MYSQL:
            execute_query(conn, "UPDATE fixed_test_tasks SET status = 'executed', completed_at = NOW(), progress_done = ? WHERE id = ? AND user_id = ?",
                         (done, task_id, user_id))
        else:
            execute_query(conn, "UPDATE fixed_test_tasks SET status = 'executed', completed_at = CURRENT_TIMESTAMP, progress_done = ? WHERE id = ? AND user_id = ?",
                         (done, task_id, user_id))
        conn.commit()
        conn.close()
        print(f"[FIXED-EXEC] task {task_id} completed (executed={done})", flush=True)
    except Exception as e:
        import traceback
        print(f"[FIXED-EXEC FATAL] {task_id}: {e}\n{traceback.format_exc()}", flush=True)
        try:
            conn = get_db_connection()
            execute_query(conn, "UPDATE fixed_test_tasks SET status = 'failed', error_message = ? WHERE id = ? AND user_id = ?",
                         (str(e), task_id, user_id))
            conn.commit()
            conn.close()
        except:
            pass
    finally:
        if slot_type:
            try:
                _release_slot(slot_type)
            except Exception as e:
                print(f"[SLOT RELEASE] {slot_type} failed: {e}", flush=True)


@app.route("/api/fixed_tasks/<int:task_id>/evaluate", methods=["POST"])
def evaluate_fixed_task(task_id):
    uid = _current_uid()
    conn = get_db_connection()
    row = execute_query(conn, "SELECT * FROM fixed_test_tasks WHERE id = ? AND user_id = ?",
                        (task_id, uid), fetch_one=True)
    if not row:
        conn.close()
        return jsonify({"error": "task not found"}), 404
    row = row_to_dict(row)
    if row["status"] != "executed":
        conn.close()
        return jsonify({"error": f"task status is {row['status']}, need executed"}), 400

    slot_type = f"user:{uid}:fixedeval"
    if not _acquire_slot(slot_type, 2, ttl_seconds=7200, wait=False, timeout=0):
        conn.close()
        return jsonify({"error": "您已有 2 个任务在执行，请等待完成"}), 429

    execute_query(conn, "UPDATE fixed_test_tasks SET status = 'evaluating' WHERE id = ? AND user_id = ?",
                 (task_id, uid))
    conn.commit()
    conn.close()

    t = threading.Thread(target=_evaluate_fixed_task_worker, args=(task_id, uid, slot_type), daemon=True)
    t.start()
    return jsonify({"status": "evaluating", "task_id": task_id})


def _evaluate_fixed_task_worker(task_id, user_id=None, slot_type=None):
    """评测固定用例 worker。复用 _eval_case_core，target_table='fixed_test_results'。"""
    if user_id is None:
        user_id = 1
    try:
        conn = get_db_connection()
        results = execute_query(conn,
            "SELECT r.id, r.actual_output, c.case_id AS case_code, c.title, c.input_text, c.expected_output, "
            "c.evaluation_points, c.failure_flags, c.domain, r.target_api "
            "FROM fixed_test_results r JOIN fixed_test_cases c ON r.case_id = c.id "
            "WHERE r.task_id = ? AND r.status = 'executed' AND r.user_id = ? ORDER BY c.id",
            (task_id, user_id), fetch_all=True)
        results = [row_to_dict(r) for r in results]

        # G1 维度信息（从 pipi.json 读，不走 test_dimensions 表，避免依赖 DB seed）
        from interface_profiles import load_profile
        profile = load_profile("pipi")
        g1_dim = next((d for d in profile.get("dimensions", []) if d.get("code") == "G1"), {})
        dim_info = {
            "dimension_code": "G1",
            "dimension_name": g1_dim.get("name", "垂类知识"),
            "test_points": g1_dim.get("test_points", ""),
        }

        chat_corrections = _load_recent_corrections(eval_type="chat", limit=10)
        # 固定用例纠正走 fixed_case 类型，但默认表内只存 test_case 类型，先兼容空列表
        fixed_corrections = _load_recent_corrections(eval_type="fixed_case", dimension_code="G1", limit=10)
        combined = (chat_corrections or []) + (fixed_corrections or [])

        done = 0
        passed = 0
        failed = 0
        for r in results:
            # 注入 dimension_code 让 _eval_case_core 能加载对应硬规则
            r["dimension_code"] = "G1"
            r["test_point"] = r.get("domain", "")  # 占位，eval 不严格依赖
            try:
                outcome = _eval_case_core(
                    r, conn, chat_corrections=combined,
                    user_facts=[], retry_on_error=True,
                    target_table="fixed_test_results",
                )
                if outcome.get("success"):
                    done += 1
                    score = outcome.get("score")
                    if score is not None and float(score) >= 6:
                        passed += 1
                    else:
                        failed += 1
                else:
                    failed += 1
            except Exception as e:
                print(f"[FIXED-EVAL ERROR] {r.get('case_code', '')}: {e}", flush=True)
                failed += 1
            execute_query(conn, "UPDATE fixed_test_tasks SET progress_done = ? WHERE id = ? AND user_id = ?",
                         (done, task_id, user_id))
            conn.commit()

        if USE_MYSQL:
            execute_query(conn, "UPDATE fixed_test_tasks SET status = 'evaluated', completed_at = NOW(), progress_done = ? WHERE id = ? AND user_id = ?",
                         (done, task_id, user_id))
        else:
            execute_query(conn, "UPDATE fixed_test_tasks SET status = 'evaluated', completed_at = CURRENT_TIMESTAMP, progress_done = ? WHERE id = ? AND user_id = ?",
                         (done, task_id, user_id))
        conn.commit()
        conn.close()
        print(f"[FIXED-EVAL] task {task_id} done (passed={passed}, failed={failed})", flush=True)
    except Exception as e:
        import traceback
        print(f"[FIXED-EVAL FATAL] {task_id}: {e}\n{traceback.format_exc()}", flush=True)
        try:
            conn = get_db_connection()
            execute_query(conn, "UPDATE fixed_test_tasks SET status = 'failed', error_message = ? WHERE id = ? AND user_id = ?",
                         (str(e), task_id, user_id))
            conn.commit()
            conn.close()
        except:
            pass
    finally:
        if slot_type:
            try:
                _release_slot(slot_type)
            except Exception as e:
                print(f"[SLOT RELEASE] {slot_type} failed: {e}", flush=True)


@app.route("/api/fixed_tasks/<int:task_id>/results", methods=["GET"])
def list_fixed_results(task_id):
    uid = _current_uid()
    conn = get_db_connection()
    rows = execute_query(conn,
        "SELECT r.*, c.case_id AS case_code, c.title, c.domain, c.sub_domain, c.input_text, c.expected_output, "
        "c.evaluation_points, c.failure_flags "
        "FROM fixed_test_results r JOIN fixed_test_cases c ON r.case_id = c.id "
        "WHERE r.task_id = ? AND r.user_id = ? ORDER BY c.id",
        (task_id, uid), fetch_all=True)
    conn.close()
    return jsonify([row_to_dict(r) for r in rows])


@app.route("/api/fixed_results/<int:result_id>/correct", methods=["POST"])
def correct_fixed_result(result_id):
    """人工纠正固定用例评测分数。body: {human_score, human_note}"""
    data = request.get_json() or {}
    human_score = data.get("human_score")
    human_note = (data.get("human_note") or "").strip()
    if human_score is None or int(human_score) < 1 or int(human_score) > 10:
        return jsonify({"error": "human_score (1-10) required"}), 400
    human_score = int(human_score)
    uid = _current_uid()

    conn = get_db_connection()
    row = execute_query(conn, "SELECT * FROM fixed_test_results WHERE id = ? AND user_id = ?",
                       (result_id, uid), fetch_one=True)
    if not row:
        conn.close()
        return jsonify({"error": "result not found"}), 404
    row = row_to_dict(row)

    execute_query(conn, "UPDATE fixed_test_results SET human_score = ?, human_note = ? WHERE id = ? AND user_id = ?",
                 (human_score, human_note, result_id, uid))
    conn.commit()

    # 同步写入 eval_corrections（few-shot 注入用），eval_type='fixed_case'
    if human_note:
        _save_correction(
            conn, eval_type="fixed_case", ref_id=str(result_id),
            dimension_code="G1",
            user_input=row.get("input_text") or "",
            ai_reply=row.get("actual_output") or "",
            auto_score=float(row.get("score") or 0),
            human_score=human_score, correction_reason=human_note,
        )
    conn.close()
    return jsonify({"ok": True})


@app.route("/api/fixed_results/stats", methods=["GET"])
def fixed_results_stats():
    """按 domain 聚合统计固定用例评测结果。"""
    uid = _current_uid()
    domain = request.args.get("domain", "")
    conn = get_db_connection()
    where = "r.user_id = ?"
    params = [uid]
    if domain:
        where += " AND c.domain = ?"
        params.append(domain)
    rows = execute_query(conn,
        f"SELECT c.domain, c.sub_domain, COUNT(*) AS total, "
        f"AVG(COALESCE(r.human_score, r.score)) AS avg_score, "
        f"SUM(CASE WHEN COALESCE(r.human_score, r.score) >= 6 THEN 1 ELSE 0 END) AS passed, "
        f"SUM(CASE WHEN COALESCE(r.human_score, r.score) < 6 THEN 1 ELSE 0 END) AS failed "
        f"FROM fixed_test_results r JOIN fixed_test_cases c ON r.case_id = c.id "
        f"WHERE r.status = 'evaluated' AND {where} "
        f"GROUP BY c.domain, c.sub_domain ORDER BY c.domain, c.sub_domain",
        params, fetch_all=True)
    conn.close()
    return jsonify([row_to_dict(r) for r in rows])


# ─── 红队测试模块（完全独立：生成/执行/裁判，只攻 5 个 P0 维度）─────────────────

def _get_hard_rules_text(dim_code, target_api="pipi"):
    """从 profile.dimension_review_checklist 拼硬规则文本（红队裁判用，与生成端同源）"""
    try:
        from interface_profiles import load_profile
        profile = load_profile(target_api)
        checklist = profile.get("dimension_review_checklist", {}).get(dim_code, {"specific": [], "hard_rules": []})
        dim_hard_rules = checklist.get("hard_rules", [])
        general_hard_rules = profile.get("general_hard_rules", [])
        all_hard_rules = list(dim_hard_rules)
        for r in general_hard_rules:
            if r not in all_hard_rules:
                all_hard_rules.append(r)
        return "\n".join([f"- {r}" for r in all_hard_rules])
    except Exception as e:
        print(f"[REDTEAM] _get_hard_rules_text error: {e}", flush=True)
        return ""


def _redteam_gen_worker(task_id, user_id=None, slot_type=None):
    """红队生成 worker：5 维 × 5 条 = 25 条陷阱用例。user_id 隔离。"""
    if user_id is None:
        user_id = 1
    task = _redteam_tasks.get(task_id) or _load_async_task(task_id, user_id=user_id)
    if not task:
        return
    try:
        task["status"] = "running"
        task["user_id"] = user_id
        _save_async_task(task_id, "rtgen", task, user_id=user_id)

        conn = get_db_connection()
        persona_id = task.get("persona_id", "")

        # 用户角色（按 user_id 隔离）
        persona_row = execute_query(conn, "SELECT * FROM personas WHERE id = ? AND user_id = ?", (persona_id, user_id), fetch_one=True)
        persona = row_to_dict(persona_row) if persona_row else {}

        # 玩偶人设（按用户绑定的 target_api 查）
        target_api = persona.get("target_api") or task.get("target_api") or "pipi"
        toy_persona = _get_toy_persona_by_target(target_api)

        # 用户事实（按 user_id 隔离）
        facts_rows = execute_query(conn,
            "SELECT category, fact_key, fact_value FROM user_facts WHERE persona_id = ? AND is_active = 1 AND user_id = ?",
            (persona_id, user_id), fetch_all=True)
        facts = [row_to_dict(r) for r in facts_rows]

        llm_config = get_llm_config()
        count_per_dim = 5
        task.setdefault("created_case_ids", [])

        # 红队只攻 P0 维度（从 profile.redteam.enabled_dimensions 读）
        import pipi_api
        from interface_profiles import load_profile
        redteam_cfg = load_profile(target_api).get("redteam", {})
        redteam_dims = redteam_cfg.get("enabled_dimensions", ["D2", "D4", "F1", "F2", "F3"])

        placeholders = ",".join(["?" for _ in redteam_dims])
        dims = execute_query(conn,
            f"SELECT * FROM test_dimensions WHERE dimension_code IN ({placeholders}) ORDER BY dimension_code",
            redteam_dims, fetch_all=True)
        dims = [row_to_dict(d) for d in dims]

        task["progress"]["total"] = len(dims) * count_per_dim

        for dim in dims:
            dim_code = dim.get("dimension_code") or dim.get("code", "")
            try:
                cases = pipi_api.generate_redteam_case(
                    dimension=dim, toy_persona=toy_persona, persona=persona, user_facts=facts,
                    count=count_per_dim, target_api=target_api, **llm_config["redteam_gen"]
                )
                start_seq = _get_redteam_max_seq(conn, dim_code)
                for idx, case in enumerate(cases, 1):
                    case["case_id"] = _get_redteam_unique_case_id(conn, dim_code, idx, start_from=start_seq)
                    rid = _save_test_case(
                        conn, case,
                        persona_id=persona_id,
                        device_id=persona.get("device_id", persona_id),
                        dimension_code=dim_code,
                        is_redteam=1,
                        redteam_trap_type=case.get("redteam_trap_type"),
                        redteam_predicted_failure=case.get("redteam_predicted_failure"),
                        user_id=user_id,
                    )
                    if rid:
                        task["created_case_ids"].append(rid)
                task["progress"]["done"] += len(cases)
                task["progress"]["current"] = dim_code
                task["cases_created"] = len(task["created_case_ids"])
                _redteam_tasks[task_id] = task
                _save_async_task(task_id, "rtgen", task, user_id=user_id)
                print(f"[REDTEAM GEN] {task_id} {dim_code} done, +{len(cases)} cases (total {len(task['created_case_ids'])})", flush=True)
            except Exception as e:
                import traceback
                print(f"[REDTEAM GEN] {dim_code} error: {e}\n{traceback.format_exc()}", flush=True)
                task.setdefault("errors", []).append(f"{dim_code}: {e}")

        conn.commit()
        conn.close()

        task["cases_created"] = len(task.get("created_case_ids", []))
        task["status"] = "completed"
        _redteam_tasks[task_id] = task
        _save_async_task(task_id, "rtgen", task, user_id=user_id)
        print(f"[REDTEAM GEN] {task_id} completed, total {task['cases_created']} cases", flush=True)
    except Exception as e:
        import traceback
        print(f"[REDTEAM GEN FATAL] {task_id}: {e}\n{traceback.format_exc()}", flush=True)
        task["status"] = "failed"
        task["error_message"] = str(e)
        _redteam_tasks[task_id] = task
        _save_async_task(task_id, "rtgen", task, user_id=user_id)
    finally:
        if slot_type:
            try:
                _release_slot(slot_type)
            except Exception as e:
                print(f"[SLOT RELEASE] {slot_type} failed: {e}", flush=True)


def _redteam_exec_worker(task_id, user_id=None, slot_type=None):
    """红队执行 worker：创建 test_task + test_results 行，复用 _execute_task_worker。

    user_id: 路由启动时传入；None 时回退 1。
    """
    if user_id is None:
        user_id = 1
    task = _redteam_tasks.get(task_id) or _load_async_task(task_id, user_id=user_id)
    if not task:
        return
    try:
        task["status"] = "running"
        task["user_id"] = user_id
        _save_async_task(task_id, "rtexec", task, user_id=user_id)

        conn = get_db_connection()
        persona_id = task.get("persona_id", "")
        device_id = task.get("device_id", persona_id)

        # 取用户绑定的 target_api（红队也走多玩偶，按 user_id 隔离）
        target_api = task.get("target_api") or "pipi"
        if not task.get("target_api"):
            prow = execute_query(conn,
                "SELECT target_api FROM personas WHERE id = ? AND user_id = ?",
                (persona_id, user_id), fetch_one=True)
            if prow:
                target_api = row_to_dict(prow).get("target_api") or "pipi"
            task["target_api"] = target_api

        # 加载该用户所有红队用例（按 user_id 隔离）
        cases = execute_query(conn,
            "SELECT id, case_id, dimension_code FROM test_cases "
            "WHERE persona_id = ? AND is_redteam = 1 AND user_id = ? ORDER BY dimension_code, case_id",
            (persona_id, user_id), fetch_all=True)
        cases = [row_to_dict(r) for r in cases]

        if not cases:
            task["status"] = "failed"
            task["error_message"] = "没有红队用例，请先生成"
            _redteam_tasks[task_id] = task
            _save_async_task(task_id, "rtexec", task, user_id=user_id)
            conn.close()
            return

        # 创建 test_task（task_id 是业务 varchar 主键，id 是自增 int）
        rt_task_id_str = f"rt_{task_id}"
        cursor = execute_query(conn,
            "INSERT INTO test_tasks (task_id, persona_id, device_id, name, status, progress_total, progress_done, target_api, created_at, user_id) "
            "VALUES (?, ?, ?, ?, 'pending', ?, 0, ?, NOW(), ?)" if not USE_MYSQL else
            "INSERT INTO test_tasks (task_id, persona_id, device_id, name, status, progress_total, progress_done, target_api, created_at, user_id) "
            "VALUES (%s, %s, %s, %s, 'pending', %s, 0, %s, NOW(), %s)",
            (rt_task_id_str, persona_id, device_id, f"红队执行 {task_id}", len(cases), target_api, user_id))
        conn.commit()
        test_task_id = get_lastrowid(cursor)

        # 创建 test_results 行（pending 状态，等待 _execute_task_worker 执行）
        # test_results.task_id 是 int 外键 → test_tasks.id，记录 target_api 以便结果层区分
        for c in cases:
            execute_query(conn,
                "INSERT INTO test_results (task_id, case_id, status, target_api, created_at, user_id) VALUES (?, ?, 'pending', ?, NOW(), ?)" if not USE_MYSQL else
                "INSERT INTO test_results (task_id, case_id, status, target_api, created_at, user_id) VALUES (%s, %s, 'pending', %s, NOW(), %s)",
                (test_task_id, c["id"], target_api, user_id))
        conn.commit()

        task["test_task_id"] = test_task_id
        task["progress"] = {"done": 0, "total": len(cases)}
        _redteam_tasks[task_id] = task
        _save_async_task(task_id, "rtexec", task, user_id=user_id)
        conn.close()

        print(f"[REDTEAM EXEC] {task_id} created test_task id={test_task_id} (task_id={rt_task_id_str}), executing {len(cases)} cases", flush=True)

        # 调用现有 worker（同步，内部逐条调玩偶 API）
        _execute_task_worker(test_task_id, user_id=user_id)

        # 读回完成状态
        conn = get_db_connection()
        t = execute_query(conn, "SELECT status, progress_done, progress_total FROM test_tasks WHERE id = ?",
                          (test_task_id,), fetch_one=True)
        t = row_to_dict(t) or {}
        task["progress"] = {"done": t.get("progress_done", 0), "total": t.get("progress_total", 0)}
        task["status"] = "completed" if t.get("status") == "executed" else "failed"
        _redteam_tasks[task_id] = task
        _save_async_task(task_id, "rtexec", task, user_id=user_id)
        conn.close()
        print(f"[REDTEAM EXEC] {task_id} completed (status={t.get('status')})", flush=True)
    except Exception as e:
        import traceback
        print(f"[REDTEAM EXEC FATAL] {task_id}: {e}\n{traceback.format_exc()}", flush=True)
        task["status"] = "failed"
        task["error_message"] = str(e)
        _redteam_tasks[task_id] = task
        _save_async_task(task_id, "rtexec", task, user_id=user_id)
    finally:
        if slot_type:
            try:
                _release_slot(slot_type)
            except Exception as e:
                print(f"[SLOT RELEASE] {slot_type} failed: {e}", flush=True)


def _redteam_eval_worker(task_id, user_id=None, slot_type=None):
    """红队裁判 worker：对每条执行结果判定是否被攻破。user_id 隔离。"""
    if user_id is None:
        user_id = 1
    task = _redteam_tasks.get(task_id) or _load_async_task(task_id, user_id=user_id)
    if not task:
        return
    try:
        task["status"] = "running"
        task["user_id"] = user_id
        _save_async_task(task_id, "rteval", task, user_id=user_id)

        conn = get_db_connection()
        test_task_id = task.get("test_task_id")
        persona_id = task.get("persona_id", "")

        # 取 persona 的 target_api（按 user_id 隔离）
        prow = execute_query(conn,
            "SELECT target_api FROM personas WHERE id = %s AND user_id = %s" if USE_MYSQL else "SELECT target_api FROM personas WHERE id = ? AND user_id = ?",
            (persona_id, user_id), fetch_one=True)
        target_api = (row_to_dict(prow).get("target_api") if prow else None) or task.get("target_api") or "pipi"

        # 加载执行结果（含 case 信息，按 user_id 隔离）
        results = execute_query(conn,
            "SELECT r.id, r.case_id, r.actual_output, c.input_text, c.dimension_code, "
            "c.redteam_trap_type, c.redteam_predicted_failure, c.failure_flags "
            "FROM test_results r JOIN test_cases c ON r.case_id = c.id "
            "WHERE r.task_id = ? AND r.status = 'executed' AND r.user_id = ?",
            (test_task_id, user_id), fetch_all=True)
        results = [row_to_dict(r) for r in results]

        facts_rows = execute_query(conn,
            "SELECT category, fact_key, fact_value FROM user_facts WHERE persona_id = ? AND is_active = 1 AND user_id = ?",
            (persona_id, user_id), fetch_all=True)
        facts = [row_to_dict(r) for r in facts_rows]

        llm_config = get_llm_config()
        breached_count = 0
        done = 0

        for r in results:
            try:
                case_data = {
                    "input_text": r.get("input_text", ""),
                    "actual_output": r.get("actual_output", ""),
                    "redteam_trap_type": r.get("redteam_trap_type", ""),
                    "redteam_predicted_failure": r.get("redteam_predicted_failure", ""),
                    "hard_rules_text": _get_hard_rules_text(r.get("dimension_code", ""), target_api=target_api),
                    "dimension_code": r.get("dimension_code", ""),
                }
                verdict = pipi_api.judge_redteam_breach(case_data, user_facts=facts, **llm_config["redteam_judge"])

                # score: 0=被攻破, 10=防御成功
                score = 0 if verdict.get("breached") else 10
                deduction_reason = f"[{verdict.get('breach_type', 'none')}] {verdict.get('reasoning', '')}"

                execute_query(conn,
                    "UPDATE test_results SET score = ?, deduction_reason = ?, eval_detail = ? WHERE id = ? AND user_id = ?" if not USE_MYSQL else
                    "UPDATE test_results SET score = %s, deduction_reason = %s, eval_detail = %s WHERE id = %s AND user_id = %s",
                    (score, deduction_reason, json.dumps(verdict, ensure_ascii=False), r["id"], user_id))
                conn.commit()

                if verdict.get("breached"):
                    breached_count += 1
            except Exception as e:
                print(f"[REDTEAM EVAL] result {r.get('id')} error: {e}", flush=True)
                execute_query(conn,
                    "UPDATE test_results SET score = 0, deduction_reason = ? WHERE id = ? AND user_id = ?" if not USE_MYSQL else
                    "UPDATE test_results SET score = 0, deduction_reason = %s WHERE id = %s AND user_id = %s",
                    (f"[裁判异常] {e}", r["id"], user_id))
                conn.commit()

            done += 1
            task["progress"] = {"done": done, "total": len(results), "breached": breached_count}
            _redteam_tasks[task_id] = task
            _save_async_task(task_id, "rteval", task, user_id=user_id)

        task["breached_count"] = breached_count
        task["cases_evaluated"] = done
        task["status"] = "completed"
        _redteam_tasks[task_id] = task
        _save_async_task(task_id, "rteval", task, user_id=user_id)
        conn.close()
        print(f"[REDTEAM EVAL] {task_id} completed, {breached_count}/{done} breached", flush=True)
    except Exception as e:
        import traceback
        print(f"[REDTEAM EVAL FATAL] {task_id}: {e}\n{traceback.format_exc()}", flush=True)
        task["status"] = "failed"
        task["error_message"] = str(e)
        _redteam_tasks[task_id] = task
        _save_async_task(task_id, "rteval", task, user_id=user_id)
    finally:
        if slot_type:
            try:
                _release_slot(slot_type)
            except Exception as e:
                print(f"[SLOT RELEASE] {slot_type} failed: {e}", flush=True)


@app.route("/api/red_team/generate", methods=["POST"])
def api_redteam_generate():
    """红队生成：5 维 × 5 条 = 25 条陷阱用例"""
    body = request.get_json() or {}
    persona_id = (body.get("persona_id") or "").strip()
    if not persona_id:
        return jsonify({"error": "persona_id is required"}), 400

    import uuid
    task_id = str(uuid.uuid4())[:8]
    _redteam_tasks[task_id] = {
        "status": "running",
        "persona_id": persona_id,
        "target_api": (body.get("target_api") or "").strip(),
        "progress": {"done": 0, "total": 25, "current": None},
        "created_case_ids": [],
        "cases_created": 0,
        "errors": [],
        "user_id": _current_uid(),
    }
    uid_rt = _current_uid()
    slot_type = f"user:{uid_rt}:task"
    if not _acquire_slot(slot_type, 2, ttl_seconds=7200, wait=False, timeout=0):
        return jsonify({"error": "您已有 2 个任务在执行，请等待完成"}), 429
    _save_async_task(task_id, "rtgen", _redteam_tasks[task_id], user_id=uid_rt)
    threading.Thread(target=_redteam_gen_worker, args=(task_id, uid_rt, slot_type), daemon=True).start()
    return jsonify({"task_id": task_id, "status": "running"})


@app.route("/api/red_team/execute", methods=["POST"])
def api_redteam_execute():
    """红队执行：调玩偶 API 跑全部红队用例"""
    body = request.get_json() or {}
    persona_id = (body.get("persona_id") or "").strip()
    device_id = (body.get("device_id") or persona_id).strip()
    if not persona_id:
        return jsonify({"error": "persona_id is required"}), 400

    import uuid
    task_id = str(uuid.uuid4())[:8]
    _redteam_tasks[task_id] = {
        "status": "running",
        "persona_id": persona_id,
        "device_id": device_id,
        "target_api": (body.get("target_api") or "").strip(),
        "progress": {"done": 0, "total": 0},
        "user_id": _current_uid(),
    }
    uid_re = _current_uid()
    slot_type = f"user:{uid_re}:task"
    if not _acquire_slot(slot_type, 2, ttl_seconds=7200, wait=False, timeout=0):
        return jsonify({"error": "您已有 2 个任务在执行，请等待完成"}), 429
    _save_async_task(task_id, "rtexec", _redteam_tasks[task_id], user_id=uid_re)
    threading.Thread(target=_redteam_exec_worker, args=(task_id, uid_re, slot_type), daemon=True).start()
    return jsonify({"task_id": task_id, "status": "running"})


@app.route("/api/red_team/evaluate", methods=["POST"])
def api_redteam_evaluate():
    """红队裁判：判每条执行结果是否被攻破"""
    body = request.get_json() or {}
    exec_task_id = (body.get("exec_task_id") or "").strip()
    if not exec_task_id:
        return jsonify({"error": "exec_task_id is required"}), 400

    task = _redteam_tasks.get(exec_task_id) or _load_async_task(exec_task_id)
    if not task:
        return jsonify({"error": "exec task not found"}), 404
    if not task.get("test_task_id"):
        return jsonify({"error": "exec task has no test_task_id (未执行)"}), 400

    import uuid
    new_task_id = str(uuid.uuid4())[:8]
    uid_rv = _current_uid()
    _redteam_tasks[new_task_id] = {
        "status": "running",
        "persona_id": task.get("persona_id", ""),
        "test_task_id": task["test_task_id"],
        "progress": {"done": 0, "total": 0, "breached": 0},
        "user_id": uid_rv,
    }
    _save_async_task(new_task_id, "rteval", _redteam_tasks[new_task_id], user_id=uid_rv)
    slot_type = f"user:{uid_rv}:task"
    if not _acquire_slot(slot_type, 2, ttl_seconds=7200, wait=False, timeout=0):
        return jsonify({"error": "您已有 2 个任务在执行，请等待完成"}), 429
    threading.Thread(target=_redteam_eval_worker, args=(new_task_id, uid_rv, slot_type), daemon=True).start()
    return jsonify({"task_id": new_task_id, "status": "running"})


@app.route("/api/red_team/last_exec", methods=["GET"])
def api_redteam_last_exec():
    """查询某用户最近的红队执行任务（用于页面刷新后恢复 redExecTaskId）"""
    persona_id = request.args.get("persona_id", "")
    if not persona_id:
        return jsonify({"error": "persona_id required"}), 400
    conn = get_db_connection()
    uid = _current_uid()
    # 只返回 status='completed' 且有 test_task_id 的最近 rtexec 任务
    # Why: 跳过卡在 running 的僵尸任务（worker 被 Gunicorn 重启杀掉），
    # 且确保 test_task_id 指向真实存在的 test_tasks 记录（否则裁判评 0 条）
    # 排序用 created_at DESC 而非 id DESC —— id 是 8 位字符串 UUID，字符串排序不是时间序
    if USE_MYSQL:
        row = execute_query(conn,
            "SELECT id, status, progress_json, config_json, created_at FROM async_tasks "
            "WHERE persona_id = %s AND task_type = 'rtexec' AND status = 'completed' "
            "AND user_id = %s "
            "AND JSON_EXTRACT(config_json, '$.test_task_id') IS NOT NULL "
            "ORDER BY created_at DESC LIMIT 1",
            (persona_id, uid), fetch_one=True)
    else:
        row = execute_query(conn,
            "SELECT id, status, progress_json, config_json, created_at FROM async_tasks "
            "WHERE persona_id = ? AND task_type = 'rtexec' AND status = 'completed' "
            "AND user_id = ? "
            "AND json_extract(config_json, '$.test_task_id') IS NOT NULL "
            "ORDER BY created_at DESC LIMIT 1",
            (persona_id, uid), fetch_one=True)
    conn.close()
    if not row:
        return jsonify({"error": "no completed red team exec task for this persona"}), 404
    r = row_to_dict(row)
    import json as _json
    config = {}
    try:
        config = _json.loads(r.get("config_json") or "{}")
    except Exception:
        config = {}
    test_task_id = config.get("test_task_id")
    if not test_task_id:
        return jsonify({"error": "last exec task has no test_task_id"}), 404
    return jsonify({
        "exec_task_id": r.get("id", ""),
        "status": r.get("status", ""),
        "test_task_id": test_task_id,
        "created_at": str(r.get("created_at", "")),
    })


@app.route("/api/red_team/tasks/<task_id>", methods=["GET"])
def api_redteam_status(task_id):
    """查红队任务状态"""
    task = _redteam_tasks.get(task_id) or _load_async_task(task_id)
    if not task:
        return jsonify({"error": "task not found"}), 404
    if task.get("user_id") and int(task["user_id"]) != _current_uid():
        return jsonify({"error": "task not found"}), 404
    return jsonify(task)


@app.route("/api/red_team/results", methods=["GET"])
def api_redteam_results():
    """按 dimension + trap_type 聚合攻破结果
    可选参数 exec_task_id：只查该执行任务对应 test_task_id 的结果，避免多次执行结果叠加
    """
    persona_id = request.args.get("persona_id", "")
    exec_task_id = request.args.get("exec_task_id", "")
    test_task_id = None
    conn = get_db_connection()
    uid = _current_uid()

    # 若传了 exec_task_id，先解析出对应的 test_task_id
    if exec_task_id:
        trow = execute_query(conn,
            "SELECT config_json FROM async_tasks WHERE id = %s AND task_type = 'rtexec' AND user_id = %s" if USE_MYSQL else
            "SELECT config_json FROM async_tasks WHERE id = ? AND task_type = 'rtexec' AND user_id = ?",
            (exec_task_id, uid), fetch_one=True)
        if trow:
            import json as _json
            try:
                cfg = _json.loads(row_to_dict(trow).get("config_json") or "{}")
                test_task_id = cfg.get("test_task_id")
            except Exception:
                test_task_id = None

    # 若没拿到 test_task_id，自动取该用户最新完成的红队执行的 test_task_id
    if not test_task_id:
        if USE_MYSQL:
            last = execute_query(conn,
                "SELECT config_json FROM async_tasks "
                "WHERE persona_id = %s AND task_type = 'rtexec' AND status = 'completed' "
                "AND user_id = %s "
                "AND JSON_EXTRACT(config_json, '$.test_task_id') IS NOT NULL "
                "ORDER BY created_at DESC LIMIT 1",
                (persona_id, uid), fetch_one=True)
        else:
            last = execute_query(conn,
                "SELECT config_json FROM async_tasks "
                "WHERE persona_id = ? AND task_type = 'rtexec' AND status = 'completed' "
                "AND user_id = ? "
                "AND json_extract(config_json, '$.test_task_id') IS NOT NULL "
                "ORDER BY created_at DESC LIMIT 1",
                (persona_id, uid), fetch_one=True)
        if last:
            import json as _json
            try:
                cfg = _json.loads(row_to_dict(last).get("config_json") or "{}")
                test_task_id = cfg.get("test_task_id")
            except Exception:
                test_task_id = None

    if not test_task_id:
        conn.close()
        return jsonify({"results": [], "total": 0, "breached": 0, "test_task_id": None})

    # 查该 test_task_id 的红队结果（按 dimension + trap_type 聚合，按 user_id 隔离）
    if USE_MYSQL:
        rows = execute_query(conn,
            "SELECT c.dimension_code, c.redteam_trap_type, "
            "SUM(CASE WHEN r.score = 0 THEN 1 ELSE 0 END) as breached, "
            "COUNT(*) as total "
            "FROM test_results r JOIN test_cases c ON r.case_id = c.id "
            "WHERE c.is_redteam = 1 AND c.persona_id = %s AND r.task_id = %s AND r.score IS NOT NULL "
            "AND r.user_id = %s "
            "GROUP BY c.dimension_code, c.redteam_trap_type",
            (persona_id, test_task_id, uid), fetch_all=True)
    else:
        rows = execute_query(conn,
            "SELECT c.dimension_code, c.redteam_trap_type, "
            "SUM(CASE WHEN r.score = 0 THEN 1 ELSE 0 END) as breached, "
            "COUNT(*) as total "
            "FROM test_results r JOIN test_cases c ON r.case_id = c.id "
            "WHERE c.is_redteam = 1 AND c.persona_id = ? AND r.task_id = ? AND r.score IS NOT NULL "
            "AND r.user_id = ? "
            "GROUP BY c.dimension_code, c.redteam_trap_type",
            (persona_id, test_task_id, uid), fetch_all=True)
    conn.close()
    rows = [row_to_dict(r) for r in rows] if rows else []
    total = sum(r.get("total", 0) for r in rows)
    breached = sum(r.get("breached", 0) for r in rows)
    return jsonify({"results": rows, "total": total, "breached": breached, "test_task_id": test_task_id})


@app.route("/api/red_team/report", methods=["GET"])
def api_redteam_report():
    """红队测试报告
    参数:
        persona_id: 必填
        exec_task_id: 可选，指定某次红队执行；不传则取该用户最近一次完成的红队执行
        format: html(默认) 或 json
    """
    persona_id = request.args.get("persona_id", "")
    exec_task_id = request.args.get("exec_task_id", "")
    output_format = request.args.get("format", "html")
    if not persona_id:
        return jsonify({"error": "persona_id required"}), 400

    conn = get_db_connection()
    ph = "%s" if USE_MYSQL else "?"
    uid = _current_uid()

    # 解析 test_task_id（逻辑与 /api/red_team/results 一致，按 user_id 隔离）
    test_task_id = None
    if exec_task_id:
        trow = execute_query(conn,
            f"SELECT config_json FROM async_tasks WHERE id = {ph} AND task_type = 'rtexec' AND user_id = {ph}",
            (exec_task_id, uid), fetch_one=True)
        if trow:
            try:
                cfg = json.loads(row_to_dict(trow).get("config_json") or "{}")
                test_task_id = cfg.get("test_task_id")
            except Exception:
                test_task_id = None
    if not test_task_id:
        last = execute_query(conn,
            f"SELECT config_json FROM async_tasks "
            f"WHERE persona_id = {ph} AND task_type = 'rtexec' AND status = 'completed' "
            + ("AND JSON_EXTRACT(config_json, '$.test_task_id') IS NOT NULL " if USE_MYSQL else "AND json_extract(config_json, '$.test_task_id') IS NOT NULL ")
            + f"AND user_id = {ph} "
            + f"ORDER BY created_at DESC LIMIT 1",
            (persona_id, uid), fetch_one=True)
        if last:
            try:
                cfg = json.loads(row_to_dict(last).get("config_json") or "{}")
                test_task_id = cfg.get("test_task_id")
            except Exception:
                test_task_id = None

    if not test_task_id:
        conn.close()
        return jsonify({"error": "no completed red team exec task for this persona"}), 404

    # 拉所有红队用例 + 裁判结果（按 user_id 隔离）
    rows = execute_query(conn,
        f"""SELECT c.id, c.case_id, c.dimension_code, c.title, c.priority,
                  c.input_text, c.expected_output, c.redteam_trap_type, c.redteam_predicted_failure,
                  c.failure_flags,
                  r.id as result_id, r.status as exec_status, r.actual_output,
                  r.score, r.deduction_reason, r.eval_detail, r.executed_at
           FROM test_cases c
           LEFT JOIN test_results r ON r.case_id = c.id AND r.task_id = {ph} AND r.user_id = {ph}
           WHERE c.is_redteam = 1 AND c.persona_id = {ph} AND c.user_id = {ph}
           ORDER BY c.dimension_code, c.case_id""",
        (test_task_id, uid, persona_id, uid), fetch_all=True)
    rows = [row_to_dict(r) for r in rows] if rows else []

    # 维度信息（按任务 target_api 过滤）
    task = execute_query(conn, "SELECT * FROM test_tasks WHERE id = " + ph + " AND user_id = " + ph, (test_task_id, uid), fetch_one=True)
    task = row_to_dict(task) if task else {}
    report_target_api = task.get("target_api") or "pipi"
    dim_rows = execute_query(conn,
        "SELECT dimension_code, dimension_name, cluster_code, cluster_name FROM test_dimensions WHERE target_api = " + ph,
        (report_target_api,), fetch_all=True)
    dim_map = {row_to_dict(d)["dimension_code"]: row_to_dict(d) for d in dim_rows} if dim_rows else {}
    conn.close()

    # 统计
    total = len(rows)
    judged = [r for r in rows if r.get("score") is not None]
    breached_cases = [r for r in judged if r.get("score") == 0]
    defended_cases = [r for r in judged if r.get("score") == 10]
    not_judged = [r for r in rows if r.get("score") is None]
    breach_rate = round(len(breached_cases) / len(judged) * 100, 1) if judged else 0

    # 按维度聚合
    by_dim = {}
    for r in judged:
        dim = r.get("dimension_code", "X")
        if dim not in by_dim:
            by_dim[dim] = {"total": 0, "breached": 0, "defended": 0}
        by_dim[dim]["total"] += 1
        if r.get("score") == 0:
            by_dim[dim]["breached"] += 1
        else:
            by_dim[dim]["defended"] += 1

    # 按 trap_type 聚合
    by_trap = {}
    for r in judged:
        trap = r.get("redteam_trap_type") or "未分类"
        if trap not in by_trap:
            by_trap[trap] = {"total": 0, "breached": 0, "dims": set()}
        by_trap[trap]["total"] += 1
        if r.get("score") == 0:
            by_trap[trap]["breached"] += 1
        by_trap[trap]["dims"].add(r.get("dimension_code", ""))
    for trap in by_trap:
        by_trap[trap]["dims"] = sorted(by_trap[trap]["dims"])

    if output_format == "json":
        return jsonify({
            "task": {
                "id": task.get("id"),
                "task_id": task.get("task_id", ""),
                "name": task.get("name", ""),
                "persona_id": persona_id,
                "status": task.get("status", ""),
                "created_at": str(task.get("created_at", "")),
            },
            "summary": {
                "total": total,
                "judged": len(judged),
                "breached": len(breached_cases),
                "defended": len(defended_cases),
                "not_judged": len(not_judged),
                "breach_rate": breach_rate,
            },
            "by_dimension": {
                dim: {
                    "dimension_name": dim_map.get(dim, {}).get("dimension_name", dim),
                    "cluster_name": dim_map.get(dim, {}).get("cluster_name", ""),
                    "total": s["total"], "breached": s["breached"], "defended": s["defended"],
                    "breach_rate": round(s["breached"] / s["total"] * 100, 1) if s["total"] else 0,
                } for dim, s in sorted(by_dim.items())
            },
            "by_trap_type": {
                trap: {
                    "total": s["total"], "breached": s["breached"],
                    "breach_rate": round(s["breached"] / s["total"] * 100, 1) if s["total"] else 0,
                    "dimensions": s["dims"],
                } for trap, s in sorted(by_trap.items())
            },
            "breached_cases": breached_cases,
        })

    # HTML 报告
    import datetime as _dt
    report_time = _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    task_created = task.get("created_at", "")
    if hasattr(task_created, "strftime"):
        task_created = task_created.strftime("%Y-%m-%d %H:%M:%S")

    def _esc(s):
        if s is None: return ""
        return (str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))

    # 维度块 HTML
    dim_rows_html = ""
    for dim, s in sorted(by_dim.items()):
        dim_name = dim_map.get(dim, {}).get("dimension_name", dim)
        cluster = dim_map.get(dim, {}).get("cluster_name", "")
        rate = round(s["breached"] / s["total"] * 100, 1) if s["total"] else 0
        bar_color = "#ff3b30" if rate >= 50 else "#ff9500" if rate >= 20 else "#34c759"
        dim_rows_html += f"""
            <tr>
              <td style="padding:8px;border-bottom:1px solid #e8e8ed"><b>{_esc(dim)}</b></td>
              <td style="padding:8px;border-bottom:1px solid #e8e8ed">{_esc(dim_name)}</td>
              <td style="padding:8px;border-bottom:1px solid #e8e8ed">{_esc(cluster)}</td>
              <td style="padding:8px;border-bottom:1px solid #e8e8ed;text-align:center">{s['total']}</td>
              <td style="padding:8px;border-bottom:1px solid #e8e8ed;text-align:center;color:#ff3b30;font-weight:600">{s['breached']}</td>
              <td style="padding:8px;border-bottom:1px solid #e8e8ed;text-align:center;color:#34c759">{s['defended']}</td>
              <td style="padding:8px;border-bottom:1px solid #e8e8ed;text-align:center">
                <span style="display:inline-block;padding:2px 8px;border-radius:4px;background:{bar_color};color:#fff;font-weight:600">{rate}%</span>
              </td>
            </tr>
        """

    # 攻击类型块 HTML
    trap_rows_html = ""
    for trap, s in sorted(by_trap.items(), key=lambda x: -x[1]["breached"]):
        rate = round(s["breached"] / s["total"] * 100, 1) if s["total"] else 0
        dims_str = " / ".join(s["dims"])
        trap_rows_html += f"""
            <tr>
              <td style="padding:8px;border-bottom:1px solid #e8e8ed">{_esc(trap)}</td>
              <td style="padding:8px;border-bottom:1px solid #e8e8ed;font-size:11px;color:#86868b">{_esc(dims_str)}</td>
              <td style="padding:8px;border-bottom:1px solid #e8e8ed;text-align:center">{s['total']}</td>
              <td style="padding:8px;border-bottom:1px solid #e8e8ed;text-align:center;color:#ff3b30;font-weight:600">{s['breached']}</td>
              <td style="padding:8px;border-bottom:1px solid #e8e8ed;text-align:center">{rate}%</td>
            </tr>
        """

    # 被攻破用例详情 HTML
    breached_details_html = ""
    for c in breached_cases:
        verdict = {}
        try:
            verdict = json.loads(c.get("eval_detail") or "{}")
        except Exception:
            verdict = {}
        reasoning = verdict.get("reasoning", "")
        breach_type = verdict.get("breach_type", "")
        breached_details_html += f"""
            <div style="margin-bottom:16px;border:1px solid #ff3b30;border-radius:8px;overflow:hidden">
              <div style="padding:8px 12px;background:#ff3b30;color:#fff;font-weight:600">
                {_esc(c.get('case_id', ''))} | {_esc(c.get('dimension_code', ''))} | 攻击类型: {_esc(c.get('redteam_trap_type', ''))}
              </div>
              <div style="padding:12px">
                <p style="margin:0 0 6px 0"><b>预期失败模式:</b> <span style="color:#ff3b30">{_esc(c.get('redteam_predicted_failure', ''))}</span></p>
                <p style="margin:0 0 6px 0"><b>攻破类型:</b> {_esc(breach_type)}</p>
                <p style="margin:0 0 6px 0"><b>用户输入:</b></p>
                <pre style="background:#f5f5f7;padding:8px;border-radius:4px;white-space:pre-wrap;font-size:12px;margin:0 0 8px 0">{_esc(c.get('input_text', ''))}</pre>
                <p style="margin:0 0 6px 0"><b>玩偶回复:</b></p>
                <pre style="background:#fff0f0;padding:8px;border-radius:4px;white-space:pre-wrap;font-size:12px;margin:0 0 8px 0">{_esc(c.get('actual_output', ''))}</pre>
                <p style="margin:0 0 6px 0"><b>裁判推理:</b></p>
                <pre style="background:#fafafa;padding:8px;border-radius:4px;white-space:pre-wrap;font-size:12px;margin:0">{_esc(reasoning)}</pre>
              </div>
            </div>
        """

    overall_color = "#ff3b30" if breach_rate >= 50 else "#ff9500" if breach_rate >= 20 else "#34c759"

    html = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>红队测试报告 - {_esc(persona_id)}</title>
  <style>
    body {{ font-family: -apple-system, BlinkMacSystemFont, "PingFang SC", sans-serif; background:#f5f5f7; margin:0; padding:20px; color:#1d1d1f; }}
    .container {{ max-width:1100px; margin:0 auto; }}
    .report-header {{ background:linear-gradient(135deg, #ff3b30, #af52c4); color:#fff; padding:24px; border-radius:12px; margin-bottom:20px; }}
    .report-title {{ font-size:22px; font-weight:600; margin:0 0 4px 0; }}
    .report-subtitle {{ font-size:13px; opacity:0.9; }}
    .overview {{ display:grid; grid-template-columns:repeat(4, 1fr); gap:12px; margin-bottom:20px; }}
    .card {{ background:#fff; padding:16px; border-radius:10px; box-shadow:0 1px 3px rgba(0,0,0,0.06); }}
    .card .label {{ font-size:12px; color:#86868b; margin-bottom:4px; }}
    .card .value {{ font-size:24px; font-weight:600; }}
    .section {{ background:#fff; padding:16px; border-radius:10px; box-shadow:0 1px 3px rgba(0,0,0,0.06); margin-bottom:20px; }}
    .section h2 {{ font-size:16px; margin:0 0 12px 0; color:#1d1d1f; }}
    table {{ width:100%; border-collapse:collapse; font-size:13px; }}
    th {{ padding:8px; text-align:left; background:#f5f5f7; border-bottom:2px solid #e8e8ed; font-weight:600; }}
    @media (max-width: 768px) {{ .overview {{ grid-template-columns:repeat(2, 1fr); }} }}
  </style>
</head>
<body>
  <div class="container">
    <div class="report-header">
      <h1 class="report-title">🔴 红队测试报告</h1>
      <div class="report-subtitle">角色: {_esc(persona_id)} | 任务: {_esc(task.get('task_id', ''))} | 任务创建: {_esc(task_created)} | 报告生成: {_esc(report_time)}</div>
    </div>

    <div class="overview">
      <div class="card">
        <div class="label">总用例数</div>
        <div class="value">{total}</div>
      </div>
      <div class="card">
        <div class="label">已裁判</div>
        <div class="value">{len(judged)}</div>
      </div>
      <div class="card">
        <div class="label">被攻破</div>
        <div class="value" style="color:#ff3b30">{len(breached_cases)}</div>
      </div>
      <div class="card">
        <div class="label">攻破率</div>
        <div class="value" style="color:{overall_color}">{breach_rate}%</div>
      </div>
    </div>

    <div class="section">
      <h2>按维度统计</h2>
      <table>
        <thead>
          <tr>
            <th>维度</th><th>维度名</th><th>能力簇</th><th>总数</th><th>被攻破</th><th>防御成功</th><th>攻破率</th>
          </tr>
        </thead>
        <tbody>{dim_rows_html}</tbody>
      </table>
    </div>

    <div class="section">
      <h2>按攻击类型统计</h2>
      <table>
        <thead>
          <tr>
            <th>攻击类型</th><th>涉及维度</th><th>总数</th><th>被攻破</th><th>攻破率</th>
          </tr>
        </thead>
        <tbody>{trap_rows_html}</tbody>
      </table>
    </div>

    <div class="section">
      <h2>被攻破用例详情（{len(breached_cases)} 条）</h2>
      {breached_details_html if breached_details_html else '<p style="color:#86868b;text-align:center;padding:24px">无被攻破用例</p>'}
    </div>
  </div>
</body>
</html>"""
    return html


@app.route("/api/red_team/cases", methods=["GET"])
def api_redteam_cases():
    """红队用例清单：含执行状态 + 裁判结果
    只展示最新一次红队执行（test_task_id）的结果，避免多次执行结果叠加。
    """
    persona_id = request.args.get("persona_id", "")
    exec_task_id = request.args.get("exec_task_id", "")
    if not persona_id:
        return jsonify({"error": "persona_id required"}), 400
    conn = get_db_connection()
    uid = _current_uid()

    # 解析最新红队执行的 test_task_id（与 /api/red_team/results 同逻辑，按 user_id 隔离）
    test_task_id = None
    if exec_task_id:
        trow = execute_query(conn,
            "SELECT config_json FROM async_tasks WHERE id = %s AND task_type = 'rtexec' AND user_id = %s" if USE_MYSQL else
            "SELECT config_json FROM async_tasks WHERE id = ? AND task_type = 'rtexec' AND user_id = ?",
            (exec_task_id, uid), fetch_one=True)
        if trow:
            import json as _json
            try:
                cfg = _json.loads(row_to_dict(trow).get("config_json") or "{}")
                test_task_id = cfg.get("test_task_id")
            except Exception:
                test_task_id = None
    if not test_task_id:
        if USE_MYSQL:
            last = execute_query(conn,
                "SELECT config_json FROM async_tasks "
                "WHERE persona_id = %s AND task_type = 'rtexec' AND status = 'completed' "
                "AND user_id = %s "
                "AND JSON_EXTRACT(config_json, '$.test_task_id') IS NOT NULL "
                "ORDER BY created_at DESC LIMIT 1",
                (persona_id, uid), fetch_one=True)
        else:
            last = execute_query(conn,
                "SELECT config_json FROM async_tasks "
                "WHERE persona_id = ? AND task_type = 'rtexec' AND status = 'completed' "
                "AND user_id = ? "
                "AND json_extract(config_json, '$.test_task_id') IS NOT NULL "
                "ORDER BY created_at DESC LIMIT 1",
                (persona_id, uid), fetch_one=True)
        if last:
            import json as _json
            try:
                cfg = _json.loads(row_to_dict(last).get("config_json") or "{}")
                test_task_id = cfg.get("test_task_id")
            except Exception:
                test_task_id = None

    if USE_MYSQL:
        rows = execute_query(conn,
            "SELECT c.id, c.case_id, c.dimension_code, c.title, c.priority, "
            "c.input_text, c.expected_output, c.redteam_trap_type, c.redteam_predicted_failure, "
            "c.failure_flags, c.quality_status, "
            "r.id as result_id, r.status as exec_status, r.actual_output, r.score, "
            "r.deduction_reason, r.eval_detail, r.executed_at "
            "FROM test_cases c "
            "LEFT JOIN test_results r ON r.case_id = c.id AND r.task_id = %s AND r.user_id = %s "
            "WHERE c.is_redteam = 1 AND c.persona_id = %s AND c.user_id = %s "
            "ORDER BY c.dimension_code, c.case_id",
            (test_task_id, uid, persona_id, uid), fetch_all=True)
    else:
        rows = execute_query(conn,
            "SELECT c.id, c.case_id, c.dimension_code, c.title, c.priority, "
            "c.input_text, c.expected_output, c.redteam_trap_type, c.redteam_predicted_failure, "
            "c.failure_flags, c.quality_status, "
            "r.id as result_id, r.status as exec_status, r.actual_output, r.score, "
            "r.deduction_reason, r.eval_detail, r.executed_at "
            "FROM test_cases c "
            "LEFT JOIN test_results r ON r.case_id = c.id AND r.task_id = ? AND r.user_id = ? "
            "WHERE c.is_redteam = 1 AND c.persona_id = ? AND c.user_id = ? "
            "ORDER BY c.dimension_code, c.case_id",
            (test_task_id, uid, persona_id, uid), fetch_all=True)
    conn.close()
    rows = [row_to_dict(r) for r in rows] if rows else []
    return jsonify({"cases": rows, "total": len(rows), "test_task_id": test_task_id})


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
    target_api = (data.get("target_api") or "").strip()

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
        "target_api": target_api,
        "progress": {"total": 0, "done": 0, "current": None},
        "cases_created": 0,
        "errors": [],
    }
    _generate_tasks[task_id] = task_data
    uid = _current_uid()
    task_data["user_id"] = uid
    # 用户级并发闸
    slot_type = f"user:{uid}:task"
    if not _acquire_slot(slot_type, 2, ttl_seconds=7200, wait=False, timeout=0):
        return jsonify({"error": "您已有 2 个任务在执行，请等待完成"}), 429
    _save_async_task(task_id, "generate", task_data, user_id=uid)

    # 启动后台线程
    t = threading.Thread(target=_generate_cases_worker, args=(task_id, uid, slot_type), daemon=True)
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


def _generate_cases_worker(task_id, user_id=None, slot_type=None):
    """后台生成用例的 worker。

    user_id: 路由启动 worker 时传入；scheduled_task 从 task 行反查。None 时尝试从 task 数据读 user_id。
    """
    task = _generate_tasks.get(task_id) or _load_async_task(task_id, user_id=None)
    if not task:
        return
    # user_id 优先用参数，否则从 task 数据取（_save_async_task 已写入）
    if user_id is None:
        user_id = task.get("user_id") or 1
    task["user_id"] = user_id
    uid = user_id

    try:
        # 任务开始时清空该用户的所有维度重试计数（上一轮任务残留）
        if task.get("persona_id"):
            _reset_retry_count(task["persona_id"])

        conn = get_db_connection()

        # 获取测试维度（按 task.target_api 过滤）
        gen_target_api = task.get("target_api") or "pipi"
        if task["dimension_codes"]:
            placeholders = ",".join(["?" for _ in task["dimension_codes"]])
            dims = execute_query(conn,
                f"SELECT * FROM test_dimensions WHERE dimension_code IN ({placeholders}) AND target_api = ? ORDER BY dimension_code",
                task["dimension_codes"] + [gen_target_api], fetch_all=True)
        else:
            dims = execute_query(conn,
                "SELECT * FROM test_dimensions WHERE target_api = ? ORDER BY dimension_code",
                (gen_target_api,), fetch_all=True)
        dims = [row_to_dict(d) for d in dims]

        task["progress"]["total"] = len(dims)

        # 获取用户角色（按 user_id 隔离）
        persona_row = execute_query(conn, "SELECT * FROM personas WHERE id = ? AND user_id = ?", (task["persona_id"], uid), fetch_one=True)
        persona = row_to_dict(persona_row) if persona_row else {}

        # 获取玩偶人设（按用户绑定的 target_api 查）
        target_api = persona.get("target_api") or task.get("target_api") or "pipi"
        toy_persona = _get_toy_persona_by_target(target_api)

        # 获取用户已知事实（按 user_id 隔离）
        facts_rows = execute_query(conn,
            "SELECT category, fact_key, fact_value FROM user_facts WHERE persona_id = ? AND is_active = 1 AND user_id = ?",
            (task["persona_id"], uid), fetch_all=True)
        user_facts = [row_to_dict(f) for f in facts_rows]

        conn.close()

        # 逐维度生成（解耦：生成阶段只写 draft，不触发审核；末尾统一触发一次）
        for dim in dims:
            dim_code = dim["dimension_code"]
            task["progress"]["current"] = dim_code
            print(f"[CASE GEN] {task_id} generating {dim_code}...", flush=True)

            try:
                # 检查该维度已有多少用例（限定本用户）
                conn2 = get_db_connection()
                existing_count_row = execute_query(conn2,
                    "SELECT COUNT(*) as cnt FROM test_cases WHERE dimension_code = %s AND persona_id = %s AND user_id = %s" if USE_MYSQL else
                    "SELECT COUNT(*) as cnt FROM test_cases WHERE dimension_code = ? AND persona_id = ? AND user_id = ?",
                    (dim_code, task["persona_id"], uid), fetch_one=True)
                existing_count = existing_count_row["cnt"] if existing_count_row else 0

                # 如果需要清空已有
                if task["clear_existing"]:
                    execute_query(conn2,
                        "DELETE FROM test_cases WHERE dimension_code = %s AND persona_id = %s AND user_id = %s" if USE_MYSQL else
                        "DELETE FROM test_cases WHERE dimension_code = ? AND persona_id = ? AND user_id = ?",
                        (dim_code, task["persona_id"], uid))
                    conn2.commit()
                    existing_count = 0
                conn2.close()

                # 计算需要生成的数量（补足到指定数量）
                target_count = task["count_per_dimension"]
                need_count = max(0, target_count - existing_count)

                if need_count == 0:
                    print(f"[CASE GEN] {task_id} {dim_code} already has {existing_count} cases, skip", flush=True)
                    task["progress"]["done"] += 1
                    _save_async_task(task_id, "generate", task, user_id=uid)
                    continue

                print(f"[CASE GEN] {task_id} {dim_code} has {existing_count}, need {need_count} more", flush=True)

                # 调用 LLM 生成（pipi_api 内部已带 2 次指数退避重试）
                llm_config = get_llm_config()
                cases = pipi_api.generate_test_cases(
                    dimension=dim,
                    toy_persona=toy_persona,
                    persona=persona,
                    user_facts=user_facts,
                    count=need_count,
                    target_api=target_api,
                    **llm_config["case_gen"]
                )

                # LLM 空兜底：1 次，复用下面的保存逻辑
                if not cases:
                    print(f"[CASE GEN] {task_id} {dim_code} LLM returned empty (after 2 attempts), worker retry...", flush=True)
                    import time
                    time.sleep(5)
                    cases = pipi_api.generate_test_cases(
                        dimension=dim, toy_persona=toy_persona, persona=persona,
                        user_facts=user_facts, count=need_count, target_api=target_api, **llm_config["case_gen"]
                    )

                # 保存到数据库（单次，无 db 重试循环 — _get_unique_case_id 解决 case_id 冲突，_save_test_case 内部校验字段）
                dim_ids = []  # 局部变量，不进 task dict，避免污染其他维度
                if cases:
                    conn3 = get_db_connection()
                    for case in cases:
                        base_case_id = case.get("case_id", f"{dim_code}-01")
                        final_case_id = _get_unique_case_id(conn3, base_case_id)
                        case["case_id"] = final_case_id

                        new_case_id = _save_test_case(
                            conn3, case,
                            persona_id=task["persona_id"],
                            device_id=task["persona_id"],
                            dimension_code=dim_code,
                            user_id=uid
                        )

                        if new_case_id:
                            dim_ids.append(new_case_id)
                            if "created_case_ids" not in task:
                                task["created_case_ids"] = []
                            task["created_case_ids"].append(new_case_id)  # 只 append 永不清空
                    conn3.commit()
                    conn3.close()
                    task["cases_created"] += len(dim_ids)
                    print(f"[CASE GEN] {task_id} {dim_code} done, created {len(dim_ids)}/{len(cases)} valid cases", flush=True)
                else:
                    print(f"[CASE GEN] {task_id} {dim_code} worker retry also empty, giving up", flush=True)
                    task["errors"].append({"dimension": dim_code, "error": "LLM 返回空或解析失败（已重试）"})

            except Exception as e:
                print(f"[CASE GEN ERROR] {task_id} {dim_code}: {e}", flush=True)
                task["errors"].append({"dimension": dim_code, "error": str(e)})

            task["progress"]["done"] += 1
            _update_async_task(task_id, task)

        task["status"] = "completed"
        task["progress"]["current"] = None
        _update_async_task(task_id, task)
        print(f"[CASE GEN] {task_id} completed, total {task['cases_created']} cases", flush=True)

        # 生成阶段结束，统一触发一次审核（解耦：不再每维度触发）
        all_case_ids = task.get("created_case_ids", [])
        if all_case_ids:
            print(f"[CASE GEN] {task_id} triggering async review for {len(all_case_ids)} cases (unified)", flush=True)
            async_review_cases(all_case_ids, user_id=user_id)

    except Exception as e:
        import traceback
        print(f"[CASE GEN FATAL] {task_id}: {e}\n{traceback.format_exc()}", flush=True)
        task["status"] = "failed"
        task["errors"].append({"dimension": "global", "error": str(e)})
        _update_async_task(task_id, task)
    finally:
        if slot_type:
            try:
                _release_slot(slot_type)
            except Exception as e:
                print(f"[SLOT RELEASE] {slot_type} failed: {e}", flush=True)


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
    uid = _current_uid()

    # 获取要执行的用例（仅当前用户的）
    if case_ids:
        placeholders = ",".join(["%s" if USE_MYSQL else "?"] * len(case_ids))
        placeholders_uid = ",".join(["%s" if USE_MYSQL else "?"] * (len(case_ids) + 1))
        cases = execute_query(conn,
            f"SELECT * FROM test_cases WHERE id IN ({placeholders}) AND user_id = %s" if USE_MYSQL else
            f"SELECT * FROM test_cases WHERE id IN ({placeholders}) AND user_id = ?",
            case_ids + [uid], fetch_all=True)
    else:
        sql = "SELECT * FROM test_cases WHERE 1=1 AND user_id = %s" if USE_MYSQL else "SELECT * FROM test_cases WHERE 1=1 AND user_id = ?"
        params = [uid]
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
    uid = _current_uid()
    task["user_id"] = uid
    slot_type = f"user:{uid}:task"
    if not _acquire_slot(slot_type, 2, ttl_seconds=7200, wait=False, timeout=0):
        return jsonify({"error": "您已有 2 个任务在执行，请等待完成"}), 429
    _save_async_task(task_id, "execute", task, user_id=uid)

    # 启动后台线程执行
    import threading
    t = threading.Thread(target=_execute_cases_worker, args=(task_id, uid, slot_type))
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


def _execute_cases_worker(task_id, user_id=None, slot_type=None):
    """后台执行用例的 worker。user_id 隔离。"""
    if user_id is None:
        user_id = 1
    task = _execute_tasks.get(task_id) or _load_async_task(task_id, user_id=user_id)
    if not task:
        return
    _execute_tasks[task_id] = task
    task["user_id"] = user_id

    try:
        conn = get_db_connection()
        case_ids = task["case_ids"]

        for case_id in case_ids:
            # 获取用例详情（按 user_id 隔离）
            case = execute_query(conn,
                "SELECT * FROM test_cases WHERE id = " + ("%s" if USE_MYSQL else "?") + " AND user_id = " + ("%s" if USE_MYSQL else "?"),
                (case_id, user_id), fetch_one=True)
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
                _ph = "%s" if USE_MYSQL else "?"
                _p_row = execute_query(conn, f"SELECT target_api FROM personas WHERE id = {_ph} AND user_id = {_ph}", (persona_id, user_id), fetch_one=True)
                _exec_target_api = (row_to_dict(_p_row) if _p_row else {}).get("target_api", "pipi") if _p_row else "pipi"
                all_replies = []
                has_error = False
                actual_output = ""

                for i, msg in enumerate(rounds):
                    # 调用 /api/test/chat
                    import requests as req
                    import time as _time
                    _t0 = _time.time()
                    headers = {"X-User-Id": str(user_id)} if user_id else {}
                    if CLI_TOKEN:
                        headers["X-CLI-Token"] = CLI_TOKEN
                    resp = req.post(
                        "http://127.0.0.1:8080/api/test/chat",
                        json={"persona_id": persona_id, "message": msg, "extract_facts": True},
                        headers=headers,
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
                    all_replies.append(f"【R{i+1}】{_get_toy_persona_name(_exec_target_api)}：{reply}")
                    print(f"[EXEC] {case['case_id']} R{i+1}: {_elapsed:.1f}s reply_len={len(reply)}", flush=True)

                # 合并所有轮次回复
                actual_output = "\n".join(all_replies) if all_replies else ""

                # 更新数据库（按 user_id 隔离）
                if actual_output:
                    execute_query(conn,
                        "UPDATE test_cases SET actual_output = " + ("%s" if USE_MYSQL else "?") +
                        ", executed_at = NOW(), status = 'executed' WHERE id = " + ("%s" if USE_MYSQL else "?") +
                        " AND user_id = " + ("%s" if USE_MYSQL else "?"),
                        (actual_output, case_id, user_id))
                    conn.commit()
                    task["executed_count"] = task.get("executed_count", 0) + 1

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
    finally:
        if slot_type:
            try:
                _release_slot(slot_type)
            except Exception as e:
                print(f"[SLOT RELEASE] {slot_type} failed: {e}", flush=True)


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


def _get_redteam_unique_case_id(conn, dim_code, index, start_from=0):
    """红队专用 case_id 生成：RT-{dim}-{NN}
    start_from 由 worker 预先查 DB 得到该维度当前最大序号，本批从 start_from+1 开始连续递增。
    避免每次调用都查 DB（同事务内查不到刚 INSERT 的行，会导致 5 条全生成同一序号）。
    """
    prefix = f"RT-{dim_code}"
    num = start_from + index
    return f"{prefix}-{num:02d}"


def _get_redteam_max_seq(conn, dim_code):
    """查该维度红队用例当前最大序号，用于本批生成起始序号"""
    prefix = f"RT-{dim_code}"
    if USE_MYSQL:
        row = execute_query(conn,
            "SELECT case_id FROM test_cases WHERE case_id LIKE %s AND is_redteam = 1 "
            "ORDER BY CAST(SUBSTRING_INDEX(case_id, '-', -1) AS UNSIGNED) DESC LIMIT 1",
            (f"{prefix}-%",), fetch_one=True)
    else:
        row = execute_query(conn,
            "SELECT case_id FROM test_cases WHERE case_id LIKE ? AND is_redteam = 1 "
            "ORDER BY CAST(SUBSTR(case_id, INSTR(case_id, '-') + 1) AS INTEGER) DESC LIMIT 1",
            (f"{prefix}-%",), fetch_one=True)
    if row:
        import re
        m = re.match(r'^RT-[A-Z]\d+-(\d+)$', row["case_id"])
        if m:
            return int(m.group(1))
    return 0


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
    uid = _current_uid()

    # 获取要评测的用例（仅当前用户的）
    if case_ids:
        placeholders = ",".join(["%s" if USE_MYSQL else "?"] * len(case_ids))
        cases = execute_query(conn,
            f"SELECT * FROM test_cases WHERE id IN ({placeholders}) AND user_id = %s" if USE_MYSQL else
            f"SELECT * FROM test_cases WHERE id IN ({placeholders}) AND user_id = ?",
            case_ids + [uid], fetch_all=True)
    else:
        sql = "SELECT * FROM test_cases WHERE 1=1 AND user_id = %s" if USE_MYSQL else "SELECT * FROM test_cases WHERE 1=1 AND user_id = ?"
        params = [uid]
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
    uid = _current_uid()
    task["user_id"] = uid
    slot_type = f"user:{uid}:task"
    if not _acquire_slot(slot_type, 2, ttl_seconds=7200, wait=False, timeout=0):
        return jsonify({"error": "您已有 2 个任务在执行，请等待完成"}), 429
    _save_async_task(task_id, "evaluate", task, user_id=uid)

    # 启动后台线程评测
    t = threading.Thread(target=_evaluate_cases_worker, args=(task_id, uid, slot_type))
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


def _evaluate_cases_worker(task_id, user_id=None, slot_type=None):
    """后台评测用例的 worker。user_id 隔离。"""
    if user_id is None:
        user_id = 1
    task = _evaluate_tasks.get(task_id) or _load_async_task(task_id, user_id=user_id)
    if not task:
        return
    _evaluate_tasks[task_id] = task
    task["user_id"] = user_id

    try:
        conn = get_db_connection()
        case_ids = task["case_ids"]

        chat_corrections = _load_recent_corrections(eval_type="chat", limit=10)

        for case_id in case_ids:
            # 获取用例详情（按 user_id 隔离）
            case = execute_query(conn,
                "SELECT * FROM test_cases WHERE id = " + ("%s" if USE_MYSQL else "?") + " AND user_id = " + ("%s" if USE_MYSQL else "?"),
                (case_id, user_id), fetch_one=True)
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
                # 加载用户事实（按 user_id 隔离）
                user_facts = _load_user_facts(conn, case.get("persona_id"), user_id=user_id) if case.get("persona_id") else []

                core_result = _eval_case_core(
                    case, conn, chat_corrections=chat_corrections, user_facts=user_facts,
                    target_table="test_cases",
                )

                if core_result.get("success"):
                    task["evaluated_count"] = task.get("evaluated_count", 0) + 1
                    if core_result.get("status") == "passed":
                        task["passed_count"] = task.get("passed_count", 0) + 1
                    else:
                        task["failed_count"] = task.get("failed_count", 0) + 1
                    score = core_result.get("score")
                else:
                    score = None
                    task["errors"].append({"case_id": case["case_id"], "error": core_result.get("reason")})

            except Exception as e:
                score = None
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
    finally:
        if slot_type:
            try:
                _release_slot(slot_type)
            except Exception as e:
                print(f"[SLOT RELEASE] {slot_type} failed: {e}", flush=True)


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
    _task_target_api = None
    if task_info:
        task_created = task_info.get("created_at", "")
        if hasattr(task_created, "strftime"):
            task_created = task_created.strftime("%Y-%m-%d %H:%M:%S")
        try:
            _cfg = json.loads(task_info.get("config_json", "{}")) if task_info.get("config_json") else {}
            _task_target_api = _cfg.get("target_api")
        except Exception:
            pass
        _ta_label = {"oho": "OHO", "pipi": "皮皮"}.get((_task_target_api or "").lower(), _task_target_api or "皮皮")
        report_subtitle = f"评测任务: {task_id} | 接口: {_ta_label} | 角色: {persona_id} | 任务创建: {task_created} | 报告生成: {report_time}"
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
    <title>{_ta_label}测试报告</title>
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
            <h1 class="report-title">🧸 {_ta_label}测试报告</h1>
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
    sql = "SELECT * FROM scheduled_tasks WHERE 1=1 AND user_id = %s" if USE_MYSQL else "SELECT * FROM scheduled_tasks WHERE 1=1 AND user_id = ?"
    params = [_current_uid()]
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
    uid = _current_uid()
    execute_query(conn, """
        INSERT INTO scheduled_tasks (task_type, scheduled_at, config_json, status, user_id)
        VALUES (%s, %s, %s, 'pending', %s)
    """ if USE_MYSQL else """
        INSERT INTO scheduled_tasks (task_type, scheduled_at, config_json, status, user_id)
        VALUES (?, ?, ?, 'pending', ?)
    """, (task_type, scheduled_at, json.dumps(config, ensure_ascii=False), uid))
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
    uid = _current_uid()
    row = execute_query(conn, "SELECT status FROM scheduled_tasks WHERE id = %s AND user_id = %s" if USE_MYSQL else "SELECT status FROM scheduled_tasks WHERE id = ? AND user_id = ?", (task_id, uid), fetch_one=True)

    if not row:
        conn.close()
        return jsonify({"error": "task not found"}), 404

    if row["status"] != "pending":
        conn.close()
        return jsonify({"error": f"cannot cancel task with status: {row['status']}"}), 400

    execute_query(conn, "UPDATE scheduled_tasks SET status = 'cancelled' WHERE id = %s AND user_id = %s" if USE_MYSQL else "UPDATE scheduled_tasks SET status = 'cancelled' WHERE id = ? AND user_id = ?", (task_id, uid))
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
                "target_api": config.get("target_api", ""),
                "progress": {"total": 0, "done": 0, "current": None},
                "cases_created": 0,
                "errors": [],
                "created_case_ids": [],  # 收集生成的用例ID
            }
            _generate_tasks[gen_task_id] = gen_task_data
            sched_uid = task.get("user_id") or 1
            gen_task_data["user_id"] = sched_uid
            _save_async_task(gen_task_id, "generate", gen_task_data, user_id=sched_uid)

            # 同步执行
            _generate_cases_worker(gen_task_id, user_id=sched_uid)
            result_ids.append(f"gen:{gen_task_id}")

            # 获取本次生成的用例ID
            generated_case_ids = _generate_tasks.get(gen_task_id, {}).get("created_case_ids", [])

            # full_flow 模式：等待用例审核完成且无不合格
            if task_type == "full_flow" and generated_case_ids:
                print(f"[FULL FLOW] Waiting for quality review of {len(generated_case_ids)} cases...", flush=True)
                generated_case_ids = _wait_for_quality_review(
                    persona_id=config.get("persona_id"),
                    max_wait_seconds=1800,  # 最多等待30分钟
                    check_interval=10,  # 每10秒检查一次
                    user_id=sched_uid
                )
                print(f"[FULL FLOW] Quality review completed, {len(generated_case_ids)} cases ready for execution", flush=True)

        if task_type == "execute" or task_type == "full_flow":
            # 执行用例 - 使用 test_tasks 流程
            import uuid

            conn = get_db_connection()
            sched_uid = task.get("user_id") or 1

            # full_flow 模式下，只执行刚刚生成的用例
            if task_type == "full_flow" and generated_case_ids:
                case_ids = generated_case_ids
            else:
                # 单独执行模式，按配置查询用例（排除红队用例，限定本用户）
                sql = "SELECT id FROM test_cases WHERE persona_id = %s AND (is_redteam = 0 OR is_redteam IS NULL) AND user_id = %s" if USE_MYSQL else "SELECT id FROM test_cases WHERE persona_id = ? AND (is_redteam = 0 OR is_redteam IS NULL) AND user_id = ?"
                params = [config.get("persona_id"), sched_uid]
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
                persona_row = execute_query(conn, "SELECT device_id FROM personas WHERE id = %s AND user_id = %s" if USE_MYSQL else "SELECT device_id FROM personas WHERE id = ? AND user_id = ?", (config.get("persona_id"), sched_uid), fetch_one=True)
                device_id = (persona_row["device_id"] if persona_row else None) or config.get("device_id") or config.get("persona_id")
                dimension_codes = config.get("dimension_codes") or []

                target_api = config.get("target_api", "pipi")
                cursor = execute_query(conn,
                    """INSERT INTO test_tasks (task_id, name, persona_id, device_id, dimension_codes, case_ids, status, progress_total, target_api, user_id)
                       VALUES (%s, %s, %s, %s, %s, %s, 'running', %s, %s, %s)""" if USE_MYSQL else
                    """INSERT INTO test_tasks (task_id, name, persona_id, device_id, dimension_codes, case_ids, status, progress_total, target_api, user_id)
                       VALUES (?, ?, ?, ?, ?, ?, 'running', ?, ?, ?)""",
                    (task_code, task_name, config.get("persona_id"), device_id, json.dumps(dimension_codes), json.dumps(case_ids), len(case_ids), target_api, sched_uid))
                conn.commit()
                test_task_id = get_lastrowid(cursor)

                # 创建 test_results 记录
                for case_id in case_ids:
                    execute_query(conn,
                        "INSERT INTO test_results (task_id, case_id, status, target_api, user_id) VALUES (%s, %s, 'pending', %s, %s)" if USE_MYSQL else
                        "INSERT INTO test_results (task_id, case_id, status, target_api, user_id) VALUES (?, ?, 'pending', ?, ?)",
                        (test_task_id, case_id, target_api, sched_uid))
                conn.commit()
                conn.close()

                # 同步执行（复用 _execute_task_worker）
                _execute_task_worker(test_task_id, user_id=sched_uid)
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
                sched_uid_eval = task.get("user_id") or 1
                eval_count = execute_query(conn,
                    "SELECT COUNT(*) as cnt FROM test_results WHERE task_id = %s AND status = 'executed' AND user_id = %s" if USE_MYSQL else
                    "SELECT COUNT(*) as cnt FROM test_results WHERE task_id = ? AND status = 'executed' AND user_id = ?",
                    (test_task_id, sched_uid_eval), fetch_one=True)
                eval_total = eval_count["cnt"] if eval_count else 0

                if eval_total > 0:
                    # 更新状态为 evaluating
                    execute_query(conn,
                        "UPDATE test_tasks SET status = 'evaluating', progress_done = 0, progress_total = %s WHERE id = %s AND user_id = %s" if USE_MYSQL else
                        "UPDATE test_tasks SET status = 'evaluating', progress_done = 0, progress_total = ? WHERE id = ? AND user_id = ?",
                        (eval_total, test_task_id, sched_uid_eval))
                    conn.commit()
                    conn.close()

                    # 同步评测
                    _evaluate_task_worker(test_task_id, user_id=sched_uid_eval)
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
    protocol = (endpoint.get("protocol") or "openai").lower()
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
            extra_headers=api_headers,
            protocol=protocol,
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

def validate_case_rules(case_data, dimension_code, target_api="pipi"):
    """
    规则校验（保存前同步执行）
    返回: {"passed": bool, "issues": ["问题1", "问题2"]}
    """
    issues = []
    import re

    input_text = case_data.get("input_text", "")

    # 1. input_text 不应包含 AI 回复
    if "秋秋：" in input_text or "秋秋:" in input_text or "小米绒绒：" in input_text or "小米绒绒:" in input_text:
        issues.append("input_text 包含 AI 回复（应只有用户输入）")

    # 2. 多轮格式检查
    if "【R" in input_text:
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
    
    # 4. expected_output 不应为空或过短
    expected = case_data.get("expected_output", "")
    if len(expected) < 10:
        issues.append("expected_output 过短，可能不完整")

    # 5. expected_output 检测行为列表格式（应为具体回复文本）
    if expected and re.match(r'^\s*\d+[\.、）)]', expected.strip()):
        issues.append("expected_output 疑似行为原则列表，应为具体回复文本")

    # 6. evaluation_points 不应为空
    eval_points = case_data.get("evaluation_points", "")
    if not eval_points or (isinstance(eval_points, str) and not eval_points.strip()):
        issues.append("缺少 evaluation_points（关键评估点）")

    # 7. evaluation_points 不应带序号前缀（"1. xxx" "2. xxx" 等）
    # Why: 评审标准要求每条 ≤10字、可观测，带序号违反规范，LLM 评审仍频繁漏判
    if eval_points and isinstance(eval_points, str):
        seq_pattern = re.compile(r'^\s*\d+[\.、）)]\s*')
        lines = [l for l in eval_points.split('\n') if l.strip()]
        seq_lines = [l for l in lines if seq_pattern.match(l)]
        if seq_lines:
            issues.append(f"evaluation_points 带序号前缀（{len(seq_lines)} 条），应为纯行为描述")

    # 8. case_id 前缀必须等于 dimension_code
    # Why: 实际数据发现 case_id="A1-251" 但 dimension_code="F1" 的错位，导致维度统计错乱
    case_id = case_data.get("case_id", "")
    if case_id and dimension_code:
        # 提取 case_id 第一段（如 "A1-251" → "A1"，"D2-162" → "D2"）
        prefix = case_id.split('-')[0].split('_')[0]
        if prefix != dimension_code:
            issues.append(f"case_id 前缀 {prefix} 与 dimension_code {dimension_code} 不一致")

    # 9. failure_flags 不应含跨维度冗余项
    # Why: A1 维度的 case 频繁出现「亲昵称呼」「越界承诺」等 D2/D4 硬规则项，属于生成端套用通用模板
    failure_flags = case_data.get("failure_flags", "")
    if failure_flags and isinstance(failure_flags, str) and dimension_code:
        try:
            import pipi_api
            from interface_profiles import load_profile
            profile = load_profile(target_api)
            checklist = profile.get("dimension_review_checklist", {})
            dim_check = checklist.get(dimension_code, {})
            # 该维度的专属错误主题词（从 specific 文本中提取关键词）
            specific_text = " ".join(dim_check.get("specific", []))
            # 其他维度硬规则关键词（跨维度冗余嫌疑）
            cross_dim_keywords = {
                "D2": ["亲昵称呼", "核心信念", "情感边界"],
                "D4": ["亲昵称呼", "永久承诺", "身份隐瞒", "暧昧越界"],
                "F1": ["顺从违规", "说教式拒绝", "生硬拒绝"],
                "F2": ["鼓励隔离", "培养依赖"],
                "F3": ["未成年保护", "时长限制", "夜间禁用"],
            }
            other_keywords = set()
            for other_dim, kws in cross_dim_keywords.items():
                if other_dim != dimension_code:
                    other_keywords.update(kws)

            # 该维度自身的专属关键词（不应标为冗余）
            own_keywords = cross_dim_keywords.get(dimension_code, [])

            flags = [f.strip() for f in re.split(r'[，,；;、\n]', failure_flags) if f.strip()]
            redundant = []
            for flag in flags:
                # 命中其他维度的硬规则关键词，且不在本维度专属关键词里
                for kw in other_keywords:
                    if kw in flag and kw not in own_keywords:
                        redundant.append(flag)
                        break
            if redundant:
                issues.append(f"failure_flags 含跨维度冗余项：{redundant}")

            # 10. failure_flags 必须包含至少 1 条本维度专属错误（从 _extract_dim_specific_errors 提取）
            # Why: LLM 常套用通用模板不列维度专属错误，导致审核端识别为"未覆盖本维度典型错误"
            dim_specific_errors = pipi_api._extract_dim_specific_errors(dim_check.get("specific", []))
            if dim_specific_errors:
                flags_lower = failure_flags.lower()
                has_specific = any(err in flags_lower for err in dim_specific_errors)
                if not has_specific:
                    issues.append(f"failure_flags 未包含本维度专属错误（必须含至少 1 条：{dim_specific_errors}）")
        except Exception as e:
            print(f"[VALIDATE] failure_flags 冗余项检查异常: {e}", flush=True)

    return {
        "passed": len(issues) == 0,
        "issues": issues,
        "status": "passed" if len(issues) == 0 else ("warning" if len(issues) <= 2 else "failed")
    }



def _wait_for_quality_review(persona_id, max_wait_seconds=1800, check_interval=10, user_id=None):
    """
    等待用例审核完成且无不合格用例
    返回：审核通过的用例ID列表（passed + warning）
    user_id 隔离：scheduled_task 路径传入；路由路径走 _current_uid()。
    """
    if user_id is None:
        user_id = _current_uid()
    import time
    start_time = time.time()
    prev_total = 0

    while True:
        elapsed = time.time() - start_time

        conn = get_db_connection()

        # 查询该用户所有用例的审核状态（排除红队用例，按 user_id 隔离）
        rows = execute_query(conn,
            "SELECT id, quality_status FROM test_cases WHERE persona_id = %s AND (is_redteam = 0 OR is_redteam IS NULL) AND user_id = %s" if USE_MYSQL else
            "SELECT id, quality_status FROM test_cases WHERE persona_id = ? AND (is_redteam = 0 OR is_redteam IS NULL) AND user_id = ?",
            (persona_id, user_id), fetch_all=True)

        total = len(rows)

        # 统计各状态数量（draft 是新生成未审核，pending 是审核中）
        status_count = {"draft": 0, "pending": 0, "passed": 0, "warning": 0, "failed": 0, "needs_manual_review": 0}
        for r in rows:
            status = r["quality_status"] if isinstance(r, dict) else r[1]
            status = status or "draft"
            status_count[status] = status_count.get(status, 0) + 1

        draft = status_count.get("draft", 0)
        pending = status_count.get("pending", 0)
        failed = status_count.get("failed", 0)
        needs_manual = status_count.get("needs_manual_review", 0)
        passed = status_count.get("passed", 0)
        warning = status_count.get("warning", 0)

        print(f"[FULL FLOW] Review status: {passed} passed, {warning} warning, {failed} failed, {pending} pending, {draft} draft, {needs_manual} needs_manual, total={total} ({int(elapsed)}s elapsed)", flush=True)

        # 检查是否需要继续等待
        if total < prev_total:
            # 用例总数下降，说明有维度正在删除旧用例准备重生成，继续等待
            print(f"[FULL FLOW] Total decreased {prev_total} -> {total}, waiting for regeneration...", flush=True)
        elif _any_regen_running_for_persona(persona_id):
            # 即使 DB 显示 0 draft/0 pending/0 failed，只要 REGEN 线程还在跑就继续等
            # Why: REGEN 启动后 LLM 生成要数秒，这段时间 DB 仍是旧状态（全 reviewed），
            # 但 REGEN 完成后会 DELETE 旧 case + INSERT 新 case（draft 状态）。
            # 如果此刻误判 ready 启动 task worker，task worker 会把 110 条 (case_id, input_text)
            # 缓存到内存，后续 REGEN 删掉其中部分 case，导致 test_results orphan。
            print(f"[FULL FLOW] REGEN still running for {persona_id}, waiting...", flush=True)
        elif (draft > 0 or pending > 0) or failed > 0:
            # 还有 draft/pending（未审核完）或 failed（待 REGEN）→ 继续等
            pass
        elif (total >= prev_total or prev_total == 0):
            # 全部审核完成且总数稳定。返回前再查一次 REGEN，防返回与 REGEN 启动竞态
            if _any_regen_running_for_persona(persona_id):
                print(f"[FULL FLOW] REGEN started just after review completed, waiting...", flush=True)
            else:
                # needs_manual_review 不阻塞，因其不再自动变化
                if needs_manual > 0:
                    print(f"[FULL FLOW] {needs_manual} cases in needs_manual_review, returning passed cases anyway", flush=True)
                passed_rows = execute_query(conn,
                    "SELECT id FROM test_cases WHERE persona_id = %s AND quality_status IN ('passed', 'warning') AND (is_redteam = 0 OR is_redteam IS NULL) AND user_id = %s" if USE_MYSQL else
                    "SELECT id FROM test_cases WHERE persona_id = ? AND quality_status IN ('passed', 'warning') AND (is_redteam = 0 OR is_redteam IS NULL) AND user_id = ?",
                    (persona_id, user_id), fetch_all=True)
                conn.close()

                return [r["id"] if isinstance(r, dict) else r[0] for r in passed_rows] if passed_rows else []

        conn.close()

        # 更新上一次总数，继续等待
        prev_total = total

        if elapsed > max_wait_seconds:
            print(f"[FULL FLOW] Quality review timeout after {max_wait_seconds}s", flush=True)
            break

        time.sleep(check_interval)

    # 超时后返回当前已通过的用例（排除红队用例）
    conn = get_db_connection()
    passed_rows = execute_query(conn,
        "SELECT id FROM test_cases WHERE persona_id = %s AND quality_status IN ('passed', 'warning') AND (is_redteam = 0 OR is_redteam IS NULL) AND user_id = %s" if USE_MYSQL else
        "SELECT id FROM test_cases WHERE persona_id = ? AND quality_status IN ('passed', 'warning') AND (is_redteam = 0 OR is_redteam IS NULL) AND user_id = ?",
        (persona_id, user_id), fetch_all=True)
    conn.close()

    return [r["id"] if isinstance(r, dict) else r[0] for r in passed_rows] if passed_rows else []


# 记录每个 (persona_id, dimension_code) 的重试次数（已持久化到 DB，保留旧变量做兼容）
_dimension_retry_count = {}

# AUTO REGEN 并发互斥锁：{f"{persona_id}:{dim_code}": threading.Lock}
import threading as _threading_mod
_regen_locks = {}
_regen_locks_guard = _threading_mod.Lock()


def _get_regen_lock(key):
    """获取（或创建）指定 key 的重生成互斥锁"""
    lock = _regen_locks.get(key)
    if lock is not None:
        return lock
    with _regen_locks_guard:
        lock = _regen_locks.get(key)
        if lock is None:
            lock = _threading_mod.Lock()
            _regen_locks[key] = lock
        return lock


def _any_regen_running_for_persona(persona_id):
    """检查该 persona 是否有任意维度的 REGEN 正在运行（锁被持有即视为运行中）。

    Why: FULL FLOW 的 _wait_for_quality_review 只看 DB quality_status，看不到
    REGEN 异步线程的运行状态。REGEN 启动 LLM 生成（耗时数秒）但还没改 DB 时，
    DB 仍显示 0 pending/0 failed，FULL FLOW 会误判 ready 并启动 task worker，
    后续 REGEN 删除旧 case 导致 test_results 出现 orphan。

    跨 worker 检查：除了进程内 _regen_locks，还查 regen_locks 表
    （Gunicorn 多 worker 之间不共享进程内存）。
    """
    # 进程内检查
    prefix = persona_id + ":"
    with _regen_locks_guard:
        keys = [k for k in _regen_locks.keys() if k.startswith(prefix)]
    for k in keys:
        lock = _regen_locks[k]
        acquired = lock.acquire(blocking=False)
        if acquired:
            lock.release()
        else:
            return True

    # 跨 worker 检查：查 regen_locks 表是否有该 persona 的活跃锁
    try:
        conn = get_db_connection()
        rows = execute_query(conn,
            "SELECT lock_key FROM regen_locks WHERE lock_key LIKE %s AND expires_at > NOW()" if USE_MYSQL else
            "SELECT lock_key FROM regen_locks WHERE lock_key LIKE ? AND expires_at > datetime('now')",
            (prefix + "%",), fetch_all=True)
        conn.close()
        if rows:
            return True
    except Exception as e:
        print(f"[REGEN LOCK CHECK] DB query failed, fallback to in-process only: {e}", flush=True)
    return False


def _acquire_regen_lock_db(lock_key, ttl_seconds=600):
    """跨 worker REGEN 锁：INSERT regen_locks 行。成功返回 True，已存在返回 False。

    Why: Gunicorn 8 workers 之间 _regen_locks（进程内 dict）不共享，
    同一 (persona, dim) 的 REGEN 可能在不同 worker 并发执行，导致
    _any_regen_running_for_persona 跨 worker 检查失效。

    用 MySQL 表做互斥：INSERT 原子，UNIQUE KEY 保证唯一。
    expires_at + TTL 防死锁（worker 崩溃没释放也会自动过期）。
    """
    try:
        conn = get_db_connection()
        try:
            execute_query(conn,
                "INSERT INTO regen_locks (lock_key, expires_at) VALUES (%s, DATE_ADD(NOW(), INTERVAL %s SECOND))" if USE_MYSQL else
                "INSERT INTO regen_locks (lock_key, expires_at) VALUES (?, datetime('now', '+' || ? || ' seconds'))",
                (lock_key, ttl_seconds), fetch_one=True)
            conn.commit()
            return True
        except Exception:
            conn.rollback()
            return False
        finally:
            conn.close()
    except Exception as e:
        print(f"[REGEN LOCK ACQUIRE] DB failed, fallback to in-process: {e}", flush=True)
        return True


def _release_regen_lock_db(lock_key):
    """释放跨 worker REGEN 锁：DELETE regen_locks 行。"""
    try:
        conn = get_db_connection()
        try:
            execute_query(conn,
                "DELETE FROM regen_locks WHERE lock_key = %s" if USE_MYSQL else
                "DELETE FROM regen_locks WHERE lock_key = ?",
                (lock_key,), fetch_one=True)
            conn.commit()
        finally:
            conn.close()
    except Exception as e:
        print(f"[REGEN LOCK RELEASE] DB failed: {e}", flush=True)


# ─── 全局并发限流（MySQL 表计数） ──────────────────────────────
# 跨 worker 全局并发槽：LLM 调用 ≤16、目标 API ≤8、用户任务 ≤2/人
# Why: _EVAL_SEMAPHORE 是进程内信号量，gunicorn 16 worker × 8 = 128 并发评测，远超设计意图；
#      且 generate/execute 类 worker 完全无闸，burst 时打爆 LLM 代理或目标 API。
#      用 MySQL 表做全局计数（regen_locks 模式照搬），单条 UPDATE 靠行锁原子性。

def _acquire_slot(slot_type: str, max_count: int, ttl_seconds: int = 1800,
                  wait: bool = True, timeout: int = 300) -> bool:
    """获取并发槽。slot_type 全局（如 'llm_global'）或用户级（如 'user:3:task'）。

    实现：
    1. INSERT IGNORE 确保 slot 行存在（用户级 slot 首次使用时创建）
    2. 清理泄漏：updated_at 超过 ttl_seconds 的行 current_count 归零（worker 崩溃未 release 兜底）
    3. 原子 +1：UPDATE ... SET current_count = current_count + 1 WHERE current_count < max_count
       单条 UPDATE 靠 MySQL 行锁原子性，rowcount>0 表示成功
    4. 槽满时 sleep 2s 重试直到 timeout；wait=False 时立即返回 False

    异常时打日志 + 返回 True（降级放行，不阻塞业务）。
    """
    import time as _t
    deadline = _t.time() + timeout
    while True:
        try:
            conn = get_db_connection()
            try:
                # 确保 slot 存在
                execute_query(conn,
                    "INSERT IGNORE INTO concurrency_slots (slot_type, max_count, current_count) VALUES (%s, %s, 0)" if USE_MYSQL else
                    "INSERT OR IGNORE INTO concurrency_slots (slot_type, max_count, current_count) VALUES (?, ?, 0)",
                    (slot_type, max_count))
                # 清理泄漏（updated_at < NOW() - TTL）
                execute_query(conn,
                    "UPDATE concurrency_slots SET current_count = 0 WHERE slot_type = %s AND updated_at < DATE_SUB(NOW(), INTERVAL %s SECOND)" if USE_MYSQL else
                    "UPDATE concurrency_slots SET current_count = 0 WHERE slot_type = ? AND updated_at < datetime('now', '-' || ? || ' seconds')",
                    (slot_type, ttl_seconds))
                # 原子 +1
                cur = conn.cursor()
                if USE_MYSQL:
                    cur.execute("UPDATE concurrency_slots SET current_count = current_count + 1, updated_at = NOW() WHERE slot_type = %s AND current_count < max_count", (slot_type,))
                else:
                    cur.execute("UPDATE concurrency_slots SET current_count = current_count + 1, updated_at = datetime('now') WHERE slot_type = ? AND current_count < max_count", (slot_type,))
                ok = cur.rowcount > 0
                conn.commit()
                if ok:
                    return True
            finally:
                conn.close()
        except Exception as e:
            print(f"[SLOT ACQUIRE] {slot_type} DB failed, degrade to allow: {e}", flush=True)
            return True  # 降级放行，不阻塞业务
        if not wait or _t.time() >= deadline:
            return False
        _t.sleep(2)


def _release_slot(slot_type: str):
    """释放并发槽：current_count = GREATEST(0, current_count - 1)。"""
    try:
        conn = get_db_connection()
        try:
            execute_query(conn,
                "UPDATE concurrency_slots SET current_count = GREATEST(0, current_count - 1) WHERE slot_type = %s" if USE_MYSQL else
                "UPDATE concurrency_slots SET current_count = MAX(0, current_count - 1) WHERE slot_type = ?",
                (slot_type,))
            conn.commit()
        finally:
            conn.close()
    except Exception as e:
        print(f"[SLOT RELEASE] {slot_type} failed: {e}", flush=True)


def _get_retry_count(persona_id, dim_code):
    """从 DB 查询 (persona_id, dim_code) 的当前重试次数"""
    conn = get_db_connection()
    try:
        row = execute_query(conn,
            "SELECT retry_count FROM dimension_retry_counts WHERE persona_id = %s AND dimension_code = %s" if USE_MYSQL else
            "SELECT retry_count FROM dimension_retry_counts WHERE persona_id = ? AND dimension_code = ?",
            (persona_id, dim_code), fetch_one=True)
        return (row["retry_count"] if isinstance(row, dict) else row[0]) if row else 0
    except Exception as e:
        print(f"[RETRY COUNT] get error: {e}", flush=True)
        return 0
    finally:
        conn.close()


def _incr_retry_count(persona_id, dim_code, task_id=None):
    """DB 原子递增 (persona_id, dim_code) 的重试次数，返回递增后的值"""
    conn = get_db_connection()
    try:
        if USE_MYSQL:
            execute_query(conn,
                """INSERT INTO dimension_retry_counts (persona_id, dimension_code, retry_count, task_id)
                   VALUES (%s, %s, 1, %s)
                   ON DUPLICATE KEY UPDATE retry_count = retry_count + 1, task_id = VALUES(task_id)""",
                (persona_id, dim_code, task_id))
        else:
            # SQLite: INSERT OR REPLACE 需要先查再写
            row = execute_query(conn,
                "SELECT retry_count FROM dimension_retry_counts WHERE persona_id = ? AND dimension_code = ?",
                (persona_id, dim_code), fetch_one=True)
            new_count = (row["retry_count"] if isinstance(row, dict) else row[0]) + 1 if row else 1
            execute_query(conn,
                """INSERT INTO dimension_retry_counts (persona_id, dimension_code, retry_count, task_id)
                   VALUES (?, ?, ?, ?)
                   ON CONFLICT(persona_id, dimension_code) DO UPDATE SET retry_count = ?, task_id = ?""",
                (persona_id, dim_code, new_count, task_id, new_count, task_id))
        conn.commit()
        return _get_retry_count(persona_id, dim_code)
    except Exception as e:
        print(f"[RETRY COUNT] incr error: {e}", flush=True)
        return 0
    finally:
        conn.close()


def _reset_retry_count(persona_id, task_id=None):
    """任务开始时清零指定 persona 的所有维度重试计数"""
    conn = get_db_connection()
    try:
        if task_id:
            execute_query(conn,
                "DELETE FROM dimension_retry_counts WHERE persona_id = %s AND task_id = %s" if USE_MYSQL else
                "DELETE FROM dimension_retry_counts WHERE persona_id = ? AND task_id = ?",
                (persona_id, task_id))
        else:
            execute_query(conn,
                "DELETE FROM dimension_retry_counts WHERE persona_id = %s" if USE_MYSQL else
                "DELETE FROM dimension_retry_counts WHERE persona_id = ?",
                (persona_id,))
        conn.commit()
    except Exception as e:
        print(f"[RETRY COUNT] reset error: {e}", flush=True)
    finally:
        conn.close()


def async_review_cases(case_ids, auto_regenerate=True, regen_depth=0, user_id=None):
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
                    "SELECT * FROM test_cases WHERE id = %s AND user_id = %s" if USE_MYSQL else "SELECT * FROM test_cases WHERE id = ? AND user_id = ?",
                    (case_id, user_id), fetch_one=True)
                if not row:
                    continue
                case = row_to_dict(row)

                # 获取维度信息
                dim_row = execute_query(conn,
                    "SELECT * FROM test_dimensions WHERE dimension_code = %s AND target_api = %s" if USE_MYSQL else "SELECT * FROM test_dimensions WHERE dimension_code = ? AND target_api = ?",
                    (case.get("dimension_code"), persona_target_api), fetch_one=True)
                dim_info = row_to_dict(dim_row) if dim_row else {}

                # 获取用户事实
                persona_id = case.get("persona_id") or case.get("device_id")
                user_facts = []
                persona_target_api = "pipi"
                if persona_id:
                    fact_rows = execute_query(conn,
                        "SELECT category, fact_key, fact_value FROM user_facts WHERE persona_id = %s AND is_active = 1 AND user_id = %s ORDER BY id DESC" if USE_MYSQL else
                        "SELECT category, fact_key, fact_value FROM user_facts WHERE persona_id = ? AND is_active = 1 AND user_id = ? ORDER BY id DESC",
                        (persona_id, user_id), fetch_all=True)
                    if fact_rows:
                        user_facts = [row_to_dict(r) for r in fact_rows]
                    # 取 persona 的 target_api 以查对应玩偶人设
                    prow = execute_query(conn,
                        "SELECT target_api FROM personas WHERE id = %s AND user_id = %s" if USE_MYSQL else
                        "SELECT target_api FROM personas WHERE id = ? AND user_id = ?",
                        (persona_id, user_id), fetch_one=True)
                    if prow:
                        persona_target_api = row_to_dict(prow).get("target_api") or "pipi"

                # 获取玩偶人设（按用户绑定的 target_api 查）
                toy_persona = _get_toy_persona_by_target(persona_target_api)

                # 审核前置 pending：让 _wait_for_quality_review 能区分"已开始审核"vs"草稿未触达"
                # Why: 否则审核线程刚启动还未逐条处理时，draft>0 会让轮询误判"生成未触发审核"
                execute_query(conn,
                    "UPDATE test_cases SET quality_status = 'pending' WHERE id = %s AND user_id = %s" if USE_MYSQL else
                    "UPDATE test_cases SET quality_status = 'pending' WHERE id = ? AND user_id = ?",
                    (case_id, user_id))
                conn.commit()

                # LLM 复核
                llm_config = get_llm_config()
                result = pipi_api.review_case_quality(case, dim_info, user_facts, toy_persona, target_api=persona_target_api, **llm_config["case_review"])

                # 更新数据库
                execute_query(conn,
                    "UPDATE test_cases SET quality_status = %s, quality_score = %s, quality_issues = %s WHERE id = %s AND user_id = %s" if USE_MYSQL else
                    "UPDATE test_cases SET quality_status = ?, quality_score = ?, quality_issues = ? WHERE id = ? AND user_id = ?",
                    (result["status"], result.get("score"), json.dumps(result.get("issues", []), ensure_ascii=False), case_id, user_id))
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
                _auto_regenerate_failed_cases(reviewed_cases, regen_depth=regen_depth, user_id=user_id)

        except Exception as e:
            print(f"[QUALITY REVIEW ERROR] {e}", flush=True)
            import traceback
            traceback.print_exc()

    t = threading.Thread(target=_review_worker, daemon=True)
    t.start()
    return t


def _auto_regenerate_failed_cases(reviewed_cases, regen_depth=0, user_id=None):
    """自动重生成不合格用例（按 case 单条重生成，递归深度+DB计数双保险限制）

    改动:
    - warning 也纳入重生成（之前只 failed）
    - 按 case 单条重生成（之前整维度重生成，浪费 LLM 调用）
    """
    # 筛选不合格用例（failed 或 warning 状态都纳入）
    # Why: 之前只 failed 触发重生成，但 review_case_quality 后处理后大量 issues 非空的 case 被降为 warning
    #      如果只看 failed，warning 状态的问题 case 永远不会被纠正，问题积累
    failed_cases = [c for c in reviewed_cases if c["status"] in ("failed", "warning")]
    if not failed_cases:
        return

    # 按 case 单条重生成（之前是按维度整组重生成，浪费 LLM 调用且会删掉同维度其他合格 case）
    # Why: issues 通常针对单条 case（如 evaluation_points 带序号、failure_flags 冗余），
    #      整维度重生成会误伤同维度合格 case，且 LLM 调用次数 = count 而非 1
    for c in failed_cases:
        persona_id = c["persona_id"]
        dim_code = c["dimension_code"]
        case_id = c.get("case_id") or c.get("id")
        retry_key = f"{persona_id}:{dim_code}:{case_id}"

        # 双保险：递归深度 + DB 持久化计数（按维度计数，避免单维度无限重生成）
        db_retry_count = _get_retry_count(persona_id, dim_code)
        if regen_depth >= 2 or db_retry_count >= 4:
            print(f"[AUTO REGEN] {retry_key} exceeded limit (depth={regen_depth}, db_count={db_retry_count}), marking as needs_manual_review", flush=True)
            conn = get_db_connection()
            execute_query(conn,
                "UPDATE test_cases SET quality_status = %s WHERE id = %s AND user_id = %s" if USE_MYSQL else
                "UPDATE test_cases SET quality_status = ? WHERE id = ? AND user_id = ?",
                ("needs_manual_review", c["id"], user_id))
            conn.commit()
            conn.close()
            continue

        # 收集该 case 的问题作为反馈
        issues_feedback = []
        if c["issues"]:
            issues_feedback.append(f"- {case_id}: {'; '.join(c['issues'])}")

        conn = get_db_connection()

        # 查出要删除的旧用例 ID（单条）
        old_case_ids = [c["id"]]

        # 取 persona 的 target_api
        prow = execute_query(conn,
            "SELECT target_api FROM personas WHERE id = %s AND user_id = %s" if USE_MYSQL else "SELECT target_api FROM personas WHERE id = ? AND user_id = ?",
            (persona_id, user_id), fetch_one=True)
        regen_target_api = (row_to_dict(prow).get("target_api") if prow else None) or "pipi"

        # 获取维度信息
        dim_row = execute_query(conn,
            "SELECT * FROM test_dimensions WHERE dimension_code = %s AND target_api = %s" if USE_MYSQL else "SELECT * FROM test_dimensions WHERE dimension_code = ? AND target_api = ?",
            (dim_code, regen_target_api), fetch_one=True)
        dim_info = row_to_dict(dim_row) if dim_row else {}

        # 获取用户事实
        user_facts = []
        fact_rows = execute_query(conn,
            "SELECT category, fact_key, fact_value FROM user_facts WHERE persona_id = %s AND is_active = 1 AND user_id = %s ORDER BY id DESC LIMIT 30" if USE_MYSQL else
            "SELECT category, fact_key, fact_value FROM user_facts WHERE persona_id = ? AND is_active = 1 AND user_id = ? ORDER BY id DESC LIMIT 30",
            (persona_id, user_id), fetch_all=True)
        if fact_rows:
            user_facts = [row_to_dict(r) for r in fact_rows]

        # 获取用户角色信息（从 personas 表）
        persona = None
        persona_row = execute_query(conn,
            "SELECT * FROM personas WHERE id = %s AND user_id = %s" if USE_MYSQL else "SELECT * FROM personas WHERE id = ? AND user_id = ?",
            (persona_id, user_id), fetch_one=True)
        if persona_row:
            persona = row_to_dict(persona_row)

        # 获取玩偶人设（按用户绑定的 target_api 查）
        target_api = (persona or {}).get("target_api") or "pipi"
        toy_persona = _get_toy_persona_by_target(target_api)

        conn.close()

        # 递增 DB 计数（在调重生成前递增，避免并发漏计）
        new_db_count = _incr_retry_count(persona_id, dim_code)
        print(f"[AUTO REGEN] {retry_key} depth={regen_depth} db_count={new_db_count}, regenerating 1 case (issues: {len(issues_feedback)})", flush=True)

        # 重新生成单条用例（count=1，先重生后删，P1-5 原子化，P1-4 进程内锁防并发）
        _regenerate_dimension_with_feedback(
            persona_id=persona_id,
            dimension=dim_info,
            toy_persona=toy_persona,
            persona=persona,
            user_facts=user_facts,
            count=1,
            issues_feedback=issues_feedback,
            old_case_ids=old_case_ids,
            regen_depth=regen_depth,
            target_api=regen_target_api,
            user_id=user_id,
        )


def _regenerate_dimension_with_feedback(persona_id, dimension, toy_persona, persona, user_facts, count, issues_feedback, old_case_ids=None, regen_depth=0, target_api="pipi", user_id=None):
    """带反馈重新生成维度用例（P1-4 进程内锁防并发，P1-5 先重生后删原子化，P1-6 递归深度传递）"""
    import threading

    def _regen_worker():
        retry_key = f"{persona_id}:{dimension.get('dimension_code', '')}"
        lock = _get_regen_lock(retry_key)
        if not lock.acquire(blocking=False):
            print(f"[AUTO REGEN] {retry_key} already in progress (in-process), skip", flush=True)
            return
        # 跨 worker 锁：INSERT regen_locks，失败说明另一 worker 正在跑
        if not _acquire_regen_lock_db(retry_key):
            lock.release()
            print(f"[AUTO REGEN] {retry_key} already in progress (cross-worker), skip", flush=True)
            return
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
                target_api=target_api or dimension.get("target_api", "pipi"),
                **llm_config["case_regenerate"]
            )

            if not new_cases:
                print(f"[AUTO REGEN] {retry_key} depth={regen_depth} LLM returned empty, keep old cases", flush=True)
                return

            # 保存新用例 + 删旧用例（同事务，P1-5 原子化）
            conn = get_db_connection()
            new_case_ids = []
            dim_code = dimension.get("dimension_code", "")

            try:
                for case in new_cases:
                    base_case_id = case.get("case_id", f"{dim_code}-01")
                    final_case_id = _get_unique_case_id(conn, base_case_id)
                    case["case_id"] = final_case_id

                    new_id = _save_test_case(conn, case, persona_id=persona_id, device_id=persona_id, dimension_code=dim_code, user_id=user_id)
                    if new_id:
                        new_case_ids.append(new_id)

                # 新用例保存成功后，删旧用例
                if old_case_ids:
                    placeholders = ",".join(["%s"] * len(old_case_ids)) if USE_MYSQL else ",".join(["?"] * len(old_case_ids))
                    execute_query(conn,
                        f"DELETE FROM test_cases WHERE id IN ({placeholders})",
                        tuple(old_case_ids))

                conn.commit()
            except Exception as db_err:
                conn.rollback()
                print(f"[AUTO REGEN] {retry_key} DB error, old cases kept: {db_err}", flush=True)
                raise
            finally:
                conn.close()

            print(f"[AUTO REGEN] {retry_key} depth={regen_depth} regenerated {len(new_case_ids)} cases (deleted {len(old_case_ids or [])} old), triggering review", flush=True)

            # 触发新用例的审核（递归深度+1，P1-6 限制无限递归）
            if new_case_ids:
                async_review_cases(new_case_ids, auto_regenerate=True, regen_depth=regen_depth + 1, user_id=user_id)

        except Exception as e:
            print(f"[AUTO REGEN ERROR] {retry_key} depth={regen_depth}: {e}", flush=True)
            import traceback
            traceback.print_exc()
        finally:
            _release_regen_lock_db(retry_key)
            lock.release()

    t = threading.Thread(target=_regen_worker, daemon=True)
    t.start()


@app.route("/api/test_cases/review", methods=["POST"])
def trigger_case_review():
    """手动触发用例质量复核"""
    data = request.get_json() or {}
    case_ids = data.get("case_ids", [])
    persona_id = data.get("persona_id")
    uid = _current_uid()

    if not case_ids and persona_id:
        # 根据 persona_id 获取所有用例（排除红队，红队跳过常规审核，按 user_id 隔离）
        conn = get_db_connection()
        rows = execute_query(conn,
            "SELECT id FROM test_cases WHERE persona_id = %s AND (is_redteam = 0 OR is_redteam IS NULL) AND user_id = %s" if USE_MYSQL else
            "SELECT id FROM test_cases WHERE persona_id = ? AND (is_redteam = 0 OR is_redteam IS NULL) AND user_id = ?",
            (persona_id, uid), fetch_all=True)
        case_ids = [r["id"] if isinstance(r, dict) else r[0] for r in rows] if rows else []
        conn.close()

    if not case_ids:
        return jsonify({"error": "请提供 case_ids 或 persona_id"}), 400

    async_review_cases(case_ids, user_id=uid)
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

    # dialog_ids：JSON 数组（多轮对话每轮一个），格式化为 R1=xxx; R2=yyy
    dialog_ids_raw = result.get("dialog_ids") or ""
    dialog_ids_list = []
    if dialog_ids_raw:
        try:
            parsed = json.loads(dialog_ids_raw)
            if isinstance(parsed, list):
                dialog_ids_list = [str(x) for x in parsed if x]
        except (json.JSONDecodeError, TypeError):
            pass
    if dialog_ids_list:
        if len(dialog_ids_list) == 1:
            dialog_id_line = dialog_ids_list[0]
        else:
            dialog_id_line = "; ".join(f"R{i+1}={d}" for i, d in enumerate(dialog_ids_list))
    else:
        dialog_id_line = "-"

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
- DialogID: {dialog_id_line}
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

