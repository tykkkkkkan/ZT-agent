"""
验证 ZT → 营销 的出站协调通知（此前这条链路是断的）

背景：营销侧早就实现了 `POST /api/operations/coordination/inbound`（event=return_requested），
但 ZT-agent 全仓没有任何调用方 —— 产品文案声称的「收到退货申请 → 通知营销助手复盘」
实际从未发生过。本脚本验证修复后的四条关键性质：

  ① 关闭（未配置 URL）：不发任何请求，退货申请依然成功 —— 单机零依赖
  ② 开启：退货申请成功 → 通知真的发出，报文含商品维度与订单号，且**不外发客户隐私**
  ③ 投递失败（营销服务未启动）：退货申请依然成功，不抛异常、不阻塞主流程
  ④ 无关联商品的历史订单：安全跳过；重复提交退货不会重复通知

⚠️ 不使用 Django TestCase：本项目的 orders/inventory 是 `managed=False` 的历史表，
测试库不会为它们建表。因此改为「造临时订单 → 断言 → finally 里删除」，
并且只走 SHIPPED→RETURNING（该跃迁**不触碰库存、不动钱包**），确保零业务副作用。

用法：./.venv/Scripts/python.exe scripts/verify_coordination_notify.py
"""
import json
import os
import sys
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, HTTPServer

import django

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
django.setup()

from django.conf import settings                            # noqa: E402
from django.utils import timezone                            # noqa: E402

from agent.models import Inventory, Orders, OrderStatus, Products, Transaction  # noqa: E402
from agent.services import OrderTransitionError, transition_order                # noqa: E402

RECEIVED = []
PASS, FAIL = [], []
CREATED = []


class _Handler(BaseHTTPRequestHandler):
    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        RECEIVED.append({
            "path": self.path,
            "token": self.headers.get("X-Coord-Token", ""),
            "body": json.loads(raw.decode("utf-8")),
        })
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b'{"success": true, "message": "ok"}')

    def log_message(self, *a):
        pass


def check(label, cond, detail=""):
    (PASS if cond else FAIL).append(label)
    print(f"  {'✓' if cond else '✗'} {label}" + (f"  ← {detail}" if detail else ""))


def start_server():
    srv = HTTPServer(("127.0.0.1", 0), _Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_port}"


def make_order(product, with_product=True):
    """造一笔临时「已发货」订单（仅用于触发退货申请，不动库存/钱包）。"""
    o = Orders.objects.create(
        order_no=f"DDTST{uuid.uuid4().hex[:12].upper()}",
        customer_name="通知链路测试",
        phone="13800000000",
        product_id=product.id if with_product else None,
        product_name=product.name if with_product else "历史商品",
        product_sku=(product.sku or "") if with_product else "",
        quantity=1,
        unit_price=product.retail_price or 0,
        total_price=product.retail_price or 0,
        status=OrderStatus.SHIPPED,
        shipped_at=timezone.now(),
        created_at=timezone.now(),
    )
    CREATED.append(o.pk)
    return o


def submit_return(order, reason="饵料受潮结块"):
    transition_order(order, to_status=OrderStatus.RETURNING, return_reason=reason)


def wait_for_requests(n=1, timeout=5.0):
    end = time.time() + timeout
    while time.time() < end and len(RECEIVED) < n:
        time.sleep(0.05)


def cleanup():
    """删除临时订单（含可能产生的流水，理论上 RETURNING 不产生）。"""
    if not CREATED:
        return
    Transaction.objects.filter(order_id__in=CREATED).delete()
    Orders.objects.filter(pk__in=CREATED).delete()
    print(f"\n已清理临时订单 {len(CREATED)} 笔，真实业务数据未被污染")


