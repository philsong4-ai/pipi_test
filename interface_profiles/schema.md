# Profile JSON Schema

每个 `interface_profiles/<target_api>.json` 描述一个测试接口的完整 profile。加载器 `interface_profiles/_loader.py` 按文件名加载，未知 target_api 回退 `pipi.json`。

## 顶层字段

| 字段 | 类型 | 说明 |
|---|---|---|
| `profile_version` | int | profile schema 版本，当前 1 |
| `target_api` | str | 必须与文件名一致，且与 `toy_persona.target_api` / `personas.target_api` 一致 |
| `display_name` | str | 前端展示名（如「皮皮 AI 陪伴玩偶」） |
| `product_type` | str | 产品类型枚举：`companion_toy` / `productivity_hardware` / `other` |
| `identity` | obj | 通用文案：system prompt 模板、默认人设文本、角色标签 |
| `dimensions` | array | 维度定义（与 `test_dimensions` 表互为镜像；DB 是运行时源，JSON 是 seed） |
| `dimension_gen_requirements` | obj | 按 dim_code 索引的生成端专属要求（轮数/结构/示例） |
| `dimension_review_checklist` | obj | 按 dim_code 索引的审核端 checklist（specific + hard_rules） |
| `general_hard_rules` | array | 所有维度适用的通用硬规则（生成端用） |
| `redteam` | obj | 红队配置：启用维度、trap_types 枚举、按维度的 attack_hints |
| `fact_taxonomy` | obj | 事实分类：valid_categories / category_names / multi_entity_fields |
| `prompts` | obj | LLM Prompt 模板，按函数名索引，`{placeholder}` + `.format()` 注入 |
| `report` | obj | 报告层配置：扣分关键词列表 |

## identity

```yaml
identity:
  system_prompt_template: "你是皮皮，设备ID是{device_id}"  # build_system_prompt 用
  default_toy_persona_text: "秋秋是一个温暖..."             # evaluate_chat_reply 默认人设
  role_label_user: "用户"                                   # input_text 中用户角色标签
  role_label_ai: "秋秋"                                      # input_text / 评测 prompt 中 AI 角色标签
  product_category_label: "AI陪伴产品"                       # generate_test_cases system 第一行
  user_role_label_in_input: "用户"                          # input_text 中的用户标签（与 role_label_user 同义）
  ai_role_label_in_input: "秋秋"                            # input_text 中的 AI 标签
```

## prompts

每个 prompt 是 `{system, user}` 对，`{placeholder}` 用 `.format(**ctx)` 注入。

**关键约束**：
- JSON 示例中的花括号必须**双写转义**（`{{` / `}}`），否则 `.format()` 会误解析
- 占位符名称必须与调用方传入的 `fmt_ctx` dict key 一致
- 多行字符串用 `\n` 转义，不用 YAML `|` 块

## 加载机制

```python
from interface_profiles import load_profile
profile = load_profile("pipi")  # 未知 target_api 自动回退 pipi
```

- 模块级 dict 缓存，进程生命周期内只读一次磁盘
- Gunicorn 多 worker 各自独立缓存，改 JSON 后需 `start_web.sh restart`
- `load_persona_presets(target_api)` 动态 import `persona_presets/<ta>.py`，保留 lambda 灵活性

## 新增接口流程（零代码改动）

1. 新建 `interface_profiles/<target_api>.json`（参考 pipi.json 结构）
2. 新建 `interface_profiles/persona_presets/<target_api>.py`（定义 PERSONA_TEMPLATES）
3. DB seed：`INSERT INTO test_dimensions (target_api, dimension_code, ...) VALUES ('<ta>', 'O1', ...)`
4. `toy_persona` 插入新行 `target_api='<ta>'`
5. `api_endpoints` 插入新行 `code='<ta>'`
6. `start_web.sh restart`，前端选 target_api 即可看到新维度

无需改任何 `.py` 文件。
