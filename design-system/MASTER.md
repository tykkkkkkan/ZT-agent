# 中渔天下 · 设计系统 Master 文件

> **逻辑（Logic）**：本文件是全局唯一设计依据。当为具体页面开发时，严格遵循以下规则；
> 若需要页面级特例，在 `design-system/pages/` 下建立同名 `.md` 覆盖（本文件优先）。
>
> **项目**：中渔天下（钓鱼饵料生产批发 · B2B + 小 B 官网）
> **生成日期**：2026-08-29
> **产品类型**：垂类电商 + 品牌官网 + AI 客服（信息获取 + 自助服务 + 转化）
> **设计母题**：水 / 鱼 / 浮漂 / 波浪 / 饵料 ——「钓鱼江湖 · 匠心饵料」
> **风格**：自然质朴（宣纸暖白）＋ 专业可信（潭绿主色）＋ 渔火点睛（暖橙强调）

---

## 1. 设计依据（Design Rationale）

| 维度 | 结论 |
|---|---|
| **目标用户** | ① 个体钓友（关心价格/库存/到货/用法）② 经销商（批发价/阶梯政策/库存/订单跟踪）③ 企业老板（销量/库存预警/客户分析）④ 客服/销售（后台） |
| **核心任务** | 信息获取（产品/工厂/资质/定制/联系）＋ 自助服务（AI 对话、在线下单、订单查询、定制提交、留言）＋ 转化（浏览→咨询/下单/定制） |
| **内容特点** | 钓鱼饵料 B2B；调性=江湖/匠人/自然/丰收；真实素材=红虫颗粒/九一八/螺鲤3号等产品名、2000㎡/3 产线/500+ 经销商等数据 |
| **页面类型** | 营销首页 / 产品列表 / 工厂介绍 / 定制表单 / 在线下单 / 订单查询 / 联系留言 / 全局 AI 客服浮窗 |
| **字体策略** | 标题用**思源宋体（Noto Serif SC）**——匠人/江湖气质，区别于通用黑体；正文用**思源黑体（Noto Sans SC）** |
| **配色策略** | 70% 宣纸暖白底 ＋ 20% 潭绿（主色/信任） ＋ 7% 湖蓝（次级/水） ＋ 3% 渔火橙（CTA/价格/强调） ＋ 墨绿黑（正文/夜钓页脚） |

---

## 2. 色彩 Token（Color）

所有颜色通过 `tailwind.config` 的 `extend.colors` 在全局定义；同时为兼容历史 class，
**Tailwind 默认色板（stone/slate/emerald/green/blue/sky/cyan/teal/indigo/orange/amber/red/white）已被重映射到本体系**（见第 10 节「实现」）。

### 2.1 语义主色
| 角色 | Token | Hex | 用途 / 备注 |
|---|---|---|---|
| 页面底 | `--paper` | `#F6F1E7` | 宣纸暖白，全站背景 |
| 深色带 | `--paper-deep` | `#ECE4D4` | 区块底/分隔带（≈ stone-100） |
| 卡片底 | `--paper-card` | `#FBF8F1` | 卡片/浮层（≈ white 重映射值） |
| 主色·潭绿 | `--jade-600` | `#1F6B54` | 主按钮、链接、主标题强调、焦点环 |
| 主色·潭绿浅 | `--jade-100` | `#DCEDE6` | 浅底徽标、输入框底色 |
| 次级·湖蓝 | `--lake-500` | `#2C7C8C` | 渐变辅色、次级信息 |
| 强调·渔火橙 | `--fire-500` | `#E2703A` | 价格、强调、大字标题；**仅用于 ≥18px 或加粗文本/图标** |
| 强调·渔火橙(深) | `--fire-700` | `#B5481C` | **实心 CTA 背景**（白字对比 ≥7:1，达 AA） |
| 正文·墨绿黑 | `--ink-900` | `#15211D` | 正文、标题 |
| 次级文字 | `--ink-500` | `#738079` | 说明文字（与纸面对比 ≥4.5:1） |
| 页脚·夜钓深潭 | `--ink-900-deep` | `#0C2A24` | 深潭墨绿页脚（≈ slate-900 重映射） |

