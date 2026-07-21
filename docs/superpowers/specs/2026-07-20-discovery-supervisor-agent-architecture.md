# Discovery Supervisor Agent 架构与流程

> 生成日期：2026-07-20 | 基于 `backend/app/services/job_discovery/deepagents_runner.py`

## 1. 结构图

```
┌─────────────────────────────────────────────────────────────────────┐
│                   Discovery Supervisor Agent（主 Agent）              │
│  ┌─────────────┐  ┌──────────────────┐  ┌──────────────────────┐   │
│  │ 🧠 LLM 大脑  │  │ 📋 规划层          │  │ 🧠 记忆               │   │
│  │ DeepSeek    │  │ Plan→Act→Verify  │  │ 任务上下文 + 证据     │   │
│  │ v4-flash    │  │ →Finish 循环     │  │ + 候选 + 预算        │   │
│  └─────────────┘  └──────────────────┘  └──────────────────────┘   │
│                                                                      │
│  ┌──────────────────────── Tools（9个）───────────────────────────┐  │
│  │ ① triage_link         ② run_web_navigation → 委托子Agent       │  │
│  │ ③ parse_wechat_article ④ ocr_images_from_urls  ⭐新增          │  │
│  │ ⑤ run_ocr             ⑥ extract_jd_candidates                  │  │
│  │ ⑦ standardize_from_record_fields  ⑧ verify_evidence            │  │
│  │ ⑨ package_candidates  ⑩ finish_with_manual_review              │  │
│  └──────────────────────────────────────────────────────────────┘  │
│                                                                      │
│  ┌── 子Agent ───────────────────────────────────────────────────┐   │
│  │          🤖 Web Navigation Agent（独立 LLM 循环）              │   │
│  │  open_url | open_rendered_url | extract_rendered_job_evidence │   │
│  │  ocr_images_from_urls ⭐ | read_dom | extract_links           │   │
│  │  click_link | get_visible_text | screenshot | go_back         │   │
│  │  职责：打开页面 → 提取链接 → 点击导航 → 收集 JD 证据           │   │
│  └──────────────────────────────────────────────────────────────┘   │
│                                                                      │
│  ┌── Skills（4个确定性函数，无 LLM）──────────────────────────────┐  │
│  │  🔗 link_triage       → 域名+路径 模式匹配，分类 URL            │  │
│  │  📰 wechat_parser     → HTML解析 + 正则回退 + ReadGZH 兼容     │  │
│  │  🔬 ocr_pipeline      → PaddleOCR + Tesseract + WebP→PNG      │  │
│  │  📊 jd_extraction     → 结构化提取 + 模糊标题 + 非结构化兜底   │  │
│  └──────────────────────────────────────────────────────────────┘   │
└─────────────────────────────────────────────────────────────────────┘
```

**分层关系：** Agent（LLM 自主决策）→ Tool（Agent 可调用的函数）→ Skill（被 Tool 调用的确定性逻辑）。子Agent 本身也是 Agent，有独立的 LLM 循环。

**新增项（⭐）：** `ocr_images_from_urls` 同时挂载在 Supervisor、Web Nav Agent 和 standalone agent 三个位置。

---

## 2. 时序图

