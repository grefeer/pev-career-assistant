# Wave 4：多站点适配与通用扩展框架设计

## 0. 文档信息

| 项目 | 内容 |
| --- | --- |
| 日期 | 2026-07-17 |
| 状态 | 设计已校准，待实施 |
| 适用基线 | Wave 3（大疆 Moka）验收通过后 |
| 上游设计 | `2026-07-16-mvp-parallel-delivery-design.md`（第 9 节波次 4）、`2026-07-17-wave3-dji-moka-vertical-slice-design.md` |
| 预估工期 | 2 周（第 8–9 周） |

## 0.1 本次优化结论

Wave 4 的核心不是“再写两个站点适配器”，而是把 Wave 3 的 Moka 纵向能力沉淀成可重复、可治理、可回滚的站点接入流程。本文对原设计做三点校准：

1. **可执行适配器与观察站点分层**：小鹏、讯飞是可执行 `SiteAdapter`；小红书、汇川只做 `ObservationSiteAdapter`，不能注册成可派发任务的执行适配器。
2. **共享控件库先从回归证据提取**：只有在 Moka、Xpeng、iFlytek 至少两个站点都用到且测试语义一致时，才进入 `executor/adapters/common/`，避免过早抽象。
3. **熔断必须后端权威**：本地 adapter registry 可用于执行器选择，但站点状态、错误计数、熔断和版本发布状态必须以 MySQL 中的 `site_adapters` 为准，避免多设备本地状态分叉。

## 1. 背景

Wave 3 完成了第一个真实站点——大疆 Moka 的纵向闭环，建立了 `SiteAdapter` Protocol 和大疆 Moka 的具体实现。Wave 4 的目标是将这套框架扩展为可复用的多站点接入体系，支持并行接入小鹏飞书招聘、科大讯飞 zhiye.com，同时建立标准化的新站点接入流程，使后续站点适配不再依赖"从头设计"。

## 2. 目标与非目标

### 2.1 目标

1. 建立标准化的新站点接入流程：从页面采样到灰度投递的 5 步可复制方法论。
2. 提取共享控件库（`CommonControls`），减少站点间的重复代码。
3. 实现小鹏飞书招聘（Feishu Recruitment）站点适配器。
4. 实现科大讯飞 zhiye.com 站点适配器。
5. 建立页面回归测试框架：使用脱敏页面快照批量验证所有已注册适配器。
6. 实现站点级别的熔断、版本管理和监控。
7. 小红书和汇川技术作为"观察站点"：只收集页面样本和字段识别，不创建投递任务。
8. 建立站点接入准入清单：授权账号、页面采样脱敏、域名 allowlist、隐私边界、真实只读验收和单任务灰度证据缺一不可。

### 2.2 非目标

- 不追求一次编写适配所有招聘站点。每个站点仍需要独立的适配器实现，共享的只是接口和通用控件。
- 不为小红书和汇川技术创建投递能力。它们仅在 Wave 4 中作为数据采集目标。
- 不实现跨站点的字段语义统一映射（这是 Wave 5 各站点都稳定后的优化项）。
- 不破坏 Wave 3 已确定的 `SiteAdapter` Protocol；可新增旁路的 `ObservationSiteAdapter`，但不能让只读观察站点伪装成可执行站点。
- 不把任意招聘域名纳入自动化。每个站点都必须显式注册域名 allowlist、适配器版本和熔断状态。

## 3. 标准站点接入流程

Wave 4 的核心交付不是两个新适配器本身，而是使"添加新站点"变成有章可循的工作。每个新站点走以下 5 步流程：

### 步骤 1：页面采样

| 活动 | 产出 |
|---|---|
| 人工使用授权账号登录招聘网站，导航到投递表单 | 完整表单截图 + URL 模式 |
| 使用 Playwright 录制每个页面的简化 DOM | `regression/fixtures/{site}/page_{n}.html` |
| 人工标注每个字段的：语义类型、HTML 控件类型、选择器、是否必填 | `regression/fixtures/{site}/fields.yaml` |
| 标注页面类型：单页/多页首页/多页中间/多页末页 | 页面拓扑图 |
| 执行脱敏检查：删除表单值、姓名、电话、邮箱、身份证、Cookie、token、完整截图 | `redaction_report.json` |
| 记录授权来源、测试账号范围、禁止动作和灰度负责人 | `site_intake.yaml` |