### 2.2 完整阶梯（Tailwind 命名）
- **brand / jade**：50 `#EAF4EF` · 100 `#DCEDE6` · 200 `#BFE0D3` · 300 `#8FC4B1` · 400 `#4FA386` · 500 `#2C8466` · 600 `#1F6B54` · 700 `#185647` · 800 `#124037` · 900 `#0C2A24`
- **sunset / fire**：50 `#FCEFE6` · 100 `#FBE6D8` · 200 `#F7CBB2` · 300 `#F4B495` · 400 `#ED935F` · 500 `#E2703A` · 600 `#D65F2A` · 700 `#B5481C`
- **lake**：50 `#EBF5F7` · 100 `#D2E9ED` · 200 `#A6D4DC` · 300 `#6FBAC6` · 400 `#3E9AA9` · 500 `#2C7C8C` · 600 `#236472` · 700 `#1B4E5A` · 800 `#143B44`
- **ink**：900 `#15211D` · 800 `#243029` · 700 `#3A473F` · 600 `#54615A` · 500 `#738079` · 400 `#9AA89F` · 300 `#C3CCC4`
- **paper**：DEFAULT `#F6F1E7` · deep `#ECE4D4` · card `#FBF8F1`
- **状态色（重映射）**：emerald→jade 阶梯（成功/现货）；amber/orange→sunset 阶梯（警告/价格）；red→`#C73320` 系（错误）

### 2.3 对比度底线（WCAG AA）
- 正文/普通文本：墨绿黑 `#15211D` on 纸面 `#F6F1E7` ≈ **12:1**（远超）
- 潭绿 `#1F6B54` on 纸面 ≈ **5.7:1**（通过）
- 渔火橙 `#E2703A` **仅用于大号/加粗文本或图标**；作实心按钮背景时必须用 `#B5481C`（白字 ≈ 7:1）
- 禁止：渔火橙浅色（`#E2703A`/`#ED935F`）作小号白底正文（≈3:1，不达标）

---

## 3. 字体（Typography）

| 用途 | 字体 | 字重 | 备注 |
|---|---|---|---|
| 标题 H1–H3 | Noto Serif SC（思源宋体） | 600–900 | 匠人/江湖气质；H1 用 `tracking-tight` |
| 正文 / UI | Noto Sans SC（思源黑体） | 400–700 | 默认 |
| 数字/价格 | Noto Sans SC + `tabular-nums` | 700 | 价格用渔火橙，等宽对齐 |

### 3.1 字号阶梯（Type Scale）
| Token | 桌面 | 移动 | 用途 |
|---|---|---|---|
| `--text-display` | 56px / 3.5rem | 36px / 2.25rem | H1 Hero |
| `--text-h1` | 48px / 3rem | 30px / 1.875rem | H1 内页 |
| `--text-h2` | 32–36px / 2–2.25rem | 26px / 1.625rem | H2 区块标题 |
| `--text-h3` | 20px / 1.25rem | 18px / 1.125rem | H3 卡片标题 |
| `--text-body` | 16px / 1rem | 16px | 正文 |
| `--text-sm` | 14px / 0.875rem | 14px | 辅助说明 |
| `--text-xs` | 12px / 0.75rem | 12px | 标签/页脚 |

> 移动端 H1 用 `text-4xl`，桌面 `lg:text-5xl xl:text-6xl`（Hero）。所有标题 `font-display` 类。

---

## 4. 间距 / 圆角 / 阴影（Spacing · Radius · Shadow）

### 4.1 间距阶梯（8px 基准）
| Token | 值 | 用途 |
|---|---|---|
| `--space-1` | 4px | 图标与文字微距 |
| `--space-2` | 8px | 行内间距 |
| `--space-3` | 12px | 控件内边距小 |
| `--space-4` | 16px | 标准内边距 |
| `--space-6` | 24px | 卡片内边距 / 区块间距 |
| `--space-8` | 32px | 大间距 |
| `--space-12` | 48px | 区块上下 |
| `--space-16` | 64px | Hero/页脚节奏 |

### 4.2 区块垂直节奏（Section Rhythm）
- 移动：`py-12`（48px） · 桌面：`lg:py-20`（80px）
- 容器内边距：`px-6`（24px） · 容器宽：`max-w-7xl`（1280px）居中
- 统一类：`.section`（移动 48px / ≥768px 80px 上下）、`.container-x`（1280 居中 + 24 侧边）

