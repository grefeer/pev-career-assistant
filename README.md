# LangGraph Multi-Agent Career Assistant

一个围绕真实求职任务构建的多智能体 AI 应用项目。  
项目以 `LangGraph` 为工作流编排核心，围绕岗位分析、简历匹配、低分回路优化和最终求职建议生成构建可并行、可回路、可恢复的状态图系统，并进一步升级为 `Vue + FastAPI + Docker Compose` 的完整前后端分离版本。

![Graph Overview](./docs/graph-overview.svg)

## 项目简介

这个项目不是简单聊天机器人，也不是普通问答 Demo，而是一个更贴近真实业务的小型智能系统：

- 用户可以注册 / 登录并拥有自己的分析空间
- 用户可以创建多个分析会话并基于 `thread_id` 持续追踪状态
- 用户可以从已核验职位、已确认档案版本出发生成证据化匹配报告
- 用户可以审查定制简历草稿、批准附件并创建不可变投递快照
- 管理员可以同步、补全、审核和核验真实职位来源

## 核心亮点

- 真实场景驱动：围绕“找第一段实习”设计业务流程，而不是泛化聊天
- 证据化匹配：Web 路径基于 MySQL 中已核验职位和已确认档案版本
- 多智能体协作：CLI 演示仍保留 `Supervisor`、`Job Analyst`、`Resume Reviewer`、`Resume Optimizer`、`Career Coach`
- `LangGraph` 核心能力完整落地：`Command`、`Send`、子图、状态聚合、低分回路、checkpoint
- 多模型分工：支持按角色分配不同模型，兼顾效果与成本
- `Redis 8 checkpoint`：支持自定义 `thread_id`、跨实例会话恢复和历史快照读取
- MySQL 权威数据：账户、会话、设备、投递任务和审计记录均以 MySQL 为准
- 客户端加密对象存储：简历等对象在写入 S3/MinIO 前使用 AES-GCM 加密
- 前后端分离：`FastAPI` 后端 + `Vue 3 + Vite` 前端
- Docker 部署：支持通过 `docker compose up --build` 一键启动

## 技术栈

- Python 3.13
- LangGraph
- DeepSeek API（OpenAI-compatible）
- langchain-openai
- FastAPI
- Vue 3
- Vite
- MySQL（权威数据）
- Redis 8（LangGraph checkpoint；SQLite 仅用于开发）
- S3/MinIO（客户端加密对象存储）
- Docker Compose
- Nginx
- pypdf

## 系统流程

### 主流程

1. `Supervisor` 根据当前状态决定下一步动作
2. 使用 `Send` 并行分发岗位到岗位分析子图
3. 使用 `Send` 并行分发岗位到匹配子图
4. 如果当前轮整体匹配偏低，则进入简历优化回路
5. `Career Coach` 生成最终报告与推荐岗位

### 岗位分析子图

- `extract_requirements_node`
- `position_job_node`

### 匹配子图

- `score_match_node`
- `finalize_match_node`

### 低分回路触发条件

- 当前轮没有高匹配岗位
- 当前轮平均分低于阈值
- 当前优化轮次未超过上限

## 项目结构

```text
.
├── README.md
├── requirements.txt
├── docker-compose.yml
├── .env.example
├── backend
│   ├── Dockerfile
│   └── app
│       ├── __init__.py
│       ├── main.py
│       └── schemas.py
├── frontend
│   ├── Dockerfile
│   ├── index.html
│   ├── nginx.conf
│   ├── package.json
│   ├── package-lock.json
│   ├── tsconfig.json
│   ├── vite.config.ts
│   └── src
│       ├── api.ts
│       ├── App.vue
│       ├── main.ts
│       ├── styles.css
│       └── types.ts
├── docs
│   └── graph-overview.svg
└── src
    ├── __init__.py
    ├── agents.py
    ├── auth_service.py
    ├── graph.py
    ├── main.py
    ├── models.py
    ├── prompts.py
    ├── resume_parser.py
    ├── session_service.py
    ├── utils.py
    └── cli
        └── data
            ├── jobs.json
            ├── sample_resume.md
            └── sample_resume.pdf
```

## 后端能力（FastAPI）

主要接口：

