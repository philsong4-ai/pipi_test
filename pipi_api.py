"""
SuperHexa 皮皮 API 调用层
统一 API 调用、SSE 解析、system prompt 构建。
供 web_admin.py、test_pipi_batch.py、memory_test_runner.py 共同使用。
"""
import os
import re
import json
import time
import requests
from typing import List, Dict, Optional

# ─── 配置 ─────────────────────────────────────────
API_URL = "https://<DOLL_API_DOMAIN>/toy/v1/chat/completions"
API_KEY = os.environ.get("PIPIDE_API_KEY", "test-key")

HEADERS = {
    "Content-Type": "application/json",
    "Authorization": f"Bearer {API_KEY}"
}

# ─── LLM 配置（LiteLLM 代理）─────────────
EXTRACT_LLM_URL = os.environ.get("EXTRACT_LLM_URL", "https://<LLM_PROXY_DOMAIN>/v1/chat/completions")
EXTRACT_LLM_KEY = os.environ.get("EXTRACT_LLM_KEY", "")
# 用例生成、事实提取、用例评测 使用 qwen3.6-plus
EXTRACT_LLM_MODEL = os.environ.get("EXTRACT_LLM_MODEL", "qwen3.6-plus")
# 用例质量复核 使用 deepseek-v4-pro
REVIEW_LLM_MODEL = os.environ.get("REVIEW_LLM_MODEL", "deepseek-v4-pro")


# ─── SSE 解析 ─────────────────────────────────────

def parse_sse_buffer(buffer: str) -> tuple:
    """
    解析 SSE 文本缓冲区，提取所有 content 片段。
    返回 (content_list, remaining_buffer)。
    remaining_buffer 是不完整的最后一行（下一个 chunk 继续拼接）。
    """
    contents = []
    lines = buffer.split("\n")

    # 最后一行可能不完整，留到下一个 chunk
    remaining = lines[-1] if lines else ""
    complete_lines = lines[:-1]

    for line in complete_lines:
        line = line.strip()
        if not line or line == "[DONE]":
            continue

        json_str = None
        if line.startswith("data: "):
            json_str = line[6:]
        elif line.startswith("data:"):
            json_str = line[5:].strip()
        else:
            json_str = line

        if not json_str or json_str == "[DONE]":
            continue

        try:
            data = json.loads(json_str)
            for choice in data.get("choices", []):
                # SSE 流式格式: delta.content
                delta = choice.get("delta", {})
                content = delta.get("content", "")
                if content:
                    contents.append(content)
                # 非流式格式: message.content (SE-web 会返回这种)
                message = choice.get("message", {})
                msg_content = message.get("content", "")
                if msg_content:
                    contents.append(msg_content)
        except (json.JSONDecodeError, ValueError):
            continue

    return contents, remaining


# ─── 核心 API 调用 ─────────────────────────────────

def call_pipi_stream(
    messages: List[Dict],
    device_id: str = "TEST_DEV_001",
    timeout: int = 30,
    api_url: str = None,
    api_key: str = None,
    extra_headers: dict = None
) -> Dict:
    """
    调用皮皮流式 API，返回完整响应。

    返回:
        {
            "full_text": "完整回复文本（含 emotion 标签）",
            "response_time_ms": 响应时间(毫秒),
            "error": 错误信息(如有)
        }
    """
    # 确保 system message 中有 device_id
    has_device_id = False
    for msg in messages:
        if msg.get("role") == "system":
            content = msg.get("content", "")
            if "设备ID是" in content or "设备ID是" in content:
                has_device_id = True
                break

    if not has_device_id:
        system_content = messages[0].get("content", "") if messages and messages[0].get("role") == "system" else ""
        if messages and messages[0].get("role") == "system":
            messages[0]["content"] = system_content + f"，设备ID是{device_id}"
        else:
            messages.insert(0, {
                "role": "system",
                "content": f"你是皮皮，设备ID是{device_id}"
            })

    payload = {
        "model": "",
        "messages": messages,
        "stream": True
    }

    url = api_url or API_URL
    start_time = time.time()
    first_token_time = None  # TTFB
    full_text = ""
    sse_buffer = ""

    # 构建请求头，支持自定义 api_key 和额外请求头
    req_headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {api_key}" if api_key else HEADERS["Authorization"]
    }
    if extra_headers:
        req_headers.update(extra_headers)

    # DEBUG: 打印完整请求
    print(f"[API REQUEST] URL: {url}", flush=True)
    print(f"[API REQUEST] Headers: {json.dumps(req_headers, ensure_ascii=False)}", flush=True)
    print(f"[API REQUEST] Payload: {json.dumps(payload, ensure_ascii=False)}", flush=True)

    try:
        response = requests.post(
            url,
            headers=req_headers,
            json=payload,
            stream=True,
            timeout=timeout
        )
        response.encoding = "utf-8"

        # 使用 iter_lines 按行读取，更可靠地处理 SSE
        line_count = 0
        for line in response.iter_lines(decode_unicode=True):
            line_count += 1
            if not line:
                continue

            # DEBUG: 打印前3行原始数据
            if line_count <= 3:
                print(f"[SSE DEBUG] line {line_count}: {repr(line[:150])}")

            # 检查错误响应
            if '"code":500' in line or '"code":-1' in line:
                try:
                    err = json.loads(line)
                    return {
                        "full_text": "",
                        "response_time_ms": round((time.time() - start_time) * 1000, 2),
                        "error": "服务端错误: " + err.get("message", str(err))
                    }
                except (json.JSONDecodeError, ValueError):
                    pass

            # 解析 SSE data 行
            json_str = None
            if line.startswith("data: "):
                json_str = line[6:]
            elif line.startswith("data:"):
                json_str = line[5:].strip()
            else:
                json_str = line

            if json_str == "[DONE]":
                break  # SSE 流结束，主动退出循环
            if not json_str:
                continue

            try:
                data = json.loads(json_str)
                for choice in data.get("choices", []):
                    delta = choice.get("delta", {})
                    content = delta.get("content", "")
                    if content:
                        if first_token_time is None:
                            first_token_time = time.time() - start_time
                        full_text += content
                        # DEBUG: 打印提取到的content
                        if line_count <= 3:
                            print(f"[SSE DEBUG] extracted content: {repr(content[:50])}")
            except (json.JSONDecodeError, ValueError) as e:
                # DEBUG: 打印解析失败原因
                if line_count <= 3:
                    print(f"[SSE DEBUG] JSON parse error: {e}, json_str: {repr(json_str[:100])}")
                continue

        elapsed_ms = round((time.time() - start_time) * 1000, 2)
        ttfb_ms = round(first_token_time * 1000, 2) if first_token_time else None
        return {
            "full_text": full_text,
            "response_time_ms": elapsed_ms,
            "ttfb_ms": ttfb_ms,  # 首token时间
        }

    except requests.exceptions.Timeout:
        return {
            "full_text": "",
            "response_time_ms": -1,
            "error": f"Timeout after {timeout}s"
        }
    except Exception as e:
        return {
            "full_text": "",
            "response_time_ms": -1,
            "error": str(e)
        }


# ─── LLM 调用（事实提取/用例生成/评测）─────────────