### 步骤 2：拓扑与控件定义

```python
# executor/adapters/{site}/topology.py

SITE_TOPOLOGY = {
    "pages": [
        {"index": 1, "class": "multi_page_first", "fingerprint": "sha256:abc..."},
        {"index": 2, "class": "multi_page_middle", "fingerprint": "sha256:def..."},
        # ...
        {"index": 6, "class": "multi_page_last", "fingerprint": "sha256:xyz..."},
    ],
    "submit_indicator": ".submit-btn, button:has-text('提交')",
    "save_indicator": ".save-btn, button:has-text('保存')",
    "next_indicator": ".next-btn, button:has-text('下一步')",
}
```

### 步骤 3：模拟验收

- 用步骤 1 采样的脱敏页面快照（静态 HTML）驱动适配器。
- Playwright 加载本地 fixture HTML，不访问真实网站。
- 验证所有字段的填写策略产生正确 `FillResult`。
- 验证安全门规则：末页自动停止、提交按钮不点击、歧义按钮不点击。

```bash
# 每个站点的模拟验收命令
python -m pytest executor/regression/test_regression.py --site=xpeng -v
python -m pytest executor/regression/test_regression.py --site=iflytek -v
```

### 步骤 4：真实站点只读验证

- 使用授权账号完成人工登录。
- 导航到投递表单，Executor 遍历所有页面。
- 识别全部字段，填写非敏感测试数据，回读验证。
- 到达末页后自动停止，不点击提交。
- 字段识别率必须 ≥ 95%。

### 步骤 5：真实投递灰度

- 使用真实 `ApplicationSnapshot` 创建单任务。
- Executor 填写全部非敏感字段。
- 学生本人在审查界面确认所有字段，手动填写敏感字段。
- 学生本人点击提交。
- Executor 观察提交结果并上报。

## 4. 共享控件库

从 Moka 适配器中提取通用控件策略，形成 `executor/adapters/common/` 模块：

```
executor/adapters/common/
├── __init__.py
├── text_input.py        # <input type="text">, <textarea>
├── select.py            # <select>, 自定义下拉
├── date_picker.py       # <input type="date">, 自定义日历
├── rich_text.py         # contenteditable, iframe 编辑器
├── radio_checkbox.py    # <input type="radio/checkbox">
├── file_upload.py       # <input type="file">
├── multi_select.py      # 自定义多选组件
└── wait_utils.py        # 服务器响应等待、文件上传完成轮询
```

每个通用控件函数接受选择器和值，返回 `FillResult`。站点适配器优先使用通用控件，只在站点有特殊行为时覆盖：

提取规则：

1. 只抽取已在至少两个站点通过 fixture 回归的控件策略。
2. 通用控件不得包含站点域名、页面文案、业务字段名或招聘平台特有选择器。
3. 通用控件只处理“控件操作 + 回读”，页面拓扑、字段语义和安全门仍由站点适配器负责。
4. 每个通用控件至少覆盖正常值、空值、特殊字符、回读不一致和超时五类测试。

```python
# executor/adapters/xpeng/adapter.py

from executor.adapters.common.text_input import fill_text_input
from executor.adapters.common.select import fill_select
from executor.adapters.common.file_upload import upload_via_input

class XpengFeishuAdapter:
    # 大部分字段使用通用控件
    def fill_field(self, page, field_key, value):
        selector = self._field_map[field_key]["selector"]
        ctrl_type = self._field_map[field_key]["type"]

        if ctrl_type == "text_input":
            return fill_text_input(page, selector, value)
        elif ctrl_type == "select":
            return fill_select(page, selector, value)
        # ...

    # 飞书特有：部分下拉需要先点击触发远程搜索
    def fill_field(self, page, field_key, value):
        if field_key == "university":
            return self._fill_remote_search_select(page, value)
        return super().fill_field(page, field_key, value)
```

