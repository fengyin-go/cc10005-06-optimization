"""反爬对抗业务规则：状态流转、字段校验、可执行判定与失败记录收口。"""
from __future__ import annotations

from typing import Any

from app.services.antibot_config import (
    assess_entry,
    normalize_candidate,
    validate_structured,
)
from app.services.antibot_trial import run_trial
from app.store import store

MODULE = "antibot"
FAILURE_MODULE = "antibot_failure"
# 兼容旧登记入口的最低必填字段；结构化校验在 service 层另外把关。
REQUIRED_FIELDS = ["策略编号", "验证码类型", "识别服务"]
STATUS_ORDER = ["草稿", "待校验", "已启用", "已停用"]
ACTION_RULES = {"提交校验": "待校验", "启用策略": "已启用", "停用策略": "已停用"}
NEGATIVE_ACTIONS = ["停用策略"]
FAILURE_STATUS_FLOW = ["待处理", "处理中", "已处理"]


class AntibotService:
    # ------------------------------------------------------------------ #
    # 策略列表/详情：旧配置照读，但统一附加可执行标记
    # ------------------------------------------------------------------ #
    def list_entries(
        self,
        *,
        keyword: str | None = None,
        status: str | None = None,
        executable_only: bool = False,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        rows = store.rows(MODULE)
        if keyword:
            rows = [row for row in rows if keyword in str(row.get("策略编号", ""))]
        if status:
            rows = [row for row in rows if row.get("status") == status]
        if executable_only:
            rows = [row for row in rows if assess_entry(row)["executable"]]
        total = len(rows)
        start = max(page - 1, 0) * size
        page_rows = [normalize_candidate(row) for row in rows[start:start + size]]
        return page_rows, total

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        entry = store.find(MODULE, entry_id)
        return normalize_candidate(entry) if entry else None

    # ------------------------------------------------------------------ #
    # 登记：结构化字段通过校验才允许进入可执行链路
    # ------------------------------------------------------------------ #
    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        missing = [field for field in REQUIRED_FIELDS if not str(values.get(field) or "").strip()]
        if missing:
            return None, [f"缺少必填字段：{field}" for field in missing]

        errors = validate_structured(values)
        rows = store.rows(MODULE)
        entry: dict[str, Any] = {"id": max((int(row.get("id", 0)) for row in rows), default=0) + 1}
        # 原样保存运营录入的结构化内容，触发条件保留为 list 而非压成文字。
        entry.update({
            "策略编号": str(values.get("策略编号")).strip(),
            "验证码类型": str(values.get("验证码类型")).strip(),
            "识别服务": str(values.get("识别服务")).strip(),
            "触发条件": values.get("触发条件"),
            "等待时长": values.get("等待时长"),
            "重试上限": values.get("重试上限"),
            "降级方案": str(values.get("降级方案") or "").strip(),
            "优先级": values.get("优先级"),
            "策略状态": "随状态流转自动维护",
        })
        entry["status"] = STATUS_ORDER[0]
        entry["pending"] = True
        entry["abnormal"] = False
        rows.append(entry)
        # 结构化问题不阻断保存（允许先存草稿），但明确告诉运营它还不能参与裁决。
        return normalize_candidate(entry), errors

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"对抗策略 {entry_id} 不存在或已归档"
        if action not in ACTION_RULES:
            return None, f"动作「{action}」不属于反爬对抗可执行范围"
        # 启用动作是进入自动裁决的闸门：不可执行的旧配置禁止启用。
        if action == "启用策略" and not assess_entry(entry)["executable"]:
            reasons = "；".join(assess_entry(entry)["reasons"])
            return None, f"策略不具备可执行判定条件，无法启用：{reasons}"
        target = ACTION_RULES[action]
        entry["status"] = target
        entry["pending"] = target != STATUS_ORDER[-1]
        entry["abnormal"] = action in NEGATIVE_ACTIONS
        return normalize_candidate(entry), f"对抗策略已{action}"

    # ------------------------------------------------------------------ #
    # 样本试算：纯沙箱，不调用真实外部服务、不消耗生产任务次数
    # ------------------------------------------------------------------ #
    def trial(self, sample: dict[str, Any], scenario: str) -> tuple[dict[str, Any] | None, str]:
        try:
            return run_trial(sample, scenario), ""
        except ValueError as exc:
            return None, str(exc)

    # ------------------------------------------------------------------ #
    # 失败记录：预算耗尽后的可处理产物，独立于试算落库
    # ------------------------------------------------------------------ #
    def list_failure_records(
        self,
        *,
        handle_status: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        rows = store.rows(FAILURE_MODULE)
        if handle_status:
            rows = [row for row in rows if row.get("处理状态") == handle_status]
        total = len(rows)
        start = max(page - 1, 0) * size
        return rows[start:start + size], total

    def materialize_failure(self, payload: dict[str, Any]) -> tuple[dict[str, Any] | None, str]:
        """把一次试算推演的失败结果落成可处理记录；试算本身不产生任何记录。"""
        trial_result, error = self.trial(payload.get("sample") or {}, str(payload.get("scenario") or ""))
        if error:
            return None, error
        if trial_result is None or not trial_result.get("failure_record"):
            return None, "本次试算未触发预算耗尽，没有可落库的失败记录"

        rows = store.rows(FAILURE_MODULE)
        record = dict(trial_result["failure_record"])
        record["id"] = max((int(row.get("id", 0)) for row in rows), default=0) + 1
        record["试算编号"] = trial_result["trial_id"]
        rows.append(record)
        return record, ""

    def resolve_failure(self, record_id: int, note: str | None) -> tuple[dict[str, Any] | None, str]:
        record = store.find(FAILURE_MODULE, record_id)
        if record is None:
            return None, f"失败记录 {record_id} 不存在或已归档"
        if record.get("处理状态") == "已处理":
            return None, "该失败记录已处理，不能重复办结"
        record["处理状态"] = "已处理"
        record["处理备注"] = (note or "").strip() or "运营人工办结"
        return record, ""

    def export_entries(self) -> list[dict[str, Any]]:
        return [normalize_candidate(row) for row in store.rows(MODULE)]