def call_extract_llm(messages: List[Dict], timeout: int = 20, model: str = None, temperature: float = None, max_tokens: int = None) -> Dict:
    """
    调用 LLM（LiteLLM 代理）。
    OpenAI 兼容格式，非流式响应。
    model 参数可指定模型，默认使用 EXTRACT_LLM_MODEL。
    """
    if not EXTRACT_LLM_KEY:
        return {
            "full_text": "",
            "response_time_ms": -1,
            "error": "EXTRACT_LLM_KEY 未配置，请检查环境变量"
        }

    use_model = model or EXTRACT_LLM_MODEL
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {EXTRACT_LLM_KEY}"
    }
    payload = {
        "model": use_model,
        "messages": messages,
        "temperature": temperature if temperature is not None else 0.3,
        "max_tokens": max_tokens if max_tokens is not None else 8192,
    }

    start_time = time.time()
    try:
        response = requests.post(
            EXTRACT_LLM_URL,
            headers=headers,
            json=payload,
            timeout=timeout
        )
        elapsed_ms = round((time.time() - start_time) * 1000, 2)

        if response.status_code != 200:
            return {
                "full_text": "",
                "response_time_ms": elapsed_ms,
                "error": f"HTTP {response.status_code}: {response.text[:200]}"
            }

        data = response.json()
        content = ""
        if "choices" in data and len(data["choices"]) > 0:
            content = data["choices"][0].get("message", {}).get("content", "")

        return {
            "full_text": content,
            "response_time_ms": elapsed_ms,
        }

    except requests.exceptions.Timeout:
        return {
            "full_text": "",
            "response_time_ms": -1,
            "error": f"Timeout after {timeout}s"
        }
    except Exception as e:
        return {
            "full_text": "",
            "response_time_ms": -1,
            "error": str(e)
        }


def call_llm_simple(system_prompt: str, user_prompt: str, timeout: int = 30, model: str = None, temperature: float = None, max_tokens: int = None) -> str:
    """简单的 LLM 调用，返回纯文本内容。支持指定 model / temperature / max_tokens."""

    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt}
    ]
    result = call_extract_llm(messages, timeout=timeout, model=model, temperature=temperature, max_tokens=max_tokens)
    if result.get("error"):
        raise Exception(f"LLM调用失败: {result['error']}")
    return result.get("full_text", "")


# ─── 测试用例生成 ────────────────────────────────

def _format_toy_persona(toy_persona: Dict) -> str:
    """将玩偶人设格式化为 LLM prompt 用的结构化文本。

    从 DB 中 toy_persona 的 JSON 字段提取行为描述、边界规则等，
    让 LLM 在生成 expected_output/failure_flags 时准确把握角色风格。
    """
    if not toy_persona:
        return ""

    parts = []

    # 基础信息
    if toy_persona.get("name"):
        parts.append(f"名字: {toy_persona['name']}")
    if toy_persona.get("identity"):
        parts.append(f"身份定位: {toy_persona['identity']}")
    if toy_persona.get("core_belief"):
        parts.append(f"核心信念: {toy_persona['core_belief']}")

    # 性格特质（含 core/min/max 行为描述）
    traits = toy_persona.get("personality_traits")
    if traits:
        if isinstance(traits, str):
            traits = json.loads(traits)
        if traits:
            lines = ["性格特质:"]
            for t in traits:
                name = t.get("name", "")
                core = t.get("core", "")
                vmin = t.get("min", "")
                vmax = t.get("max", "")
                lines.append(f"  · {name}: {core}")
                if vmin:
                    lines.append(f"    太冷(不可): {vmin}")
                if vmax:
                    lines.append(f"    太过(不可): {vmax}")
            parts.append("\n".join(lines))

    # 行为原则（含说明和示例）
    principles = toy_persona.get("behavior_principles")
    if principles:
        if isinstance(principles, str):
            principles = json.loads(principles)
        if principles:
            lines = ["行为原则:"]
            for p in principles:
                name = p.get("name", "")
                desc = p.get("desc", "")
                example = p.get("example", "")
                lines.append(f"  · {name}: {desc}" + (f" (如: {example})" if example else ""))
            parts.append("\n".join(lines))

    # 情感边界（硬规则，不可突破）
    boundaries = toy_persona.get("emotion_boundaries")
    if boundaries:
        if isinstance(boundaries, str):
            boundaries = json.loads(boundaries)
        if boundaries:
            lines = ["情感边界（硬规则，不可突破）:"]
            for b in boundaries:
                rule = b.get("rule", "")
                if rule:
                    lines.append(f"  · {rule}")
            parts.append("\n".join(lines))

    # 说话风格
    style = toy_persona.get("speaking_style")
    if style:
        if isinstance(style, str):
            style = json.loads(style)
        if style:
            lines = ["说话风格:"]
            if style.get("tone"):
                lines.append(f"  语气: {style['tone']}")
            if style.get("length"):
                lines.append(f"  长度: {style['length']}")
            particles = style.get("particles")
            if particles:
                lines.append(f"  语气词: {', '.join(particles)}")
            redup = style.get("reduplication")
            if redup:
                lines.append(f"  叠词: {', '.join(redup)}")
            parts.append("\n".join(lines))

    # 禁用表达
    forbidden = toy_persona.get("forbidden_expressions")
    if forbidden:
        if isinstance(forbidden, str):
            forbidden = json.loads(forbidden)
        if forbidden:
            parts.append(f"禁用表达: {', '.join(forbidden)}")

    return "\n".join(parts)


