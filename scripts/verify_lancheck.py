"""契约验证：现场演示自检命令 `manage.py lancheck` —— ZT-agent

为什么值得单独验：这条命令是**去外地演示前唯一要跑的东西**，它自己坏了
（报错、输出乱码、给了错的地址）比不跑还糟 —— 现场没人能debug它。

六节：
  【1】命令能被 Django 正常加载，且取不到地址时给提示而非崩溃
  【2】输出里必须给出**可粘贴的完整地址**（/index.html、/healthz、/pay/confirm/）
  【3】输出必须全部是 **GBK 可编码**的字符
        —— 直接跑在控制台时 Python 走 UTF-16，什么字符都能打；
           一旦重定向到文件 / 管道（`> log.txt`），stdout 退化成 cp936，
           一个 U+2713(✓) 就足够让命令 UnicodeEncodeError 崩掉。
           所以刻意只用 √ × ─ · ※ 这些 GBK 也有的符号。
  【4】ALLOWED_HOSTS 通了要说通、没通要说清怎么改；端口状态要如实说
  【5】`--serve`：把「自检 + 启动」并成一步，但**端口已有服务时必须拦下来**
        —— Windows 允许 `0.0.0.0:8000` 与 `127.0.0.1:8000` 同时监听，
           再起一个不报错，却会导致 localhost 命中旧进程（最难查的一类）。
        —— 同时守住「默认只读」的承诺：不带 --serve 时绝不启动服务。
  【6】`--serve` 模式的输出同样 GBK 可编码（各分支都要，不只是默认分支）
  【7】`--tunnel`：公网地址这条路
        —— 公网域名与局域网 IP 的检查方式**必须不同**：前者要发真正的 HTTP
           请求（走的是和手机一样的路），后者才用 socket 直连。
           曾经对 `https://xxx.trycloudflare.com` 报出「请用 runserver 0.0.0.0:443
           启动」—— 驴唇不对马嘴。
        —— 云梯客户端缺失 / 隧道建立失败时，必须**明确中止**，绝不悄悄降级成
           局域网地址（降级会让你以为隧道通了，实际码里还是局域网 IP）。
  【8】双击启动器 `start-demo.bat`：纯 ASCII、CRLF、走 `py -3.12`

用法：./.venv/Scripts/python.exe scripts/verify_lancheck.py
"""
import io
import os
import socket
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


def run(port="8000", out=None, **kw):
    """跑一次 lancheck，返回 (stdout 文本, stderr 文本)。"""
    o, e = out or io.StringIO(), io.StringIO()
    call_command("lancheck", port=port, **kw, stdout=o, stderr=e)
    return o.getvalue(), e.getvalue()


def gbk_ok(text):
    try:
        text.encode("gbk")
        return True, ""
    except UnicodeEncodeError as exc:
        bad = text[exc.start:exc.end]
        return False, (f"第 {exc.start} 字符 {bad!r}（U+{ord(bad[0]):04X}）不在 GBK 里，"
                       "请换成 √ × ─ · ※ 之类的符号")


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

if sys.prefix != getattr(sys, "base_prefix", sys.prefix):
    check("在虚拟环境里跑时会提醒「改用系统 Python」（防火墙规则按 exe 路径匹配）",
          "虚拟环境" in text)
else:
    check("非虚拟环境 → 无需提醒（本项在此环境下不适用）", True)

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
    check("告诉用户可以用 --serve 省掉第二条命令", "--serve" in text)
else:
    check("取不到地址时，输出里给出了原因", "取不到本机联网地址" in text)

# ════════════════════════════════════════════════════════════════
print("\n【3】输出必须 GBK 可编码（重定向到文件时不会崩）")
# ════════════════════════════════════════════════════════════════
ok, why = gbk_ok(text)
check("全部输出都能用 GBK 编码（`> out.txt` 不会 UnicodeEncodeError）", ok, why)

# ════════════════════════════════════════════════════════════════
print("\n【4】四种状态各说各的话")
# ════════════════════════════════════════════════════════════════
with override_settings(ALLOWED_HOSTS=["*"]):
    t, _ = run()
    check("ALLOWED_HOSTS=* → 判定放行", "√ ALLOWED_HOSTS = *" in t)
    check("ALLOWED_HOSTS=* → 说明「换网络不用改配置」", "换任何网络" in t and "不用再改配置" in t)

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

# ════════════════════════════════════════════════════════════════
print("\n【5】--serve：两步并成一步，但该拦的要拦")
# ════════════════════════════════════════════════════════════════
from agent.management.commands import lancheck as lc  # noqa: E402

