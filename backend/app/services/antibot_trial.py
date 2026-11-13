"""对抗策略试算引擎：对同一份挑战样本比较多条启用策略的候选结果。

设计约束（对应需求）：

1. 纯沙箱：识别服务的“返回结果”由场景参数在内存里模拟，不发起任何网络请求、
   不调用真实验证码服务，也不消耗生产任务次数（试算不写生产计数，只在最后按
   运营显式确认时才落一条失败记录）。
2. 可重复：同一样本 + 同一批策略的试算结果完全确定，试算编号由输入内容哈希得到。
3. 重叠可裁决：所有命中样本的启用策略都作为候选返回，按优先级规则排序，并为
   每个落选候选写清楚淘汰理由。
4. 预算联动：识别服务不可用时，等待与重试次数同时受对抗策略的重试上限和关联
   任务的重试预算约束，取两者较小值；切换降级方案不会重置计数。
"""
from __future__ import annotations

import hashlib
import json
from typing import Any

from app.services.antibot_config import assess_entry
from app.store import store

# 试算支持的模拟场景。均为本地确定性推演，没有任何外部 I/O。
SCENARIOS = {
    "service_unavailable": "识别服务持续不可用（用于验证等待/重试预算与降级联动）",
    "recover_on_attempt": "识别服务在第 N 次尝试后恢复",
    "solved_first_try": "首次尝试即识别成功",
    "unrecognized": "服务可用但持续无法识别样本",
}


# --------------------------------------------------------------------------- #
# 触发条件匹配
# --------------------------------------------------------------------------- #
def _match_condition(condition: dict[str, Any], context: dict[str, Any]) -> bool:
    operator = condition["operator"]
    expected = condition["value"]
    if operator == "http_status":
        return int(context.get("http_status", 0)) == int(expected)
    if operator == "header_contains":
        headers = context.get("headers") or {}
        haystack = " ".join(str(value) for value in headers.values())
        return str(expected) in haystack
    if operator == "body_contains":
        return str(expected) in str(context.get("body_excerpt") or "")
    if operator == "risk_level_gte":
        return int(context.get("risk_level", 0)) >= int(expected)
    return False


def _entry_matches_sample(entry: dict[str, Any], sample: dict[str, Any]) -> bool:
    parsed = assess_entry(entry)["parsed"]
    if parsed["captcha_type"] != sample.get("captcha_type"):
        return False
    conditions = parsed["conditions"]
    context = sample.get("context") or {}
    # 触发条件之间是 AND：全部命中才算该策略适用于本样本。
    return all(_match_condition(condition, context) for condition in conditions)


# --------------------------------------------------------------------------- #
# 候选排序与淘汰理由
# --------------------------------------------------------------------------- #
def _specificity(entry: dict[str, Any]) -> int:
    """触发条件越多、越具体，条件重叠时排序越靠前。"""
    return len(assess_entry(entry)["parsed"]["conditions"])


def rank_candidates(entries: list[dict[str, Any]], sample: dict[str, Any]) -> list[dict[str, Any]]:
    """给命中样本的策略排序并写明每条候选的入选/淘汰理由。"""
    ranked = sorted(
        entries,
        key=lambda row: (
            -int(row.get("优先级") or 0),
            -_specificity(row),
            int(row.get("id") or 0),
        ),
    )
    winner_id = ranked[0]["id"] if ranked else None
    candidates: list[dict[str, Any]] = []
    for position, entry in enumerate(ranked):
        parsed = assess_entry(entry)["parsed"]
        if entry["id"] == winner_id:
            decision = "选中"
            reason = "优先级最高且触发条件全部命中"
        else:
            winner = ranked[0]
            decision = "淘汰"
            if int(winner.get("优先级") or 0) > int(entry.get("优先级") or 0):
                reason = f"与策略 {winner.get('策略编号')} 触发条件重叠，但优先级 {entry.get('优先级')} 低于对方 {winner.get('优先级')}"
            elif _specificity(winner) > _specificity(entry):
                reason = f"优先级相同，策略 {winner.get('策略编号')} 触发条件更具体（{_specificity(winner)} 条 > {_specificity(entry)} 条）"
            else:
                reason = f"优先级与条件粒度均相同，按策略编号稳定次序让位给 {winner.get('策略编号')}"
        candidates.append({
            "rank": position + 1,
            "decision": decision,
            "reason": reason,
            "strategy_id": entry["id"],
            "策略编号": entry.get("策略编号"),
            "验证码类型": parsed["captcha_type"],
            "识别服务": parsed["recognition_service"],
            "优先级": parsed["priority"],
            "等待时长": parsed["wait_seconds"],
            "重试上限": parsed["retry_cap"],
            "降级方案": parsed["fallback_plan"],
            "命中触发条件": len(parsed["conditions"]),
        })
    return candidates