### 4.3 圆角 / 阴影
| Token | 值 | 用途 |
|---|---|---|
| `--radius-sm` | 8px | 输入框、小标签 |
| `--radius-lg` | 16px | 卡片、按钮 |
| `--radius-xl` | 20px | 大卡、聊天窗 |
| `--shadow-sm` | `0 1px 2px rgba(21,33,29,.06)` | 静态卡 |
| `--shadow-md` | `0 4px 14px rgba(31,107,84,.10)` | hover 卡 |
| `--shadow-lg` | `0 16px 40px rgba(31,107,84,.16)` | 浮层/成功卡 |

---

## 5. 组件规范与全状态（Components & States）

> 每个可交互组件必须覆盖以下状态；过渡统一 `150–300ms ease`。

### 5.1 按钮 Button
| 状态 | 主按钮（潭绿） | 次按钮（描边） | CTA（渔火橙深） |
|---|---|---|---|
| default | `bg-jade-600 text-white` | `border-2 border-jade-600 text-jade-600 bg-white` | `bg-fire-700 text-white` |
| hover | `bg-jade-700` + `translateY(-1px)` | `bg-jade-600 text-white` | `bg-fire-700 brightness-95` + 微浮 |
| active | `translateY(0) brightness-95` | 同 hover | 同 |
| focus-visible | 焦点环 `outline 2px jade` | 同 | 同 |
| disabled | `opacity-50 cursor-not-allowed` | 同 | 同 |
| loading | 内嵌 spinner，禁用点击 | — | — |

规则：所有按钮 `cursor-pointer`；图标按钮必须 `aria-label`；圆角 `--radius-lg`；内边距 `px-6 py-3`；字重 600。

### 5.2 卡片 Card
- default：`bg-paper-card border border-jade-100 rounded-xl shadow-sm`
- hover：`-translate-y-1 shadow-md`（仅可点击卡）
- 入场：`float-up / card-enter`（≤500ms，尊重 reduced-motion）

### 5.3 表单字段 Field
- default：`border border-stone-200 rounded-lg px-4 py-3 text-sm bg-white`
- focus：`border-jade-600 + ring 4px rgba(31,107,84,.14)`，`outline:none`
- error：`border-red-500 + 提示文字 red`
- disabled：`bg-stone-100 opacity-60 cursor-not-allowed`
- 必须配套 `<label>`（可见或 `.sr-only`）；占位符用 `--ink-400`

### 5.4 徽标 / 标签 Badge
- 胶囊 `rounded-full px-3 py-1 text-xs`；类型色：现货=`jade`、缺货/警告=`sunset`、主推=`sunset`
- 状态徽：已发货=`jade`、未发货=`sunset`（均为浅底深字，对比达标）

### 5.5 聊天气泡 Chat Bubble（AI 客服）
| 角色 | 样式 |
|---|---|
| 用户 | 渔火橙渐变 `bg-gradient(sunset-500→sunset-600) text-white`，右下小圆角 |
| AI | `bg-paper-card border border-jade-100 text-ink-900`，左下小圆角，小鱼头像 |
| 错误 | `bg-sunset-50 border border-sunset-200 text-fire-700` |
| 流式光标 | `cursor-blink` 潭绿竖线 |
| 空/加载 | 空状态插画 + 文案；加载用 spinner |

### 5.6 导航 / 页脚 / FAB
- 导航：`sticky top-0` 毛玻璃；激活项 `aria-current=page` + 潭绿下划线；移动端汉堡 `aria-label`
- 页脚：深潭墨绿 `#0C2A24`，联系信息用 SVG 图标，链接 hover 转 `jade-300`
- FAB：右下圆形潭绿渐变，涟漪动效，`aria-label="打开 AI 客服"`

---

## 6. 动效（Motion · 钓鱼母题）

所有动效都与「水/钓」相关，且必须可被 `prefers-reduced-motion` 关闭。