parser = lc.Command().create_parser("manage.py", "lancheck")
opts = parser.parse_args([])
check("默认不启动服务 —— 「只读」的承诺没破", opts.serve is False)
check("--serve 开关存在", parser.parse_args(["--serve"]).serve is True)
check("--force 开关存在", parser.parse_args(["--force"]).force is True)
check("--host 默认 0.0.0.0（不是只绑回环）", opts.host == "0.0.0.0")
check("--port 仍是老参数（不破坏既有用法）",
      parser.parse_args(["--port", "8123"]).port == "8123")

# 找一个空闲端口，真起一个监听来模拟「端口上已经有服务在跑」
#
# ⚠️ 必须配一个 accept 线程：`listen(1)` 的 backlog 只有 1，而本脚本在一次运行里
#    会对同一端口探测至少两次（先看状态、再看拦路检查）—— 没有 accept 时第一个
#    连接占满队列，第二个直接被拒，于是拦路检查会误判成"端口是空的"。
#    （这个坑当场抓到过：`_busy_on_loopback` 第一次 True、第二次 False。）
srv, probe, stopper, acceptor = None, None, None, None
for cand in range(18123, 18163):
    s = socket.socket()
    try:
        s.bind(("127.0.0.1", cand))
        s.listen(64)
    except OSError:
        s.close()
        continue
    srv, probe = s, str(cand)
    break
check("找到一个空闲端口用来模拟「已有服务在跑」", probe is not None,
      "18123-18162 全被占用")

if probe:
    import threading  # noqa: E402

    stopper = threading.Event()

    def _accept_forever():
        srv.settimeout(0.2)
        while not stopper.is_set():
            try:
                conn, _ = srv.accept()
            except (OSError, socket.timeout):
                continue
            conn.close()

    acceptor = threading.Thread(target=_accept_forever, daemon=True)
    acceptor.start()
    check("假服务真的在接连接（否则探测会被 backlog 骗到）",
          lc.Command._busy_on_loopback(probe) and lc.Command._busy_on_loopback(probe))

if probe:
    calls, orig = [], lc.call_command
    # 只桩掉真正启动服务那一步：本脚本不该真的把服务器跑起来。
    lc.call_command = lambda *a, **k: calls.append((a, k))
    try:
        # ① 端口被占 + 没 --force → 必须拒绝
        t, e = run(port=probe, serve=True)
        blob = t + e
        check("端口已被占用时 --serve 拒绝再起一个（防住两个 runserver 并存）",
              not calls and "这次不起" in blob, f"calls={calls}")
        check("拒绝时说明了原因（Windows 允许 0.0.0.0 与 127.0.0.1 同端口并存）",
              "同时" in blob)
        check("拒绝时给出三条出路（用现有的 / 先停掉 / --force）", "--force" in blob)
        check("拒绝分支的输出也 GBK 可编码", gbk_ok(blob)[0], gbk_ok(blob)[1])

        # ② 端口被占 + --force → 真的去起
        t, e = run(port=probe, serve=True, force=True)
        check("--force 时确实去调用了 runserver", bool(calls), f"calls={calls}")
        if calls:
            args, kw = calls[0]
            check("绑定地址是 0.0.0.0（不是回环）",
                  args[:2] == ("runserver", f"0.0.0.0:{probe}"), f"{args} {kw}")
            check("用 --noreload 启动（演示时不会被手滑改文件打断）",
                  kw.get("use_reloader") is False, f"{kw}")
        check("--force 分支的输出也 GBK 可编码", gbk_ok(t + e)[0], gbk_ok(t + e)[1])
    finally:
        lc.call_command = orig

    srv.close()  # 端口空出来，测「正常启动」那条路
    if stopper:
        stopper.set()

    calls.clear()
    lc.call_command = lambda *a, **k: calls.append((a, k))
    try:
        # ③ 端口空闲 + --serve → 正常启动（被桩住，不会真起服务）
        t, e = run(port=probe, serve=True)
        check("端口空闲时 --serve 会启动服务", bool(calls), f"calls={calls}")
        check("启动前打印了「绑定 0.0.0.0:<port>」（绑错就是手机连不上）",
              f"绑定 0.0.0.0:{probe}" in t, t[-400:])

        # ④ 不带 --serve → 绝不启动服务
        calls.clear()
        t_ro, _ = run(port=probe)
        check("默认（不带 --serve）绝不启动服务", not calls, f"calls={calls}")
        check("默认模式下会提示可以用 --serve 省一步", "--serve" in t_ro)
    finally:
        lc.call_command = orig

# ════════════════════════════════════════════════════════════════
print("\n【6】--help 能正常打印（现场手忙脚乱时唯一的说明书）")
# ════════════════════════════════════════════════════════════════
helped = lc.Command().create_parser("manage.py", "lancheck").format_help()
check("--help 里列出了 --serve", "--serve" in helped)
check("--help 里列出了 --host 与 --port", "--host" in helped and "--port" in helped)
check("--help 输出 GBK 可编码", gbk_ok(helped)[0], gbk_ok(helped)[1])

