"""现场演示联网自检 —— 告诉你该用哪个地址打开收银台（可选：顺手把服务和隧道都起起来）

为什么需要它
------------
手机扫码要能打开，二维码里的地址必须是**手机访问得到**的地址。本地开发最容易错
的一步不是代码，而是"在电脑浏览器里用哪个地址打开收银台"：

  · 用 `localhost:8000` 打开 → 程序得靠 `lan_ip()` 去猜网卡（多网卡会猜错）；
  · 用局域网 / 热点 IP 打开 → 用的是**你请求时的那个真实域名**，必定正确。

去外地演示时网络每次都变（连别人的 Wi-Fi 或手机热点），本命令把"该用哪个地址"
算出来，省掉在 `ipconfig` 输出里用肉眼挑网卡这一步 —— `lan_ip()` 是问内核
"我从哪张网卡出去"，比人眼准。

默认只读不写：不改库、不起服务、不动任何文件。
加 `--serve` 则自检完**接着启动服务**，把"自检 + 启动"两条命令并成一条。
再加 `--tunnel` 则连**内网穿透**也一起托管：手机不必和电脑同一个网络，
用 4G 扫码都能打开 —— 而且始终只有这一个终端。

用法
----
    python manage.py lancheck                    # 只看地址与检查
    python manage.py lancheck --serve            # 自检完直接起服务（Ctrl+C 结束）
    python manage.py lancheck --serve --tunnel   # 服务 + 隧道，手机任意网络可扫
    python manage.py lancheck --serve --port 8001
"""
import os
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

from django.conf import settings
from django.core.management import call_command
from django.core.management.base import BaseCommand

from agent.cashier import lan_ip

# 输出里只用 GBK 也有的符号（√ × ─ · ※）：控制台直接跑没问题，
# 重定向到文件时（stdout 退化成 cp936）也不会 UnicodeEncodeError。
LINE = "─" * 58

# cloudflared quick tunnel 会把它分配到的域名打在日志里
TUNNEL_URL_RE = re.compile(r"https://[a-z0-9][a-z0-9-]*\.trycloudflare\.com")

# 等隧道上层要多久。现场网络慢时给得宽一点；超时就明确失败，不悄悄降级 ——
# 悄悄降级会让用户以为"隧道通了"，实际码里还是局域网地址，最后还是打不开。
TUNNEL_WAIT = 35.0


def find_cloudflared() -> str:
    """按优先级找一个可用的 cloudflared.exe。

    顺序：环境变量 `CLOUDFLARED` > PATH > 仓库外的 `_tools/` > 常见安装目录。
    `_tools/` 刻意放在**仓库之外**：49MB 的二进制不该进版本库，
    也不该出现在 `git status` 里干扰提交。
    """
    candidates = []
    env = (os.getenv("CLOUDFLARED") or "").strip()
    if env:
        candidates.append(env)
    which = shutil.which("cloudflared")
    if which:
        candidates.append(which)
    here = Path(__file__).resolve()
    for parent in list(here.parents)[:6]:
        candidates.append(str(parent / "_tools" / "cloudflared" / "cloudflared.exe"))
    candidates += [
        r"C:\Program Files (x86)\cloudflared\cloudflared.exe",
        r"C:\Program Files\cloudflared\cloudflared.exe",
    ]
    for c in candidates:
        if c and os.path.isfile(c):
            return c
    return ""


