"""
agent/tools.py

Day5: Agent 工具函数，真正查询 agent_db 数据库。
工具名与 system_prompt.md 中的描述一致。
"""
from agent.models import Products, Inventory, Orders


def query_product(name: str) -> str:
    """按产品名查询，返回中文描述。"""
    try:
        product = Products.objects.filter(name__icontains=name).first()
    except Exception as e:
        return f"查询产品出错：{e}"

    if not product:
        return f"没找到叫「{name}」的产品，麻烦确认下名字？"

    lines = [
        f"产品：{product.name}",
        f"规格：{product.spec or '无'}",
        f"适用鱼种：{product.target_fish or '无'}",
        f"零售价：{product.retail_price} 元/包",
        f"批发价：{product.wholesale_price} 元/包",
    ]
    if product.description:
        lines.append(f"描述：{product.description}")
    return "\n".join(lines)


def check_inventory(name: str) -> str:
    """按产品名查库存，低于预警线要预警。"""
    try:
        product = Products.objects.filter(name__icontains=name).first()
        if not product:
            return f"没找到叫「{name}」的产品，无法查库存。"
        inv = Inventory.objects.filter(product=product).first()
    except Exception as e:
        return f"查询库存出错：{e}"

    if not inv:
        return f"「{product.name}」暂无库存记录。"

    lines = [
        f"产品：{product.name}",
        f"当前库存：{inv.stock} 包",
        f"预警线：{inv.alert_line} 包",
    ]
    if inv.stock == 0:
        lines.append("⚠️ 已无库存，请尽快补货！")
    elif inv.stock < inv.alert_line:
        lines.append(f"⚠️ 库存偏少，低于预警线 {inv.alert_line}，建议尽快下单。")
    else:
        lines.append("库存充足。")
    return "\n".join(lines)


def get_order_status(order_no: str) -> str:
    """按订单号查询订单状态。"""
    try:
        order = Orders.objects.filter(order_no=order_no).first()
    except Exception as e:
        return f"查询订单出错：{e}"

    if not order:
        return f"没找到订单号「{order_no}」，麻烦确认下号码？"

    lines = [
        f"订单号：{order.order_no}",
        f"客户：{order.customer_name or '无'}",
        f"商品：{order.product_name or '无'}",
        f"数量：{order.quantity or 0}",
        f"总价：{order.total_price or 0} 元",
        f"状态：{order.status or '未知'}",
        f"下单时间：{order.created_at or '未知'}",
    ]
    return "\n".join(lines)


# 工具注册表：AI 输出 {"tool": "xxx", "arg": "xxx"} 时，映射到对应函数
TOOL_REGISTRY = {
    "query_product": query_product,
    "check_inventory": check_inventory,
    "get_order_status": get_order_status,
}


def call_tool(tool_name: str, arg: str) -> str:
    """根据工具名和参数执行对应函数，返回结果字符串。"""
    func = TOOL_REGISTRY.get(tool_name)
    if not func:
        return f"未知工具：{tool_name}，支持的工具：{list(TOOL_REGISTRY.keys())}"
    return func(arg)