# ════════════════════════════════════════════════════════════════
print("\n【7】--tunnel：公网地址这条路")
# ════════════════════════════════════════════════════════════════
from agent.cashier import _looks_public, lan_reachability  # noqa: E402

check("--tunnel 开关存在", parser.parse_args(["--tunnel"]).tunnel is True)
check("域名被判为公网（走 HTTP 探测）", _looks_public("abc-def.trycloudflare.com") is True)
check("私有 IP 不被判为公网（走端口探测）", _looks_public("192.168.31.78") is False)
check("回环不被判为公网", _looks_public("127.0.0.1") is False)

# ⚠️ 用 `.invalid` 域名而不是 `203.0.113.9` 这种"文档保留 IP"：
#    Python 的 `ipaddress` 把 TEST-NET 段也算作 `is_private`，
#    于是它会被判成内网地址、走到端口探测那条路上去（第一次就踩了这个）。
#    域名则一定判为公网，且 `.invalid` 解析必然失败，跑得又快又确定。
# 用 override_settings 固定 ALLOWED_HOSTS，免得用户 .env 换了值就让断言飘。
with override_settings(ALLOWED_HOSTS=["*"]):
    msg_public = lan_reachability("http://demo-does-not-exist.invalid/pay/confirm/ABC/")
    check("公网地址连不上 → 报「打不开」，且不提 runserver",
          "打不开" in msg_public and "runserver" not in msg_public, repr(msg_public))

    if ip and not _looks_public(ip):
        # 私有 IP 仍走原来的端口探测：这里必须给出"怎么起服务"的可执行提示
        msg_local = lan_reachability(f"http://{ip}:9/pay/confirm/ABC/")
        check("局域网地址仍走端口探测那套（提示 runserver 0.0.0.0）",
              "runserver" in msg_local, repr(msg_local))

if probe:
    calls.clear()
    _orig_call, _orig_find = lc.call_command, lc.find_cloudflared
    lc.call_command = lambda *a, **k: calls.append((a, k))
    lc.find_cloudflared = lambda: ""
    try:
        t, e = run(port=probe, tunnel=True)  # 只给 --tunnel，不给 --serve
        blob = t + e
        check("--tunnel 隐含 --serve（不写 --serve 也会进「启动服务」段）",
              "启动服务" in blob, blob[-400:])
        check("没有云梯客户端时给出明确的失败原因",
              "没找到 cloudflared.exe" in blob and "隧道没起来" in blob)
        check("没有云梯客户端时告诉用户该把 exe 放哪",
              "_tools" in blob and "CLOUDFLARED" in blob)
        check("隧道起不来时**绝不**悄悄降级成局域网地址（不调用 runserver）",
              not calls, f"calls={calls}")
        check("该分支输出 GBK 可编码", gbk_ok(blob)[0], gbk_ok(blob)[1])
    finally:
        lc.call_command = _orig_call
        lc.find_cloudflared = _orig_find

_exe = lc.find_cloudflared()
if _exe:
    check(f"本机找得到云梯客户端 → --tunnel 可用",
          os.path.isfile(_exe), _exe)
else:
    check("本机没装云梯客户端（--tunnel 会在运行时明确报错，属预期）", True)

# ════════════════════════════════════════════════════════════════
print("\n【8】双击启动器 start-demo.bat")
# ════════════════════════════════════════════════════════════════
_bat = os.path.join(BASE, "start-demo.bat")
_raw = open(_bat, "rb").read() if os.path.isfile(_bat) else b""
check("start-demo.bat 存在（双击即跑，一个终端）", bool(_raw))
if _raw:
    check("内容纯 ASCII —— cmd 按 cp936 读 .bat，UTF-8 中文会变乱码",
          all(b < 128 for b in _raw))
    check("行尾全是 CRLF（.bat 用 LF 容易出怪问题）",
          _raw.count(b"\n") == _raw.count(b"\r\n"),
          f"LF {_raw.count(b'\\n')} 个，其中 CRLF {_raw.count(b'\\r\\n')} 个")
    check("用 py -3.12 启动（防火墙规则按 exe 路径匹配，venv 的 python 对不上）",
          b"py -3.12" in _raw)
    check("入口是 lancheck --serve（不是裸 runserver 127.0.0.1）",
          b"lancheck --serve" in _raw)
    check("支持透传参数（start-demo.bat --tunnel）", b"%*" in _raw)
    check("出错时留窗（否则用户看不到原因，窗口一闪就没）", b"pause" in _raw)

print()
print("=" * 64)
print(f"  通过 {len(PASS)} 项 / 失败 {len(FAIL)} 项")
print("=" * 64)
if FAIL:
    print("失败项：")
    for f in FAIL:
        print(f"  × {f}")
    sys.exit(1)
