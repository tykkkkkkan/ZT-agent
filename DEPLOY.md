# ZT-agent Docker 部署指南（新手版）

> 适用对象：第一次用 Docker、从没部署过项目的人。
> 目标：几条命令把 ZT-agent（数据库 + 缓存 + Web 服务）在本机跑起来，浏览器能访问。

---

## 一、先确认 Docker 真的在运行

只「下载安装」还不够，必须**启动 Docker Desktop**。

1. 双击桌面 **Docker Desktop** 图标启动。
2. 看电脑右下角任务栏（系统托盘）有没有一个 **小鲸鱼 🐳 图标**。
   - 鼠标移上去显示 **「Docker Desktop is running」** → 正常，继续。
   - 显示「starting…」→ 等它变成 running（第一次可能要 1～2 分钟）。
   - 找不到图标 → 去开始菜单搜 Docker 打开。
3. 打开一个**终端**验证（任选其一）：
   - **PowerShell**（推荐，Windows 自带）：开始菜单搜 `PowerShell`
   - **Git Bash**（你一直在用）：开始菜单搜 `Git Bash`

在终端里输入下面任一行，能打印出版本号就说明 Docker 可用：

```powershell
docker --version
docker compose version
```

> 如果提示 `docker: command not found`，说明 Docker Desktop 没启动，回到第 1 步。

---

## 二、进入项目目录

把路径换成你自己的项目位置（下面是你当前的路径）：

```powershell
# PowerShell 写法
cd "D:\TYKKKKKK\Trae\中渔天下\ZT-agent"
```

```bash
# 如果你用 Git Bash，写法不同（盘符变成 /d/）
cd /d/tykkkkkk/Trae/中渔天下/ZT-agent
```

> 小技巧：在文件管理器里进入 `ZT-agent` 文件夹，在地址栏输入 `powershell` 回车，就能直接在该目录打开终端，不用手敲 `cd`。

---

## 三、一行命令启动（核心步骤）

```powershell
docker compose up --build
```

就这一条。**第一次会做三件事，比较慢，耐心等：**

| 阶段 | 在干什么 | 大概耗时 |
|---|---|---|
| 拉镜像 | 从网上下载 MySQL 8、Redis、Python 3.12 三个基础镜像（约 1GB） | 几分钟（看网速） |
| 构建 web | 按 `Dockerfile` 把项目装进镜像、安装所有依赖（含 chromadb） | 1～3 分钟 |
| 启动容器 | 依次启动 db → redis → web，自动建表、灌初始数据 | 几十秒 |

**你看到终端最后出现类似下面的日志，就说明起来了：**

```
web_1  | Watching for file changes with StatReloader
web_1  | Starting development server at http://0.0.0.0:8000/
```

> `docker compose up` 默认会「霸占」这个终端窗口（日志一直滚）。**不要关这个窗口**，关了服务就停了。
> 想让它后台跑，用 `docker compose up -d --build`（加 `-d` = 后台/守护模式），之后用 `docker compose logs -f web` 看日志。

---

## 四、验证是否成功

打开浏览器，访问下面三个地址（任选一个先试）：

| 地址 | 正常表现 |
|---|---|
| http://localhost:8001/healthz | 页面显示 `{"status":"ok"}` 或 `{"status":"degraded"}` |
| http://localhost:8001/ | 看到项目首页 / 登录页 |
| http://localhost:8001/api/schema/swagger-ui/ | 看到 API 文档界面（Swagger） |

**只要 `/healthz` 返回 `status`，部署就成功了。** 🎉

> 如果浏览器打不开：等 1 分钟再试（MySQL 第一次启动要初始化），详见文末「故障排查」。

---

## 五、日常常用命令（背下这几条就够）

在 `ZT-agent` 目录下运行：