- `POST /api/auth/register`
- `POST /api/auth/login`
- `GET /api/auth/me`
- `POST /api/admin/job-sources/{source_key}/sync`（管理员只读同步腾讯智能表来源）
- `GET /api/jobs`（认证用户分页查询已核验 `verified` 职位）
- `GET /api/jobs/{job_id}`（认证用户查询已核验 `verified` 职位详情）
- `GET /api/admin/jobs/review-queue`（管理员查询待补全、待审核和已拒绝职位）
- `PATCH /api/admin/jobs/{job_id}/completion`（管理员保存补全草稿）
- `POST /api/admin/jobs/{job_id}/decision`（管理员核验、拒绝或失效职位）
- `GET /api/sessions`
- `POST /api/sessions`
- `POST /api/sessions/{thread_id}/activate`
- `GET /api/sessions/{thread_id}`
- `GET /api/sessions/{thread_id}/history`
- `POST /api/matches`
- `POST /api/resume-drafts`
- `POST /api/resume-drafts/{draft_id}/approve`
- `POST /api/application-snapshots`
- `GET /api/health`

后端职责：

- 用户认证与 MySQL 账户管理
- 会话与 `thread_id` 管理
- 基于已核验职位和已确认档案版本生成证据化匹配报告
- 读取 Redis 8 checkpoint 当前状态与历史快照（开发环境可选 SQLite）
- 处理简历资产、档案确认、定制草稿、批准附件和投递快照

## 前端能力（Vue）

前端工作台支持：

- 注册 / 登录
- 查看并切换历史会话
- 新建分析会话
- 查看职位、档案、匹配、定制简历和投递快照
- 上传简历资产并确认档案事实
- 基于已核验职位启动证据化匹配
- 审查定制简历草稿并创建投递快照
- 查看当前会话摘要
- 查看历史快照
- 查看岗位分析结果和当前轮匹配结果

前端运行截图：

![Frontend Screenshot](./docs/image.png)

## 本地开发

推荐通过 Docker Compose 启动完整平台，避免宿主进程误用 `.env` 中的容器 DNS 名称（`mysql`、`redis`、`minio`）。

### 1. 生成环境变量

按[平台基础运行手册](./docs/runbooks/platform-foundation.md#生成和设置环境变量)生成并设置 MySQL、Redis、MinIO、认证和对象加密变量。不要在命令参数、日志或版本库中保存密码。

### 2. 启动完整平台

```powershell
docker compose up --build -d
docker compose ps
```

访问：

- 前端：`http://127.0.0.1:5173`
- 后端存活检查：`http://127.0.0.1:8000/api/health/live`
- 后端就绪检查：`http://127.0.0.1:8000/api/health/ready`

## Docker 部署

在项目根目录执行：

```bash
docker compose up --build
```

后台运行：

```bash
docker compose up --build -d
```

访问：

- 前端：`http://localhost:5173`
- 后端：`http://localhost:8000`
- 存活检查：`http://localhost:8000/api/health/live`
- 就绪检查：`http://localhost:8000/api/health/ready`

Compose 的 MySQL、Redis、MinIO、后端和前端宿主端口均可配置。完整的环境变量生成、迁移、恢复、备份与密钥轮换步骤见[平台基础运行手册](./docs/runbooks/platform-foundation.md)。

## 运行时数据说明

这些文件通常不建议提交：

- `.env`
- `checkpoints/`
- 旧版本本地账户文件（仅遗留；不会迁移到 MySQL）
- `frontend/node_modules/`
- `frontend/dist/`

当前 `.gitignore` 已忽略这些本地运行数据。

## 适合在简历和面试中展示的点

- 用 `LangGraph` 把真实业务流程而不是简单问答做成状态图
- 使用 `Command`、`Send`、子图、低分回路和 Redis 8 checkpoint
- 使用 `FastAPI` 封装工作流与会话管理接口
- 使用 `Vue` 构建独立工作台，完成前后端联调
- 使用 Docker Compose 做完整部署验证

## 设计与实施文档

- [校园招聘职业助手总体设计](./docs/superpowers/specs/2026-07-14-campus-recruitment-career-assistant-design.md)
- [平台基础权威数据实施计划](./docs/superpowers/plans/2026-07-14-platform-foundation-authoritative-data.md)
- [真实职位同步纵向闭环设计](./docs/superpowers/specs/2026-07-15-real-job-sync-vertical-slice-design.md)
- [真实职位同步纵向闭环实施计划](./docs/superpowers/plans/2026-07-15-real-job-sync-vertical-slice.md)
- [平台基础运行手册](./docs/runbooks/platform-foundation.md)