| 动效 | 类 | 用途 | 时长 |
|---|---|---|---|
| 浮漂轻晃 | `.bobber` | 浮漂/Logo 点缀 | 3s ease-in-out 无限 |
| 鱼游动 | `.fish-swim` | 鱼/AI 标记 | 5–6s 无限 |
| 水波涟漪 | `.ripple` / `.ripple-ring` | 江面/ FAB | 3s 无限 |
| 波浪流动 | `.wave-flow` | 分隔线 | 12–14s linear 无限 |
| 卡片浮起 | `.float-up` / `.card-enter` | 入场 | ≤500ms |
| 气泡入场 | `.bubble-in` | 聊天 | 250ms |
| 打字光标 | `.cursor-blink` | AI 流式 | 800ms step |
| 弹窗/ FAB 入场 | `.modal-enter` / `.fab-enter` | 浮层 | 300/400ms |

规则：hover 过渡 `150–300ms`；禁止 `scale` 导致布局位移；装饰动效 `prefers-reduced-motion: reduce` 时 `animation:none`。

---

## 7. 响应式（Responsive）

| 断点 | 视口 | 规则 |
|---|---|---|
| 移动 | 375px | 单列；导航折叠为汉堡；Hero 字号 `text-4xl`；卡片栅格 `grid-cols-1/2`；聊天窗高 `auto/min(600px)` |
| 平板 | 768px | 双列；导航展开；区块 `lg:py-20` |
| 桌面 | 1024px | 多列（产品 3 列、首页 5 列） |
| 大屏 | 1440px | 容器 `max-w-7xl` 居中，不无限拉伸 |

要求：无横向滚动；固定导航不遮挡内容（跳转链接 + `scroll-margin`）；触摸目标 ≥44×44px。

---

## 8. 可访问性（Accessibility · WCAG AA）

1. **对比度**：正文 ≥4.5:1，大字/图标 ≥3:1（渔火橙仅大号）。
2. **焦点可见**：所有可聚焦元素 `:focus-visible` 显示 2px 潭绿环（键盘可达）。
3. **跳转链接**：每页首行 `<a class="skip-link" href="#main">跳到主内容</a>`。
4. **语义结构**：`header/nav/main/footer` 地标；标题层级 H1→H2→H3 不跳跃；`<main id="main" tabindex="-1">`。
5. **ARIA**：导航 `aria-label`、激活项 `aria-current="page"`、图标按钮 `aria-label`、汉堡 `aria-label`、`aria-expanded`；聊天区 `aria-live="polite"`；错误横幅 `role="alert"`。
6. **表单**：每个控件配套 `<label>`；必填用可见 `*` + `aria-required`；错误 `aria-describedby`。
7. **媒体**：图片 `alt` 完整；装饰 SVG `aria-hidden="true"`。
8. **动效**：尊重 `prefers-reduced-motion`。
9. **减少依赖**：无后端时页面结构与文案完整渲染（API 失败显示友好错误，不白屏）。

---

## 9. 禁用项（Anti-Patterns）

- ❌ **emoji 作图标** —— 一律用内联 SVG（Lucide/自绘，currentColor）
- ❌ 正文小字号使用浅渔火橙（对比不达标）
- ❌ 瞬时状态无过渡（必须 150–300ms）
- ❌ 焦点态不可见（键盘导航必须可见）
- ❌ 低对比文字（<4.5:1）
- ❌ 移动端横向滚动 / 固定导航遮挡内容
- ❌ 纯装饰动效干扰阅读（关闭 reduced-motion）
- ❌ 在 HTML 写死旧色值（如 `#0d9488`/`#14b8a6`/`#f97316`）——统一走 token
- ❌ 破坏既有路由/文案/功能（下单/查单/定制/留言/AI 流式对话保持不变）

---

## 10. 实现（Implementation Notes）

- **单一样式源**：`frontend/assets/theme.css` 承载全部 token、组件、状态、动效、a11y。
- **色板重映射**：7 个页面的 `tailwind.config.extend.colors` 将 Tailwind 默认色板
  （stone/slate/emerald/green/blue/sky/cyan/teal/indigo/orange/amber/red/white）整体重映射到本体系，
  使历史 utility class 自动统一，无需逐处改 HTML。
- **删除冲突内联样式**：各页 `<head>` 内残留的旧色 `<style>` 已移除，统一由 `theme.css` 提供。
- **图标替换**：导航/页脚/CTA/特性卡/状态徽标中的 emoji 已替换为内联 SVG。
- **保留**：所有路由、真实中文文案、Vue SSE 流式对话、各表单提交与后端接口、FAB + AI 弹窗。