```powershell
# 启动（前台，日志可见，关窗口即停）
docker compose up

# 启动（后台，推荐日常用）
docker compose up -d --build

# 看 web 服务实时日志（Ctrl+C 退出查看，不影响服务）
docker compose logs -f web

# 停止服务（保留数据库数据）
docker compose down

# 重启（改了代码后用，会重新构建 web 镜像）
docker compose up -d --build

# 彻底清空（连数据库一起删，慎用！）
docker compose down -v
```

---

## 六、配置说明（你基本不用改）

项目根目录的 `.env` 文件**已经配好了**，直接用即可。关键几项：

| 变量 | 当前值 | 说明 |
|---|---|---|
| `DEEPSEEK_API_KEY` | 已填 | AI 对话用的密钥，没有它聊天会报错 |
| `DB_PASSWORD` | 已填 | MySQL 密码，db 和 web 共用，保持一致 |
| `RAG_BACKEND` | `embedding` | 走真实向量检索（Chroma + bge-m3）；想零依赖可改成 `tfidf` |
| `EMBEDDING_API_KEY` | 已填 | 向量化模型密钥，embedding 模式必需 |
| `DEBUG` | `True` | 本地调试用；上线再改成 `False` |
| `CORS_ALLOWED_ORIGINS` | 含 8000 与 8001 | 前端跨域白名单。**换了 web 端口要同步加进来** |
| `DJANGO_SECRET_KEY` | 已填 | Django 密钥。**留空会导致 web 容器反复重启**（见坑 6）；生产环境请换一个新的随机串 |

> 这些变量通过 `docker-compose.yml` 里的 `env_file: .env` **整份注入容器**，
> 不需要在 compose 里逐项重复声明。只有 `DB_HOST`、`DB_PORT`、`REDIS_URL` 这类
> 「必须写成容器内地址」的项，才由 compose 的 `environment` 显式覆盖。
> 好处：以后往 `.env` 加新变量（如 `SENTRY_DSN`、`LOG_LEVEL`），容器自动拿到，不用改 compose。

> 改了 `.env` 之后，要重启才能生效：`docker compose down` 然后 `docker compose up -d --build`。

---

## 七、三个容器分别是干嘛的（理解用，不懂跳过）

```
┌─────────────────────────────────────────────┐
│              你的电脑 (localhost)             │
│                                              │
│  浏览器 ──:8001──►  web 容器 (Django 网站)   │
│                        │                      │
│            ┌───────────┼────────────┐         │
│            ▼           ▼            ▼         │
│        db 容器     redis 容器    chroma_data  │
│       (MySQL 8)   (缓存/限流)   (向量库文件)  │
└─────────────────────────────────────────────┘
```

- **web**：真正跑你代码的 Django 服务。容器内监听 8000，映射到宿主机 **8001**。
- **db**：MySQL 数据库，存商品/订单/对话等。容器内 3306，映射到宿主机 **3307**。数据存在名为 `mysql_data` 的卷里，**不会因重启丢失**。
- **redis**：缓存 + 接口限流计数器。宿主机 6379。
- **chroma_data**：向量检索的持久化目录（绑定挂载到本项目文件夹）。

> **端口为什么不是默认的？** 本机 3306 已被安装的 MySQL 占用、8000 已被一个 python 进程
> 占用。容器之间通信走 Docker 内部网络（web 用 `db:3306`、`redis:6379`），**与宿主机映射无关**，
> 所以换端口不影响服务之间协作，只影响你从浏览器/客户端连接时用的地址。

---

## 八、新手最容易踩的 5 个坑

### 坑 1：端口被占用（本项目已预先调整好）

报错形如：

```
Error response from daemon: ports are not available: exposing port TCP 0.0.0.0:3306 -> 127.0.0.1:0:
listen tcp 0.0.0.0:3306: bind: Only one usage of each socket address (protocol/network address/port)
                   is normally permitted.
```

