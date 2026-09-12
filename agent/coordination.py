"""
agent/coordination.py — ZT-agent 主动向营销 Agent 推送协调事件（出站钩子）

背景（这是一条**断掉的链路**）
------------------------------
营销 Agent 侧早就实现了入站端点：
    POST /api/operations/coordination/inbound
    event = "return_requested" → 登记一条「ZT 告知退货申请」协调事件，
    供运营在面板里复盘该商品的售后策略。

但 ZT-agent 侧**从来没有调用过它** —— 全仓搜索无任何调用方。
于是产品页面写着「前台收到退货申请 → 通知营销助手复盘该商品售后策略」，
实际这条链路是死的：用户申请退货，营销侧一无所知。

本模块把出站侧补齐，并遵守三条约束：

  1. **绝不影响主流程**：通知在 `transaction.on_commit` 之后、以守护线程发出，
     超时 3 秒、异常只记日志。营销 Agent 没启动 / 挂了，退货申请照样成功。
  2. **可关闭**：未配置 `MKT_AGENT_BASE_URL` 时整体静默跳过（默认关闭，
     本地开发与单机部署不会因为另一个服务不在而产生噪音日志）。
  3. **不泄露隐私**：只发商品维度与订单号，**不发客户姓名与手机号**
     —— 营销侧做的是商品售后策略复盘，不需要个人信息。
"""
from __future__ import annotations

import json
import logging
import threading
import urllib.error
import urllib.request
from urllib.parse import urljoin

from django.conf import settings

logger = logging.getLogger(__name__)

# 出站事件名（与营销侧 handle_inbound_from_zt 的取值一致）
EVENT_RETURN_REQUESTED = "return_requested"

# 送达超时（秒）：必须短 —— 它是旁路通知，不能拖慢退货申请接口
DELIVER_TIMEOUT = 3


def _endpoint() -> str:
    base = (getattr(settings, "MKT_AGENT_BASE_URL", "") or "").strip()
    if not base:
        return ""
    return urljoin(base.rstrip("/") + "/", "api/operations/coordination/inbound")


def is_enabled() -> bool:
    return bool(_endpoint())


def _post(url: str, payload: dict) -> tuple[bool, str]:
    """同步发送（只在后台线程里调用）。返回 (ok, message)。"""
    headers = {"Content-Type": "application/json"}
    secret = (getattr(settings, "COORD_SHARED_SECRET", "") or "").strip()
    if secret:
        headers["X-Coord-Token"] = secret
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=body, method="POST", headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=DELIVER_TIMEOUT) as resp:
            return 200 <= resp.status < 300, f"HTTP {resp.status}"
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = e.read()[:200].decode("utf-8", "ignore")
        except Exception:  # noqa: BLE001
            pass
        return False, f"HTTP {e.code} {detail}"
    except Exception as e:  # noqa: BLE001 — 网络/超时/解析都不该冒泡
        return False, f"{type(e).__name__}: {e}"


def _deliver(url: str, payload: dict) -> None:
    ok, message = _post(url, payload)
    if ok:
        logger.info("已通知营销 Agent：%s", payload.get("event"))
    else:
        # 降级为 warning 而非 error：营销 Agent 未部署属于正常情况，
        # 真正的失败可在运营页面「跨 Agent 协作」里从缺失的事件看出来。
        logger.warning("通知营销 Agent 失败（%s）：%s", message, payload.get("event"))


def notify_async(event: str, payload: dict) -> bool:
    """异步投递一条协调事件。返回是否已投递（False = 未启用，直接跳过）。

    永不抛异常、永不阻塞调用方。
    """
    url = _endpoint()
    if not url:
        return False
    try:
        # daemon=True：进程退出不等待，避免测试/重启时卡住
        threading.Thread(
            target=_deliver, args=(url, {"event": event, **payload}),
            name=f"coord-notify-{event}", daemon=True,
        ).start()
        return True
    except Exception:  # noqa: BLE001 — 线程创建失败也不能影响主流程
        logger.warning("协调事件投递线程创建失败：%s", event, exc_info=True)
        return False


def notify_return_requested(order) -> bool:
    """用户提交退货申请 → 通知营销 Agent 复盘该商品售后策略。

    :param order: Orders 实例（须已进入「退货申请中」状态）
    """
    if order is None:
        return False
    product_id = order.product_id or 0
    if not product_id:
        # 历史订单可能没有关联商品；没有商品维度，营销侧无法复盘，跳过
        return False
    name = order.product_name or f"商品#{product_id}"
    reason = (order.return_reason or "").strip()
    detail = (
        f"「{name}」收到一笔退货申请（订单 {order.order_no}）"
        + (f"，客户填写原因：{reason[:80]}" if reason else "")
        + "。建议复盘该商品的质量描述、包装与售后策略。"
    )
    return notify_async(EVENT_RETURN_REQUESTED, {
        "product_id": int(product_id),
        # 只发商品标识与订单号，刻意不带客户姓名/手机号（营销侧无需个人信息）
        "product_name": name,
        "sku": order.product_sku or "",
        "order_no": order.order_no,
        "detail": detail,
    })
