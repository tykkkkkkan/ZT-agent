# AGENTS.md — ZT-agent 项目约定

## 项目是什么

渔具电商的智能客服系统：Django 5 后端 + Vue3 前端 + MySQL + DeepSeek + Chroma，双路 RAG 加 ReAct Agent。仓库根目录就是后端工程。

## 启动方式（先看清楚再动手）

- 本地开发：`start.bat` 起 8000 端口的 Django 并自动开浏览器，`stop.bat` 停。
- Docker：`docker compose up -d --build`，Web 映射 **8001**，MySQL 映射 **3307**。
- **本地是 8000，Docker 是 8001，别记混**。
- 数据库是混合 managed 模式：`agent_db.sql` 里的四张基础业务表不由 Django 自动创建，首次部署只跑 `migrate` 是不够的，要先导 `agent_db.sql`。

## 目录

- 后端代码在根目录（`manage.py` 所在层），Vue3 前端在独立的前端目录。
- 本地虚拟环境 `.venv`、前端 `node_modules`、以及 `.env` 都**不要手动改、不要提交、不要打印内容**。

## 干活规矩

- 这是用户的毕业/求职主力项目，改动务必最小化，只碰跟任务相关的那几个文件。
- 改后端代码前先看有没有对应的测试或启动脚本能验证；改完至少让 Django 能正常启动（`python manage.py check`）。
- 需要连 MySQL 时用 `.env` 里的配置，不要硬编码账号密码，也不要打印出来。
- 不要在这台机器上开 Django 6.x：本机 MySQL 8.0.46 低于 Django 6 要求的 8.4，会直接报 NotSupportedError，项目锁 5.2 LTS 是有原因的。
- 同层还有 `marketing-agent` 和 `outputs` 等目录，不是本项目的一部分，不要顺手改。
