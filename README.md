# 中渔天下 AI Agent

> 基于 Django + Vue + DeepSeek + MySQL 的企业智能客服 Demo，具备 **Agent Function Calling 工具调用、RAG 检索增强问答、真·流式输出、对话持久化、后台管理** 等能力。

---

## 技术栈

| 层级 | 技术 | 说明 |
|------|------|------|
| 前端 | Vue 3 (CDN) + axios | 单页多页面企业官网，AI 客服聊天 |
| 后端 | Django 5.2 + Django原生视图 | RESTful API，SSE 真流式输出 |
| AI | DeepSeek API (deepseek-chat) | Function Calling 工具调用 + RAG 检索增强 |
| RAG | 自研轻量引擎（纯标准库） | 切块 → TF-IDF 向量化 → 余弦召回 → BM25 重排 |
| 数据库 | MySQL 8.0 + PyMySQL | 产品/库存/订单/对话/留言 |
| 后台 | Django Admin + SimpleUI | 模型注册、列表展示、分组菜单 |
| 部署 | 本地开发 | `python manage.py runserver` |

---

## 功能特性

- [x] **Agent Function Calling**：基于 OpenAI 兼容 `tools` + `tool_calls` 的标准工具调用（ReAct 循环，最多 3 轮），AI 自动判断意图并调用 `query_product` / `check_inventory` / `get_order_status` / `calculate_quote` / `search_knowledge` / `guide_to_contact`
- [x] **顾问引导式客服（不代客下单）**：AI 先帮顾客明确产品选择（查产品/库存/报价），顾客表达购买或定制意向时调用 `guide_to_contact` 引导，前端自动渲染「定制服务 / 联系我们」按钮卡片收尾，由人工承接交易
- [x] **在线下单**：消费者通过 `/order.html` 主动下单（产品下拉 + 单价联动 + 总额自动计算），后端自动生成订单号写入数据库；Django Admin 可按状态筛选、列表内编辑状态、`📦 标记为已发货` 一键操作
- [x] **RAG 检索增强问答**：企业知识库问答走完整 RAG Pipeline（文档切块 → 向量化存储 → 余弦召回 → BM25 重排），替代原硬编码关键词匹配
- [x] **真·流式输出**：`stream=True` 边生成边输出，非"先全量再逐字回放"，打字机体验更真实
- [x] **Prompt 版本管理**：System Prompt 独立到 `prompts/` 目录，`load_prompt(name)` 按版本加载，便于实验与调优
- [x] **对话历史持久化**：每次对话存入 MySQL `conversations` 表，支持多轮上下文
- [x] **会话记忆与恢复**：localStorage 存储 session_id，进入页面/打开弹窗时自动从后端 `/api/agent/history/<sid>/` 拉取历史，跳转、刷新均不丢消息；支持一键「新对话」
- [x] **钓鱼主题 UI**：「江畔渔火」湖青配色 + 落日橙点缀，水波背景、波浪头栏、小鱼头像、浮漂动画等定制元素，去模板化
- [x] **用户留言系统**：前端联系表单 → 后端 API → MySQL `contact_messages` → Admin 后台管理
- [x] **多页面企业官网**：首页 / 产品 / 工厂 / 定制 / 联系，5 个独立页面
- [x] **后台数据管理**：SimpleUI 后台分组展示产品、库存、订单、留言、对话记录
- [x] **关键词兜底**：AI 未按预期输出时，用规则识别意图，保证稳定性

---

## 项目结构

```
ZT-agent/
├── agent/                      # Django App
│   ├── __init__.py
│   ├── admin.py                # ★ 注册所有模型到 Admin 后台
│   ├── apps.py
│   ├── models.py               # ★ Products / Inventory / Orders / Conversations / ContactMessage
│   ├── rag.py                  # ★ RAG 引擎（切块/向量化/召回/重排，纯标准库）
│   ├── knowledge_data.py       # ★ 企业知识库语料 + 惰性构建 RAG 引擎
│   ├── llm.py                  # ★ LLM 客户端封装（chat / chat_stream）
│   ├── prompts.py              # ★ Prompt 版本管理（load_prompt）
│   ├── agent.py                # ★ ReAct Agent 循环 + 真流式（agent_stream）
│   ├── tools.py                # ★ 工具函数 + 工具 Schema（Function Calling 定义）
│   ├── urls.py                 # ★ API 路由
│   ├── views.py                # ★ 聊天(流式) / 联系表单 / 历史查询 视图
│   └── migrations/
├── config/                     # Django 配置
│   ├── __init__.py             # PyMySQL 注册 MySQLdb
│   ├── asgi.py
│   ├── settings.py             # ★ 数据库/AI/CORS/SimpleUI 配置
│   ├── urls.py                 # ★ 根路由（包含前端页面托管）
│   └── wsgi.py
├── frontend/                   # Vue 前端（统一 Tailwind 湖青主题）
│   ├── index.html              # 首页 + AI 客服
│   ├── products.html           # 产品中心（动态从后端拉产品）
│   ├── factory.html            # 工厂介绍
│   ├── custom.html             # 定制服务
│   ├── order.html              # ★ 在线下单
│   ├── order_query.html        # ★ 订单查询（按手机号）
│   ├── contact.html            # 联系我们 + 留言表单
│   ├── style.css               # 公共样式（旧版，保留兼容）
│   └── app.js                  # ★ Vue3 逻辑（流式聊天 + session_id）
├── prompts/
│   ├── system_prompt.md        # 产品视角 System Prompt（v1，参考文档）
│   └── system_prompt_agent.md  # ★ Agent 运行时 System Prompt（由 load_prompt 加载）
├── agent_db.sql                # ★ 数据库初始化脚本（建表 + 初始数据）
├── PRD.md                      # 产品需求文档
├── 需求分析.md                  # 需求分析文档
├── test_rag.py                 # ★ RAG 引擎独立单测（无需 Django/MySQL/API Key）
├── test_agent.py               # Day2 裸调验证脚本（历史）
├── .env                        # 环境变量（不入库）
├── .gitignore
├── manage.py
└── README.md                   # 本文件
```

