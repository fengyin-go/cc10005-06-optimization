"""对抗策略裁决的单元测试：不依赖网络，直接验证领域规则。

运行：cd backend && .venv/bin/python -m unittest discover -s tests -v
"""
from __future__ import annotations

import unittest

from app.services.antibot_config import assess_entry, validate_structured
from app.services.antibot_trial import (
    resolve_retry_budget,
    run_trial,
)
from app.store import store


SLIDER_SAMPLE = {
    "sample_id": "SAMPLE-T-001",
    "task_ref": "SPID-0002",
    "captcha_type": "滑块",
    "context": {"http_status": 403, "body_excerpt": "请完成滑块验证", "risk_level": 2},
}


class ExecutabilityTests(unittest.TestCase):
    def test_legacy_text_only_config_is_readable_but_not_executable(self):
        legacy = store.find("antibot", 3)
        self.assertIsNotNone(legacy, "旧配置必须仍然可以读取")
        view = assess_entry(legacy)
        self.assertFalse(view["executable"])
        self.assertTrue(view["reasons"], "必须给出不可执行的具体原因")

    def test_structured_config_is_executable(self):
        structured = store.find("antibot", 4)
        view = assess_entry(structured)
        self.assertTrue(view["executable"], view["reasons"])
        self.assertEqual(view["parsed"]["retry_cap"], 4)
        self.assertEqual(view["parsed"]["wait_seconds"], 3)

    def test_validate_structured_rejects_bad_values(self):
        errors = validate_structured({
            "验证码类型": "神秘验证码",
            "识别服务": "某内部服务",
            "触发条件": [{"operator": "weird", "value": 1}],
            "等待时长": "三秒",
            "重试上限": -1,
            "降级方案": "看着办",
            "优先级": "高",
        })
        self.assertTrue(errors)
        # 每个坏字段都应被点到名
        joined = "；".join(errors)
        for field in ("验证码类型", "识别服务", "触发条件", "等待时长", "重试上限", "降级方案", "优先级"):
            self.assertIn(field, joined)

    def test_validate_structured_accepts_good_values(self):
        errors = validate_structured({
            "验证码类型": "滑块",
            "识别服务": "本地识别",
            "触发条件": [{"operator": "http_status", "value": 403}],
            "等待时长": 2,
            "重试上限": 3,
            "降级方案": "延迟重试",
            "优先级": 1,
        })
        self.assertEqual(errors, [])


class RetryBudgetTests(unittest.TestCase):
    def test_budget_is_min_of_strategy_cap_and_task_budget(self):
        strategy = store.find("antibot", 4)  # 重试上限 4
        budget = resolve_retry_budget(strategy, "SPID-0002")  # 任务预算 2
        self.assertEqual(budget["strategy_retry_cap"], 4)
        self.assertEqual(budget["task_retry_budget"], 2)
        self.assertEqual(budget["effective_retry_budget"], 2)
        self.assertEqual(budget["total_attempts_allowed"], 3)

    def test_missing_task_policy_falls_back_to_strategy_cap_with_warning(self):
        strategy = store.find("antibot", 4)
        budget = resolve_retry_budget(strategy, "SPID-UNKNOWN")
        self.assertEqual(budget["effective_retry_budget"], 4)
        self.assertTrue(any("没有可用的重试策略" in item for item in budget["warnings"]))