class Command(BaseCommand):
    help = "打印现场演示该用的访问地址，检查配置（--serve 起服务，--tunnel 再托管内网穿透）"

    def add_arguments(self, parser):
        parser.add_argument("--port", default="8000", help="服务端口（默认 8000）")
        parser.add_argument(
            "--host", default="0.0.0.0",
            help="监听地址（默认 0.0.0.0 = 所有网卡；只绑 127.0.0.1 手机连不上）",
        )
        parser.add_argument(
            "--serve", action="store_true",
            help="自检完直接启动服务，把两步并成一步",
        )
        parser.add_argument(
            "--tunnel", action="store_true",
            help="再托管一条内网穿透隧道：手机不必和电脑同网，4G 也能扫（隐含 --serve）",
        )
        parser.add_argument(
            "--force", action="store_true",
            help="端口上已有服务在跑时，仍然强行再起一个（正常情况不建议）",
        )

    def handle(self, *args, **options):
        port = str(options["port"]).strip().lstrip(":")
        host = str(options["host"]).strip() or "0.0.0.0"
        tunnel = bool(options["tunnel"])
        serve = bool(options["serve"]) or tunnel  # --tunnel 隐含 --serve
        ip = lan_ip()

        self.stdout.write(LINE)
        self.stdout.write(" 现场演示联网自检")
        self.stdout.write(LINE)
        self.stdout.write("")

        if not ip and not tunnel:
            self.stderr.write("  × 取不到本机联网地址 —— 电脑当前没有可用的网络连接。")
            self.stderr.write("    请先连上手机热点（或任意局域网），再跑一次本命令。")
            if not serve:
                return
            self.stderr.write("")
        elif not ip:
            self.stdout.write("  ※ 取不到本机联网地址，但走 --tunnel 不依赖它 —— 继续。")
            self.stdout.write("")
        else:
            base = f"http://{ip}:{port}"
            self.stdout.write(f"  本机联网地址：{ip}")
            self.stdout.write("")
            self.stdout.write("  ① 在电脑浏览器里打开这个 —— 收银台入口")
            self.stdout.write(f"       {base}/index.html")
            self.stdout.write("     ※ 必须用上面这个地址，别用 localhost。")
            self.stdout.write("")
            self.stdout.write("  ② 手机上直接打开这个 —— 能出内容就说明整条路通了")
            self.stdout.write(f"       {base}/healthz")
            self.stdout.write("")
            self.stdout.write("  ③ 收银台上开票后，二维码里会写成（手机扫的就是它）")
            self.stdout.write(f"       {base}/pay/confirm/<票据>/")
            self.stdout.write("")
            if tunnel:
                self.stdout.write(f"  这就把服务和隧道一起起在 {host}:{port} —— 隧道域名稍后打印。")
            elif serve:
                self.stdout.write(f"  这就把服务起在 {host}:{port} —— 起来之后用上面的地址打开即可。")
            else:
                self.stdout.write("  现场启动命令（照抄；注意是 0.0.0.0，不是 127.0.0.1）")
                self.stdout.write(f"       python manage.py runserver {host}:{port}")
                self.stdout.write("  懒得敲两条？下一条就够：")
                self.stdout.write(f"       python manage.py lancheck --serve --port {port}")
            self.stdout.write("")

        # ── 检查 ────────────────────────────────────────────────
        self.stdout.write(LINE)
        self.stdout.write(" 检查")
        self.stdout.write(LINE)

        hosts = list(getattr(settings, "ALLOWED_HOSTS", None) or [])
        shown = ",".join(hosts) if hosts else "(空)"
        if "*" in hosts:
            self.stdout.write(f"  √ ALLOWED_HOSTS = {shown}")
            self.stdout.write("      通配 —— 换任何网络 / 隧道域名都不用再改配置。")
        elif ip and ip in hosts:
            self.stdout.write(f"  √ ALLOWED_HOSTS = {shown}")
            self.stdout.write(f"      已放行 {ip}。")
        else:
            self.stdout.write(f"  × ALLOWED_HOSTS = {shown}")
            self.stdout.write(f"      没放行 {ip or '(无联网地址)'} —— 手机打开只会得到 400，而 400 的响应体是空的，")
            self.stdout.write("      手机上就是一张纯白页。改成 ALLOWED_HOSTS=* 最省事，")
            self.stdout.write("      改完必须**完全停掉再重启**服务（自动重载读不到新的 .env）。")
        self.stdout.write("")

        busy = self._busy_on_loopback(port)
        listening = bool(ip) and self._listening(ip, port)
        if listening:
            self.stdout.write(f"  √ 服务已监听在 {ip}:{port} —— 手机现在就能扫码")
            self.stdout.write("      说明它绑在了通配 / 局域网地址上，而不是只绑回环。")
        elif busy:
            self.stdout.write(f"  × 端口 {port} 上已经有一个服务在跑了，但它只绑了 127.0.0.1")
            self.stdout.write("      手机连都连不上（页面一直转圈）→ 停掉它，再用 0.0.0.0 起一个。")
        elif ip:
            self.stdout.write(f"  · 服务当前没有监听在 {ip}:{port}")
            if serve:
                self.stdout.write("      这次就是来起它的 —— 见下面「启动服务」，起来后稍等几秒再刷新。")
            else:
                self.stdout.write("      还没启动 → 照抄上面那条启动命令（或直接用 --serve）。")
            self.stdout.write("      注意别用 `runserver 127.0.0.1:8000` 这种只绑回环的写法：")
            self.stdout.write("      那样手机连都连不上（页面一直转圈），而本机自测完全正常。")
        self.stdout.write("")

        if tunnel:
            exe = find_cloudflared()
            if exe:
                self.stdout.write(f"  √ 找到隧道客户端：{exe}")
            else:
                self.stdout.write("  × 没找到 cloudflared.exe —— 隧道起不来。")
                self.stdout.write("    放一份到 <项目父目录>\\_tools\\cloudflared\\cloudflared.exe 即可，")
                self.stdout.write("    或用环境变量 CLOUDFLARED 指定它的完整路径。")
            self.stdout.write("")

        if self._in_virtualenv():
            self.stdout.write("  ※ 你在用虚拟环境里的 Python 启动 —— 现场演示建议改用系统 Python：")
            self.stdout.write(f"       {sys.executable}")
            self.stdout.write("     Windows 防火墙那条 python.exe 入站放行是**按 exe 路径**匹配的，")
            self.stdout.write("     换成 venv 里的 python 会对不上规则 → 本机一切正常，手机连不上。")
            self.stdout.write("")

        # ── 可选：直接把服务（和隧道）起起来 ────────────────────────
        if serve:
            self._serve(host, port, ip, force=bool(options["force"]), tunnel=tunnel)

    # ── 内部 ────────────────────────────────────────────────────

    def _serve(self, host: str, port: str, ip: str, *, force: bool, tunnel: bool):
        self.stdout.write(LINE)
        self.stdout.write(" 启动服务")
        self.stdout.write(LINE)

        if self._busy_on_loopback(port) and not force:
            self.stdout.write(f"  × 端口 {port} 上已经有服务在跑，这次不起。")
            self.stdout.write("")
            self.stdout.write("    这是故意的：Windows 允许 `0.0.0.0:8000` 与 `127.0.0.1:8000`")
            self.stdout.write("    **同时**监听，所以再起一个不会报错，但 `localhost` 会命中旧的那个 ——")
            self.stdout.write("    \"我明明改了配置怎么不生效\"\"我重启了怎么还是旧的\"就是这么来的。")
            self.stdout.write("")
            self.stdout.write("    · 如果那个就是你要用的服务：直接用上面的地址打开，不用再起。")
            self.stdout.write("    · 如果它是绑错了地址的旧进程：先停掉它（Ctrl+C 或 taskkill /F /PID <pid>）。")
            self.stdout.write("    · 确实想再起一个（比如换个端口做对照）：加 --force。")
            return

        tunnel_proc = None
        try:
            if tunnel:
                tunnel_proc, tunnel_url = self._start_tunnel(port)
                if not tunnel_proc:
                    self.stdout.write("  × 隧道没起来，已中止 —— 不悄悄降级成局域网地址。")
                    self.stdout.write("    （降级的话你会以为隧道通了，实际码里还是局域网 IP，最后还是打不开。）")
                    if tunnel_url:
                        self.stdout.write("")
                        self.stdout.write("    cloudflared 的输出（最后几行）：")
                        for line in tunnel_url.splitlines():
                            self.stdout.write(f"      {line}")
                    return

                # ① 让二维码里写隧道域名：reachable_base_url() 认这个环境变量，
                #    优先级高于"请求里的真实域名"，所以无论用哪个地址打开收银台，码都是它。
                os.environ["PUBLIC_BASE_URL"] = tunnel_url
                # ② 放行隧道域名，否则 Django 对它的请求一律 400（响应体为空 → 手机白屏）。
                t_host = tunnel_url.split("//", 1)[1]
                allowed = list(getattr(settings, "ALLOWED_HOSTS", None) or [])
                if "*" not in allowed and t_host not in allowed:
                    settings.ALLOWED_HOSTS = allowed + [t_host]

                self.stdout.write("")
                self.stdout.write(LINE)
                self.stdout.write(" 公网地址（二维码里就是它）")
                self.stdout.write(f"       {tunnel_url}")
                self.stdout.write(LINE)
                self.stdout.write("  · 手机**不需要**和电脑同一个网络，4G / 任何 Wi-Fi 都能扫。")
                self.stdout.write("  · 电脑浏览器随便用哪个地址打开收银台都行，码里一律是上面这个。")
                self.stdout.write("  · 注意：这个地址在公网上，拿到链接的人都能访问，演示完 Ctrl+C 即断。")
                self.stdout.write("  · 隧道域名每次启动都会变，这是正常现象（免费版固定不了）。")
                self.stdout.write("")

            self.stdout.write(f"  绑定 {host}:{port}，Ctrl+C 结束。")
            self.stdout.write("  用 --noreload 启动：演示时最怕手滑改个文件导致自动重启，")
            self.stdout.write("  重启会丢掉当前进程里的一切状态。要改代码请另开终端跑普通 runserver。")
            self.stdout.write("")
            self.stdout.flush()

            # 同进程内起服务 → 用的就是"你敲命令的那只 python"，正好也是防火墙放行的那只。
            # 注意：runserver 的静态文件服务在 DEBUG=True 下才开，本项目演示即 DEBUG。
            call_command("runserver", f"{host}:{port}", use_reloader=False, verbosity=1)
        finally:
            if tunnel_proc is not None:
                # 同一个终端、同一个进程：Ctrl+C 之后隧道跟着一起收，
                # 不留一个还在往公网暴露服务的孤儿进程。
                tunnel_proc.terminate()
                try:
                    tunnel_proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    tunnel_proc.kill()
                self.stdout.write("")
                self.stdout.write("  隧道已随之关闭。")

    def _start_tunnel(self, port: str):
        """起一条 cloudflared quick tunnel，返回 (进程, 隧道域名)。

        失败时进程为 None，第二项是 cloudflared 的日志尾部（用来告诉用户卡在哪）。
        """
        exe = find_cloudflared()
        if not exe:
            return None, ""

        self.stdout.write("  正在建立隧道（首次可能要十几秒）…")
        self.stdout.flush()

        cmd = [exe, "tunnel", "--no-autoupdate", "--url", f"http://127.0.0.1:{port}"]
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            # 不弹第二个黑窗口 —— 用户要的就是"一个终端"。
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )

        box = {"url": "", "tail": []}

        def _pump():
            for line in proc.stdout or []:
                line = line.rstrip()
                box["tail"].append(line)
                del box["tail"][:-40]  # 只留尾部，失败时够诊断就行
                if not box["url"]:
                    m = TUNNEL_URL_RE.search(line)
                    if m:
                        box["url"] = m.group(0)

        threading.Thread(target=_pump, daemon=True).start()

        deadline = time.monotonic() + TUNNEL_WAIT
        while time.monotonic() < deadline and not box["url"] and proc.poll() is None:
            time.sleep(0.2)

        if not box["url"]:
            proc.terminate()
            return None, "\n".join(box["tail"][-8:])
        return proc, box["url"]

    @staticmethod
    def _listening(ip: str, port: str) -> bool:
        """从本机连一下自己的联网地址。

        ⚠️ 只证明"服务监听在这块网卡上"，**不含防火墙** ——
        回环进的流量不过入站规则，所以这里通过不代表手机一定连得上。
        """
        try:
            with socket.create_connection((ip, int(port)), timeout=0.4):
                return True
        except (OSError, ValueError):
            return False

    @staticmethod
    def _busy_on_loopback(port: str) -> bool:
        """端口上有没有东西在监听（只绑回环的也算）。"""
        try:
            with socket.create_connection(("127.0.0.1", int(port)), timeout=0.4):
                return True
        except (OSError, ValueError):
            return False

    @staticmethod
    def _in_virtualenv() -> bool:
        """是否跑在虚拟环境里（venv 的 python 与系统 python 是不同的 exe 路径）。"""
        return getattr(sys, "base_prefix", sys.prefix) != sys.prefix