---

## 快速开始

### ① 环境准备

```powershell
# 创建虚拟环境
python -m venv .venv
.venv\Scripts\Activate.ps1

# 安装依赖
pip install django==5.2.17 PyMySQL python-dotenv openai django-cors-headers django-simpleui
```

### ② 数据库初始化

```powershell
# 1. 确保 MySQL 8.0 正在运行
# 2. Navicat 或命令行执行 agent_db.sql
mysql -u root -p123456 < agent_db.sql

# 3. 确认 .env 配置正确
# DEEPSEEK_API_KEY=sk-xxx
# DB_PASSWORD=123456
```

### ③ 环境变量

复制 `.env.example` 为 `.env`，或直接编辑 `.env`：

```ini
DEEPSEEK_API_KEY=sk-your-api-key
DB_PASSWORD=123456
```

### ④ 启动服务

```powershell
# 数据库迁移（ContactMessage 等新表）
python manage.py migrate

# 启动 Django
python manage.py runserver 127.0.0.1:8000

# 浏览器访问
# 首页：http://127.0.0.1:8000/
# 后台：http://127.0.0.1:8000/admin/
```

### ⑤ 运行 RAG 单测

```powershell
# 不依赖数据库 / API Key，验证 RAG 检索与排序
python test_rag.py
```

### ⑥ 创建管理员（可选）

```powershell
python manage.py createsuperuser
# 用户名：admin
# 密码：admin123
```

---

## API 接口

| 方法 | 路径 | 说明 |
|------|------|------|
| POST | `/api/agent/chat/` | AI 对话（SSE 真·流式） |
| POST | `/api/agent/contact/` | 用户留言提交 |
| POST | `/api/agent/custom/` | 定制需求提交 |
| GET | `/api/agent/history/<session_id>/` | 查询对话历史 |
| GET | `/api/products/` | 产品列表（下单页下拉框） |
| POST | `/api/orders/` | 创建订单（下单页提交） |
| POST | `/api/orders/query/` | 按手机号查询订单（隐私脱敏：仅返回订单号/产品/数量/状态/时间，不返回姓名/电话；带 order_no 时为双重验证查单个） |
| GET | `/api/orders/<order_no>/` | 查询单个订单（含姓名/电话，**用于后台管理系统，慎用前端暴露**） |
| GET | `/admin/` | Django Admin 后台管理（含产品/库存/订单/留言/对话/定制） |
| GET | `/order.html` | 在线下单页面 |
| GET | `/order_query.html` | 订单查询页面 |

### 对话接口请求示例

```json
POST /api/agent/chat/
Content-Type: application/json

{
    "message": "红虫颗粒多少钱？",
    "session_id": "abc123"  // 可选，不传则自动生成
}
```

### 对话接口响应（SSE 流）

```
data: {"content": "根据"}
data: {"content": "您的问题，"}
data: {"content": "红虫颗粒"}
data: {"content": "的零售价为"}
data: {"content": "15元/包。"}
data: {"done": true, "session_id": "abc123"}
```

> 顾客表达购买 / 定制意向时，会收到 `{"type": "guide_card"}` 事件，前端据此渲染「定制服务 / 联系我们」引导卡片（AI 不代客下单，交易由人工承接）。

---

## Agent 工具设计

| 工具名 | 输入 | 输出 | 数据来源 |
|--------|------|------|----------|
| `query_product` | 产品名（如"红虫颗粒"） | 规格、适用鱼种、零售/批发价 | `products` 表 |
| `check_inventory` | 产品名（如"螺鲤3号"） | 当前库存、是否低于预警线 | `inventory` + `products` 表 |
| `get_order_status` | 订单号（如"DD20240801"） | 订单状态、客户、金额 | `orders` 表 |
| `calculate_quote` | 商品清单（name + qty） | 单价、小计、合计 | `products` 表 |
| `search_knowledge` | 自然语言问题 | 相关答案片段 + 来源标题 | RAG（企业知识库） |
| `guide_to_contact` | 无 | 引导文案（去定制服务 / 联系我们） | 静态 |

