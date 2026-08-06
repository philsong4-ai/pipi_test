"""
AI陪伴玩偶批量测试脚本
API: https://<DOLL_API_DOMAIN>/stream-toy/v1/chat/completions
- OpenAI 兼容格式
- SSE 流式响应
- 用户身份通过 system prompt 中的 device_id 标识（跨会话用同一 device_id）
- 无直接记忆验证，仅通过回复内容推断

用法：
  python3 test_pipi_batch.py --quick "你是谁？"
  python3 test_pipi_batch.py --batch test_cases.xlsx
"""

import re
import sys
import os
import time
from datetime import datetime
from typing import List, Dict, Optional

from pipi_api import call_pipi_stream, build_system_prompt

# ─── 多轮对话助手 ─────────────────────────────────

_ROUND_PATTERN = re.compile(r"【R(\d+)】(.*?)(?=\n【R\d+】|$)", re.DOTALL)


def _resolve_api(target_api: str = "pipi"):
    """从 web_admin 的 api_endpoints 表查询 url/key/headers/protocol，CLI 脚本兜底走 env。"""
    api_url, api_key, extra_headers, protocol = None, None, None, "openai"
    try:
        from web_admin import get_api_config_by_code
        api_url, api_key, extra_headers, protocol = get_api_config_by_code(target_api)
    except Exception as e:
        print(f"[WARN] 无法从 api_endpoints 查到 target_api={target_api}: {e}（走 env 兜底）")
    return api_url, api_key, extra_headers, protocol


def execute_multi_round_session(
    case_input: str,
    device_id: str = "TEST_DEV_001",
    target_api: str = "pipi"
) -> Dict:
    """
    执行一个测试用例（多轮对话），每轮依次发送，累积 messages。
    相邻轮次之间无间隔（同一会话内）。
    返回每轮的回复结果。
    """
    matches = _ROUND_PATTERN.findall(case_input)

    api_url, api_key, extra_headers, protocol = _resolve_api(target_api)
    system_prompt = build_system_prompt(device_id=device_id, target_api=target_api)
    messages = [{"role": "system", "content": system_prompt}]

    round_results = []
    full_history = []

    for round_num, content in matches:
        content = content.strip()
        if not content:
            continue

        messages.append({"role": "user", "content": content})
        full_history.append({"round": f"R{round_num}", "role": "user", "content": content})

        result = call_pipi_stream(
            messages.copy(), device_id=device_id,
            api_url=api_url, api_key=api_key, extra_headers=extra_headers, protocol=protocol
        )

        if result.get("full_text"):
            messages.append({"role": "assistant", "content": result["full_text"]})
            full_history.append({
                "round": f"R{round_num}",
                "role": "assistant",
                "content": result["full_text"],
                "time_ms": result.get("response_time_ms", 0)
            })

        round_results.append({
            "round": f"R{round_num}",
            "user_input": content,
            "pipi_response": result.get("full_text", ""),
            "response_time_ms": result.get("response_time_ms", 0)
        })

    return {
        "round_results": round_results,
        "full_history": full_history,
        "total_rounds": len(round_results),
        "total_time_ms": sum(r["response_time_ms"] for r in round_results if r["response_time_ms"] > 0)
    }


# ─── 跨会话测试 ─────────────────────────────────

class CrossSessionTest:
    """
    跨会话测试管理器。
    同一个 device_id 的多轮 API 调用构成跨会话测试。

    用法:
        tester = CrossSessionTest(device_id="TEST_DEV_001")
        tester.add_session("S1", [{"role": "user", "content": "我养了只猫"}])
        tester.add_session("S2", [{"role": "user", "content": "我的猫叫什么？"}])
        tester.export_results("cross_session.json")
    """

    def __init__(self, device_id: str, persona_name: str = "", target_api: str = "pipi"):
        self.device_id = device_id
        self.persona_name = persona_name
        self.target_api = target_api
        self.sessions = []

    def add_session(self, session_id: str, messages: List[Dict],
                    timestamp: Optional[str] = None):
        """执行一个会话并记录结果"""
        if timestamp is None:
            timestamp = datetime.now().isoformat()

        api_url, api_key, extra_headers = _resolve_api(self.target_api)
        system_prompt = build_system_prompt(device_id=self.device_id, target_api=self.target_api)
        current_messages = [{"role": "system", "content": system_prompt}]

        results = []
        for msg in messages:
            current_messages.append(msg)
            if msg["role"] == "user":
                result = call_pipi_stream(
                    current_messages.copy(), device_id=self.device_id,
                    api_url=api_url, api_key=api_key, extra_headers=extra_headers, protocol=protocol
                )
                if result.get("full_text"):
                    current_messages.append({
                        "role": "assistant",
                        "content": result["full_text"]
                    })
                results.append({
                    "user_input": msg["content"],
                    "pipi_response": result.get("full_text", ""),
                    "response_time_ms": result.get("response_time_ms", 0)
                })

        self.sessions.append({
            "session_id": session_id,
            "timestamp": timestamp,
            "results": results,
            "message_history": current_messages
        })

    def export_results(self, filepath: str):
        """导出跨会话测试结果"""
        output = {
            "device_id": self.device_id,
            "persona_name": self.persona_name,
            "total_sessions": len(self.sessions),
            "sessions": self.sessions
        }
        with open(filepath, "w", encoding="utf-8") as f:
            json.dump(output, f, ensure_ascii=False, indent=2)
        print(f"结果已导出: {filepath}")


# ─── 批量测试（Excel驱动）─────────────────────────