```
Worker                Supervisor Agent         Web Nav Agent       ReadGZH/OCR/API       Tools
  │                        │                       │                    │                  │
  │  DiscoveryTaskInput    │                       │                    │                  │
  │  (url, record_fields)  │                       │                    │                  │
  ├───────────────────────>│                       │                    │                  │
  │                        │                       │                    │                  │
  │                        │ ① triage_link(url)    │                    │                  │
  │                        ├─────────────────────────────────────────────────────────────>│
  │                        │<──────────────────────────────────────────────────────────────┤
  │                        │  site_type + action    │                    │                  │
  │                        │                       │                    │                  │
  │                        │ ════════════════════════════════════════════════════          │
  │                        │  PATH A: WeChat 文章                                      │
  │                        │                       │                    │                  │
  │                        │ ② run_web_navigation  │                    │                  │
  │                        ├──────────────────────>│                    │                  │
  │                        │                       │ open_url (ReadGZH) │                  │
  │                        │                       ├───────────────────>│                  │
  │                        │                       │<───────────────────┤ HTML (UTF-8)    │
  │                        │<──────────────────────┤ evidence_pages     │                  │
  │                        │                       │                    │                  │
  │                        │ ③ parse_wechat_article│                    │                  │
  │                        ├─────────────────────────────────────────────────────────────>│
  │                        │<──────────────────────────────────────────────────────────────┤
  │                        │  title, text, image_urls, email        │                  │
  │                        │                       │                    │                  │
  │                        │ ④ ocr_images_from_urls (if images)  │                      │
  │                        ├──────────────────────────────────────>│ 微信 CDN          │
  │                        │<──────────────────────────────────────┤ OCR 文本          │
  │                        │                       │                    │                  │
  │                        │ ⑤ extract_jd_candidates(combined)  │                      │
  │                        ├─────────────────────────────────────────────────────────────>│
  │                        │<──────────────────────────────────────────────────────────────┤
  │                        │  candidates[]                        │                  │
  │                        │                       │                    │                  │
  │                        │ ════════════════════════════════════════════════════          │
  │                        │  PATH B: 官网/招聘站                                      │
  │                        │                       │                    │                  │
  │                        │ ② run_web_navigation  │                    │                  │
  │                        ├──────────────────────>│                    │                  │
  │                        │      ⚡ SPA 捷径：extract_rendered_job_evidence              │
  │                        │                       ├───────────────────>│ 阿里 API         │
  │                        │                       │<───────────────────┤ JSON × N        │
  │                        │<──────────────────────┤ evidence_pages     │                  │
  │                        │                       │                    │                  │
  │                        │ ③ extract × N (per page)                 │                  │
  │                        ├─────────────────────────────────────────────────────────────>│
  │                        │<──────────────────────────────────────────────────────────────┤
  │                        │  candidates[]                        │                  │
  │                        │                       │                    │                  │
  │                        │ ════════════════════════════════════════════════════          │
  │                        │  PATH C: 阻断                                           │
  │                        │                       │                    │                  │
  │                        │ finish_with_manual_review(reason)                        │
  │                        ├─────────────────────────────────────────────────────────────>│
  │                        │<──────────────────────────────────────────────────────────────┤
  │                        │  needs_manual_review                 │                  │
  │                        │                       │                    │                  │
  │                        │ ════════════════════════════════════════════════════          │
  │                        │  VERIFY + PACKAGE                                         │
  │                        │                       │                    │                  │
  │                        │ ⑥ verify_evidence     │                    │                  │
  │                        │   (字段映射: text→text_excerpt,                            │
  │                        │    position_title→title, ...)  │                    │
  │                        ├─────────────────────────────────────────────────────────────>│
  │                        │<──────────────────────────────────────────────────────────────┤
  │                        │  verified_candidates[]               │                  │
  │                        │                       │                    │                  │
  │                        │ ⑦ package_candidates  │                    │                  │
  │                        │   (+idempotency_key, +similarity_group_key)              │
  │                        ├─────────────────────────────────────────────────────────────>│
  │                        │<──────────────────────────────────────────────────────────────┤
  │                        │                       │                    │                  │
  │  DiscoveryRunResult    │                       │                    │                  │
  │  {status, evidence,    │                       │                    │                  │
  │   candidates, summary} │                       │                    │                  │
  │<───────────────────────┤                       │                    │                  │
```

### 三条路径说明

| 路径 | URL 类型 | 流程 | 性能 |
|------|----------|------|------|
| **🅰 WeChat** | `mp.weixin.qq.com` | ReadGZH 代理获取 → 解析正文 → OCR 图片 → 合并提取 JD → 若有邮箱则提取投递说明 | 3-4 分钟（LLM 推理） |
| **🅱 官网** | 招聘站 / 公司主页 | Web Nav Agent 导航 → **SPA 快捷方式**（阿里等已知域名直接 8 秒）→ 逐页提取 JD | 8 秒（SPA）/ 2-3 分钟（通用） |
| **🅲 阻断** | 反爬/验证码/登录 | 立即标记 `needs_manual_review`，不尝试绕过 | 秒级 |

### 关键数据流

```
Tencent 表字段（公司名/职位/地点）
        │
        ▼
  record_fields ──────────────────────┐
        │                             │
        ▼                             │
  source_url ──→ triage ──→ 获取证据 ──→ extract_jd_candidates ──→ verify ──→ package
                   │          ▲              │                        │           │
                   │          │              │ 提取失败时               │           │
                   │          │              └──→ 手动构建 ←── record_fields        │
                   │          │                                               │     │
                   │          └── 证据全空时 ──→ standardize_from_record_fields ──┘
                   │
                   └── 阻断 ──→ finish_with_manual_review
```

### 容错机制

- **编码修复**：`_fix_response_encoding()` 自动检测 UTF-8，修复 ReadGZH 中文乱码
- **字段映射**：`verify_evidence` 容错 30+ LLM 字段别名（`text→text_excerpt`, `position_title→title` 等）
- **输入健壮**：`_safe_parse_json_arg()` 处理 list/dict/str/畸形 4 种 LLM 传参格式
- **停止条件**：最多 12 次工具调用、空证据兜底、重复空提取停止、防止死循环
- **证据截断**：1500 字符/条，防止上下文溢出导致 summarization 中间件 bug
- **浏览器超时**：ReadGZH header-only 时 15 秒超时 Playwright 回退

