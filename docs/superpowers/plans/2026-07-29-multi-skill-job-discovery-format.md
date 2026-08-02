# 计划：将 4 个 skill 转为 job-discovery skill 格式

## 背景 / 纠正

此前 4 个 skill 被建成后端三层服务（API→Service→Repository），**缺少 job-discovery 的 skill 目录格式**。
用户要求：以 `skill/job-discovery/` 为唯一模板，按各自代码与功能重构为 **agent/Runtime 可调用**的 skill。
`skill/company-research/` 不一定对，需按代码核对（不照抄）。

## 目标格式（每个 skill = 4 件套 + 注册 + 可选 Runtime）

1. `skill/<name>/SKILL.md` — YAML frontmatter（`name`/`description`/`compatibility`，**无 `allowed_scripts`**）+ 分发枢纽 body
2. `skill/<name>/scripts/*.py` — 自包含 argparse CLI（stdout 输出；软错误 exit 0；路径沙箱；重依赖懒加载，如 browse.py 懒加载 playwright）
3. `skill/<name>/references/*.md` — 细节文档（渐进式披露）
4. `skill/<name>/evals/evals.json` — 评测用例
5. `skill_spec.py` 中 `SkillSpec(name, allowed_scripts, skill_type)` 加入 `SKILL_REGISTRY`
6. （browse/generate 型）后端 Runtime：`SkillArtifactStore.prepare()` 每任务克隆 + `_script_tool(..., allowed_scripts=spec.allowed_scripts)` 调脚本 + 读 `output/` 组装结果 + blocked→`needs_manual_review` + `publish_evidence`

**运行时机制（已读源码确认）**：`run_skill_script(script, cli_args, stdin)` → `subprocess.run([sys.executable, skill_dir/scripts/<script>.py, *args], cwd=skill_dir)`，返回 stdout 给调用方。新 skill **不需要**重量级 `create_deep_agent`——套用 **company-research 的轻量确定性 Runtime** 先例。

## 现状盘点

| skill | 后端三层 | skill 目录 | SkillSpec 注册 | Runtime | 测试 |
|-------|---------|-----------|---------------|---------|------|
| job-discovery | ✅ | ✅(模板) | ✅ | ✅ SkillDiscoveryRuntime | ✅ |
| company-research | ✅ | ✅(37行SKILL.md+browse.py) | ✅ | ✅ CompanyResearchRuntime | ✅(全) |
| resume-tailoring | ✅ | ❌ | ❌ | ❌ | ✅(全,后端) |
| interview-prep | ✅ | ❌ | ❌ | ❌ | ✅(全,后端) |
| application-tracking | ✅ | ❌ | ❌ | ❌ | ✅(全,后端) |

**关键约束**：resume/interview 深度耦合 MatchReport 流水线（LLM 是事务一步，产出 DB 行）；application-tracking 是纯 CRUD 状态机。
**已确认取舍**：resume/interview = 并行产物（后端不动）；application-tracking = 状态机工具 skill（持久化仍走 MySQL，后端不动）。

## 逐 skill 方案

### Skill 1: company-research — 审计 + 补齐格式对齐（小改）
- 现有 SKILL.md/browse.py/Runtime/SkillSpec 结构正确、功能可用、测试齐全 → **不重写**
- 补齐 job-discovery 格式对齐：新增 `references/`（research-guide.md + schema.md）+ `evals/evals.json`
- 核对 SKILL.md 描述与 `company_research/service.py`/`runtime.py` 实际功能一致（已核对：一致）
- 100% 覆盖已满足；smoke 已存在

### Skill 2: resume-tailoring（Phase 2）— 新建 skill 目录
- `skill/resume-tailoring/SKILL.md`：frontmatter（中文触发短语「简历针对性改写/针对岗位改简历」）+ 分发枢纽（何时改写、渐进披露、脚本表、references）
- `scripts/generate.py`：自包含 LLM 草稿生成。输入 job_snapshot+profile_facts+preferences+match_analysis（经 `--job/--profile/--prefs/--match` 文件或 stdin）；懒加载 `langchain_openai`；从 env 读 `DEEPSEEK_API_KEY`/`OPENAI_API_KEY`；**镜像** `resume_tailoring/generator.py` 的 prompt + `_parse_diffs`/`_coerce_diffs`（注释 `# mirrors backend: resume_tailoring/generator.py`）；写 `output/draft_diffs.json` + stdout JSON 摘要；解析失败写 `status=failed` 到输出并 exit 0（软错误，仿 read_evidence.py）
- `scripts/validate.py`：自包含 diff 校验（镜像 `draft_validators.validate_draft_diffs`：fact_ref 存在性、op 合法性、section 非空）
- `references/`：tailoring-guide.md + schema.md（diff 对象 schema）
- `evals/evals.json`：3 条用例
- `skill_spec.py`：`RESUME_TAILORING_SPEC = SkillSpec("resume-tailoring", frozenset({"generate","validate"}), "deterministic")` 加入 `SKILL_REGISTRY`
- `backend/app/services/resume_tailoring/runtime.py`：`ResumeTailoringRuntime`（镜像 `CompanyResearchRuntime`：clone→`_script_tool` 调 generate.py→读 `output/draft_diffs.json`→组装 `ResumeTailoringResult`→`publish_evidence`）；`schemas.py` 加 `ResumeTailoringResult`
- **后端 `ResumeDraftService` 不动**（仍用进程内 `LLMDraftGenerator`）
- 测试：`tests/unit/test_resume_tailoring_runtime.py`（mocked subprocess，镜像 `test_company_research_runtime.py`）+ `test_skill_spec` 增注册断言 + smoke `tests/integration/test_resume_tailoring_skill_smoke.py`（真 LLM，`RUN_RESUME_TAILORING_SKILL_SMOKE=1` 门控）
- 100% 行覆盖（`__main__` 用 `# pragma: no cover`）