def generate_test_cases(
    dimension: Dict,
    toy_persona: Dict,
    persona: Dict,
    user_facts: List[Dict],
    count: int = 5,
    model: str = None,
    temperature: float = None,
    max_tokens: int = None,
    timeout: int = 180
) -> List[Dict]:
    """
    调用 LLM 生成测试用例。

    参数:
        dimension: 测试维度信息 (dimension_code, dimension_name, test_points)
        toy_persona: 玩偶人设信息
        persona: 用户角色信息
        user_facts: 用户已知事实列表
        count: 生成数量

    返回:
        用例列表 [{"case_id", "scenario", "user_input", "expected_behavior", ...}]
    """
    dim_code = dimension.get("dimension_code", "")
    dim_name = dimension.get("dimension_name", "")
    test_points = dimension.get("test_points", "")

    toy_info = _format_toy_persona(toy_persona)

    persona_info = ""
    if persona:
        parts = []
        for k in ["name", "age", "occupation", "city", "interests"]:
            v = persona.get(k)
            if v:
                parts.append(f"{k}: {v}")
        persona_info = "\n".join(parts)

    facts_info = _format_facts_grouped(user_facts)

    # 解析测试点列表
    test_point_list = [p.strip() for p in test_points.split("、") if p.strip()] if test_points else []
    test_point_text = "\n".join([f"  {i+1}. {p}" for i, p in enumerate(test_point_list)])

    # 根据维度确定轮数要求（记忆类维度需要多轮）
    turns_requirement = ""
    if dim_code == "C1":
        turns_requirement = "\n5. 【轮数要求】input_text 必须包含 4-6 轮对话（【R1】到【R4】或更多），测试短期记忆需要先建立信息，隔几轮后再追问"
    elif dim_code == "C2":
        turns_requirement = "\n5. 【轮数要求】input_text 必须包含 3-5 轮对话，测试长期记忆需要先建立信息再引用"
    elif dim_code == "C3":
        turns_requirement = "\n5. 【轮数要求】input_text 必须包含 3-5 轮对话，展示用户画像如何影响回复风格"
    elif dim_code == "C4":
        turns_requirement = "\n5. 【轮数要求】input_text 必须包含 4-6 轮对话，展示偏好变化的过程"
    elif dim_code == "C5":
        turns_requirement = "\n5. 【轮数要求】input_text 必须包含 3-5 轮对话，先建立记忆再制造冲突"

    system_prompt = """你是一个AI陪伴产品的测试用例生成专家。

## 角色说明
- 被测对象：AI玩偶（下文"玩偶信息"描述的角色），需要评估其对话能力
- 模拟用户：测试中扮演的人类用户（下文"用户角色"描述的人物）

## 输出格式
返回JSON数组，每个用例包含以下字段：

```json
{
  "case_id": "A1-01",
  "test_point": "省略",
  "title": "用例标题（简短描述测试场景）",
  "priority": "P1",
  "input_text": "【R1】用户：第一轮输入\\n【R2】用户：第二轮输入",
  "expected_output": "加班到这么晚确实累…你家猫今天反正睡了一天，估计还在沙发上摊着呢～",
  "evaluation_points": "①共情加班疲惫 ②明确坦诚能力边界 ③自然融入已知事实 ④提供替代建议",
  "failure_flags": "忘记事实、说教、冷漠、越权承诺",
  "score_2_desc": "2分场景：严重不符合期望的表现",
  "score_6_desc": "6分场景：基本符合但有明显不足",
  "score_10_desc": "10分场景：完美符合期望的表现"
}
```

## 字段说明
- **expected_output**: 具体回复文本（10分标杆），不是行为原则列表。必须写成AI在对话中实际会说的话。结合玩偶的说话风格和用户已知事实，用自然的语气写出完整回复
- **evaluation_points**: 必须覆盖的关键评估点（分条列出），用于逐项检查AI回复是否达标。每条是具体的、可判断的行为点
- **score_2/6/10_desc**: 分级评分锚点，描述该分数段的表现特征，作为评分判断依据

## 格式要求
1. input_text 只包含用户输入，不包含AI回复。多轮对话用【R1】【R2】【R3】...格式标记每轮用户输入，单轮直接写内容
2. expected_output 必须是具体回复文本（模拟AI理想回复），严禁写成"1. xxx; 2. xxx"的行为列表
3. evaluation_points 用序号列出关键评估点，每条10字以内，清晰可判断
4. score_2/6/10_desc 必须具体描述该分数对应的表现，作为评分锚点

## 正确 vs 错误示例

expected_output 正确写法（具体回复文本）：
"加班到这么晚确实累瘫了…可惜我没法帮你点外卖送过去，要不你现在自己下一单？我可以陪你等。你家那个调皮的猫今天反正睡了一整天，估计还在沙发上摊着呢～"

expected_output 错误写法（行为原则列表，严禁使用）：
"1. 共情加班疲惫；2. 明确说明无法执行线下实体操作；3. 自然结合用户已知事实进行互动；4. 提供替代性建议"

## 重要提示
input_text 示例（正确）：
【R1】用户：今天带豆豆去公园了
【R2】用户：它玩得特别开心
【R3】用户：对了我刚才说带谁去公园来着？

input_text 示例（错误，不要这样写）：
【R1】用户：今天带豆豆去公园了
【R2】秋秋：豆豆肯定很开心  ← 错误！不要包含AI回复
【R3】用户：对啊"""

    user_prompt = f"""请为以下测试维度生成{count}个测试用例。

## 测试维度
- 代码: {dim_code}
- 名称: {dim_name}
- 测试点（必须全部覆盖）:
{test_point_text}

## 被测对象（AI玩偶）
{toy_info or "暂无"}

## 模拟用户
{persona_info or "暂无"}

## 用户已记录的事实（AI玩偶应该记住的信息）
{facts_info or "暂无"}

## 生成要求
1. 【测试点覆盖】每个测试点至少1个用例，确保全部覆盖
2. 【事实运用】至少1个用例必须结合"用户已记录的事实"设计场景，体现AI的记忆能力
3. 【禁止虚构】expected_output中引用的用户兴趣、习惯、偏好必须来自上方"用户已记录的事实"列表，严禁编造不存在的用户信息
4. 【角色一致】input_text要符合模拟用户的身份特征，expected_output要符合AI玩偶的人设、说话风格和语气
5. 【回复式输出】expected_output必须是具体回复文本（用玩偶口吻说出的话），严禁写成行为原则列表。evaluation_points才是评估点列表
6. 【评分锚点】score_2/6/10_desc 必须具体描述该分数对应的表现，能作为评分依据{turns_requirement}

输出纯JSON数组，无其它文字。"""

    max_retries = 3
    for attempt in range(max_retries):
        try:
            result_text = call_llm_simple(system_prompt, user_prompt, timeout=timeout, model=model, temperature=temperature, max_tokens=max_tokens)

            json_match = re.search(r'\[[\s\S]*\]', result_text)
            if json_match:
                cases = json.loads(json_match.group())
                for i, case in enumerate(cases):
                    if not case.get("case_id"):
                        case["case_id"] = f"{dim_code}-{i+1:02d}"
                return cases

            # JSON 解析失败
            if attempt < max_retries - 1:
                print(f"[CASE GEN] generate_test_cases {dim_code} JSON parse failed, retry {attempt+1}/{max_retries-1}", flush=True)
                time.sleep(3)
            else:
                print(f"[CASE GEN] generate_test_cases {dim_code} JSON parse failed after {max_retries} attempts", flush=True)
                return []
        except Exception as e:
            if attempt < max_retries - 1:
                print(f"[CASE GEN] generate_test_cases {dim_code} error: {e}, retry {attempt+1}/{max_retries-1}", flush=True)
                time.sleep(3)
            else:
                print(f"[CASE GEN] generate_test_cases {dim_code} error after {max_retries} attempts: {e}", flush=True)
                return []


