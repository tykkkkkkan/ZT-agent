"""契约验证：现场演示自检命令 `manage.py lancheck` —— ZT-agent

为什么值得单独验：这条命令是**去外地演示前唯一要跑的东西**，它自己坏了
（报错、输出乱码、给了错的地址）比不跑还糟 —— 现场没人能debug它。

四节：
  【1】命令能被 Django 正常加载，且 `--help` 不炸
  【2】输出里必须给出**可粘贴的完整地址**（/index.html、/healthz、/pay/confirm/）
  【3】输出必须全部是 **GBK 可编码**的字符
        —— 直接跑在控制台时 Python 走 UTF-16，什么字符都能打；
           一旦重定向到文件 / 管道（`> log.txt`），stdout 退化成 cp936，
           一个 U+2713(✓) 就足够让命令 UnicodeEncodeError 崩掉。
           所以刻意只用 √ × ─ · 这些 GBK 也有的符号。
  【4】ALLOWED_HOSTS 通了要说通、没通要说清怎么改；端口没监听要给出正确的启动命令

用法：./.venv/Scripts/python.exe scripts/verify_lancheck.py
"""
import io
import os
import sys

import django

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
django.setup()

from django.core.management import call_command  # noqa: E402
from django.test import override_settings  # noqa: E402

PASS, FAIL = [], []


def check(label, ok, detail=""):
    (PASS if ok else FAIL).append(label)
    mark = "√" if ok else "×"
    line = f"  {mark} {label}"
    if detail and not ok:
        line += f"\n      → {detail}"
    print(line)


def run(port="8000", out=None):
    """跑一次 lancheck，返回 (stdout 文本, stderr 文本)。"""
    o, e = out or io.StringIO(), io.StringIO()
    call_command("lancheck", port=port, stdout=o, stderr=e)
    return o.getvalue(), e.getvalue()


print("=" * 64)
print("lancheck —— 现场演示自检命令")
print("=" * 64)

# ════════════════════════════════════════════════════════════════
print("\n【1】命令可加载")
# ════════════════════════════════════════════════════════════════
try:
    text, _ = run()
    check("`manage.py lancheck` 能跑通且不抛异常", True)
except Exception as exc:  # noqa: BLE001
    text = ""
    check("`manage.py lancheck` 能跑通且不抛异常", False, repr(exc))

from agent.cashier import lan_ip  # noqa: E402

ip = lan_ip()
check("取到了本机联网地址（取不到时应给出排查提示而非崩溃）",
      bool(ip) or "取不到本机联网地址" in text,
      f"lan_ip() 返回 {ip!r}，输出里既没地址也没提示")

# ════════════════════════════════════════════════════════════════
print("\n【2】必须给出可粘贴的完整地址")
# ════════════════════════════════════════════════════════════════
if ip:
    base = f"http://{ip}:8000"
    check("给出了电脑端入口地址（/index.html）", f"{base}/index.html" in text)
    check("给出了手机端连通性探针地址（/healthz）", f"{base}/healthz" in text)
    check("说明了二维码会写成什么（/pay/confirm/）", "/pay/confirm/" in text)
    check("提醒了「别用 localhost 打开」", "别用 localhost" in text)
    check("提醒了必须绑 0.0.0.0（不是 127.0.0.1）", "0.0.0.0" in text)
else:
    check("取不到地址时，输出里给出了原因", "取不到本机联网地址" in text)

# ════════════════════════════════════════════════════════════════
print("\n【3】输出必须 GBK 可编码（重定向到文件时不会崩）")
# ════════════════════════════════════════════════════════════════
try:
    text.encode("gbk")
    check("全部输出都能用 GBK 编码（`> out.txt` 不会 UnicodeEncodeError）", True)
except UnicodeEncodeError as exc:
    bad = text[exc.start:exc.end]
    check("全部输出都能用 GBK 编码（`> out.txt` 不会 UnicodeEncodeError）", False,
          f"第 {exc.start} 字符 {bad!r}（U+{ord(bad[0]):04X}）不在 GBK 里，"
          "请换成 √ × ─ · 之类的符号")

# ════════════════════════════════════════════════════════════════
print("\n【4】三种状态各说各的话")
# ════════════════════════════════════════════════════════════════
with override_settings(ALLOWED_HOSTS=["*"]):
    t, _ = run()
    check("ALLOWED_HOSTS=* → 判定放行", "√ ALLOWED_HOSTS = *" in t)
    check("ALLOWED_HOSTS=* → 说明「换网络不用改配置」", "换任何网络都不用再改配置" in t)

with override_settings(ALLOWED_HOSTS=["127.0.0.1", "localhost"]):
    t, _ = run()
    check("ALLOWED_HOSTS 未放行 → 明确报 ×", "× ALLOWED_HOSTS" in t)
    check("未放行时提示改成 *", "ALLOWED_HOSTS=*" in t)
    check("未放行时点明后果（400 / 纯白页）", "400" in t and "纯白页" in t)
    check("未放行时提醒改完要完全重启（自动重载读不到）", "重启" in t)

# 8099 上没有任何服务 → 走「没监听」分支
t_empty, _ = run(port="8099")
check("端口没监听 → 输出「没有监听」而不是谎报通畅", "没有监听" in t_empty)
check("没监听时给出正确的启动命令（带 0.0.0.0）",
      "runserver 0.0.0.0:8099" in t_empty)
check("没监听时点明「只绑 127.0.0.1 手机连不上」", "127.0.0.1" in t_empty)

# 与真实监听状态交叉验证（8000 上有没有服务，以本机实际探测为准）
import socket  # noqa: E402

if ip:
    try:
        with socket.create_connection((ip, 8000), timeout=0.4):
            alive = True
    except OSError:
        alive = False
    t8000, _ = run(port="8000")
    said_alive = "服务已监听" in t8000
    check(f"对 8000 的监听判断与实际一致（实际 {'在跑' if alive else '没跑'}）",
          said_alive == alive,
          f"命令说 {said_alive}，实际 {alive}")

print()
print("=" * 64)
print(f"  通过 {len(PASS)} 项 / 失败 {len(FAIL)} 项")
print("=" * 64)
if FAIL:
    print("失败项：")
    for f in FAIL:
        print(f"  × {f}")
    sys.exit(1)