## 5. 站点适配器实现

### 5.1 小鹏飞书招聘（XpengFeishuAdapter）

| 项目 | 内容 |
|---|---|
| adapter_id | `xpeng.feishu` |
| 域名 | `*.feishu.cn` 招聘子域 |
| 表单类型 | 通常为单页长表单或 2-3 页分步表单 |
| 特殊处理 | 飞书 OAuth 登录（人工接管）；远程搜索下拉（学校/专业）；项目经历和实习经历分段独立 |

飞书招聘的登录流程基于飞书 OAuth，进入表单前需要进行组织认证。Wave 4 设计为：

1. Executor 导航到投递 URL。
2. 检测到飞书登录页 → `BlockerInfo("login")` → 通知用户完成登录。
3. 用户在本地浏览器中扫码或输入飞书账号密码完成登录。
4. 用户点击"继续" → Executor 检测到表单页面 → 开始自动填写。

飞书招聘表单的"远程搜索下拉"控件处理：

```python
def _fill_remote_search_select(self, page, keyword):
    # 点击输入框触发下拉
    input_el = page.locator(self._selector)
    input_el.click()
    input_el.fill(keyword)
    # 等待远程搜索结果
    page.wait_for_selector(".search-result-item", timeout=5000)
    # 点击第一个匹配项
    page.locator(".search-result-item").first.click()
    # 回读确认选中的值
    selected = input_el.input_value()
    return FillResult(
        field_key="university",
        strategy="remote_search_select",
        value_written=selected,
        readback_match=keyword in selected,
        readback_value=selected,
        confidence=0.95 if keyword in selected else 0.5,
    )
```

### 5.2 科大讯飞 zhiye.com（IflytekZhiyeAdapter）

| 项目 | 内容 |
|---|---|
| adapter_id | `iflytek.zhiye` |
| 域名 | `*.zhiye.com` |
| 表单类型 | 多页表单，有前端动态字段校验 |
| 特殊处理 | 前端实时表单校验；简历附件上传后需等待服务器处理；部分字段间有联动（学校→专业） |

zhiye.com 多页表单的逐页处理：

1. 每页填写完成后点击"下一步"。
2. 等待页面切换（URL 变化或 DOM 指纹变化）。
3. 校验新页面指纹与拓扑定义匹配。
4. 不匹配 → `BlockerInfo("page_changed")`，任务暂停。

zhiye.com 的前端动态校验：

- 填写每个字段后等待 500ms，让前端验证逻辑完成。
- 检测校验错误提示（如红色文字、错误图标）。
- 有校验错误 → 标记该字段 `confidence=0.0`，记录错误信息。
- 继续填写其他字段，不因单字段校验失败而阻塞流程。
- 最终审查界面汇总所有校验失败字段。

附件上传等待服务器处理：

```python
def upload_attachment(self, page, field_key, file_path):
    result = upload_via_input(page, field_key, file_path)
    if result.success:
        # zhiye.com 需要等待文件名出现在上传列表中
        try:
            page.wait_for_selector(
                f".upload-file-item:has-text('{os.path.basename(file_path)}')",
                timeout=15000,
            )
        except TimeoutError:
            result.success = False
            result.server_response_indicator = "upload_timeout"
    return result
```

## 6. 回归测试框架

### 6.1 页面快照 fixturing