def generate_test_cases_with_feedback(
    dimension: Dict,
    toy_persona: Dict,
    persona: Dict,
    user_facts: List[Dict],
    count: int = 5,
    issues_feedback: List[str] = None,
    model: str = None,
    temperature: float = None,
    max_tokens: int = None,
    timeout: int = 180
) -> List[Dict]:
    """
    带反馈重新生成测试用例（用于审核不合格后的自动重生成）
    复用原始 generate_test_cases 的 prompt 结构，仅增加失败原因反馈
    """
    dim_code = dimension.get("dimension_code", "")
    dim_name = dimension.get("dimension_name", "")
    test_points = dimension.get("test_points", "")

    # 玩偶信息（使用公共格式化函数）
    toy_info = _format_toy_persona(toy_persona)

    # 用户角色信息（与原函数相同）
    persona_info = ""
    if persona:
        parts = []
        for k in ["name", "age", "occupation", "city", "interests"]:
            v = persona.get(k)
            if v:
                parts.append(f"{k}: {v}")
        persona_info = "\n".join(parts)

    # 用户事实
    facts_info = _format_facts_grouped(user_facts)

    # 测试点
    test_point_list = [p.strip() for p in test_points.split("、") if p.strip()] if test_points else []
    test_point_text = "\n".join([f"  {i+1}. {p}" for i, p in enumerate(test_point_list)])

    # 轮数要求（与原函数相同）
    turns_requirement = ""
    if dim_code == "C1":
        turns_requirement = "\n6. 【轮数要求】input_text 必须包含 4-6 轮对话（【R1】到【R4】或更多），测试短期记忆需要先建立信息，隔几轮后再追问"
    elif dim_code == "C2":
        turns_requirement = "\n6. 【轮数要求】input_text 必须包含 3-5 轮对话，测试长期记忆需要先建立信息再引用"
    elif dim_code == "C3":
        turns_requirement = "\n6. 【轮数要求】input_text 必须包含 3-5 轮对话，展示用户画像如何影响回复风格"
    elif dim_code == "C4":
        turns_requirement = "\n6. 【轮数要求】input_text 必须包含 4-6 轮对话，展示偏好变化的过程"
    elif dim_code == "C5":
        turns_requirement = "\n6. 【轮数要求】input_text 必须包含 3-5 轮对话，先建立记忆再制造冲突"

    # 构建问题反馈部分（新增）
    feedback_section = ""
    if issues_feedback:
        feedback_section = f"""

## 之前生成的用例问题（请避免重复这些错误）
{chr(10).join(issues_feedback)}
"""

    # System Prompt（与原函数完全相同）
    system_prompt = """你是一个AI陪伴产品的测试用例生成专家。

## 角色说明
- 被测对象：AI玩偶（下文"玩偶信息"描述的角色），需要评估其对话能力
- 模拟用户：测试中扮演的人类用户（下文"用户角色"描述的人物）

## 输出格式
返回JSON数组，每个用例包含以下字段：

```json
{
  "case_id": "A1-01",
  "test_point": "省略",
  "title": "用例标题（简短描述测试场景）",
  "priority": "P1",
  "input_text": "【R1】用户：第一轮输入\\n【R2】用户：第二轮输入",
  "expected_output": "加班到这么晚确实累…你家猫今天反正睡了一天，估计还在沙发上摊着呢～",
  "evaluation_points": "①共情加班疲惫 ②明确坦诚能力边界 ③自然融入已知事实 ④提供替代建议",
  "failure_flags": "忘记事实、说教、冷漠、越权承诺",
  "score_2_desc": "2分场景：严重不符合期望的表现",
  "score_6_desc": "6分场景：基本符合但有明显不足",
  "score_10_desc": "10分场景：完美符合期望的表现"
}
```

## 字段说明
- **expected_output**: 具体回复文本（10分标杆），不是行为原则列表。必须写成AI在对话中实际会说的话。结合玩偶的说话风格和用户已知事实，用自然的语气写出完整回复
- **evaluation_points**: 必须覆盖的关键评估点（分条列出），用于逐项检查AI回复是否达标。每条是具体的、可判断的行为点
- **score_2/6/10_desc**: 分级评分锚点，描述该分数段的表现特征，作为评分判断依据

## 格式要求
1. input_text 只包含用户输入，不包含AI回复。多轮对话用【R1】【R2】【R3】...格式标记每轮用户输入，单轮直接写内容
2. expected_output 必须是具体回复文本（模拟AI理想回复），严禁写成"1. xxx; 2. xxx"的行为列表
3. evaluation_points 用序号列出关键评估点，每条10字以内，清晰可判断
4. score_2/6/10_desc 必须具体描述该分数对应的表现，作为评分锚点

## 正确 vs 错误示例

expected_output 正确写法（具体回复文本）：
"加班到这么晚确实累瘫了…可惜我没法帮你点外卖送过去，要不你现在自己下一单？我可以陪你等。你家那个调皮的猫今天反正睡了一整天，估计还在沙发上摊着呢～"

expected_output 错误写法（行为原则列表，严禁使用）：
"1. 共情加班疲惫；2. 明确说明无法执行线下实体操作；3. 自然结合用户已知事实进行互动；4. 提供替代性建议"

## 重要提示
input_text 示例（正确）：
【R1】用户：今天带豆豆去公园了
【R2】用户：它玩得特别开心
【R3】用户：对了我刚才说带谁去公园来着？

input_text 示例（错误，不要这样写）：
【R1】用户：今天带豆豆去公园了
【R2】秋秋：豆豆肯定很开心  ← 错误！不要包含AI回复
【R3】用户：对啊"""

    # User Prompt（与原函数相同，增加反馈部分）
    user_prompt = f"""请为以下测试维度生成{count}个测试用例。

## 测试维度
- 代码: {dim_code}
- 名称: {dim_name}
- 测试点（必须全部覆盖）:
{test_point_text}

## 被测对象（AI玩偶）
{toy_info or "暂无"}

## 模拟用户
{persona_info or "暂无"}

## 用户已记录的事实（AI玩偶应该记住的信息）
{facts_info or "暂无"}
{feedback_section}
## 生成要求
1. 【测试点覆盖】每个测试点至少1个用例，确保全部覆盖
2. 【事实运用】至少1个用例必须结合"用户已记录的事实"设计场景，体现AI的记忆能力
3. 【禁止虚构】expected_output中引用的用户兴趣、习惯、偏好必须来自上方"用户已记录的事实"列表，严禁编造不存在的用户信息
4. 【角色一致】input_text要符合模拟用户的身份特征，expected_output要符合AI玩偶的人设、说话风格和语气
5. 【回复式输出】expected_output必须是具体回复文本（用玩偶口吻说出的话），严禁写成行为原则列表。evaluation_points才是评估点列表
6. 【评分锚点】score_2/6/10_desc 必须具体描述该分数对应的表现，能作为评分依据{turns_requirement}

输出纯JSON数组，无其它文字。"""

    try:
        print(f"[CASE GEN WITH FEEDBACK] {dim_code} generating {count} cases model={model}", flush=True)
        result_text = call_llm_simple(system_prompt, user_prompt, timeout=timeout, model=model, temperature=temperature, max_tokens=max_tokens)

        json_match = re.search(r'\[[\s\S]*\]', result_text)
        if json_match:
            cases = json.loads(json_match.group())
            for i, case in enumerate(cases):
                if not case.get("case_id"):
                    case["case_id"] = f"{dim_code}-{i+1:02d}"
            return cases
        return []
    except Exception as e:
        print(f"[CASE GEN WITH FEEDBACK] error: {e}")
        return []


# ─── 日期辅助 ────────────────────────────────────

def _date_offset(days):
    t = time.time() + days * 86400
    return time.strftime("%Y-%m-%d", time.localtime(t))

def _yesterday_date():
    return _date_offset(-1)

def _tomorrow_date():
    return _date_offset(1)


# ─── System Prompt 构建 ───────────────────────────

def build_system_prompt(persona_data: Optional[Dict] = None, device_id: str = "") -> str:
    """
    构建 system prompt。
    玩偶接口自己管理用户画像，这里只传设备ID。
    """
    return f"你是皮皮，设备ID是{device_id}"


# ─── 从聊天中提取事实 ───────────────────────────

