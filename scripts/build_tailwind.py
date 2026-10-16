#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""重新生成 frontend/assets/tailwind.build.css（Tailwind 预编译产物）。

为什么需要这个脚本
------------------
页面原先用 Tailwind Play CDN（assets/vendor/tailwindcdn.js，407 KB）在浏览器里
运行时扫 DOM 生成 CSS。这条路带来的代价是：398 KiB 下载 + 主线程 ~1.1 s 的
Style & Layout。现改为**预编译成静态 CSS**（gzip 后约 10 KiB），首屏指标明显改善。

代价是：改了 html / js 里的 class 之后，**必须重跑本脚本**，否则新 class
在静态 CSS 里不存在，页面会缺样式。

依赖
----
tailwindcss CLI **3.4.17** —— 必须与原先 Play CDN 同版本，才能保证零样式漂移。
装在项目里即可：

    npm i -D tailwindcss@3.4.17

找不到时可用环境变量指定：

    TAILWIND_CLI=/path/to/node_modules/tailwindcss/lib/cli.js
    NODE_BIN=/path/to/node

用法
----
    python scripts/build_tailwind.py
"""
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FRONTEND = ROOT / "frontend"
CONFIG_SRC = FRONTEND / "assets" / "tailwind-config.js"
CONFIG_OUT = FRONTEND / "assets" / "tailwind.build.css"

# 扫描范围与原先 Play CDN 覆盖的一致：全部页面 + 全部前端脚本
CONTENT = ["./frontend/*.html", "./frontend/**/*.js"]

INPUT_CSS = "@tailwind base;\n@tailwind components;\n@tailwind utilities;\n"


def find_cli():
    env = os.environ.get("TAILWIND_CLI")
    if env and Path(env).exists():
        return env
    candidates = [
        ROOT / "node_modules" / "tailwindcss" / "lib" / "cli.js",
        FRONTEND / "node_modules" / "tailwindcss" / "lib" / "cli.js",
    ]
    for candidate in candidates:
        if candidate.exists():
            return str(candidate)
    return None


def find_node():
    env = os.environ.get("NODE_BIN")
    if env and Path(env).exists():
        return env
    return "node"


def make_config(tmp_dir):
    """把 assets/tailwind-config.js 转成 CLI 能读的 module.exports 形式。

    项目里的版本是 Play CDN 写法（`tailwind.config = {...}`），CLI 不认，
    这里做一次等价替换并补上 content 扫描入口。
    """
    src = CONFIG_SRC.read_text(encoding="utf-8")
    if "tailwind.config = {" not in src:
        sys.exit("错误：%s 里找不到 `tailwind.config = {`，配置写法变了？" % CONFIG_SRC)
    content_literal = "content: [%s]," % ", ".join('"%s"' % c for c in CONTENT)
    out = src.replace("tailwind.config = {",
                      "module.exports = {\n    " + content_literal, 1)
    path = Path(tmp_dir) / "tailwind.cli.config.js"
    path.write_text(out, encoding="utf-8")
    return str(path)


def main():
    cli = find_cli()
    if not cli:
        sys.exit(
            "错误：找不到 tailwindcss CLI。\n"
            "请先安装： npm i -D tailwindcss@3.4.17\n"
            "或用环境变量指定 TAILWIND_CLI=<...>/tailwindcss/lib/cli.js"
        )
    node = find_node()
    before = CONFIG_OUT.stat().st_size if CONFIG_OUT.exists() else 0

    with tempfile.TemporaryDirectory() as tmp:
        cfg = make_config(tmp)
        inp = Path(tmp) / "in.css"
        inp.write_text(INPUT_CSS, encoding="utf-8")
        cmd = [node, cli, "-c", cfg, "-i", str(inp),
               "-o", str(CONFIG_OUT), "--minify"]
        print("运行：%s" % " ".join(cmd))
        result = subprocess.run(cmd, cwd=str(ROOT))
        if result.returncode != 0:
            sys.exit("Tailwind 构建失败（退出码 %d）" % result.returncode)

    after = CONFIG_OUT.stat().st_size
    print("\n✅ 已生成 %s" % CONFIG_OUT.relative_to(ROOT))
    print("   体积 %d -> %d 字节" % (before, after))
    print("   扫描入口：%s" % "，".join(CONTENT))


if __name__ == "__main__":
    main()