```python
# executor/regression/test_regression.py

import pytest
from executor.adapters.registry import ADAPTER_REGISTRY

@pytest.mark.parametrize("adapter_id", list(ADAPTER_REGISTRY.keys()))
def test_adapter_page_classification(adapter_id):
    """每个适配器必须正确分类其所有注册的页面快照。"""
    adapter = ADAPTER_REGISTRY[adapter_id]
    for fixture in _load_page_fixtures(adapter_id):
        page = _load_fixture_page(fixture)
        fp = adapter.fingerprint_page(page)
        page_class = adapter.classify_topology(fp)
        assert page_class == fixture["expected_class"], (
            f"{adapter_id} page {fixture['index']}: "
            f"expected {fixture['expected_class']}, got {page_class}"
        )

@pytest.mark.parametrize("adapter_id", list(ADAPTER_REGISTRY.keys()))
def test_adapter_field_filling(adapter_id):
    """每个适配器的所有字段必须产生匹配的回读结果。"""
    adapter = ADAPTER_REGISTRY[adapter_id]
    for fixture in _load_field_fixtures(adapter_id):
        page = _load_fixture_page(fixture["page"])
        result = adapter.fill_field(page, fixture["field_key"], fixture["value"])
        assert result.readback_match, (
            f"{adapter_id} field {fixture['field_key']}: "
            f"readback mismatch: {result.readback_value}"
        )

@pytest.mark.parametrize("adapter_id", list(ADAPTER_REGISTRY.keys()))
def test_adapter_submit_blocked(adapter_id):
    """末页的提交按钮必须被安全门阻止。"""
    adapter = ADAPTER_REGISTRY[adapter_id]
    page = _load_last_page_fixture(adapter_id)
    fp = adapter.fingerprint_page(page)
    page_class = adapter.classify_topology(fp)
    assert page_class in ("multi_page_last", "single_page"), (
        f"Expected last page, got {page_class}"
    )
    # 验证 submit 按钮存在
    assert fp.has_submit_button, "Last page must have a submit button"
    # 安全门应阻止。当前 executor.safety.decide_action 返回 SafetyDecision。
    from executor.safety import PageTopology, decide_action
    topology = (
        PageTopology.MULTI_STEP_FINAL
        if page_class == "multi_page_last"
        else PageTopology.SINGLE_PAGE
    )
    decision = decide_action(
        topology=topology,
        label="提交",
        action_kind="submit",
        is_bottom_action=True,
        has_verified_next_step=False,
    )
    assert not decision.allowed
    assert decision.reason_code in {
        "final_action_forbidden",
        "single_page_bottom_action",
    }
```

### 6.2 回归测试矩阵

| 测试 | Moka | Xpeng | iFlytek | 说明 |
|---|---|---|---|---|
| 页面分类 | ✓ | ✓ | ✓ | 所有页面类型正确识别 |
| 字段填写+回读 | ✓ | ✓ | ✓ | 所有控件类型正确填写 |
| 重复经历 | ✓ | - | ✓ | Moka 和 iFlytek 有重复段 |
| 附件上传 | ✓ | ✓ | ✓ | 简历/作品集上传 |
| 末页停止 | ✓ | ✓ | ✓ | 安全门阻止提交 |
| 歧义按钮 | ✓ | ✓ | ✓ | 安全门阻止歧义按钮 |
| 恢复 | ✓ | ✓ | ✓ | 检查点保存和恢复 |
| 登录门 | ✓ | ✓ | ✓ | 检测登录/验证码/风控 |

## 7. 熔断与版本管理

### 7.1 适配器注册表

```python
# executor/adapters/registry.py

from dataclasses import dataclass, field
from executor.adapters.base import SiteAdapter

@dataclass
class AdapterRegistryEntry:
    adapter: SiteAdapter
    status: str          # "active" | "circuit_breaker_open" | "deprecated"
    error_count: int = 0
    last_error: str | None = None
    last_error_at: float | None = None

ADAPTER_REGISTRY: dict[str, AdapterRegistryEntry] = {}

CIRCUIT_BREAKER_THRESHOLD = 5

def register(adapter: SiteAdapter) -> None:
    ADAPTER_REGISTRY[adapter.adapter_id] = AdapterRegistryEntry(
        adapter=adapter,
        status="active",
    )

def record_error(adapter_id: str, error_detail: str) -> None:
    entry = ADAPTER_REGISTRY.get(adapter_id)
    if entry is None:
        return
    entry.error_count += 1
    entry.last_error = error_detail
    entry.last_error_at = time.time()
    if entry.error_count >= CIRCUIT_BREAKER_THRESHOLD:
        entry.status = "circuit_breaker_open"

def record_success(adapter_id: str) -> None:
    entry = ADAPTER_REGISTRY.get(adapter_id)
    if entry is None:
        return
    entry.error_count = max(0, entry.error_count - 1)
    if entry.status == "circuit_breaker_open" and entry.error_count < 2:
        entry.status = "active"
```