### 工作流程（ReAct / Function Calling）

```
用户提问 → LLM（携带 tools, tool_choice=auto）
    ├── 模型返回 tool_calls → 执行工具 → 结果以 role=tool 喂回 → 再问 LLM（循环，最多 3 轮）
    └── 模型直接生成 → 流式输出最终回答
```

**产品定位**：AI 客服 = 咨询顾问 + 引导转化，**不代客下单**。
- 顾客咨询产品 → 查产品/库存/报价，帮顾客明确产品选择
- 顾客表达购买 / 定制意向 → 调用 `guide_to_contact` 引导，前端渲染「📝 定制服务 / 💬 联系我们」按钮卡片收尾
- 前端兜底：AI 回复文本含引导词（定制服务/联系我们/提交需求/联系客服）时也自动渲染引导卡片

- 工具定义用标准 JSON Schema（`TOOL_SCHEMAS`），与 OpenAI 兼容接口一致。
- 相比早期"正则提取 JSON"的做法，Function Calling 的意图判定更稳定、更可扩展。

### 兜底机制

- 工具调用参数非法 / 执行异常 → `call_tool` 统一兜底返回错误描述，不中断主流程。
- 模型空回复时 → 退回最后一条工具结果。
- 达到最大工具轮次仍未收敛 → 返回最后工具结果并建议转人工。

---

## RAG 架构（检索增强问答）

`search_knowledge` 工具背后是 `agent/rag.py` 的轻量 RAG 引擎，完整实现四阶段：

```
文档切块(chunking) → 向量化存储(TF-IDF) → 召回(余弦Top-N) → 重排(BM25)
```

- **零第三方依赖**：纯标准库实现，中文按「字符 bigram」切分（无需 jieba）。
- **可插拔**：`Embedder` / `VectorStore` 均为接口化设计，方便替换为生产级方案。

### 如何替换为生产级方案（对齐实习 JD）

```python
# 1. 真实语义向量：替换 Embedder 为 OpenAI / DashScope text-embedding
#    embedder = APIEmbedder(model="text-embedding-3-small")  # 实现 fit/transform/cosine 接口

# 2. 真实向量库：把 InMemoryVectorStore 换成 Chroma / FAISS / Milvus
#    store = ChromaVectorStore(collection_name="zhongyu_kb")  # 实现 add/search 接口

# 3. LLM 重排：在 retrieve 后增加一轮 cross-encoder 或 LLM 打分，替换当前 BM25 重排
```

接口约定见 `agent/rag.py` 顶部注释，替换时只需实现同名方法，`RAGEngine` 上层逻辑无需改动。

---

## 后台管理

访问 `http://127.0.0.1:8000/admin/`，登录后可见：

### 业务数据
- **产品管理**：5 款产品（红虫颗粒、九一八、螺鲤3号、蓝鲫X5、速攻2号）
- **库存管理**：5 条库存记录（含预警线）
- **订单管理**：5 条测试订单（DD20240801 等）

### 客服与用户
- **用户留言**：来自联系表单的真实留言
- **对话记录**：AI 对话历史（按 session_id 分组）
- **定制需求**：来自定制表单的提交

### 认证与权限
- **用户**：Django Auth 用户管理
- **用户组**：权限分组

---

## 开发历程

| 阶段 | 内容 | 交付物 |
|------|------|--------|
| **Day 1-2** | 需求分析 + PRD + Agent Prompt 设计验证 | `需求分析.md`、`PRD.md`、`prompts/system_prompt.md` |
| **Day 3** | MySQL 建表 + Mock 数据 | `agent_db.sql` |
| **Day 4** | Django 骨架 + MySQL + DeepSeek 接入 + SimpleUI | `config/settings.py`、`.env` |
| **Day 5** | Agent App + 工具函数 + 真实数据库查询 | `agent/views.py`、`agent/tools.py`、`agent/models.py` |
| **Day 6** | Vue 企业官网 + 多页面 + 前后端联调 + Admin 注册 | `frontend/`、`agent/admin.py` |
| **Day 7** | SSE 流式输出 + 对话存库 + 留言系统 + README | 流式改造、`contact_messages` |
| **Day 8** | RAG 落地 + Function Calling + 真流式 + Prompt 版本化 | `rag.py`、`llm.py`、`agent.py`、`prompts.py`、`test_rag.py` |

---

## 后续规划

- [x] RAG 完整落地（切块 + 向量化 + 召回 + 重排，纯标准库）
- [ ] 向量库升级（Chroma / FAISS / Milvus）+ 真实 Embedding 模型（语义检索）
- [ ] Docker 容器化部署
- [ ] 接入企业微信 / 小程序客服
- [ ] 对接真实 ERP / 订单系统
- [ ] 角色权限隔离（钓友 / 经销商 / 老板）
- [ ] 语音输入 / 图片识别
