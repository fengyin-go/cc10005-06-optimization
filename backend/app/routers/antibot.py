"""反爬对抗接口：维护对抗策略、样本试算与失败记录处理。

- 策略列表/详情统一返回可执行判定标记，旧文字配置可读但明确不可执行。
- 试算接口是纯沙箱推演：不调用真实外部验证码服务，也不消耗生产任务次数。
- 失败记录由运营在试算确认后显式落库，预算耗尽才会生成。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from app.schemas import ActionResult, EntryPayload, PageResult
from app.services.antibot import AntibotService
from app.services.antibot_trial import SCENARIOS

router = APIRouter(prefix="/api/antibot", tags=["反爬对抗"])

service = AntibotService()

LIST_FIELDS = ["策略编号", "验证码类型", "识别服务", "触发条件", "等待时长", "重试上限", "降级方案", "策略状态"]
STATUSES = ["草稿", "待校验", "已启用", "已停用"]


class TrialPayload(BaseModel):
    """样本试算入参：同一份样本可反复提交，结果由输入内容确定性决定。"""

    sample: dict[str, Any] = Field(default_factory=dict)
    scenario: str = "service_unavailable"


class FailureResolvePayload(BaseModel):
    note: str | None = None


@router.get("", response_model=PageResult[dict])
def list_entries(
    keyword: str | None = Query(default=None, description="按策略编号检索"),
    status: str | None = Query(default=None, description="草稿、待校验、已启用、已停用"),
    executable_only: bool = Query(default=False, description="仅返回通过可执行判定的策略"),
    page: int = 1,
    size: int = 20,
) -> PageResult[dict]:
    """按策略编号与状态过滤反爬对抗列表；没有数据时返回空页，不报错。"""
    if size > 200:
        raise HTTPException(status_code=400, detail="每页最多 200 条，请缩小分页范围")
    items, total = service.list_entries(
        keyword=keyword,
        status=status,
        executable_only=executable_only,
        page=page,
        size=size,
    )
    return PageResult(items=items, total=total, page=page, size=size)


@router.get("/scenarios")
def list_scenarios() -> dict[str, Any]:
    """列出试算支持的沙箱场景，前端据此渲染选项。"""
    return {"items": [{"code": code, "label": label} for code, label in SCENARIOS.items()]}


@router.get("/failures", response_model=PageResult[dict])
def list_failure_records(
    handle_status: str | None = Query(default=None, description="待处理、处理中、已处理"),
    page: int = 1,
    size: int = 20,
) -> PageResult[dict]:
    """读取预算耗尽后生成的失败记录，供运营人工处理。"""
    items, total = service.list_failure_records(handle_status=handle_status, page=page, size=size)
    return PageResult(items=items, total=total, page=page, size=size)


@router.post("/failures")
def materialize_failure(payload: TrialPayload) -> ActionResult:
    """按试算推演落一条失败记录；试算本身不落库、不消耗生产任务次数。"""
    record, error = service.materialize_failure({"sample": payload.sample, "scenario": payload.scenario})
    if error:
        return ActionResult(ok=False, message=error)
    return ActionResult(ok=True, message="失败记录已生成，状态为待处理", entry=record)


@router.post("/failures/{record_id}/resolve", response_model=ActionResult)
def resolve_failure(record_id: int, payload: FailureResolvePayload) -> ActionResult:
    """把失败记录办结；已办结的记录不允许重复处理。"""
    record, error = service.resolve_failure(record_id, payload.note)
    if record is None:
        return ActionResult(ok=False, message=error)
    return ActionResult(ok=True, message="失败记录已办结", entry=record)


@router.post("/trial")
def run_trial(payload: TrialPayload) -> dict[str, Any]:
    """对同一挑战样本比较多条启用策略的候选结果，全程沙箱模拟、无外部调用。"""
    result, error = service.trial(payload.sample, payload.scenario)
    if error:
        raise HTTPException(status_code=400, detail=error)
    return result


@router.get("/{entry_id}", response_model=dict)
def get_entry(entry_id: int) -> dict:
    """读取单条对抗策略明细；不存在时给出可读的错误说明。"""
    entry = service.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"对抗策略 {entry_id} 不存在或已归档")
    return entry


@router.post("", response_model=ActionResult)
def create_entry(payload: EntryPayload) -> ActionResult:
    """登记一条结构化对抗策略；字段不达标时先存草稿并附不可执行原因。"""
    entry, problems = service.create_entry(payload.values)
    if entry is None:
        return ActionResult(ok=False, message="；".join(problems))
    if problems:
        return ActionResult(ok=False, message="策略已存为草稿，但暂不可执行：" + "；".join(problems), entry=entry)
    return ActionResult(ok=True, message="对抗策略已登记，可提交校验后启用", entry=entry)


@router.post("/{entry_id}/actions", response_model=ActionResult)
def run_action(entry_id: int, payload: EntryPayload) -> ActionResult:
    """对单条对抗策略执行提交校验、启用策略、停用策略；不可执行的旧配置无法启用。"""
    action = str(payload.values.get("action") or "").strip()
    entry, message = service.run_action(entry_id, action)
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry)


@router.get("/export")
def export_entries() -> dict[str, Any]:
    """导出反爬对抗清单：返回当前过滤条件下的全量数据（含可执行判定标记）。"""
    items = service.export_entries()
    return {"module": "antibot", "total": len(items), "items": items}