- **含义**：宿主机某个端口已被别的程序占用，Docker 无法再监听。
- **本项目已调整**：`db` 映射到宿主机 **3307**（避开本机 MySQL 的 3306），`web` 映射到宿主机 **8001**（避开本机 python 占用的 8000）。
- **查是谁占用**（PowerShell）：
  ```powershell
  netstat -ano | findstr ":3306"      # 最后一列是 PID
  tasklist /FI "PID eq 12345"         # 把 12345 换成上面的 PID
  ```
- **再遇到新冲突**：编辑 `docker-compose.yml` 对应服务的 `ports`，**只改冒号左边**（宿主机侧），
  例如 `"8001:8000"` → `"8002:8000"`，然后 `docker compose up -d`。
- **注意**：冒号右边是容器内端口，**不要改**（改了要和 `Dockerfile`/启动命令一起改）。
- **6379 被占用未必是冲突**：若占用者是 `com.docker.backend`，那正是 Docker 自己的 redis 容器。

### 坑 2：构建慢 / 拉不下来（国内网络）

**慢分两层，要分别处理：**

**第 1 层：Docker 拉基础镜像慢**（卡在 `Pulling image` / `FROM python:3.12-slim`）

打开 **Docker Desktop → Settings（齿轮）→ Docker Engine**，配置：

```json
"registry-mirrors": [
  "https://hub-mirror.c.163.com",
  "https://dockerproxy.com",
  "https://docker.1ms.run"
]
```

改完点 **Apply & Restart**，再 `docker compose up --build`。

> ⚠️ **不要再用 `docker.m.daocloud.io`**。该站自 2025-10 起对 `manifests` 端点要求
> Bearer Token，匿名 HEAD 请求返回 `401 Unauthorized`，会让构建在 `FROM` 阶段直接失败：
> `failed to resolve source metadata for docker.io/library/python:3.12-slim ... 401 Unauthorized`

**第 2 层：容器内 pip 装依赖慢**（卡在 `[4/5] RUN pip install ...`）

- **已内置解决**：项目 `Dockerfile` 里已把 `PIP_INDEX_URL` 指向清华源，无需任何额外配置。
  相关环境变量：
  ```
  PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple
  PIP_TRUSTED_HOST=pypi.tuna.tsinghua.edu.cn
  ```
- **本地开发也想加速**：
  ```powershell
  pip install -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple
  ```
- **已做拆包优化**：`llama-index`（会拖入数十个传递依赖）已从主清单移到
  `requirements-demo.txt`，Docker 构建只装主流程依赖，pip 阶段约 **1–3 分钟**。
  需要跑 `demo_llamaindex.py` 时再单独装：
  ```powershell
  pip install -r requirements-demo.txt -i https://pypi.tuna.tsinghua.edu.cn/simple
  ```

### 坑 3：第一次访问空白 / 500
MySQL 容器第一次启动要初始化（建库 + 灌 `agent_db.sql` 初始商品数据），可能要等 30～60 秒。
- **解法**：等一会儿刷新；或看日志 `docker compose logs -f web` 确认有没有 `Starting development server`。

### 坑 4：聊天没反应 / 报错
- 检查 `.env` 里 `DEEPSEEK_API_KEY` 是否有效（额度/过期）。
- `RAG_BACKEND=embedding` 时，第一次提问会现场构建向量索引，前几秒慢一点是正常的。

### 坑 5：改了代码看不到效果
- Docker 里跑的是「镜像里的代码」，不是你改完立刻生效。
- **每次改代码后必须重建**：`docker compose up -d --build`。

### 坑 6：web 容器反复重启（`Restarting`），浏览器 `ERR_CONNECTION_REFUSED`
**现象**：`docker compose ps` 里 `db`、`redis` 都是 `healthy`，只有 `web` 一直 `Restarting`；
浏览器访问 `http://localhost:8001` 报「拒绝连接」。

**第一步永远是看日志**，这是唯一能一次定位的方法：
```powershell
docker compose logs web --tail 60
```

