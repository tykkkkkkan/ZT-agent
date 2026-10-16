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

两条实测约定（都是踩过才写下来的）
----------------------------------
1. class 分**两组**报告：`class="..."` 属性里的（确定）与 `<script>` 字符串字面量里的
   （疑似）。页面大量用 JS 拼 HTML，只查前者会漏掉一半；而字面量里混着业务字符串，
   照单全收又会把 `'paid'`、`'不要这个'` 之类的当成 class 报缺 ——
   所以后者只收「带 `-` / `:` / `/` 的 Tailwind 形态」。
2. token 必须**以字母/数字/-/!/[ 开头、只含 ASCII**。`class="' + (on ? 'a' : 'b') + '"`
   这种拼法会把引号、加号、问号切成一堆假 token —— 早期版本没有这层过滤，
   报出来的"缺失"里一半是 `'`、`+`、`?` 这类噪声，反而盖住了真问题。
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

# 一个合法的 Tailwind class token：只含这些 ASCII 字符，且以字母/数字/-/!/[ 开头。
# 用途是把 `'`、`+`、`?`、`(on`、`支付中…` 这类**非 class** 的东西挡在外面 ——
# 页面是 JS 拼 HTML 的，`class="' + (on ? 'a' : 'b') + '"` 这种写法会把
# 引号、加号、问号切成一堆假 token（早期版本没有这层过滤，报出来的"缺失"
# 一半是噪声，反而盖住了真问题）。
VALID_TOKEN = re.compile(r"^[A-Za-z0-9_\-:./\[\]%(),#!]+$")
VALID_START = re.compile(r"^[A-Za-z0-9_\-!\[]")
# 「疑似 class」的判据：带 - / : / 的才是 Tailwind 形态。
# 不做这一步的话，拼接式写法里的变量名（`on`、`i`、`b`）会被当成 class 名报缺。
CLASSY = re.compile(r"[-:/]")

# ⚠️ 扫描一律**锚定 `class="` 之后的那段文本**（到下一个 `"` 为止）。
# 为什么不按"字符串字面量"去扫整份脚本：脚本里有正则字面量 `/[&<>"']/g`、
# 有中文引号、有注释里的撇号，任何朴素的引号配对都会错位，一旦错位就会把
# 一大段代码当成"字符串"，报出 `1e-9`、`ok:`、`status:` 这类鬼东西。
# 锚定 `class="` 之后，错位是不可能的（最坏情况只是少收几个 token）。
CLASS_CHUNK_RE = re.compile(r'class\s*=\s*"([^"]*)')
# 拼接写法里，token 两端会沾上引号/三元的标点，先削掉再校验
TRIM_CHARS = "'\"()+?;,="
JS_MARKERS = ("'", "+", "?", "(", ")")


# JS 钩子类：只用来给 querySelector / classList 选节点，**本来就不该有样式**。
# 不排掉它们，`topup-quick`（充值快捷档）这种因设计而无样式的 class 会一直报缺，
# 报到最后大家就学会无视这个脚本了 —— 那比没有脚本更糟。
HOOK_RE = re.compile(
    r"""querySelector(?:All)?\(\s*['"]\.([A-Za-z0-9_\-]+)"""
    r"""|classList\.(?:add|remove|toggle)\(\s*['"]([A-Za-z0-9_\-]+)"""
)


def read_worktree(rel):
    with open(os.path.join(BASE, rel), encoding="utf-8") as f:
        return f.read()


def hook_classes(html):
    return {a or b for a, b in HOOK_RE.findall(html)}


def read_rev(rev, rel):
    r = subprocess.run(["git", "show", f"{rev}:{rel}"], cwd=BASE,
                       capture_output=True)
    return r.stdout.decode("utf-8") if r.returncode == 0 else ""


def _tokens(chunk):
    out = set()
    for raw in chunk.split():
        tok = raw.strip(TRIM_CHARS)
        if tok and VALID_TOKEN.match(tok) and VALID_START.match(tok):
            out.add(tok)
    return out


def classes_in(html):
    """返回 (definite, suspected) 两组 class。

    definite  —— 写在 HTML 属性里的（`class="mt-6 flex"`）：确定要给样式，
                 缺一个就是真的缺。
    suspected —— 拼接出来的（`class="' + (on ? 'bg-white' : 'x') + '"`）：
                 同样是浏览器要渲染的 class，但这一段里混着变量名与标点，
                 所以只收「带 - / : / 的 Tailwind 形态」。

    已知取舍：脚本里 `classList.add('...')`、Vue `:class` 表达式中的 class
    不在覆盖范围内（本项目页面都是拼 innerHTML，用不上）。
    """
    definite, suspected = set(), set()
    for m in CLASS_CHUNK_RE.finditer(html):
        chunk = m.group(1)
        toks = _tokens(chunk)
        if any(mark in chunk for mark in JS_MARKERS):
            suspected |= {t for t in toks if CLASSY.search(t)}
        else:
            definite |= toks
    suspected -= definite
    return definite, suspected


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

        definite, suspected = classes_in(html)
        hooks = hook_classes(html)
        tokens = sorted(definite | suspected)

        def miss_of(group):
            return [t for t in sorted(group)
                    if t not in hooks
                    and not any(has_class(c, t) for c in css_now.values())]

        miss_def, miss_sus = miss_of(definite), miss_of(suspected)
        used_hooks = sorted(h for h in hooks if h in definite | suspected)
        only_wt = []
        if args.rev:
            only_wt = [
                t for t in tokens
                if not any(has_class(c, t) for c in rev_css.values())
                and any(has_class(c, t) for c in wt_css.values())
            ]

        name = os.path.basename(html_path)
        print(f"\n  {name}：{len(definite)} 个 class"
              f"（另有 {len(suspected)} 个来自脚本字符串）")
        if used_hooks:
            print(f"    · JS 钩子类（只用选择器，无需样式）：{'、'.join(used_hooks)}")
        if miss_def:
            bad = True
            print(f"    ✗ 确定缺失 {len(miss_def)} 个（{label} 的 CSS 里查不到）"
                  f"→ 需重跑 scripts/build_tailwind.py")
            for t in miss_def:
                print(f"        · {t}")
        elif not miss_sus:
            print(f"    ✓ 全部命中，无需重跑 build_tailwind.py")
        if miss_sus:
            bad = True
            print(f"    ✗ 疑似缺失 {len(miss_sus)} 个（出现在脚本字符串里，"
                  f"同样会被渲染，请确认不是拼错）：")
            for t in miss_sus:
                print(f"        · {t}")
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