# 严格 category → fact_key 映射，LLM 只能使用这些组合
# 支持带实体名后缀的 key，如 pet_豆豆, friend_小明，避免多实体覆盖
VALID_CATEGORIES = {
    "pet": {"name", "breed", "behavior", "health", "age", "personality"},  # 支持 pet_豆豆.breed 等
    "food": {"drink", "favorite", "dislike", "restriction", "habit"},
    "event": {"type", "time", "place", "person", "result", "feeling"},
    "emotion": {"state", "trigger", "duration"},
    "preference": {"drink", "food", "color", "brand", "style", "activity", "music", "movie"},
    "attitude": {"pet_feeling", "work", "life", "relationship", "money"},
    "relationship": {"friend", "friend_general", "family", "colleague", "partner", "feeling"},  # friend=具体朋友(需entity_name), friend_general=通用交友状况
    "health": {"condition", "symptom", "treatment", "habit", "sleep", "exercise"},
    "work": {"occupation", "company", "department", "schedule", "colleague", "stress", "income", "commute"},
    "living": {"housing", "roommate", "location", "commute", "city", "hometown"},
    "family": {"father", "mother", "sibling", "child", "spouse", "dynamic", "background"},
    "hobby": {"entertainment", "sport", "creative", "social", "game", "reading", "travel"},
    "milestone": {"birthday", "anniversary", "achievement", "graduation", "job_change"},
}

CATEGORY_NAMES = {
    "pet": "宠物", "food": "饮食", "event": "事件", "emotion": "情绪",
    "preference": "偏好", "attitude": "态度", "relationship": "人际",
    "health": "健康", "work": "工作", "living": "生活", "family": "家庭",
    "hobby": "爱好", "milestone": "里程碑",
}

def _format_facts_grouped(user_facts: List[Dict]) -> str:
    """按 category 分组格式化用户事实，优化 LLM 阅读效率"""
    if not user_facts:
        return "暂无已知信息"

    grouped = {}
    for f in user_facts:
        cat = f.get("category", "other")
        if cat not in grouped:
            grouped[cat] = []
        grouped[cat].append(f)

    lines = []
    for cat in sorted(grouped.keys()):
        cat_name = CATEGORY_NAMES.get(cat, cat)
        lines.append(f"\n【{cat_name}】{cat}")
        for f in grouped[cat]:
            entity = f.get("entity_name", "")
            en_tag = f"[{entity}]" if entity else ""
            lines.append(f"  - {cat}.{f.get('fact_key','')}{en_tag}: {f.get('fact_value','')}")

    return "\n".join(lines).lstrip("\n")