**最典型的一种**（本项目真实踩过）：
```
django.core.exceptions.ImproperlyConfigured: The SECRET_KEY setting must not be empty.
```
原因不是「没配 SECRET_KEY」，而是 **被注入了一个空字符串**：
- `settings.py` 里原本写的是 `os.getenv('DJANGO_SECRET_KEY', '兜底值')`；
- 但 `os.getenv(key, default)` **只在 key 完全不存在时才用 default**；
- 而 `docker-compose.yml` 里写 `DJANGO_SECRET_KEY: ${DJANGO_SECRET_KEY:-}`，
  在 `.env` 没这个变量时会向容器注入**空字符串** → 绕过兜底 → Django 启动即崩 → 重启循环。

**已修复的两处**（无需你再动手）：
1. `config/settings.py` 改为 `os.getenv('DJANGO_SECRET_KEY') or '兜底值'` —— 空字符串也走兜底；
2. `docker-compose.yml` 删掉那行 `${DJANGO_SECRET_KEY:-}`，改由 `env_file: .env` 提供。

**排查心法**：容器里 `Restarting` + `ERR_CONNECTION_REFUSED` = **应用起不来**（不是网络问题）。
网络问题会表现为超时/转圈，而不是「拒绝连接」。

---

## 九、故障排查速查表

| 现象 | 可能原因 | 处理 |
|---|---|---|
| `docker: command not found` | Docker Desktop 没启动 | 启动 Docker Desktop，等鲸鱼图标变 running |
| 卡在 Pulling / timeout | 网络拉镜像慢 | 配置国内镜像加速（坑 2） |
| `port is already allocated` / `bind: Only one usage of each socket address` | 宿主机端口被占用 | 改 `docker-compose.yml` 冒号左边端口（坑 1） |
| `/healthz` 一直转圈 | MySQL 还在初始化 | 等 1 分钟再试 |
| 浏览器 `ERR_CONNECTION_REFUSED` | 端口没人监听（容器没起来） | 先 `docker compose ps` 看 web 是否 `Restarting`（坑 6） |
| web 容器反复重启 | 配置错误（最常见：SECRET_KEY 为空） | `docker compose logs web --tail 60` 看红色报错（坑 6） |
| 聊天报错 401/429 | API Key 问题 | 检查 `.env` 的 Key 是否有效、额度是否充足 |
| 改了代码没变化 | 没重建镜像 | `docker compose up -d --build` |
| push 报 `insufficient_scope: authorization failed` | **没登录**（匿名身份 push，只有只读 token） | `docker-credential-desktop list` 确认，再 `docker login`（十一·第 2 步） |
| push 报 `denied: requested access to the resource is denied` | 用户名与登录账号不一致，或仓库名拼错 | 核对用户名 + 确认已在 Hub 建好同名仓库 |
| push 报 `unauthorized: authentication required` | 登录态失效 | 重新 `docker login` |

---

## 十、停止与清理

```powershell
# 临时停（数据保留，下次 up 直接起）
docker compose down

# 彻底清（删库删数据，重新开始用这个）
docker compose down -v

# 查看当前在跑的容器
docker compose ps
```

> 刚部署完建议先 `docker compose down`（保留数据），等确认没问题了再决定要不要 `down -v` 重来。

---

## 十一、把镜像推送到 Docker Hub（分享给别人的前提）

### 先理解两件事的区别

| | GitHub 仓库 | Docker Hub 镜像仓库 |
|---|---|---|
| 存的是 | **源码** | **构建好的运行环境**（Python + 依赖 + 代码，打包成一个文件） |
| 别人拿到后 | 还得自己 `pip install` 或 `docker build`（5~10 分钟） | 直接 `docker pull` 就能跑（几十秒） |
| 类比 | 菜谱 | 做好的菜 |

推送到 Docker Hub 的价值：**别人不需要 Python 环境、不需要装依赖、不需要构建**，一条命令跑起来。