def batch_test_from_xlsx(
    xlsx_path: str,
    output_dir: str = "./results",
    device_id_prefix: str = "TEST_DEV"
):
    """
    从 xlsx 文件中读取测试用例并批量执行。
    使用 openpyxl 读取（服务器无 pandas）。
    """
    from openpyxl import load_workbook

    os.makedirs(output_dir, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")

    wb = load_workbook(xlsx_path, data_only=True)
    ws = wb.active

    # 读取表头
    headers = [cell.value for cell in ws[1]]

    all_results = []
    for row in ws.iter_rows(min_row=2, values_only=True):
        if row[0] is None:
            continue
        row_data = dict(zip(headers, row))
        case_id = str(row_data.get("case_id", ""))
        input_text = str(row_data.get("input_text", ""))
        device_id = row_data.get("device_id") or f"{device_id_prefix}_{len(all_results)+1:03d}"

        if not input_text or input_text == "None":
            print(f"[跳过] {case_id}: input_text 为空")
            continue

        print(f"[执行] {case_id} (device={device_id})...")

        start_time = time.time()
        result = execute_multi_round_session(input_text, device_id=device_id)
        elapsed_ms = round((time.time() - start_time) * 1000, 2)

        result_entry = {
            "case_id": case_id,
            "device_id": device_id,
            "persona": row_data.get("persona", ""),
            "dimension": row_data.get("dimension", ""),
            "total_time_ms": elapsed_ms,
            "rounds": result["round_results"],
            "timestamp": datetime.now().isoformat()
        }
        all_results.append(result_entry)
        print(f"  ✓ {len(result['round_results'])} 轮, {elapsed_ms}ms")

    # 保存结果
    result_file = os.path.join(output_dir, f"results_{timestamp}.json")
    with open(result_file, "w", encoding="utf-8") as f:
        json.dump(all_results, f, ensure_ascii=False, indent=2)

    # 生成统计
    stats = {
        "timestamp": timestamp,
        "total_cases": len(all_results),
        "avg_time_ms": round(
            sum(r["total_time_ms"] for r in all_results) / len(all_results), 2
        ) if all_results else 0,
        "min_time_ms": min(r["total_time_ms"] for r in all_results) if all_results else 0,
        "max_time_ms": max(r["total_time_ms"] for r in all_results) if all_results else 0,
        "per_case": [
            {"case_id": r["case_id"], "total_time_ms": r["total_time_ms"],
             "rounds": len(r["rounds"])}
            for r in all_results
        ]
    }
    stats_file = os.path.join(output_dir, f"stats_{timestamp}.json")
    with open(stats_file, "w", encoding="utf-8") as f:
        json.dump(stats, f, ensure_ascii=False, indent=2)

    print(f"\n======== 批量测试完成 ========")
    print(f"执行用例: {len(all_results)} 条")
    print(f"平均响应: {stats['avg_time_ms']}ms")
    print(f"结果文件: {result_file}")
    print(f"统计文件: {stats_file}")

    return all_results, stats


# ─── 单用例测试（快速调试用）────────────────────

def quick_test(messages_text: str, device_id: str = "TEST_DEV_001", target_api: str = "pipi"):
    """快速测试单条对话。"""
    api_url, api_key, extra_headers, protocol = _resolve_api(target_api)
    system_prompt = build_system_prompt(device_id=device_id, target_api=target_api)

    if messages_text.startswith("【R"):
        matches = _ROUND_PATTERN.findall(messages_text)
        if matches:
            # 只发第一轮
            _, first_content = matches[0]
            user_content = first_content.strip()
        else:
            user_content = messages_text
    else:
        user_content = messages_text

    single_messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_content}
    ]

    print(f"\n>>> [请求] device={device_id} target_api={target_api}")
    print(f">>> 用户: {user_content[:100]}")

    result = call_pipi_stream(
        single_messages, device_id=device_id,
        api_url=api_url, api_key=api_key, extra_headers=extra_headers, protocol=protocol
    )

    print(f"<<< 皮皮: {result.get('full_text', '')}")
    print(f"<<< 耗时: {result.get('response_time_ms', 0)}ms")
    return result


# ─── 主入口 ─────────────────────────────────────

if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--batch":
        xlsx_path = sys.argv[2] if len(sys.argv) > 2 else "test_cases.xlsx"
        batch_test_from_xlsx(xlsx_path)
    elif len(sys.argv) > 1 and sys.argv[1] == "--quick":
        msg = sys.argv[2] if len(sys.argv) > 2 else "你好呀"
        target_api = "pipi"
        if "--target-api" in sys.argv:
            idx = sys.argv.index("--target-api")
            if idx + 1 < len(sys.argv):
                target_api = sys.argv[idx + 1]
        quick_test(msg, target_api=target_api)
    else:
        print("=" * 50)
        print("AI陪伴玩偶测试脚本")
        print("=" * 50)
        print("\n用法:")
        print(f"  # 快速测试单条消息")
        print(f"  python3 {sys.argv[0]} --quick \"你是谁？\"")
        print()
        print(f"  # 快速测试多轮对话")
        print(f"  python3 {sys.argv[0]} --quick \"【R1】用户：今天心情不好\\n【R2】用户：工作好累\"")
        print()
        print(f"  # 批量执行 xlsx 中的测试用例")
        print(f"  python3 {sys.argv[0]} --batch test_cases.xlsx")
        print()
        print(">>> 执行默认连通性测试...")
        quick_test("你好，你是谁？")
