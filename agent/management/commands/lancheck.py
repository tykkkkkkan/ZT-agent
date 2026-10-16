"""现场演示联网自检 —— 告诉你该用哪个地址打开收银台

为什么需要它
------------
手机扫码要能打开，二维码里的地址必须是**手机访问得到**的地址。本地开发最容易错
的一步不是代码，而是"在电脑浏览器里用哪个地址打开收银台"：

  · 用 `localhost:8000` 打开 → 程序得靠 `lan_ip()` 去猜网卡（多网卡会猜错）；
  · 用局域网 / 热点 IP 打开 → 用的是**你请求时的那个真实域名**，必定正确。

去外地演示时网络每次都变（连别人的 Wi-Fi 或手机热点），本命令把"该用哪个地址"
算出来，省掉在 `ipconfig` 输出里用肉眼挑网卡这一步 —— `lan_ip()` 是问内核
"我从哪张网卡出去"，比人眼准。

它只读不写：不改库、不起服务、不动任何文件。

用法
----
    python manage.py lancheck
    python manage.py lancheck --port 8001
"""
import socket

from django.conf import settings
from django.core.management.base import BaseCommand

from agent.cashier import lan_ip

# 输出里只用 GBK 也有的符号（√ × ─ ·）：控制台直接跑没问题，
# 重定向到文件时（stdout 退化成 cp936）也不会 UnicodeEncodeError。
LINE = "─" * 58


class Command(BaseCommand):
    help = "打印现场演示该用的访问地址，并检查 ALLOWED_HOSTS 与端口绑定"

    def add_arguments(self, parser):
        parser.add_argument("--port", default="8000", help="服务端口（默认 8000）")

    def handle(self, *args, **options):
        port = str(options["port"]).strip().lstrip(":")
        ip = lan_ip()

        self.stdout.write(LINE)
        self.stdout.write(" 现场演示联网自检")
        self.stdout.write(LINE)
        self.stdout.write("")

        if not ip:
            self.stderr.write("  × 取不到本机联网地址 —— 电脑当前没有可用的网络连接。")
            self.stderr.write("    请先连上手机热点（或任意局域网），再跑一次本命令。")
            return

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
        self.stdout.write("  现场启动命令（照抄；注意是 0.0.0.0，不是 127.0.0.1）")
        self.stdout.write(f"       python manage.py runserver 0.0.0.0:{port}")
        self.stdout.write("")

        # ── 检查 ────────────────────────────────────────────────
        self.stdout.write(LINE)
        self.stdout.write(" 检查")
        self.stdout.write(LINE)

        hosts = list(getattr(settings, "ALLOWED_HOSTS", None) or [])
        shown = ",".join(hosts) if hosts else "(空)"
        if "*" in hosts:
            self.stdout.write(f"  √ ALLOWED_HOSTS = {shown}")
            self.stdout.write("      通配 —— 换任何网络都不用再改配置。")
        elif ip in hosts:
            self.stdout.write(f"  √ ALLOWED_HOSTS = {shown}")
            self.stdout.write(f"      已放行 {ip}。")
        else:
            self.stdout.write(f"  × ALLOWED_HOSTS = {shown}")
            self.stdout.write(f"      没放行 {ip} —— 手机打开只会得到 400，而 400 的响应体是空的，")
            self.stdout.write("      手机上就是一张纯白页。改成 ALLOWED_HOSTS=* 最省事，")
            self.stdout.write("      改完必须**完全停掉再重启**服务（自动重载读不到新的 .env）。")
        self.stdout.write("")

        listening = self._listening(ip, port)
        if listening:
            self.stdout.write(f"  √ 服务已监听在 {ip}:{port} —— 手机现在就能扫码")
            self.stdout.write("      说明它绑在了通配 / 局域网地址上，而不是只绑回环。")
        else:
            self.stdout.write(f"  × 服务没有监听在 {ip}:{port}")
            self.stdout.write("      还没启动 → 照抄上面那条启动命令；")
            self.stdout.write("      已经在跑 → 说明它只绑了 127.0.0.1（手机连都连不上，一直转圈），")
            self.stdout.write("                 停掉，再按上面那条重启。")
        self.stdout.write("")

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