### 第 1 步：注册账号并建仓库

1. 打开 <https://hub.docker.com/> 注册（用户名只能是小写字母/数字，一旦确定不能改）。
2. 登录后点右上角 **Create repository**。
3. 填写：
   - **Name**：`zt-agent`
   - **Visibility**：选 **Public**（免费账号只送 1 个 Private 仓库，分享用 Public）
4. 建好后你的镜像全名就是：`用户名/仓库名:标签`，本项目用的是 `tykkkkkk/zt-agent:latest`。

> 💡 **Docker Hub 用户名与 GitHub 用户名是两个独立账号，本项目里它们恰好不同：**
> | 平台 | 用户名 | 用在哪里 |
> |---|---|---|
> | Docker Hub | `tykkkkkk` | 镜像命名空间：`tykkkkkk/zt-agent:latest` |
> | GitHub | `tykkkkkkan` | 仓库地址：`github.com/tykkkkkkan/ZT-agent` |
>
> 若你要换成自己的账号，**只替换 `用户名/zt-agent` 形式的镜像地址**，
> 不要动 `github.com/...` 的地址，两者串了会 push 失败或 clone 失败。

### 第 2 步：登录（PowerShell）

```powershell
docker login
# Username: tykkkkkk     ← Docker Hub 用户名（不是 GitHub 用户名）
# Password: 推荐用 Personal Access Token：
#   hub.docker.com → 右上角头像 → Account settings → Personal access tokens
#   → 新建一个 Read & Write 权限的 token，用它当密码
#   注：个人账号未开两步验证时，直接填登录密码也能成功；
#       但开启 2FA 或加入启用 SSO 的组织后，必须改用 token。
```

**⚠️ 登录完必须验证一次，不要只看 `Login Succeeded`：**

```powershell
docker-credential-desktop list
# 期望输出：{"https://index.docker.io/v1/":"tykkkkkk"}
# 输出 {} 或 credentials not found in native keychain → 登录没落盘，重新登录
```

> **为什么必须验证**：`.docker\config.json` 里的 `auths` 键**登出后也会残留**，
> 看到它并不代表已登录；真正的凭据由 Windows 凭据管理器保管，只有上面这条命令能查。
>
> 登录没生效时 push 会报：
> ```
> push access denied, repository does not exist or may require authorization:
> server message: insufficient_scope: authorization failed
> ```
> **这不是网络问题，也不是仓库没建** —— 是匿名身份在 push，Docker Hub 只发给匿名请求
> 只读 token，所以回 `insufficient_scope`（作用域不足）。重新 `docker login` 即可。