class TrialTests(unittest.TestCase):
    def test_trial_is_repeatable_and_makes_no_external_calls(self):
        first = run_trial(SLIDER_SAMPLE, "service_unavailable")
        second = run_trial(SLIDER_SAMPLE, "service_unavailable")
        self.assertEqual(first["trial_id"], second["trial_id"], "同样输入必须得到同一试算编号")
        self.assertEqual(first["trace"], second["trace"], "试算轨迹必须可重复")
        self.assertEqual(first["external_calls"], 0)
        self.assertEqual(second["external_calls"], 0)

    def test_overlapping_strategies_are_ranked_with_elimination_reasons(self):
        result = run_trial(SLIDER_SAMPLE, "service_unavailable")
        # ANTI-1001（双条件、优先级10）与 ANTI-1002（单条件、优先级5）都命中
        decisions = {item["策略编号"]: item for item in result["candidates"]}
        self.assertEqual(decisions["ANTI-1001"]["decision"], "选中")
        self.assertEqual(decisions["ANTI-1002"]["decision"], "淘汰")
        self.assertIn("优先级", decisions["ANTI-1002"]["reason"])
        # 点选策略不应混入滑块样本的候选
        self.assertNotIn("ANTI-1003", decisions)
        # 旧文字配置必须出现在跳过清单里且说明原因
        skipped_codes = {item["策略编号"] for item in result["skipped_strategies"]}
        self.assertIn("ANTI-0003", skipped_codes)

    def test_fallback_switch_does_not_reset_counter(self):
        result = run_trial(SLIDER_SAMPLE, "service_unavailable")
        # 主策略 ANTI-1001 预算 2 次重试 => 共 3 次尝试；切备用服务后计数继续累加
        attempts = [step for step in result["trace"] if step["simulated_result"] != "fallback_switch"]
        self.assertEqual([step["attempt"] for step in attempts], [1, 2, 3])
        self.assertTrue(all(step["counter_reset"] is False for step in result["trace"]))
        switch_steps = [step for step in result["trace"] if step["simulated_result"] == "fallback_switch"]
        self.assertTrue(switch_steps, "应包含一次降级切换轨迹")
        self.assertIn("不重置", switch_steps[0]["detail"])
        self.assertEqual(result["outcome"], "budget_exhausted")

    def test_budget_exhaustion_produces_processable_failure_record(self):
        result = run_trial(SLIDER_SAMPLE, "service_unavailable")
        record = result["failure_record"]
        self.assertIsNotNone(record)
        self.assertEqual(record["处理状态"], "待处理")
        self.assertEqual(record["累计尝试次数"], 3)
        self.assertEqual(record["生效重试预算"], 2)
        self.assertEqual(record["关联任务"], "SPID-0002")

    def test_recovery_scenario_succeeds_within_budget(self):
        result = run_trial({**SLIDER_SAMPLE, "recover_on_attempt": 2}, "recover_on_attempt")
        self.assertEqual(result["outcome"], "solved")
        self.assertIsNone(result["failure_record"])

    def test_solved_first_try_stops_immediately(self):
        result = run_trial(SLIDER_SAMPLE, "solved_first_try")
        self.assertEqual(result["outcome"], "solved")
        self.assertEqual(len(result["trace"]), 1)

    def test_trial_without_matching_strategy(self):
        sample = {**SLIDER_SAMPLE, "captcha_type": "拼图"}
        result = run_trial(sample, "service_unavailable")
        self.assertEqual(result["candidates"], [])
        self.assertEqual(result["outcome"], "no_strategy")

    def test_invalid_inputs_are_rejected(self):
        with self.assertRaises(ValueError):
            run_trial({**SLIDER_SAMPLE, "task_ref": ""}, "service_unavailable")
        with self.assertRaises(ValueError):
            run_trial(SLIDER_SAMPLE, "real_production_call")


class ServiceWorkflowTests(unittest.TestCase):
    def setUp(self):
        from app.services.antibot import AntibotService
        self.service = AntibotService()

    def test_legacy_strategy_cannot_be_enabled(self):
        entry, message = self.service.run_action(1, "启用策略")  # ANTI-0001 是文字旧配置
        self.assertIsNone(entry)
        self.assertIn("不可执行", message)

    def test_structured_draft_can_be_enabled_and_trial_failure_can_be_materialized(self):
        entry, message = self.service.run_action(4, "启用策略")
        # id=4 在种子里已启用；再次启用仍应返回成功而不是报错
        self.assertIsNotNone(entry, message)

        before = len(store.rows("antibot_failure"))
        record, error = self.service.materialize_failure(
            {"sample": SLIDER_SAMPLE, "scenario": "service_unavailable"}
        )
        self.assertEqual(error, "")
        self.assertEqual(record["处理状态"], "待处理")
        self.assertEqual(len(store.rows("antibot_failure")), before + 1, "显式落库才新增失败记录")

        resolved, error = self.service.resolve_failure(record["id"], "已转人工处理")
        self.assertEqual(error, "")
        self.assertEqual(resolved["处理状态"], "已处理")
        again, error = self.service.resolve_failure(record["id"], None)
        self.assertIsNone(again)
        self.assertIn("已处理", error)

    def test_trial_does_not_consume_failure_records_or_task_counts(self):
        before = len(store.rows("antibot_failure"))
        result, error = self.service.trial(SLIDER_SAMPLE, "service_unavailable")
        self.assertEqual(error, "")
        self.assertIsNotNone(result["failure_record"], "试算返回失败记录内容")
        self.assertEqual(len(store.rows("antibot_failure")), before, "试算本身不得落库")


if __name__ == "__main__":
    unittest.main()