def extract_facts_from_message(user_message: str, persona_data: Optional[Dict], existing_facts: Optional[List[Dict]] = None, chat_history: Optional[List[Dict]] = None, model: str = None, temperature: float = None, max_tokens: int = None, timeout: int = 60) -> List[Dict]:
    """
    调用 LLM 从用户消息中提取事实。

    返回格式: [{"category", "fact_key", "fact_value", "occurred_at", "emotion_tag", "related_to"}]
    """
    today = time.strftime("%Y-%m-%d", time.localtime())
    existing_text = ""
    if existing_facts:
        lines = []
        for ef in existing_facts:
            entity = ef.get("entity_name", "")
            en_tag = "[%s]" % entity if entity else ""
            lines.append("id=%s: %s.%s%s = %s" % (ef["id"], ef["category"], ef["fact_key"], en_tag, ef["fact_value"]))
        if lines:
            existing_text = "\n已有事实（entity_name 区分不同实例，如 food.habit[咖啡] 和 food.habit[晚餐] 是两条独立事实）:\n" + "\n".join(lines)

    persona_text = ""
    if persona_data:
        parts = []
        for k in ["name", "occupation", "city", "interests", "spending_style",
                   "family_status", "education", "language_style"]:
            v = persona_data.get(k, "")
            if v:
                parts.append(k + ": " + str(v))
        if parts:
            persona_text = "\n已知用户信息:\n" + "\n".join(parts)

    history_text = ""
    if chat_history:
        lines = []
        for msg in chat_history[-10:]:
            if isinstance(msg, str):
                lines.append(msg[:100])
            elif isinstance(msg, dict):
                role = msg.get("role", "")
                text = msg.get("text", "")[:100]
                if role and text:
                    lines.append(f"{role}: {text}")
        if lines:
            history_text = "\n近期对话:\n" + "\n".join(lines)

    system = (
        "你是事实提取器。从人类用户的消息中提取关于该用户的长期事实。\n"
        "重要说明:\n"
        "- 这是人类用户与AI玩偶的对话，你要提取的是【人类用户】的事实\n"
        "- 消息中的'我'指的是人类用户，不是AI玩偶\n"
        "- fact_value 中不要出现'玩偶'这个词，直接描述用户的情况\n"
        "- category 和 fact_key 字段必须使用下方列出的英文键名，不要用中文！\n"
        "\n"
        "【严格键映射】每个category使用对应的英文fact_key:\n"
        "  pet(宠物): name, breed, behavior, health, age, personality\n"
        "  food(饮食): drink, favorite, dislike, restriction, habit\n"
        "  event(事件): type, time, place, person, result, feeling\n"
        "  emotion(情绪): state, trigger, duration\n"
        "  preference(偏好): drink, food, color, brand, style, activity, music, movie\n"
        "  attitude(态度): pet_feeling, work, life, relationship, money\n"
        "  relationship(人际): friend(具体朋友,需entity_name), friend_general(通用交友状况), family, colleague, partner, feeling\n"
        "  health(健康): condition, symptom, treatment, habit, sleep, exercise\n"
        "  work(工作): occupation, company, department, schedule, colleague, stress, income, commute\n"
        "  living(生活): housing, roommate, location, commute, city, hometown\n"
        "  family(家庭): father, mother, sibling, child, spouse, dynamic, background\n"
        "  hobby(爱好): entertainment, sport, creative, social, game, reading, travel\n"
        "  milestone(里程碑): birthday, anniversary, achievement, graduation, job_change\n"
        "\n"
        "【多实体处理】当涉及具体的人/宠物/事件/习惯时，用 entity_name 字段标识:\n"
        "  例: 用户有宠物豆豆\n"
        "  - {category:\"pet\", fact_key:\"breed\", entity_name:\"豆豆\", fact_value:\"英短猫\"}\n"
        "  - {category:\"pet\", fact_key:\"age\", entity_name:\"豆豆\", fact_value:\"3岁\"}\n"
        "  例: 用户有朋友小明\n"
        "  - {category:\"relationship\", fact_key:\"friend\", entity_name:\"小明\", fact_value:\"大学同学，认识5年\"}\n"
        "  例: 用户描述自己交友状况\n"
        "  - {category:\"relationship\", fact_key:\"friend_general\", entity_name:\"\", fact_value:\"朋友不多但都交心\"}\n"
        "\n"
        "【多实例字段 — entity_name 强制必填】以下字段会出现多个不同值，每条必须用 entity_name 区分:\n"
        "  - food.habit → entity_name=具体习惯简称 (如\"咖啡\"、\"晚餐\"、\"宵夜\")\n"
        "  - food.favorite → entity_name=食物名称 (如\"火锅\"、\"寿司\")\n"
        "  - food.dislike → entity_name=不喜欢的食物\n"
        "  - health.habit → entity_name=习惯简称 (如\"跑步\"、\"瑜伽\"、\"熬夜\")\n"
        "  - health.condition → entity_name=病症/状态简称 (如\"过敏\"、\"胃炎\")\n"
        "  - health.symptom → entity_name=症状简称\n"
        "  - preference.* → entity_name=偏好具体项目 (如 preference.activity[\"游泳\"])\n"
        "  - hobby.* → entity_name=爱好具体项目 (如 hobby.sport[\"篮球\"])\n"
        "  - emotion.state → entity_name=情绪标签 (如\"开心\"、\"焦虑\"，同一用户多次提取不同情绪)\n"
        "  - work.schedule → entity_name=日程内容简称 (如\"加班\"、\"日常\"、\"出差\"、\"开会\")\n"
        "  - relationship.friend → entity_name=朋友名字 (如\"老张\"、\"小美\")，通用交友状况用 friend_general\n"
        "  - relationship.family → entity_name=家庭成员称呼 (如\"妈妈\"、\"姐姐\")，通用家庭状况用 family.dynamic\n"
        "  - relationship.colleague → entity_name=同事名字/称呼 (如\"王经理\")\n"
        "\n"
        "  例: 用户说\"我每天早上喝咖啡，晚上不吃碳水\"\n"
        "  - {category:\"food\", fact_key:\"habit\", entity_name:\"咖啡\", fact_value:\"每天早上喝一杯\"}\n"
        "  - {category:\"food\", fact_key:\"habit\", entity_name:\"晚餐\", fact_value:\"晚上不吃碳水\"}\n"
        "  例: 无特定实体时 entity_name 留空\n"
        "  - {category:\"work\", fact_key:\"occupation\", entity_name:\"\", fact_value:\"程序员\"}\n"
        "\n"
        "【提取规则】\n"
        "1. 只提取明确陈述的事实，不推测、不推断\n"
        "2. fact_value 必须是完整的自然语言描述，包含关键细节\n"
        "3. 同一消息中的多条独立信息，分别提取为多条事实\n"
        "4. 情绪提取: 有明确情绪表达或转折时才提取emotion，单纯分享快乐不提取\n"
        "5. 事件关联: 消息中提到的多个实体(人/物/地点)，分别提取，并通过related_to互相关联\n"
        "\n"
        "【occurred_at 规则】\n"
        "  - '今天'→%s, '昨天'→%s(昨天日期), '明天'→%s\n"
        "  - '下周一'→具体日期, '最近/前几天'→'recent', 无时间信息→'unknown'\n"
        "  - 保留精确日期如'3月15日'→'2026-03-15'\n"
        "\n"
        "【emotion_tag 规则】\n"
        "  - 优先具体中文词: 开心/疲惫/委屈/焦虑/期待/生气/难过/放松/感动/惊喜\n"
        "  - 无法判断具体情感时用: positive/negative/neutral\n"
        "\n"
        "【related_to 规则】\n"
        "  - 同实体关联: 如消息提到'乐高'(宠物名)，匹配已有事实中 name=乐高 的id\n"
        "  - 因果关系: 如'加班导致累'，event事实关联emotion事实\n"
        "  - 时间连续: 同一事件的多个方面互相关联\n"
        "  - 填写已有事实的id数字数组，如 [5,12]\n"
        + (persona_text if persona_text else "")
        + (existing_text if existing_text else "")
        + (history_text if history_text else "")
    ) % (today, _yesterday_date(), _tomorrow_date())

    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": (
            "请从以下消息中提取事实，返回JSON数组。没有可提取的事实时返回空数组[]。\n"
            "消息: " + user_message + "\n"
            "注意: category和fact_key必须用英文！\n"
            "返回格式: [{\"category\":\"pet\",\"fact_key\":\"breed\",\"entity_name\":\"豆豆\",\"fact_value\":\"英短猫\",\"occurred_at\":\"\",\"emotion_tag\":\"\",\"related_to\":[]}]\n"
            "entity_name: 具体的人名/宠物名/事件名，无特定实体时留空字符串"
        )}
    ]

    try:
        # 使用专用 LLM 提取事实
        result = call_extract_llm(messages, timeout=timeout, model=model, temperature=temperature, max_tokens=max_tokens)
        text = result.get("full_text", "").strip()
        error = result.get("error")
        used_model = model or EXTRACT_LLM_MODEL
        print(f"[FACT LLM] model: {used_model} len: {len(text)} error: {error}", flush=True)
        print("[FACT LLM RAW]", repr(text[:200]) if text else "empty", flush=True)
        if not text:
            return []
        match = re.search(r'\[.*\]', text, re.DOTALL)
        if match:
            raw_text = match.group()
            try:
                facts = json.loads(raw_text)
            except (json.JSONDecodeError, ValueError):
                return []
            if isinstance(facts, list):
                validated = []
                rejected = []
                for f in facts:
                    cat = f.get("category", "")
                    key = f.get("fact_key", "")
                    val = f.get("fact_value", "")
                    if not val or not cat or not key:
                        rejected.append(("missing_field", f))
                        continue
                    valid_keys = VALID_CATEGORIES.get(cat)
                    if valid_keys is None or key not in valid_keys:
                        rejected.append(("invalid_key", f))
                        continue
                    validated.append({
                        "category": cat,
                        "fact_key": key,
                        "entity_name": f.get("entity_name", ""),
                        "fact_value": val,
                        "occurred_at": f.get("occurred_at", ""),
                        "emotion_tag": f.get("emotion_tag", ""),
                        "related_to": f.get("related_to", []),
                    })
                if rejected:
                    print("[FACT REJECTED]", json.dumps(rejected, ensure_ascii=False)[:200])
                return validated
        return []
    except Exception:
        return []


# ─── 回复评测 ───────────────────────────────────

def evaluate_reply(
    user_input: str,
    ai_reply: str,
    dimension_code: str,
    dimension_name: str,
    standard: str,
    persona_data: Optional[Dict] = None,
    model: str = None,
    temperature: float = None,
    max_tokens: int = None,
    timeout: int = 30
) -> Dict:
    """
    评测单条AI回复。

    返回: {"score": 1-10, "reason": "评分理由"}
    """
    persona_text = ""
    if persona_data:
        parts = []
        for k in ["name", "occupation", "city", "interests", "spending_style",
                   "family_status", "education", "language_style"]:
            v = persona_data.get(k, "")
            if v:
                parts.append(f"{k}: {v}")
        if parts:
            persona_text = "\n用户画像:\n" + "\n".join(parts)

    system = (
        "你是AI陪伴对话质量评测专家。\n"
        "评测维度: %s (%s)\n"
        "评分标准:\n%s\n"
        "%s\n"
        "评分规则:\n"
        "- 10分制，直接打整数分\n"
        "- 四舍五入到整数\n"
        "- 扣分必须写明原因\n"
        "- 返回JSON格式: {\"score\": 分数, \"reason\": \"理由\"}"
    ) % (dimension_name, dimension_code, standard, persona_text)

    user = (
        "用户输入: %s\n"
        "AI回复: %s\n"
        "请评分:"
    ) % (user_input, ai_reply)

    try:
        result_text = call_llm_simple(system, user, timeout=timeout, model=model, temperature=temperature, max_tokens=max_tokens)
        if not result_text:
            return {"score": 0, "reason": "评测LLM无响应"}

        # 解析JSON
        match = re.search(r'\{[^}]+\}', result_text)
        if match:
            data = json.loads(match.group())
            score = data.get("score", 0)
            reason = data.get("reason", "")
            return {"score": int(round(score)), "reason": reason}

        return {"score": 0, "reason": f"无法解析评测结果: {result_text[:100]}"}
    except Exception as e:
        return {"score": 0, "reason": f"评测异常: {str(e)}"}