> **国内直连 Docker Hub 常常超时。** 按下面顺序处理，不要跳过第 ① 步直接去翻菜单。
>
> **① 先开代理软件的系统代理（多数情况这一步就够了）**
>
> Clash Verge → 打开「系统代理」开关 → **完全退出** Docker Desktop（托盘鲸鱼图标右键 → Quit，不是 Restart）→ 重新启动。
>
> 原理：Docker Desktop 的代理模式默认是「跟随系统代理」，Windows 系统代理一旦指向代理端口，它会自动使用，**不需要进任何设置页**。
>
> 验证系统代理是否生效（PowerShell）：
> ```powershell
> reg query "HKCU\Software\Microsoft\Windows\CurrentVersion\Internet Settings" /v ProxyEnable   # 0x1 = 已开启
> reg query "HKCU\Software\Microsoft\Windows\CurrentVersion\Internet Settings" /v ProxyServer   # 应显示 127.0.0.1:<端口>
> ```
> 最直接的判据：打开 Clash Verge 的「连接」页面，执行 `docker push`，观察是否出现
> `registry-1.docker.io` / `production.cloudflare.docker.com` 的连接记录。出现 = 代理链路已通。
>
> **② 若仍超时，再手动指定**
>
> **Settings（齿轮）→ 左侧 `Resources` → 展开二级项 `Proxies`** —— 它在**二级菜单**里，这是"找不到"的主因；
> 也可用 Settings 窗口左上角的搜索框直接搜 `proxy` 跳转。
>
> 打开 **Manual proxy configuration**，HTTP 与 HTTPS 都填 `http://127.0.0.1:<端口>`，
> Bypass 填 `localhost,127.0.0.1,::1,192.168.0.0/16,172.16.0.0/12,10.0.0.0/8`，然后 Apply & restart。
>
> > ⚠️ **不要把 `registry-1.docker.io` / `*.docker.com` 加进 Bypass** —— 那会让 registry 流量绕过代理，等于白配。
> >
> > ⚠️ **不要去 `Settings → Docker Engine` 里给 `daemon.json` 加 `proxies` 字段。** 官方文档明确写着：
> > *Proxy configurations specified in the `daemon.json` are ignored by Docker Desktop.*
> > 网上大量教程这么写，但在 Docker Desktop 上是**静默无效**的（只对 Linux 上的原生 dockerd 生效）。
> >
> > ⚠️ 若 `Resources` 下完全没有 `Proxies`：托盘图标右键确认当前是 **Switch to Linux containers**
> > （处于 Windows 容器模式时部分网络相关 Tab 会被隐藏）。
>
> **③ 仍不通 → 换国内镜像仓库（终局方案）**
>
> 阿里云「容器镜像服务 ACR」个人版免费，国内推送/拉取都是本地速度：
> ```powershell
> docker login --username=<阿里云账号全名> registry.cn-hangzhou.aliyuncs.com
> docker tag <你的用户名>/zt-agent:latest registry.cn-hangzhou.aliyuncs.com/<命名空间>/zt-agent:latest
> docker push registry.cn-hangzhou.aliyuncs.com/<命名空间>/zt-agent:latest
> ```
> 同时把 `docker-compose.hub.yml` 的 `image:` 改成这个地址，仓库类型设为「公开」，对方免登录即可拉取。
>
> **取舍**：Docker Hub 地址通用、知名度高，适合投海外岗位；阿里云 ACR 快，适合国内受众。两者可以都推一份。

### 第 3 步：构建 + 打标签 + 推送

```powershell
cd D:\TYKKKKKK\Trae\中渔天下\ZT-agent

# ① 构建，同时打上两个标签：带版本号 + latest
#    标签规范：用户名/仓库名:版本
docker build -t tykkkkkk/zt-agent:v1.0.0 -t tykkkkkk/zt-agent:latest .

# ② 推送（两个标签各推一次）
docker push tykkkkkk/zt-agent:v1.0.0
docker push tykkkkkk/zt-agent:latest

# ③ 查看本地镜像及体积（推送前可用它估算上传时间）
docker images tykkkkkk/zt-agent
```

**关于 `docker tag`（改标签 / 补标签，不会重新构建）**：

```powershell
# 给已有镜像再补一个标签（例如按日期标记一次发布）
docker tag tykkkkkk/zt-agent:v1.0.0 tykkkkkk/zt-agent:2026-09-10
docker push tykkkkkk/zt-agent:2026-09-10
```

> 命名规则：`用户名/仓库名:标签`。省略 `:标签` 时默认是 `:latest`；
> 仓库名前不加用户名（如 `docker build -t zt-agent .`）就无法 push，必须带上用户名作为命名空间。

**用 Compose 的简写（等效，少打几个字）**：

```powershell
# docker-compose.yml 里 web 服务已写好 image: tykkkkkk/zt-agent:latest
# 所以构建产物会自动带上这个名字，不需要再 docker tag
docker compose build
docker compose push web      # 只推 web！直接 `docker compose push` 会连 mysql/redis 官方镜像一起推，权限不足会报错
```

### 第 4 步：验证推送成功