执行器本地注册表只用于快速选择实现类；是否允许派发必须以后端下发的 `AdapterRef` 为准。后端在以下边界重新读取 MySQL 中的 `site_adapters`：

1. 创建 `ApplicationTask` 时：校验职位 `apply_url` 命中 active adapter 的域名 allowlist。
2. 派发任务时：冻结 `adapter_id`、`adapter_version` 和 `adapter_status_at_dispatch`。
3. Executor 拉取 payload 时：若适配器已熔断或版本被禁用，返回 `executor_payload_unavailable`。
4. Executor 上报失败时：按稳定错误码累计站点错误，达到阈值后后端熔断。

### 7.2 版本策略

- 适配器版本使用 semver（`MAJOR.MINOR.PATCH`）。
- MAJOR：页面拓扑结构不兼容变更（旧指纹全部失效）。
- MINOR：新增页面、新增字段、新增控件类型支持。
- PATCH：选择器修复、回读逻辑优化、错误处理改进。
- 核心规则：检查点中保存的 `adapter_version` 与当前注册的 `MAJOR` 版本不一致时，拒绝恢复任务，避免用新版适配器处理旧版页面的不一致状态。

## 8. 观察站点

小红书和汇川技术作为 Wave 4 的"观察站点"：

- 收集页面样本和字段标注（步骤 1）。
- 实现独立的 `ObservationSiteAdapter`，只包含 `fingerprint_page`、`classify_topology`、`detect_blocker` 和 `extract_field_candidates`。
- 不实现 `fill_field`、`handle_repeat_section` 和 `upload_attachment`，也不进入可执行 `ADAPTER_REGISTRY`。
- 后端不为此类站点的快照创建投递任务。
- 目的：积累页面样本和学习真实站点的控件多样性，为 Wave 5+ 的通用字段语义映射提供数据。

```python
class ObservationSiteAdapter(Protocol):
    adapter_id: str
    supported_domains: list[str]
    version: str

    def fingerprint_page(self, page: Page) -> PageFingerprint: ...
    def classify_topology(self, fp: PageFingerprint) -> str: ...
    def detect_blocker(self, page: Page) -> BlockerInfo | None: ...
    def extract_field_candidates(self, page: Page) -> list[dict[str, str]]: ...
```

观察站点的产物只能进入 `observed_sites` 和脱敏 fixture，不得生成 `AdapterRef`，不得创建 `ApplicationTask`。

## 9. 安全与隐私

1. 所有 Wave 3 的安全边界继续适用：不回传 Cookie/密码/验证码/DOM/截图/表单值。
2. 不同站点使用独立的 Playwright user data directory，隔离登录态。
3. 站点适配器代码中不得硬编码任何 URL、选择器以外的敏感信息。
4. 每个站点适配器的诊断输出独立存储，不同站点的诊断信息不混合。

## 10. 错误处理

| 场景 | 处理 |
|---|---|
| 站点页面结构变更（指纹不匹配） | BlockerInfo → READY_FOR_REVIEW，上报"页面变更" |
| 单个站点连续失败 5 次 | 熔断该站点适配器，不影响其他站点 |
| 后端已熔断但本地执行器仍有旧 registry | 拉取 payload 返回 409，不下发目标 URL |
| 飞书 OAuth 登录过期 | 通知用户重新登录，保留当前页检查点 |
| zhiye.com 前端校验阻止翻页 | 标记校验字段，汇总上报，学生手动修正 |
| 远程搜索下拉（学校）无结果 | 填入原始文本，标记低置信度，学生审查 |
| 附件上传服务器无响应 | 超时后标记失败，学生在审查界面手动上传 |