def main():
    product = Products.objects.filter(is_active=True).first()
    if product is None:
        print("库中无在售商品，无法验证")
        return 1
    inv = Inventory.objects.filter(product_id=product.id).first()
    print(f"测试商品：#{product.id} {product.name}")
    print(f"库存隔离检查：RETURNING 跃迁不改库存/不动钱包，"
          f"当前 stock={inv.stock if inv else '-'} reserved={inv.reserved_stock if inv else '-'}")

    srv, base_url = start_server()
    print(f"本地模拟营销 Agent：{base_url}\n")
    real_base = getattr(settings, "MKT_AGENT_BASE_URL", "")

    try:
        # ── ① 关闭：零依赖 ─────────────────────────────────────
        print("① 未配置 MKT_AGENT_BASE_URL（关闭态）")
        RECEIVED.clear()
        settings.MKT_AGENT_BASE_URL = ""
        order = make_order(product)
        submit_return(order)
        wait_for_requests(1, timeout=0.6)
        order.refresh_from_db()
        check("退货申请成功", order.status == OrderStatus.RETURNING, order.status)
        check("未发出任何请求（关闭态零依赖）", len(RECEIVED) == 0, f"收到 {len(RECEIVED)}")

        # ── ② 开启：通知发出且内容正确 ─────────────────────────
        print("\n② 配置 MKT_AGENT_BASE_URL（开启态）")
        RECEIVED.clear()
        settings.MKT_AGENT_BASE_URL = base_url
        order2 = make_order(product)
        submit_return(order2)
        wait_for_requests(1)
        order2.refresh_from_db()
        check("退货申请成功", order2.status == OrderStatus.RETURNING, order2.status)
        check("通知已送达（1 笔）", len(RECEIVED) == 1, f"收到 {len(RECEIVED)}")
        if RECEIVED:
            got = RECEIVED[0]
            body = got["body"]
            check("路径指向营销侧入站端点",
                  got["path"].endswith("/api/operations/coordination/inbound"), got["path"])
            check("事件名为 return_requested", body.get("event") == "return_requested",
                  str(body.get("event")))
            check("携带商品 id / 名称",
                  body.get("product_id") == product.id and body.get("product_name") == product.name,
                  f"#{body.get('product_id')} {body.get('product_name')}")
            check("携带订单号", body.get("order_no") == order2.order_no, str(body.get("order_no")))
            check("detail 说明这是一笔退货申请",
                  "退货申请" in (body.get("detail") or ""), (body.get("detail") or "")[:70])
            # 隐私边界：营销侧做商品售后复盘，不需要个人信息
            blob = json.dumps(body, ensure_ascii=False)
            check("报文不含客户姓名", order2.customer_name not in blob, order2.customer_name)
            check("报文不含客户手机号", (order2.phone or "13800000000") not in blob,
                  order2.phone or "")

        # ── ③ 共享密钥 ─────────────────────────────────────────
        print("\n③ 配置 COORD_SHARED_SECRET（与营销侧鉴权对齐）")
        RECEIVED.clear()
        real_secret = getattr(settings, "COORD_SHARED_SECRET", "")
        settings.COORD_SHARED_SECRET = "shared-secret-xyz"
        submit_return(make_order(product))
        wait_for_requests(1)
        check("请求已送达", len(RECEIVED) == 1, f"收到 {len(RECEIVED)}")
        if RECEIVED:
            check("携带 X-Coord-Token 头", RECEIVED[0]["token"] == "shared-secret-xyz",
                  RECEIVED[0]["token"] or "(空)")
        settings.COORD_SHARED_SECRET = real_secret

        # ── ④ 投递失败不影响主流程 ─────────────────────────────
        print("\n④ 营销 Agent 未启动（投递必然失败）")
        settings.MKT_AGENT_BASE_URL = "http://127.0.0.1:9"   # 保留端口，连接必失败
        order4 = make_order(product)
        try:
            submit_return(order4, reason="服务不可达测试")
            raised = None
        except Exception as e:  # noqa: BLE001
            raised = e
        order4.refresh_from_db()
        check("退货申请未因通知失败而抛异常", raised is None,
              f"{type(raised).__name__}: {raised}" if raised else "")
        check("退货申请依然成功", order4.status == OrderStatus.RETURNING, order4.status)

        # ── ⑤ 无商品维度 / 重复提交 ────────────────────────────
        print("\n⑤ 边界：无关联商品的历史订单 + 重复提交")
        RECEIVED.clear()
        settings.MKT_AGENT_BASE_URL = base_url
        order5 = make_order(product, with_product=False)
        submit_return(order5)
        wait_for_requests(1, timeout=0.8)
        order5.refresh_from_db()
        check("无商品的历史订单退货申请仍成功", order5.status == OrderStatus.RETURNING, order5.status)
        check("无商品维度时不发通知", len(RECEIVED) == 0, f"收到 {len(RECEIVED)}")

        RECEIVED.clear()
        order6 = make_order(product)
        submit_return(order6)
        wait_for_requests(1)
        first = len(RECEIVED)
        try:
            submit_return(order6)     # 重复提交，应被状态机拒绝
            dup_raised = None
        except OrderTransitionError as e:
            dup_raised = e
        wait_for_requests(2, timeout=0.6)
        check("首次提交通知 1 笔", first == 1, f"收到 {first}")
        check("重复提交被状态机拒绝",
              dup_raised is not None and "退货申请中" in str(dup_raised), str(dup_raised))
        check("重复提交不产生第二次通知", len(RECEIVED) == 1, f"收到 {len(RECEIVED)}")

        # ── ⑥ 库存与账目零副作用 ───────────────────────────────
        print("\n⑥ 副作用检查（RETURNING 不应动库存/钱包）")
        if inv:
            inv.refresh_from_db()
            print(f"    库存：stock={inv.stock} reserved={inv.reserved_stock}")
        tx_count = Transaction.objects.filter(order_id__in=CREATED).count()
        check("临时订单未产生任何钱包流水", tx_count == 0, f"{tx_count} 笔")

    finally:
        settings.MKT_AGENT_BASE_URL = real_base
        srv.shutdown()
        cleanup()

    print("\n" + "=" * 64)
    print(f"结果：{len(PASS)} 通过 / {len(FAIL)} 失败")
    for f in FAIL:
        print("  ✗", f)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