```powershell
# 方式一：浏览器打开 https://hub.docker.com/r/tykkkkkk/zt-agent/tags 看有没有 v1.0.0
# 方式二：本机删掉镜像再拉回来（最可靠的验证）
docker rmi tykkkkkk/zt-agent:v1.0.0
docker pull tykkkkkk/zt-agent:v1.0.0
```

### 注意事项

1. **镜像里不含密钥**：`.dockerignore` 已排除 `.env` / `*.key` / `*.pem`，密钥不会被打进镜像，
   公开到 Docker Hub 也安全。
2. **但源码会公开**：镜像里包含 `COPY . .` 拷进去的源码。若不想公开源码，把仓库设为 Private
   （但那样别人需要 `docker login` 才能拉，分享成本变高）。
3. **体积**：首次推送可能上传 500MB~1.5GB，取决于 chromadb 等依赖，国内上传较慢，耐心等一次。
   之后同一个基础层不会重复上传，只传发生变化的那几层，通常 1~2 分钟。
4. **多架构**：你构建的是 `linux/amd64` 镜像。Intel/AMD 的电脑可直接用；Apple Silicon（M 系列）Mac
   会自动用 Rosetta/qemu 模拟运行，本项目是纯 Python，一般可正常跑，速度略慢。
5. **拉取额度**：Docker Hub 免费额度为匿名用户 100 次拉取 / 6 小时，登录用户 200 次 / 6 小时，
   分享给少数人完全够用。

---

## 十二、别人怎么用我的镜像

把这一节内容写进 README，对方照着做就能跑起来。推荐 **方式 A**。

### 方式 A：克隆仓库 + 用镜像跑（推荐）

适合"想看源码 + 想跑起来"的人（面试官、同学）。

```powershell
# 1. 拉源码（只需要 agent_db.sql、.env.example 这几个文件）
git clone https://github.com/tykkkkkkan/ZT-agent.git
cd ZT-agent

# 2. 准备环境变量
cp .env.example .env
#    .env.example 默认 RAG_BACKEND=tfidf（零依赖，不用任何 Key 也能跑）
#    想让 AI 对话可用，再填一行 DEEPSEEK_API_KEY=sk-xxx

# 3. 用镜像启动（注意 -f 指定的是 hub 文件，不会触发本地构建）
docker compose -f docker-compose.hub.yml up -d

# 4. 验证
#    浏览器打开 http://localhost:8001/healthz
```

### 方式 B：只要镜像，不要源码

适合只想快速看一眼效果的人 —— 但本项目是多容器（web + MySQL + Redis），
仍需 `docker-compose.hub.yml` 和 `agent_db.sql` 两个文件才能一键起，
所以实际上还是要 `git clone`。纯 `docker run` 单条命令只适用于单容器项目，
本项目不适用，**不要为了炫技而给出跑不通的一行命令**。

### 方式 C：只检查镜像能不能拉

```powershell
docker pull tykkkkkk/zt-agent:latest
docker image inspect tykkkkkk/zt-agent:latest --format "{{.Size}} {{.Architecture}}"
```

### 对方会遇到的三个坑（提前写清楚）

| 现象 | 原因 | 处理 |
|---|---|---|
| 拉取超时 | 国内直连 Docker Hub 慢 | 配镜像加速，或登录 Docker Hub 账号提高额度 |
| `port is already allocated` | 8001 / 3307 / 6379 被占用 | 改 `docker-compose.hub.yml` 里冒号左边的端口 |
| 知识库检索没结果 | `chroma_data/` 不在仓库里，默认走 TF-IDF 无需处理；若切 `RAG_BACKEND=embedding` 则需重建索引 | `docker compose -f docker-compose.hub.yml exec web python manage.py rebuild_rag` |

---

**一句话总结**：
- 自己本地跑：`docker compose up -d --build` → http://localhost:8001/healthz
- 推给别人：`docker login` → `docker compose build` → `docker compose push web` → 对方 `docker compose -f docker-compose.hub.yml up -d`