# --------------------------------------------------------------------------- #
# 重试预算（策略上限与关联任务预算同时约束，取较小值）
# --------------------------------------------------------------------------- #
def resolve_retry_budget(strategy: dict[str, Any], task_ref: str) -> dict[str, Any]:
    """汇总对抗策略重试上限与关联任务重试预算，返回最终可用预算。"""
    strategy_cap = int(strategy.get("重试上限") or 0)

    warnings: list[str] = []
    task_budget: int | None = None
    for retry_row in store.rows("retry"):
        if str(retry_row.get("关联任务") or "").strip() != task_ref:
            continue
        if retry_row.get("status") != "已启用":
            warnings.append(f"重试策略 {retry_row.get('策略编号')} 未启用，预算不生效")
            continue
        raw = retry_row.get("最大重试次数")
        try:
            task_budget = int(raw)
        except (TypeError, ValueError):
            warnings.append(f"重试策略 {retry_row.get('策略编号')} 的最大重试次数「{raw}」不可解析，已忽略")
    if task_budget is None:
        warnings.append(f"关联任务 {task_ref} 没有可用的重试策略，仅按对抗策略上限约束")
        task_budget = strategy_cap

    effective = min(strategy_cap, task_budget)
    return {
        "task_ref": task_ref,
        "strategy_retry_cap": strategy_cap,
        "task_retry_budget": task_budget,
        "effective_retry_budget": effective,
        "total_attempts_allowed": effective + 1,  # 首次尝试 + budget 次重试
        "warnings": warnings,
    }


# --------------------------------------------------------------------------- #
# 沙箱识别：纯内存模拟，绝不访问外部服务
# --------------------------------------------------------------------------- #
def _simulated_recognition(
    scenario: str,
    attempt: int,
    sample: dict[str, Any],
    service: str,
) -> dict[str, Any]:
    """根据场景返回该次尝试的模拟结果；任何场景都不产生真实调用。"""
    if scenario == "solved_first_try" and attempt == 1:
        return {"outcome": "solved", "detail": "沙箱模拟：首次尝试识别通过"}
    if scenario == "recover_on_attempt":
        recover_at = int(sample.get("recover_on_attempt") or 2)
        if attempt >= recover_at:
            return {"outcome": "solved", "detail": f"沙箱模拟：第 {attempt} 次尝试时服务恢复并识别通过"}
        return {"outcome": "unavailable", "detail": f"沙箱模拟：{service} 暂时不可用（{attempt}/{recover_at}）"}
    if scenario == "service_unavailable":
        return {"outcome": "unavailable", "detail": f"沙箱模拟：{service} 持续不可用"}
    if scenario == "unrecognized":
        return {"outcome": "unrecognized", "detail": "沙箱模拟：服务可用但未识别出有效结果"}
    return {"outcome": "error", "detail": f"未知试算场景 {scenario}"}


# --------------------------------------------------------------------------- #
# 试算主流程
# --------------------------------------------------------------------------- #
def _trial_id(sample: dict[str, Any], strategy_ids: list[int], scenario: str) -> str:
    payload = json.dumps(
        {"sample": sample, "strategies": strategy_ids, "scenario": scenario},
        ensure_ascii=False,
        sort_keys=True,
    )
    return "TRIAL-" + hashlib.sha256(payload.encode("utf-8")).hexdigest()[:12]


