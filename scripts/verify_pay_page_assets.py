"""契约验证：收银台两个页面的**资源可达性** —— ZT-agent

为什么单独有这么一个脚本：`/pay/<order_no>/` 与 `/pay/confirm/<token>/` 是本站仅有的
**嵌套路径**页面（其余页面都挂在根上，例如 `/order.html`）。挂在根上时
`href="assets/theme.css"` 恰好能解析对，所以这个写法一路"看着没问题"；
一旦页面挂到两层路径下，同一个相对路径会解析成
`/pay/ZT-xxx/assets/theme.css` → **404**，后果是：

    · theme.css / tailwind.build.css 全丢 → 页面变成裸 HTML（"太丑"）
    · auth.js 404 → 全局 `AUTH` 未定义 → 初始化第一行就抛错
      → 订单信息永远停在「加载中…」、二维码永远停在「正在生成二维码…」
      （看起来像"支付功能没做完"，其实是资源根本没加载）

两个症状同一个根因。本脚本把这件事钉死：任何**新的**站内引用只要写成相对路径，
或者哪条引用的路由被删掉（404），这里就会红。

四节：
  【1】静态：两个页面里所有 href/src（**含 JS 字符串里动态拼的那些**）都必须是根绝对路径
  【2】静态：关键引用显式点名（三件套：theme.css / tailwind.build.css / auth.js）
  【3】反证：把"相对路径会 404"写进测试 —— 防止有人"顺手改回相对路径更简洁"
  【4】实测：页面里每一条站内引用，用 Django 测试客户端真发一次请求，必须 200

第 4 节比静态断言强：它证明的是"路由真的接得住"，而不是"我写对了字符串"。

用法：./.venv/Scripts/python.exe scripts/verify_pay_page_assets.py
"""
import os
import re
import sys

import django

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
django.setup()

from django.conf import settings  # noqa: E402
from django.test import Client  # noqa: E402

# Django 测试客户端的默认 Host 是 testserver，而本项目 ALLOWED_HOSTS 是白名单，
# 不放行的话每个请求都返回 400（DisallowedHost），看着像"路由全坏了"。
if "testserver" not in settings.ALLOWED_HOSTS:
    settings.ALLOWED_HOSTS.append("testserver")

PASS, FAIL = [], []

PAGES = ["frontend/pay.html", "frontend/pay_confirm.html"]
SAMPLE_ORDER = "ZT-20260101-0001"      # 页面本身不查库，只要路由在就能返回 200
SAMPLE_TOKEN = "tok-asset-check"

# 不参与「必须根绝对路径」判定的引用：外链 / 同页锚点 / 非 HTTP 协议
EXTERNAL_PREFIXES = ("http://", "https://", "//", "#", "mailto:", "data:", "javascript:")


def check(label, ok, detail=""):
    (PASS if ok else FAIL).append(label)
    mark = "✓" if ok else "✗"
    line = f"  {mark} {label}"
    if detail and not ok:
        line += f"  ← {detail}"
    print(line)
    return ok


def read(path):
    with open(os.path.join(BASE, path), encoding="utf-8") as f:
        return f.read()


def refs_in(html):
    """取出所有 href="..." / src="..." 的值（HTML 与 JS 字符串一视同仁）。

    JS 拼出来的那一串同样是浏览器要解析的地址，漏掉它等于漏掉一半入口。
    """
    return re.findall(r'(?:href|src)="([^"]*)"', html)


def internal_refs(html):
    out = []
    for r in refs_in(html):
        r = r.strip()
        if not r or r.startswith(EXTERNAL_PREFIXES):
            continue
        if "{{" in r or "{%" in r:      # 万一将来改成 Django 模板渲染
            continue
        out.append(r)
    return out