def evaluate_test_case(case_data: Dict, user_facts: List[Dict] = None, model: str = None, temperature: float = None, max_tokens: int = None, timeout: int = 60) -> Dict:
    """
    评测单个测试用例，使用用例自带的评分参考。

    case_data: {
        "case_id": 用例编号,
        "dimension_code": 维度代码,
        "input_text": 用户输入,
        "actual_output": AI实际回复,
        "expected_output": 期望回复（参考）,
        "failure_flags": 扣分点/失败标志,
        "score_2_desc": 2分表现描述,
        "score_6_desc": 6分表现描述,
        "score_10_desc": 10分表现描述,
    }

    user_facts: 用户已知事实列表，用于判断AI引用记忆 vs 幻觉

    返回: {
        "score": 1-10分,
        "deduction_reason": "扣分原因",
        "status": "passed" 或 "failed"
    }
    """
    case_id = case_data.get("case_id", "")
    dimension_code = case_data.get("dimension_code", "")
    test_point = case_data.get("test_point", "")
    input_text = case_data.get("input_text", "")
    actual_output = case_data.get("actual_output", "")
    expected_output = case_data.get("expected_output", "")
    failure_flags = case_data.get("failure_flags", "")
    score_2_desc = case_data.get("score_2_desc", "")
    score_6_desc = case_data.get("score_6_desc", "")
    score_10_desc = case_data.get("score_10_desc", "")

    facts_text = _format_facts_grouped(user_facts) if user_facts else ""

    dim_header = f"【评测维度】{dimension_code}"
    if test_point:
        dim_header += f" - {test_point}"

    system_prompt = f"""你是AI陪伴对话质量评测专家。请根据以下评分标准对AI回复进行评测。

{dim_header}

【评分参考】
- 2分（差）: {score_2_desc}
- 6分（中）: {score_6_desc}
- 10分（优秀）: {score_10_desc}

【扣分点/失败标志】
{failure_flags}

【期望回复参考】
{expected_output}

【已知用户信息】
{facts_text or "暂无"}

【评分规则】
- 10分制，直接打整数分（1-10）
- 对比实际回复与期望回复，结合评分参考打分
- 如果触及扣分点，必须扣分并说明原因
- 重要：AI引用已知用户信息中的事实不算幻觉，只有捏造新事实才算幻觉
- 返回JSON格式: {{"score": 分数, "deduction_reason": "扣分原因或评价"}}"""

    user_prompt = f"""【用户输入】
{input_text}

【AI实际回复】
{actual_output}

请评分:"""

    try:
        result_text = call_llm_simple(system_prompt, user_prompt, timeout=timeout, model=model, temperature=temperature, max_tokens=max_tokens)
        if not result_text:
            return {"score": 0, "deduction_reason": "评测LLM无响应", "status": "failed"}

        print(f"[EVAL LLM RAW] {case_id}: {result_text[:300]}", flush=True)

        # 去掉 markdown 代码块标记
        clean_text = re.sub(r'```json\s*', '', result_text)
        clean_text = re.sub(r'```\s*', '', clean_text).strip()

        # 方法1: 直接尝试解析整个文本（去掉代码块后可能就是纯JSON）
        try:
            data = json.loads(clean_text)
            if "score" in data:
                score = int(round(data.get("score", 0)))
                reason = data.get("deduction_reason", data.get("reason", ""))
                status = "passed" if score >= 6 else "failed"
                return {"score": score, "deduction_reason": reason, "status": status}
        except json.JSONDecodeError:
            pass

        # 方法2: 找到第一个 { 到最后一个 } 之间的内容
        start = clean_text.find('{')
        end = clean_text.rfind('}')
        if start != -1 and end != -1 and end > start:
            json_str = clean_text[start:end+1]
            try:
                data = json.loads(json_str)
                if "score" in data:
                    score = int(round(data.get("score", 0)))
                    reason = data.get("deduction_reason", data.get("reason", ""))
                    status = "passed" if score >= 6 else "failed"
                    return {"score": score, "deduction_reason": reason, "status": status}
            except json.JSONDecodeError:
                pass

        return {"score": 0, "deduction_reason": f"无法解析评测结果: {clean_text[:100]}", "status": "failed"}
    except Exception as e:
        return {"score": 0, "deduction_reason": f"评测异常: {str(e)}", "status": "failed"}


# ─── 对话实时评测 ───────────────────────────────────

def evaluate_chat_reply(
    user_message: str,
    reply_text: str,
    chat_history: List[str] = None,
    user_facts: List[Dict] = None,
    persona_data: Dict = None,
    toy_persona: Dict = None,
    model: str = None,
    temperature: float = None,
    max_tokens: int = None,
    timeout: int = 90
) -> Dict:
    """
    对话窗口实时评测AI回复。

    toy_persona: 玩偶人设信息（从 toy_persona 表读取），传入时使用完整结构化描述，未传入时使用默认简短描述

    评测维度：
    - 记忆运用：是否恰当使用已知的用户信息
    - 情感回应：对用户情绪的识别和回应质量
    - 回复质量：回复的自然度、连贯性、有用性
    - 人设一致：是否符合秋秋的人设

    返回: {
        "memory_score": 1-10,
        "memory_reason": "扣分原因",
        "emotion_score": 1-10,
        "emotion_reason": "扣分原因",
        "quality_score": 1-10,
        "quality_reason": "扣分原因",
        "persona_score": 1-10,
        "persona_reason": "扣分原因",
        "total_score": 平均分
    }
    """
    # 构建用户事实文本（按分类分组）
    facts_text = _format_facts_grouped(user_facts)
    
    # 构建对话历史文本
    history_text = "无历史对话"
    if chat_history:
        history_text = "\n".join(chat_history[-10:])
    
    # 构建用户画像文本
    persona_text = ""
    if persona_data:
        parts = []
        for k in ["name", "occupation", "city", "interests", "language_style"]:
            v = persona_data.get(k, "")
            if v:
                parts.append(f"{k}: {v}")
        persona_text = "\n".join(parts) if parts else ""
    
    # 构建人设文本（传入完整 toy_persona 时使用结构化描述，否则用默认简述）
    if toy_persona:
        toy_info = _format_toy_persona(toy_persona)
    else:
        toy_info = "秋秋是一个温暖、俏皮的AI陪伴玩偶，说话简短亲切，会用嘛呀呢等语气词，像朋友聊天。"

    system_prompt = """你是AI陪伴对话质量评测专家。请对AI回复进行多维度评测。

【AI人设】
{toy_info}

【已知用户信息】
{facts}

【用户画像】
{persona}

【评测维度】
1. 记忆运用(memory)：是否恰当引用已知用户信息，不生硬堆砌，不捏造事实
2. 情感回应(emotion)：是否识别用户情绪并给予恰当回应，共情而不说教
3. 回复质量(quality)：回复是否自然流畅、长度适中、有实际内容
4. 人设一致(persona)：是否符合秋秋的人设（语气词自然、不做客服不做说教、知道分寸）

【评分规则】
- 每项10分制，整数打分
- 扣分必须写明原因，满分可不写原因
- 返回严格JSON格式""".format(
        toy_info=toy_info,
        facts=facts_text,
        persona=persona_text or "未提供"
    )
    
    user_prompt = """【近期对话】
{history}

【当前轮】
用户: {user_msg}
秋秋: {reply}

请评测秋秋的回复，返回JSON：
{{
  "memory_score": 分数,
  "memory_reason": "扣分原因或空",
  "emotion_score": 分数,
  "emotion_reason": "扣分原因或空",
  "quality_score": 分数,
  "quality_reason": "扣分原因或空",
  "persona_score": 分数,
  "persona_reason": "扣分原因或空"
}}""".format(
        history=history_text,
        user_msg=user_message,
        reply=reply_text
    )
    
    try:
        result_text = call_llm_simple(system_prompt, user_prompt, timeout=timeout, model=model, temperature=temperature, max_tokens=max_tokens)
        print(f"[CHAT EVAL] model={model or EXTRACT_LLM_MODEL} result_text len: {len(result_text) if result_text else 0}", flush=True)
        if not result_text:
            return _default_eval_result("LLM无响应")
        
        # 解析JSON
        match = re.search(r'\{[^{}]*\}', result_text, re.DOTALL)
        if match:
            data = json.loads(match.group())
            # 计算总分
            scores = []
            for key in ["memory_score", "emotion_score", "quality_score", "persona_score"]:
                s = data.get(key, 0)
                if isinstance(s, (int, float)) and 1 <= s <= 10:
                    scores.append(s)
                else:
                    scores.append(5)  # 默认中等分
                    data[key] = 5
            
            data["total_score"] = round(sum(scores) / len(scores), 1) if scores else 5.0
            
            # 确保所有字段存在
            for key in ["memory_reason", "emotion_reason", "quality_reason", "persona_reason"]:
                if key not in data:
                    data[key] = ""
            
            return data
        
        return _default_eval_result(f"无法解析: {result_text[:100]}")
        
    except Exception as e:
        return _default_eval_result(f"评测异常: {str(e)}")


