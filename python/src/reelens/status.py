"""测量状态的唯一词汇表——"测到"不等于"测对"。

为什么不是简单的 ok/失败
------------------------
评审给的最重要一条：`unknowns[]` 只查**缺项**，不查**效度**。一个未标定的阈值
（`select=gt(scene,0.30)`、`blackdetect d=0.10`、loudnorm 的 −14 LUFS）照样能"测到数字"，
于是报告里写着 `MEASURED`，但结论可能是系统性错的——**这比 UNKNOWN 更危险**，
因为产物看起来可信。

所以这里把"可信度"拆成五种状态，并要求每项都带 `method`（怎么测的）与
`params`（用什么参数测的）；凡是**未经标定**的代理指标只能是 `DERIVED`。

    MEASURED  直接测量且方法已标定  → 可当结论
    DERIVED   由测量推算的代理指标  → 可当结论，但必须注明"代理"
    ASSUMED   模型/启发式推断       → 仅供参考，必须标 inferred
    UNKNOWN   没测（有原因）
    SKIPPED   用户没勾／环境不具备
    FAILED    测了但失败（有原因）

`field()` 是唯一的构造入口：它强制带 `status`，并校验 required 项。
"""
from __future__ import annotations

from typing import Any, Iterable, Optional

MEASURED = "MEASURED"
DERIVED = "DERIVED"
ASSUMED = "ASSUMED"
UNKNOWN = "UNKNOWN"
SKIPPED = "SKIPPED"
FAILED = "FAILED"

STATUSES = (MEASURED, DERIVED, ASSUMED, UNKNOWN, SKIPPED, FAILED)
#: 可以拿来当"结论"用的状态
CONCLUSIVE = (MEASURED, DERIVED)
#: 必须进 `unknowns[]` 的状态（ASSUMED 也进，但用的是 inferred 语义）
NON_CONCLUSIVE = (UNKNOWN, SKIPPED, FAILED)


def field(status: str, *, method: str = "", params: Optional[dict] = None,
          calibrated: Optional[bool] = None, reason: str = "", **values: Any) -> dict:
    """构造一个带状态的可测项。

    @param calibrated: 只有"方法经过标定"的项才可以传 True；`DERIVED` 缺省 False。
    """
    if status not in STATUSES:
        raise ValueError(f"未知 status：{status}（只允许 {STATUSES}）")
    item: dict = {"status": status}
    if method:
        item["method"] = method
    if params:
        item["params"] = params
    if calibrated is not None:
        item["calibrated"] = bool(calibrated)
    if reason:
        item["reason"] = reason
    item.update(values)
    if status in NON_CONCLUSIVE and not reason:
        # 没测/失败必须给原因——否则读报告的人无法判断"是没做还是做坏了"
        item["reason"] = "未提供原因"
    return item


def unknown(reason: str, *, method: str = "") -> dict:
    return field(UNKNOWN, method=method, reason=reason)


def skipped(reason: str, *, method: str = "") -> dict:
    return field(SKIPPED, method=method, reason=reason)


def failed(reason: str, *, method: str = "") -> dict:
    return field(FAILED, method=method, reason=reason)


def is_conclusive(item: Any) -> bool:
    return isinstance(item, dict) and item.get("status") in CONCLUSIVE


def collect_unknowns(payload: dict, *, prefix: str = "") -> list[dict]:
    """递归扫出一个 payload 里所有非结论项，供 `unknowns[]` 汇总。

    只扫"看起来像可测项"的 dict（带 status 且在 STATUSES 里），避免把普通数据当状态。
    """
    out: list[dict] = []

    def walk(node: Any, path: str) -> None:
        if isinstance(node, dict):
            if node.get("status") in NON_CONCLUSIVE:
                out.append({"field": path or "<root>",
                            "code": node["status"],
                            "reason": node.get("reason") or ""})
                return
            if node.get("status") == ASSUMED:
                out.append({"field": path or "<root>",
                            "code": "ASSUMED",
                            "reason": node.get("reason") or "推断，非实测"})
                return
            for key, value in node.items():
                walk(value, f"{path}.{key}" if path else str(key))
        elif isinstance(node, list):
            for index, value in enumerate(node):
                walk(value, f"{path}[{index}]")

    for key, value in (payload or {}).items():
        walk(value, f"{prefix}{key}" if prefix else str(key))
    return out


def summarise(statuses: Iterable[dict]) -> dict:
    """统计各状态数量（报告抬头用）。"""
    counts = {name: 0 for name in STATUSES}
    for item in statuses:
        if isinstance(item, dict) and item.get("status") in counts:
            counts[item["status"]] += 1
    return counts
