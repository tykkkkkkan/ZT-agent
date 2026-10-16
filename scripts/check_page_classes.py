#!/usr/bin/env python3
"""
页面样式类覆盖检查 —— 防止「新写的 class 静默没有样式」。

为什么需要它
------------
`frontend/assets/tailwind.build.css` 是**预编译产物**（不是 CDN 运行时生成）。
往页面里加了一个没被编译过的 class，页面不会报错、不会 404，
只会**安静地少一段样式** —— 这类问题在本地开发时极难发现，
通常要到别人电脑上打开才暴露。

用法
----
    python scripts/check_page_classes.py frontend/pay.html frontend/pay_confirm.html
    python scripts/check_page_classes.py --rev HEAD frontend/pay.html

  · 默认对着**工作区**的 CSS 检查（用于开发时自查）。
  · `--rev HEAD` 对着**已提交版本**的 CSS 检查 —— 这解决另一类坑：
    工作区里 CSS 有、但那些 class 是**别人尚未提交**的改动带来的。
    按此文件为蓝本提交后，别人拉到的版本会缺样式。
    报告会把这类 class 单独列出来（"只在工作区存在"）。

退出码：有缺失（或 --rev 模式下有"仅工作区"依赖）时为 1。
"""
import argparse
import os
import re
import subprocess
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CSS_FILES = ["frontend/assets/tailwind.build.css", "frontend/assets/theme.css"]

# Tailwind 对选择器里这些字符要加反斜杠（必须带捕获组，替换时要用 \1）
NEEDS_ESCAPE = r"([.:/\[\]%#(),!@&*'\"+~>])"


def read_worktree(rel):
    with open(os.path.join(BASE, rel), encoding="utf-8") as f:
        return f.read()


def read_rev(rev, rel):
    r = subprocess.run(["git", "show", f"{rev}:{rel}"], cwd=BASE,
                       capture_output=True)
    return r.stdout.decode("utf-8") if r.returncode == 0 else ""


def classes_in(html):
    """取出 class="..." 里的所有 token（去掉 Tailwind 变体里的 esc 转义干扰）。"""
    out = set()
    for m in re.finditer(r'class\s*=\s*"([^"]*)"', html):
        for token in m.group(1).split():
            if token:
                out.add(token)
    return out


def has_class(css, token):
    escaped = re.sub(NEEDS_ESCAPE, r"\\\1", token)
    pattern = re.escape("." + escaped) + r"(?=[\s,{:>~+).])"
    return re.search(pattern, css) is not None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("files", nargs="+")
    ap.add_argument("--rev", default=None,
                    help="对某个 git 版本的 CSS 检查（如 HEAD），并单独报告"
                         "「工作区有、该版本没有」的 class")
    args = ap.parse_args()

    rev_css = {f: read_rev(args.rev, f) for f in CSS_FILES} if args.rev else {}
    wt_css = {f: read_worktree(f) for f in CSS_FILES}
    css_now = rev_css if args.rev else wt_css
    label = f"git {args.rev}" if args.rev else "工作区"

    print("=" * 66)
    print(f"  页面样式类覆盖检查（对照：{label} 的 {len(CSS_FILES)} 个 CSS 文件）")
    print("=" * 66)

    bad = False
    for path in args.files:
        rel = path.replace("\\", "/")
        if rel.startswith("frontend/"):
            html_path = os.path.join(BASE, rel)
        else:
            html_path = os.path.abspath(path)
        with open(html_path, encoding="utf-8") as f:
            html = f.read()

        tokens = sorted(classes_in(html))
        missing = [t for t in tokens if not any(has_class(c, t) for c in css_now.values())]
        only_wt = []
        if args.rev:
            only_wt = [
                t for t in tokens
                if not any(has_class(c, t) for c in rev_css.values())
                and any(has_class(c, t) for c in wt_css.values())
            ]

        name = os.path.basename(html_path)
        print(f"\n  {name}：{len(tokens)} 个 class")
        if missing:
            bad = True
            print(f"    ✗ 缺 {len(missing)} 个（{label} 的 CSS 里查不到）→ 需重跑 scripts/build_tailwind.py")
            for t in missing:
                print(f"        · {t}")
        else:
            print(f"    ✓ 全部命中，无需重跑 build_tailwind.py")
        if only_wt:
            bad = True
            print(f"    ! {len(only_wt)} 个只在**工作区**的 CSS 里（来自别人尚未提交的改动）"
                  f"→ 单独提交这些页面时它们会缺样式：")
            for t in only_wt:
                print(f"        · {t}")

    print("\n" + "=" * 66)
    print("  结论：" + ("存在问题，见上" if bad else "全部通过"))
    print("=" * 66)
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