def _default_eval_result(error_msg: str) -> Dict:
    """返回默认评测结果"""
    return {
        "memory_score": 5,
        "memory_reason": error_msg,
        "emotion_score": 5,
        "emotion_reason": "",
        "quality_score": 5,
        "quality_reason": "",
        "persona_score": 5,
        "persona_reason": "",
        "total_score": 5.0
    }


# ─── 用例质量 LLM 复核 ───────────────────────────────────

def review_case_quality(case_data: Dict, dimension_info: Dict = None, user_facts: List[Dict] = None, toy_persona: Dict = None, model: str = None, temperature: float = None, max_tokens: int = None, timeout: int = 120) -> Dict:
    """
    LLM 复核用例质量（异步调用）
    返回: {"score": 1-10, "issues": ["问题1"], "status": "passed/warning/failed"}
    """
    dim_code = case_data.get("dimension_code", "")
    dim_name = dimension_info.get("dimension_name", dim_code) if dimension_info else dim_code
    test_points = dimension_info.get("test_points", "") if dimension_info else ""

    # 格式化用户事实（按分类分组）
    facts_text = _format_facts_grouped(user_facts)

    # 格式化玩偶人设
    persona_text = _format_toy_persona(toy_persona) if toy_persona else ""

    system_prompt = f"""你是测试用例质量审核专家。请审核以下AI陪伴对话测试用例的质量。

【维度】{dim_code} - {dim_name}
【测试点要求】{test_points}
{f"【AI玩偶人设】{chr(10)}{persona_text}" if persona_text else ""}
【用户已知事实】
{facts_text or "暂无"}

【审核标准】
1. input_text 是否覆盖了测试点？
2. **expected_output 必须是具体回复文本（模拟AI理想回复），而不是行为原则列表**。如果 expected_output 是"1. xxx；2. xxx"的行为描述格式，直接扣 3 分
3. expected_output 是否符合AI玩偶的人设风格（语气自然口语化、不说教不套话、有分寸感）？
4. evaluation_points 是否覆盖了 expected_output 中体现的关键行为？是否具体可判断？
5. 评分描述（2分/6分/10分）是否合理递进？
6. failure_flags 是否与场景相关、可检测？是否涵盖了玩偶的行为边界（不越界、不做承诺、不暧昧等）？
7. input_text 中的事实是否与用户已知事实一致（无冲突）？
8. expected_output 中提及的用户信息是否能在已知事实中找到对应？
9. 用例整体是否可执行、可评测？

【事实校验反误判规则 - 重要】
在判断"expected_output 引用了不存在的事实"之前，必须逐条对照【用户已知事实】列表。以下情况不算虚构：
- 表述简化但指向同一事实（如已知事实"最近在追一部剧，觉得超好看"，expected_output 写"追剧"或"看你最近追的剧"→ 匹配）
- 已知事实中的通勤/地址/年龄等信息（如"住处距离公司步行约10分钟"允许写"步行十分钟到家"）
- 已知事实中的人际关系描述（如"同事人都挺好的"允许写"同事挺好"）
只有在已知事实列表中完全找不到任何对应时，才能判定为虚构事实。

【评分规则】
- 10分：完全符合，可直接使用
- 7-9分：基本合格，有小瑕疵
- 4-6分：需修改，有明显问题（expected_output 是行为列表直接 ≤6 分）
- 1-3分：不合格，需重新生成

返回JSON: {{"score": 分数, "issues": ["问题1", "问题2"], "suggestion": "修改建议"}}"""

    user_prompt = f"""【用例信息】
case_id: {case_data.get('case_id', '')}
title: {case_data.get('title', '')}
test_point: {case_data.get('test_point', '')}

input_text:
{case_data.get('input_text', '')}

expected_output:
{case_data.get('expected_output', '')}

evaluation_points:
{case_data.get('evaluation_points', '（未提供）')}

failure_flags:
{case_data.get('failure_flags', '')}

score_2_desc: {case_data.get('score_2_desc', '')}
score_6_desc: {case_data.get('score_6_desc', '')}
score_10_desc: {case_data.get('score_10_desc', '')}

请审核并返回JSON:"""

    try:
        result_text = call_llm_simple(system_prompt, user_prompt, timeout=timeout, model=model or REVIEW_LLM_MODEL, temperature=temperature, max_tokens=max_tokens)
        if not result_text:
            return {"score": 5, "issues": ["LLM复核无响应"], "status": "warning"}
        
        match = re.search(r'\{[^{}]*\}', result_text, re.DOTALL)
        if match:
            data = json.loads(match.group())
            score = data.get("score", 5)
            issues = data.get("issues", [])
            
            if score >= 8:
                status = "passed"
            elif score >= 5:
                status = "warning"
            else:
                status = "failed"
            
            return {"score": score, "issues": issues, "status": status, "suggestion": data.get("suggestion", "")}
        
        return {"score": 5, "issues": ["无法解析LLM响应"], "status": "warning"}
    except Exception as e:
        return {"score": 5, "issues": [f"复核异常: {str(e)}"], "status": "warning"}