def run_trial(sample: dict[str, Any], scenario: str) -> dict[str, Any]:
    """对一份挑战样本执行完整试算，返回候选裁决、预算推演与逐次尝试轨迹。"""
    if scenario not in SCENARIOS:
        raise ValueError(f"试算场景「{scenario}」不受支持")

    captcha_type = str(sample.get("captcha_type") or "").strip()
    task_ref = str(sample.get("task_ref") or "").strip()
    if not captcha_type:
        raise ValueError("挑战样本缺少 captcha_type（验证码类型）")
    if not task_ref:
        raise ValueError("挑战样本缺少 task_ref（关联任务编号）")

    # 只看启用状态、且通过可执行判定的策略；旧文字配置在此被显式挡在裁决之外。
    skipped: list[dict[str, str]] = []
    eligible: list[dict[str, Any]] = []
    for entry in store.rows("antibot"):
        if entry.get("status") != "已启用":
            skipped.append({"策略编号": str(entry.get("策略编号")), "reason": f"状态为{entry.get('status')}，未启用策略不参与裁决"})
            continue
        assessment = assess_entry(entry)
        if not assessment["executable"]:
            skipped.append({"策略编号": str(entry.get("策略编号")), "reason": "；".join(assessment["reasons"])})
            continue
        eligible.append(entry)

    matched = [entry for entry in eligible if _entry_matches_sample(entry, sample)]
    for entry in eligible:
        if entry not in matched:
            skipped.append({"策略编号": str(entry.get("策略编号")), "reason": "验证码类型或触发条件与本样本不匹配"})

    candidates = rank_candidates(matched, sample)
    winner = matched[0] if matched else None

    result: dict[str, Any] = {
        "trial_id": "",
        "scenario": scenario,
        "scenario_label": SCENARIOS[scenario],
        "sample": sample,
        "candidates": candidates,
        "skipped_strategies": skipped,
        "budget": None,
        "trace": [],
        "outcome": "no_strategy",
        "failure_record": None,
        "external_calls": 0,  # 恒为 0：试算不允许任何真实外部调用
    }
    result["trial_id"] = _trial_id(
        sample,
        [int(entry["id"]) for entry in matched],
        scenario,
    )

    if winner is None:
        result["outcome"] = "no_strategy"
        return result

    budget = resolve_retry_budget(winner, task_ref)
    result["budget"] = budget
    wait_seconds = int(winner.get("等待时长") or 0)
    fallback_plan = str(winner.get("降级方案") or "无")

    trace: list[dict[str, Any]] = []
    # 关键不变量：attempt 在整条降级链上单调递增，切方案绝不重置。
    cumulative_attempts = 0
    chain = [{"stage": "主策略", "service": winner.get("识别服务"), "strategy_id": winner["id"]}]
    if scenario in {"service_unavailable", "unrecognized"} and fallback_plan != "无":
        chain.append({"stage": f"降级方案：{fallback_plan}", "service": "人工兜底" if fallback_plan == "人工接管" else winner.get("识别服务"), "strategy_id": winner["id"]})

    stage_index = 0
    exhausted = False
    while cumulative_attempts < budget["total_attempts_allowed"]:
        stage = chain[stage_index]
        cumulative_attempts += 1
        simulated = _simulated_recognition(scenario, cumulative_attempts, sample, stage["service"])
        trace.append({
            "attempt": cumulative_attempts,
            "stage": stage["stage"],
            "service": stage["service"],
            "waited_seconds": 0 if cumulative_attempts == 1 else wait_seconds,
            "simulated_result": simulated["outcome"],
            "detail": simulated["detail"],
            "counter_reset": False,  # 显式记录：计数未因切换方案而重置
        })
        if simulated["outcome"] == "solved":
            result["outcome"] = "solved"
            break
        # 识别失败且还有降级阶段：沿链推进，但累计尝试次数保持不变、不清零。
        if stage_index < len(chain) - 1:
            stage_index += 1
            trace.append({
                "attempt": cumulative_attempts,
                "stage": stage["stage"],
                "service": stage["service"],
                "waited_seconds": 0,
                "simulated_result": "fallback_switch",
                "detail": f"切换到{chain[stage_index]['stage']}，累计尝试次数保持 {cumulative_attempts} 不重置",
                "counter_reset": False,
            })
    else:
        exhausted = True

    if result["outcome"] != "solved":
        result["outcome"] = "budget_exhausted" if exhausted else "unresolved"
        result["failure_record"] = build_failure_record(sample, winner, budget, trace, scenario)

    result["trace"] = trace
    return result


def build_failure_record(
    sample: dict[str, Any],
    strategy: dict[str, Any],
    budget: dict[str, Any],
    trace: list[dict[str, Any]],
    scenario: str,
) -> dict[str, Any]:
    """预算耗尽后生成一条可处理的失败记录（试算阶段只返回内容，不直接落库）。"""
    return {
        "样本编号": sample.get("sample_id") or "未命名样本",
        "关联任务": sample.get("task_ref"),
        "验证码类型": sample.get("captcha_type"),
        "对抗策略": strategy.get("策略编号"),
        "失败原因": SCENARIOS.get(scenario, scenario),
        "累计尝试次数": len([step for step in trace if step["simulated_result"] != "fallback_switch"]),
        "生效重试预算": budget["effective_retry_budget"],
        "降级链路": " -> ".join(sorted({step["stage"] for step in trace}, key=lambda value: [s["stage"] for s in trace].index(value))),
        "处理状态": "待处理",
        "来源": "样本试算",
    }