## 11. 数据库变更

### 11.1 Migration `202607xx_0010`：多站点扩展

```sql
-- 站点适配器注册表已有（Wave 3 的 0009），无需新表。
-- 新增字段：

ALTER TABLE application_tasks
    ADD COLUMN site_adapter_error_count INT NOT NULL DEFAULT 0,
    ADD COLUMN site_adapter_last_error VARCHAR(256) NULL;

ALTER TABLE site_adapters
    ADD COLUMN rollout_stage VARCHAR(24) NOT NULL DEFAULT 'readonly',
    ADD COLUMN last_success_at DATETIME NULL,
    ADD COLUMN last_readonly_verified_at DATETIME NULL;

-- 观察站点管理
CREATE TABLE observed_sites (
    id VARCHAR(36) PRIMARY KEY,
    site_code VARCHAR(32) NOT NULL UNIQUE,
    display_name VARCHAR(128) NOT NULL,
    domains JSON NOT NULL,
    page_samples_count INT NOT NULL DEFAULT 0,
    status VARCHAR(16) NOT NULL DEFAULT 'observing',  -- observing | ready | active
    adapter_kind VARCHAR(16) NOT NULL DEFAULT 'observation',
    created_at DATETIME NOT NULL DEFAULT (UTC_TIMESTAMP()),
    updated_at DATETIME NOT NULL DEFAULT (UTC_TIMESTAMP()) ON UPDATE UTC_TIMESTAMP()
);
```

## 12. 测试策略

### 12.1 门禁清单

| 测试类型 | 通过标准 |
|---|---|
| 通用控件单元测试 | 每种控件至少 3 个场景（正常、空值、特殊字符） |
| 站点回归测试 | 所有已注册适配器的页面分类 + 字段填写 + 安全门 |
| 恢复测试 | 每个站点 3 种故障模式（网络中断、进程崩溃、页面变更） |
| 熔断测试 | 连续 5 次失败触发熔断，成功 2 次后恢复 |
| payload 禁发测试 | adapter 熔断、版本禁用、域名不匹配时不返回目标 URL |
| 真实只读 | 小鹏 + 讯飞各至少一次，字段识别率 ≥ 95%，0 自动提交 |
| 后端回归 | `pytest tests/unit/ tests/contract/ tests/security/` 全绿 |
| Ruff | 所有新增 `executor/adapters/` 代码无违规 |

### 12.2 站点验收矩阵

| 验收项 | Moka | Xpeng | iFlytek | 小红书 | 汇川 |
|---|---|---|---|---|---|
| 页面采样完成 | ✓ | ✓ | ✓ | ✓ | ✓ |
| 适配器实现 | ✓ | ✓ | ✓ | 只读 | 只读 |
| 模拟回归通过 | ✓ | ✓ | ✓ | 指纹+分类 | 指纹+分类 |
| 真实只读 0 提交 | ✓ | ✓ | ✓ | - | - |
| 单任务灰度投递 | ✓ | ✓ | ✓ | - | - |
| 5 任务批量投递 | ✓ | ✓ | - | - | - |

## 13. 完成定义

Wave 4 在以下条件全部满足时完成：

1. 标准化站点接入流程文档化并可执行。
2. 共享控件库覆盖 Moka/Xpeng/iFlytek 已验证且跨站复用的控件类型；站点特有控件保留在站点适配器内。
3. XpengFeishuAdapter 和 IflytekZhiyeAdapter 通过模拟回归 + 真实只读验证。
4. 小鹏和讯飞各完成至少 1 次单任务投递灰度（0 自动提交）。
5. 回归测试框架覆盖所有已注册站点适配器。
6. 熔断机制由后端权威状态驱动，正确触发、阻止 payload 下发并可人工恢复。
7. 小红书和汇川技术页面样本收集完成，并保持 observation-only，不可创建投递任务。
8. 全量默认回归保持全绿。

## 14. 实施计划输出

本设计批准后生成一份实施计划：
- `2026-07-17-wave4-multi-site-adaptation-plan.md`