### Skill 3: interview-prep（Phase 3）— 新建 skill 目录
- 同 resume-tailoring 形态：`SKILL.md` + `scripts/generate.py`（5 段 prompt，镜像 `interview_prep/generator.py` 的 `CONTENT_KEYS`+`_parse_content`+`_coerce_content`）+ `references/` + `evals/evals.json`
- `INTERVIEW_PREP_SPEC = SkillSpec("interview-prep", frozenset({"generate"}), "deterministic")`
- `backend/app/services/interview_prep/runtime.py`：`InterviewPrepRuntime`（镜像 CompanyResearchRuntime）；`schemas.py` 加结果类型
- **后端 `InterviewPrepService` 不动**
- 测试 + smoke（`RUN_INTERVIEW_PREP_SKILL_SMOKE=1`）+ 100% 覆盖

### Skill 4: application-tracking（Phase 4）— 状态机工具 skill
- `skill/application-tracking/SKILL.md`：frontmatter（中文触发「投递进度/投递跟踪/校验投递状态」）+ 分发枢纽（agent 何时用：校验状态转移、列允许下一状态、规范化状态）
- `scripts/track.py`：自包含状态机工具（镜像 `domain/application_tracking.py` 的 `ApplicationStatus`+`_TRANSITIONS`+`is_terminal`+`is_valid_transition`+`allowed_transitions`）。子命令：
  - `validate-transition --from X --to Y` → `{"valid": bool, "reason": ...}`
  - `allowed-transitions --from X` → `{"transitions": [...]}`
  - `normalize-status --status X` → `{"status": "saved"|"applied"|...}`
  - `list-statuses` → `{"statuses":[...], "terminals":[...]}`
  - 纯函数，无 DB，无持久化
- `references/`：state-machine.md（完整转移表 + 12 态说明）
- `evals/evals.json`
- `APPLICATION_TRACKING_SPEC = SkillSpec("application-tracking", frozenset({"track"}), "service")`
- **无 Runtime**（工具型，agent 经 `run_skill_script` 直接调 track.py；后端 `ApplicationTrackingService` MySQL CRUD 不动）
- 测试：`tests/unit/test_application_tracking_skill_track.py`（track.py 纯函数 + 子命令）+ `test_skill_spec` 增注册断言 + 经 `_script_tool` 调用 track.py 的集成断言（仿 `test_skill_spec::test_script_tool_accepts_a_custom_allowlist_for_a_parallel_skill`）+ smoke `tests/integration/test_application_tracking_skill_smoke.py`（真 subprocess 跑 track.py 全子命令）
- 100% 覆盖

## 执行顺序（逐 skill 垂直推进，每 skill 100% 覆盖 + smoke + 可落地后提交）

1. **company-research**：补 references/evals（小）→ ruff → 测试 → 提交
2. **resume-tailoring**：skill 目录+脚本+SkillSpec+Runtime+测试+smoke → 100% → 提交
3. **interview-prep**：同上 → 提交
4. **application-tracking**：skill 目录+脚本+SkillSpec+测试+smoke → 100% → 提交

每个 skill 完成后：`ruff check` 新文件 + 相关 `pytest -q` 全绿（既有 3 个预存失败不算回归）+ smoke 单独验证。

## 不变项 / 约束
- 后端三层 service（resume_draft_service / interview_prep.service / application_tracking.service / company_research.service）**全部不动**
- 新 skill 不引入 `create_deep_agent`（轻量确定性 Runtime，company-research 先例）
- 安全门保留：LLM skill 只读生成、永不 auto-submit；application-tracking 状态转移显式人为、无 auto-submit
- 提交信息以 `Co-Authored-By: Claude <noreply@anthropic.com>` 结尾；分支 `feat/multi-skill-expansion`
- 预存 3 个失败（blocked-site :memory: flaky / alibaba SPA adapter 2 个）不算回归，不碰
- `docs/PROJECT_GUIDE.md` 未跟踪，不碰；`scripts/` 既有 ruff F401 不碰

## 风险
- LLM skill 脚本与后端 generator 的 prompt 两份 → drift 风险（normalize.py 已接受此惯例；两份均有测试覆盖）
- smoke 测试需真 LLM/API key（门控，默认跳过）
