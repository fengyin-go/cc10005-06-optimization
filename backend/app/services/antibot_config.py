"""对抗策略的可执行判定。

一条对抗策略要能参与自动裁决，必须具备机器可解析的结构化字段（验证码类型、
触发条件、等待时长、降级方案等）。历史上只录了文字描述的旧配置仍然可以在列表
和详情里读取，但会被标记为 ``executable=False`` 并给出淘汰原因，绝不能被当成
可执行判定参与试算或线上裁决。

这里的判定全部是纯函数，不依赖任何外部服务，方便单测反复验证。
"""
from __future__ import annotations

import re
from typing import Any

# 结构化登记时允许的取值；故意收敛成枚举，避免“文字描述”混进可执行链路。
CAPTCHA_TYPES = {"滑块", "点选", "字符", "智能无感", "拼图"}
RECOGNITION_SERVICES = {"本地识别", "打码平台A", "打码平台B", "人工兜底"}
FALLBACK_PLANS = {"切换备用识别服务", "人工接管", "跳过采集", "延迟重试", "无"}

# 触发条件支持的算子（全部针对样本上下文做内存匹配，不发起任何请求）。
CONDITION_OPERATORS = {"http_status", "header_contains", "body_contains", "risk_level_gte"}

# 旧配置里出现这些字段值时，基本可以断定只有文字描述、无法结构化执行。
_TEXTUAL_PLACEHOLDER = re.compile(r"(样例|待补充|另行说明|见文档|人工确认)")

NUMERIC_FIELDS = ("priority", "retry_cap", "wait_seconds")


def _to_int(value: Any) -> int | None:
    """把等待时长/重试上限这类字段安全转成整数；转不了就返回 None。"""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, str):
        text = value.strip()
        if text.isdigit():
            return int(text)
    return None


def _conditions_from_entry(entry: dict[str, Any]) -> list[dict[str, Any]] | None:
    """读取触发条件。结构化登记存 list；旧数据可能是纯文本或占位串。"""
    raw = entry.get("触发条件")
    if isinstance(raw, list) and raw:
        normalized: list[dict[str, Any]] = []
        for item in raw:
            if not isinstance(item, dict):
                return None
            operator = str(item.get("operator") or "").strip()
            if operator not in CONDITION_OPERATORS:
                return None
            if "value" not in item:
                return None
            normalized.append({"operator": operator, "value": item["value"]})
        return normalized
    return None


def assess_entry(entry: dict[str, Any]) -> dict[str, Any]:
    """评估单条策略的可执行性，返回带 executable / reasons 的视图（不改原记录）。"""
    reasons: list[str] = []

    captcha_type = str(entry.get("验证码类型") or "").strip()
    if captcha_type not in CAPTCHA_TYPES:
        reasons.append(f"验证码类型「{captcha_type or '空'}」不在可执行枚举内，疑似纯文字描述")

    service = str(entry.get("识别服务") or "").strip()
    if service not in RECOGNITION_SERVICES:
        reasons.append(f"识别服务「{service or '空'}」不是可调用的结构化服务标识")

    conditions = _conditions_from_entry(entry)
    if conditions is None:
        reasons.append("触发条件缺少结构化算子与取值，无法自动匹配挑战样本")

    wait_seconds = _to_int(entry.get("等待时长"))
    if wait_seconds is None or wait_seconds < 0:
        reasons.append("等待时长不是非负整数秒数，无法纳入等待/重试预算")

    retry_cap = _to_int(entry.get("重试上限"))
    if retry_cap is None or retry_cap < 0:
        reasons.append("重试上限不是非负整数，无法与任务重试预算联动")

    fallback = str(entry.get("降级方案") or "").strip()
    if fallback not in FALLBACK_PLANS:
        reasons.append(f"降级方案「{fallback or '空'}」未登记为可执行的降级动作")

    priority = _to_int(entry.get("优先级"))
    if priority is None:
        reasons.append("优先级缺失或不是整数，重叠条件下无法裁决先后")

    # 旧配置常见特征：字段里全是“某某样例/待补充”这类自然语言占位。
    textual_hits = [
        field
        for field in ("验证码类型", "识别服务", "触发条件", "降级方案")
        if isinstance(entry.get(field), str) and _TEXTUAL_PLACEHOLDER.search(entry[field])
    ]
    if textual_hits:
        reasons.append(f"字段 {'、'.join(textual_hits)} 仍是文字占位描述，仅供查阅不可执行")

    return {
        "executable": not reasons,
        "reasons": reasons,
        "parsed": {
            "captcha_type": captcha_type,
            "recognition_service": service,
            "conditions": conditions or [],
            "wait_seconds": wait_seconds,
            "retry_cap": retry_cap,
            "fallback_plan": fallback,
            "priority": priority,
        },
    }


def normalize_candidate(entry: dict[str, Any]) -> dict[str, Any]:
    """给列表/详情接口统一附加可执行标记，旧配置照常读出但明确不可执行。"""
    view = dict(entry)
    view.update(assess_entry(entry))
    return view


def validate_structured(values: dict[str, Any]) -> list[str]:
    """登记/修改结构化策略时的入参校验，返回错误说明列表（空列表表示通过）。"""
    errors: list[str] = []

    captcha_type = str(values.get("验证码类型") or "").strip()
    if captcha_type not in CAPTCHA_TYPES:
        errors.append(f"验证码类型必须是：{'、'.join(sorted(CAPTCHA_TYPES))}")

    service = str(values.get("识别服务") or "").strip()
    if service not in RECOGNITION_SERVICES:
        errors.append(f"识别服务必须是：{'、'.join(sorted(RECOGNITION_SERVICES))}")

    conditions = values.get("触发条件")
    if not isinstance(conditions, list) or not conditions:
        errors.append("触发条件至少包含一条结构化规则（operator + value）")
    else:
        for index, item in enumerate(conditions, start=1):
            if not isinstance(item, dict):
                errors.append(f"第 {index} 条触发条件必须是对象")
                continue
            if str(item.get("operator") or "").strip() not in CONDITION_OPERATORS:
                errors.append(f"第 {index} 条触发条件的算子不受支持")
            if "value" not in item:
                errors.append(f"第 {index} 条触发条件缺少 value")

    for field in ("等待时长", "重试上限", "优先级"):
        number = _to_int(values.get(field))
        if number is None or number < 0:
            errors.append(f"{field}必须是非负整数")

    fallback = str(values.get("降级方案") or "").strip()
    if fallback not in FALLBACK_PLANS:
        errors.append(f"降级方案必须是：{'、'.join(sorted(FALLBACK_PLANS))}")

    return errors