def main():
    print("  收银台页面资源可达性 · 验收")
    print("=" * 64)

    # ════════════════════════════════════════════════════════════
    print("\n【1】站内引用一律根绝对路径（含 JS 动态拼的）")
    # ════════════════════════════════════════════════════════════
    htmls = {}
    for page in PAGES:
        htmls[page] = read(page)
        refs = internal_refs(htmls[page])
        check(f"{page} 有站内引用", len(refs) > 0, str(len(refs)))
        bad = [r for r in refs if not r.startswith("/")]
        check(f"{page} 没有相对路径引用", not bad, ", ".join(bad))
        # 顺带把"看起来像路径但被当外链放过去"的写法也挡掉
        check(f"{page} 没有 protocol-relative（//host/...）引用",
              not any(r.startswith("//") for r in refs_in(htmls[page])))

    # ════════════════════════════════════════════════════════════
    print("\n【2】关键引用点名")
    # ════════════════════════════════════════════════════════════
    pay, confirm = htmls[PAGES[0]], htmls[PAGES[1]]
    for page, html in (PAGES[0], pay), (PAGES[1], confirm):
        check(f"{page} 引 theme.css（带前导斜杠）", 'href="/assets/theme.css"' in html)
        check(f"{page} 引 tailwind.build.css（带前导斜杠）",
              'href="/assets/tailwind.build.css"' in html)
        check(f"{page} 引 favicon", 'href="/assets/favicon.svg"' in html)
    check("pay.html 引 auth.js（带前导斜杠）", 'src="/auth.js"' in pay)
    # 手机确认页**故意**不引 auth.js：二维码即授权，不必在手机上再登录一次
    check("pay_confirm.html 不作为脚本引入 auth.js（扫码即授权，手机端免登录）",
          not re.search(r'<script[^>]+src="[^"]*auth\.js"', confirm))
    # JS 里动态拼出来的出口链接同样是嵌套路径下的相对风险点
    check("JS 生成的「去充值」链接是根绝对路径", 'href="/profile.html#wallet"' in pay)
    check("JS 生成的「查看我的订单」链接是根绝对路径", 'href="/profile.html"' in pay)

    # ════════════════════════════════════════════════════════════
    print("\n【3】反证：相对路径在嵌套路径下确实 404")
    # ════════════════════════════════════════════════════════════
    c = Client()
    r = c.get(f"/pay/{SAMPLE_ORDER}/assets/theme.css")
    check("GET /pay/<no>/assets/theme.css → 404（这就是当初的坑）",
          r.status_code == 404, str(r.status_code))
    r = c.get(f"/pay/confirm/{SAMPLE_TOKEN}/assets/theme.css")
    check("GET /pay/confirm/<token>/assets/theme.css → 404",
          r.status_code == 404, str(r.status_code))
    r = c.get(f"/pay/{SAMPLE_ORDER}/auth.js")
    check("GET /pay/<no>/auth.js → 404（⇒ AUTH 未定义，页面永远「加载中…」）",
          r.status_code == 404, str(r.status_code))

    # ════════════════════════════════════════════════════════════
    print("\n【4】实测：两个页面 + 其全部站内引用都返回 200")
    # ════════════════════════════════════════════════════════════
    for url in (f"/pay/{SAMPLE_ORDER}/", f"/pay/confirm/{SAMPLE_TOKEN}/"):
        r = c.get(url)
        check(f"GET {url} → 200", r.status_code == 200, str(r.status_code))
        body = r.content.decode("utf-8")
        check(f"{url} 返回的是收银台页面本身", "<!DOCTYPE html>" in body)

        missing = []
        for ref in sorted(set(internal_refs(body))):
            rr = c.get(ref.split("#")[0])
            if rr.status_code != 200:
                missing.append(f"{ref} → {rr.status_code}")
        check(f"{url} 页面内 {len(set(internal_refs(body)))} 条站内引用全部 200",
              not missing, "; ".join(missing))

    # 单独把三件套点出来：它们是"能看、能跑"的最小集合
    for ref in ("/assets/theme.css", "/assets/tailwind.build.css",
                "/assets/favicon.svg", "/auth.js",
                "/index.html", "/profile.html", "/contact.html"):
        r = c.get(ref)
        check(f"GET {ref} → 200", r.status_code == 200, str(r.status_code))

    # auth.js 真的定义了全局 AUTH（200 不等于"内容对"）
    body = c.get("/auth.js").content.decode("utf-8")
    check("auth.js 里确实挂了 window.AUTH", "window.AUTH" in body or "AUTH =" in body)

    # ════════════════════════════════════════════════════════════
    print("\n【5】运行环境自检（页面依赖的运行期能力）")
    # ════════════════════════════════════════════════════════════
    # 这一节的用法有讲究：**用跑 runserver 的那个解释器来跑本脚本**。
    # 现场踩过的坑：代码与测试都跑在项目 .venv 里（有 qrcode），
    # 而 runserver 用的是另一个系统 Python（没有 qrcode）——
    # qr_svg() 按设计优雅降级返回空串，于是线上页面永远只显示
    # "二维码组件不可用"，测试却全绿。**代码对、环境缺件**这类问题
    # 只能靠"在正确的解释器上自检"暴露出来。
    print(f"      当前解释器：{sys.executable}")
    try:
        import qrcode  # noqa: F401
        has_qr = True
    except Exception as exc:  # noqa: BLE001
        has_qr = False
        missing_reason = repr(exc)
    check("运行环境里有 qrcode（缺了扫码支付会静默降级成明文链接）",
          has_qr, "" if has_qr else f"{missing_reason} —— 装：pip install qrcode==8.2")
    if has_qr:
        from agent.cashier import qr_svg  # noqa: E402
        svg = qr_svg(f"http://127.0.0.1:8000/pay/confirm/{SAMPLE_TOKEN}/")
        # SvgPathImage 会先写一行 <?xml ...?> 声明，所以判据是"含 <svg"而不是"以 <svg 开头"
        head = svg[:200]
        check("qr_svg() 真的产出 SVG（不是空串）",
              "<svg" in head and "<path" in svg, f"长度 {len(svg)}")
    else:
        check("qr_svg() 真的产出 SVG（不是空串）", False, "上游依赖缺失，跳过")

    print("\n" + "=" * 64)
    print(f"  通过 {len(PASS)} 项 / 失败 {len(FAIL)} 项")
    if FAIL:
        print("  失败项：")
        for f in FAIL:
            print(f"    ✗ {f}")
    print("=" * 64)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
