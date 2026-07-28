# 通用招聘发现 Supervisor：Skill 方法迁移设计

## 目标与边界

将 `skill/job-discovery` 中可复用的发现策略迁移为后端的程序化
Supervisor 执行能力。新 URL 的评测和生产默认路径不得加载、匹配或回退至
`adapters/`；Adapter 仅保留为历史站点的显式性能插件和诊断对照。

本设计不绕过登录、验证码、反爬、权限或付费墙；遇到这些情形必须产生可审计的
`needs_manual_review`。

## 方案

新增 `generic_supervisor/` 服务层，由 Worker 在无显式 Adapter 允许时调用。它是
确定性状态机，LLM 仅处理每页 JD 的结构化抽取，不拥有分页、重试、完成判定或
持久化决策权。

```text
URL -> Classify -> Plan -> Browse pages -> Page extraction fan-out
    -> deterministic normalize/deduplicate -> PEV verify -> persist
```

### 1. 分类与计划

`classify_url` 基于 URL、轻量公开响应、首屏渲染信号，输出不可变
`DiscoveryPlan`：

- 微信、静态详情页、静态列表、URL 型分页、卡片型 SPA、阻塞墙；
- 默认顺序为 `parallel_fetch`，仅在薄 SPA 壳时进行一次 `search_interact`；
- 每个计划包含最大页面数、最大并发、最多一次重试和禁止 Adapter 的标志；
- 计划与每个状态转换记录至 trajectory，供审计和失败诊断。

### 2. 通用浏览器执行器

`parallel_fetch` 通过公开页面的“下一页/上一页”导航 URL 差异推断分页参数，预计算
页面 URL，并使用有限浏览器池并发渲染。未能推断时只允许受预算限制的顺序点击。
执行器写出每页独立 `PageEvidence`，以 URL 和内容哈希去重；它不读取私有网络
payload，也不依赖公司/域名选择器。

### 3. 按页抽取和合并

每个页面生成一个受限抽取任务。抽取可调用结构化 LLM，但输入只包含该页证据，输出
必须符合 `NormalizedJobCandidate`。结果先持久化为页级缓存，再由确定性代码完成
规范化、正文优先去重、证据关联、质量校验和 `CoverageReport` 计算。并发只作用于
独立页面，数据库写入仍由 Worker 单一提交。

### 4. 缓存、恢复与终止

缓存键为页面 `content_hash`。已有成功抽取结果的相同哈希不再调用 LLM；网页内容变化
才重新抽取。状态机有明确的最大页面、最大并发、最大浏览尝试和 wall-clock 预算。
没有完整终止证据、详情正文缺失或存在未处理页面时，PEV 不通过；遇墙则人工复核。

### 5. Worker 与开关

新增显式 `job_discovery_generic_supervisor_enabled` 与评测专用
`job_discovery_disallow_adapters`。后者为真时：任何已匹配策略、Adapter 导入或
`path_a_adapter` 结果均为配置错误，不能静默退回 Adapter。原有 Adapter 路径和灰度
开关保持兼容，默认行为不变。

## 验收与测试

1. 单元测试：分类、一次重试、分页 URL 推断、并发去重、content-hash 缓存、墙识别、
   coverage 失败条件。
2. Worker 集成测试：`disallow_adapters=true` 时 Adapter 不会被导入或执行；输出标记为
   `path_generic_supervisor`。
3. Fixture 测试：静态、URL 分页、卡片 SPA、阻塞页；每页独立抽取和合并结果可复现。
4. Live 盲测：提供的 10 个新 URL 全部强制通用 Supervisor；评测报告同时给出
   Adapter 命中次数（必须为 0）、岗位/正文/唯一数、覆盖结论、时间、阻塞原因。

## 非目标

- 不将站点私有 API、固定 CSS 选择器或企业项目 ID 写入通用 Supervisor。
- 不以标题替代缺失 JD 正文，不伪造完整性。
- 不删除现有 Adapter，也不以历史 10 URL 的成绩作为泛化通过证据。
