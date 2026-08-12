# 多 Agent + 并行 A/B 实验项目优化任务
你现在负责对当前 Agent 项目进行一次**以测试通过率为核心目标的系统级优化**。
目标测试目录：
`tests/question/redesign`
其中包含链式题和其他测试题，当前约 **83 个测试用例**。
---
# 一、最终目标
唯一核心目标：
> **在保证方案具有泛化能力的前提下，尽可能提高 `tests/question/redesign` 的真实测试通过数量。**
本任务：
* 不以最小改动为目标
* 不要求保持现有架构
* 允许大规模重构
* 允许重新设计 Agent workflow
* 允许修改 Prompt、Tool、Context、State、Memory、Reflection、Verifier、Fallback、Retry 等机制
* 允许修改 Agent 数量和职责
* 允许改变现有 Agent Architecture
判断优化是否成功的最终标准不是：
* 架构是否优雅
* 是否符合某种 Agent 最佳实践
* 代码改动多少
* Leader 是否认为合理
而是：
> **真实测试结果是否提高，并且没有通过 hardcode、test gaming 或严重回归换取结果。**
---
# 二、严格禁止
禁止以下行为：
1. 修改测试用例以提高通过率
2. 删除测试
3. skip / xfail 失败测试
4. 修改测试判断标准
5. 针对测试文本硬编码答案
6. 根据测试文件名、case id 等特殊处理测试
7. 将测试答案写入 Prompt
8. 通过识别“当前正在测试”而采取特殊逻辑
9. 为一个 case 增加完全没有泛化能力的 patch
10. 隐藏或忽略 regression
所有优化必须作用于：
> **系统能力，而不是测试答案本身。**
---
# 三、人员配置
创建 6 个独立 Sub-Agent。
---
## Agent 1：Agent Architecture Engineer
负责：
* 整体 Agent 架构
* ReAct
* Plan-and-Execute
* Router
* Workflow
* Planner
* Executor
* Critic
* Verifier
* Multi-Agent
* Agent职责划分
* 控制流
* 子任务拆解
* 动态规划
* Replanning
核心问题：
> 当前失败是否来自 Agent 架构本身？
---
## Agent 2：Tool Engineering Engineer
负责：
* Tool Calling
* Tool Selection
* Tool Routing
* Tool Description
* 参数生成
* 参数校验
* Tool result parsing
* Tool failure
* Retry
* Tool chaining
* Tool execution order

核心问题：

> Agent 是否真正知道什么时候调用什么工具，以及调用之后如何利用工具结果？

---

## Agent 3：Context & Memory Engineer

负责：

* System Prompt
* Context Engineering
* History
* Context compression
* Context reconstruction
* Memory
* Scratchpad
* Retrieval
* 中间结果保存
* 信息过滤
* 长上下文污染
* 长链信息丢失

核心问题：

> 多步任务执行过程中，Agent 是否因为上下文设计错误而逐渐偏离任务？

---

## Agent 4：State & Workflow Engineer

负责：

* State schema
* State update
* 状态转移
* checkpoint
* task progress
* dependency
* completed / pending
* multi-step workflow
* loop
* termination
* resume
* retry
* 幂等性

核心问题：

> Agent 是否真正知道自己已经完成了什么、还缺什么？

---

## Agent 5：Reliability & Verification Engineer

负责：

* Reflection
* Self-check
* Critic
* Verifier
* Completion detection
* Retry
* Fallback
* Error recovery
* Replanning
* 异常分类
* 不确定性判断
* 最终输出校验

核心问题：

> Agent 为什么认为自己成功，但实际上测试判定失败？

优先考虑：

`Deterministic verification > External verification > LLM reflection`

---

## Agent 6：Project Leader

Leader 不负责提出普通的第 6 个方案。

Leader 负责：

* 审核 5 名工程师报告
* 建立根因假设
* 找到方案依赖关系
* 找到方案冲突
* 设计 A/B 实验
* 决定哪些方案进入候选分支
* 根据测试数据淘汰方案
* 选择下一轮 Baseline
* 控制复杂度
* 防止 over-engineering
* 防止 test overfitting

Leader 必须保持：

> Evidence-driven，而不是 Preference-driven。

---

# 四、总体执行模式

执行：

> **Baseline + 3轮并行优化实验**

每轮使用以下闭环：

`Diagnose`

→ `5 Agents 独立提出方案`

→ `Leader 筛选`

→ `生成 2~3 个候选实现`

→ `并行修改`

→ `分别运行测试`

→ `比较结果`

→ `选出 Winner`

→ `Winner 成为下一轮 Baseline`

禁止：

> 连续讨论三轮以后再统一修改。

必须：

> 每轮都真正修改代码并测试。

---

# 五、Phase 0：项目探索

任何修改之前，首先理解当前系统。

检查：

* README
* CLAUDE.md
* docs
* config
* prompts
* agents
* tools
* workflows
* graph
* state
* memory
* tests
* logs
* recent commits

重点追踪：

`tests/question/redesign`

对应的完整执行链：

`Test`

→ `Entry Point`

→ `Agent`

→ `Planner / Workflow`

→ `Context`

→ `Tool`

→ `State`

→ `Verification`

→ `Final Output`

不要只阅读 tests。

必须理解：

> 为什么系统产生当前结果。

---

# 六、Phase 1：建立 Original Baseline

首先运行：

`tests/question/redesign`

记录：

* Total
* PASS
* FAIL
* ERROR
* Success Rate
* Duration
* 每个失败测试
* 错误信息
* 最后失败步骤

格式：

```text
Original Baseline

Total:
PASS:
FAIL:
ERROR:
Success Rate:
Duration:
```

如果测试存在 randomness：

对关键案例重复运行，区分：

* Deterministic Failure
* Flaky Failure

---

# 七、失败测试分类

对所有失败案例建立 Failure Taxonomy。

例如：

* Planning Failure
* Wrong Task Decomposition
* Tool Selection Failure
* Tool Parameter Failure
* Tool Result Misinterpretation
* Context Loss
* Context Pollution
* State Loss
* Wrong State Transition
* Premature Termination
* Hallucinated Completion
* Reflection Failure
* Verification Failure
* Retry Failure
* Fallback Failure
* Parsing Failure
* Prompt Instruction Failure
* Workflow Architecture Failure
* Model Capability Boundary
* Unknown

每个 Cluster 记录：

```text
Failure Cluster:

Symptoms:
Affected Tests:
Execution Trace:
Likely Root Cause:
Evidence:
Confidence:
Potential Fix:
```

优先寻找：

> 一个 Root Cause 可以解释多个 FAIL。

而不是：

> 一个测试对应一个 Patch。

---

# 八、Round 1：寻找主要系统瓶颈

5 位工程师分别独立分析。

为了防止 Anchoring：

> 每位 Engineer 在完成自己的初始报告之前，不读取其他 Engineer 的最终建议。

每人输出：

---

## 1. Observations

列出最多 5 个关键问题。

必须包含：

* 问题
* 相关代码
* 相关失败测试
* runtime evidence
* 为什么认为它是 root cause

---

## 2. Root Cause Hypothesis

使用可证伪形式：

```text
Hypothesis:

如果 X 是主要根因，
那么修改 Y 后，
Z 类失败测试应该明显改善。

如果 Z 没有改善，则该假设可信度降低。
```

---

## 3. Proposed Solution

明确：

* 修改哪些模块
* 新增什么机制
* 删除什么机制
* 为什么修改
* 数据流如何变化

不得只写：

> “优化 Prompt”

必须说明具体优化机制。

---

## 4. Expected Impact

预测：

```text
Expected Fixed Tests:
Expected Regression Risk:
Expected Token Change:
Expected Latency Change:
Expected Complexity Change:
Confidence:
```

---

## 5. Validation

必须说明：

> 什么实验结果可以证明该方案有效。

---

# 九、Leader：候选方案筛选

Leader 阅读五份报告。

首先建立：

| Candidate | Root Cause | Evidence | Expected Gain | Risk | Complexity | Confidence |
| --------- | ---------- | -------- | ------------: | ---- | ---------- | ---------- |

然后选择：

> **最多 3 个最值得实际验证的候选方案。**

建议默认：

* Candidate A：风险较低、证据最充分
* Candidate B：不同 Root Cause / Architecture
* Candidate C：高风险高收益方案

Candidate 之间应该尽量具有**实验区分度**。

避免三个 Candidate 实际只是同一个方案的不同 Prompt wording。

---

# 十、Git Worktree 并行实验

如果当前项目是 Git Repository：

为候选方案创建独立工作树。

例如：

```text
main baseline
│
├── experiment-round1-a
├── experiment-round1-b
└── experiment-round1-c
```

优先使用：

`git worktree`

保证候选实现彼此隔离。

禁止多个 Sub-Agent 同时修改同一个工作目录。

如果环境无法使用 Git Worktree：

使用独立 branch 或独立目录复制实现同样的隔离效果。

---

# 十一、Candidate 实施原则

Candidate A/B/C 分别独立实现。

每个 Candidate 必须严格按照自己的实验假设修改。

禁止：

> A/B/C 最终全部偷偷加入所有优化。

否则实验失去意义。

每个 Candidate 记录：

```text
Candidate:
Hypothesis:
Changed Files:
Changed Architecture:
Expected Benefit:
```

---

# 十二、分层测试策略

不要每改一行代码就直接跑全部 83 个测试。

每个 Candidate 使用：

## Level 1：Targeted Tests

运行该方案理论上应该修复的 failure cluster。

例如：

```text
5~15 relevant tests
```

如果完全无改善：

可以提前判定方案失败。

---

## Level 2：Full Redesign Tests

如果 Targeted Tests 有改善：

运行完整：

`tests/question/redesign`

---

## Level 3：Regression Tests

如果项目还存在其他核心测试：

运行必要 regression tests。

避免：

> redesign 从 60→70，但整个项目大量功能损坏。

---

# 十三、Round 1 Leader Evaluation

得到：

```text
Original Baseline: X / 83

Candidate A: A / 83
Candidate B: B / 83
Candidate C: C / 83
```

同时记录：

| Candidate | PASS | New PASS | New FAIL | ERROR | Duration | Complexity |
| --------- | ---: | -------: | -------: | ----: | -------: | ---------- |

Leader 根据：

## 第一优先级

`PASS`

## 第二优先级

`Regression`

## 第三优先级

`Stability`

## 第四优先级

`Complexity`

选择 Round Winner。

---

# 十四、复杂度惩罚

如果：

```text
Candidate A = 69/83
Candidate B = 69/83
```

A 修改：

* 3 个模块
* +200 LOC

B 修改：

* 8 个模块
* +1500 LOC
* 新增多个 Agent
* 新增多个状态

优先选择 A。

除非 B 明显改善：

* stability
* generalization
* architecture correctness
* 下一阶段扩展能力

---

# 十五、结果差异很小时的判断

如果：

```text
A = 69
B = 70
```

不要自动认为 B 必胜。

需要分析：

* 是否存在 randomness
* B 的 +1 是否稳定
* 是否产生新的 FAIL
* 是否增加明显复杂度

必要时重复运行相关测试。

---

# 十六、方案组合实验

如果：

```text
A 修复 Tool Failure
B 修复 Context Failure
```

而且二者：

* 修改模块相对独立
* 没有明显冲突

Leader 可以创建：

`Candidate AB`

然后测试：

```text
A:
B:
AB:
```

检查：

> 两个优化是否 additive。

注意：

不能默认：

`A有效 + B有效 = AB更有效`

Agent 系统中可能产生 interaction effect。

必须实测。

---

# 十七、Round Winner

Round 1 最终选择表现最好的实现。

Winner 成为：

> **Round 2 Baseline**

其他实验分支保留结果，但不进入下一轮主线。

记录：

```text
Round 1 Baseline:
Round 1 Winner:
Net Gain:
Fixed Tests:
Regressed Tests:
Confirmed Root Causes:
Rejected Hypotheses:
```

---

# 十八、Round 2：基于实验结果重新诊断

Round 2 不得重复 Round 1 的讨论。

所有 Engineer 必须读取：

* Original Baseline
* Round 1 proposals
* Round 1 implementation
* A/B/C result
* Winner
* Remaining FAIL
* New execution traces

核心问题：

> 为什么剩余测试仍然失败？

注意寻找：

* 第一轮修复后暴露的新瓶颈
* secondary root causes
* interaction failures
* architecture ceiling
* tool/context/state coupling
* verifier blind spot

然后再次：

`5 Agents`

→ `Leader`

→ `2~3 Candidates`

→ `Worktrees`

→ `Targeted Test`

→ `Full Test`

→ `Winner`

得到：

```text
Round 2 Winner = Y / 83
```

---

# 十九、Round 3：攻击剩余 Failure Cluster

Round 3 使用 Round 2 Winner 作为 Baseline。

此时优先处理：

## 优先级 1

可以一次解决多个 FAIL 的 shared root cause。

## 优先级 2

链式题中的系统性失败。

## 优先级 3

State / Context / Tool / Completion 的共性问题。

## 优先级 4

高概率修复的问题。

## 优先级 5

单独 edge case。

---

# 二十、Round 3 允许激进架构实验

如果前两轮结果说明当前架构达到瓶颈，可以创建更激进 Candidate，例如：

### Candidate A

现有架构增量优化。

### Candidate B

`Planner → Executor → Deterministic Verifier`

### Candidate C

`Planner → Executor → Verifier → Conditional Replanner`

或者：

* hierarchical state
* structured scratchpad
* context reconstruction
* failure-aware routing
* verification-driven retry
* targeted reflection
* dynamic replanning
* tool result normalization
* task completion contract

但任何新机制都必须回答：

> **它解决的是哪个已经观察到的 failure cluster？**

禁止因为：

> “这是 Agent 最佳实践”

而加入。

---

# 二十一、六大核心维度

三轮过程中必须持续检查：

## 1. Architecture

* Agent 数量
* Agent职责
* workflow
* planner
* executor
* router
* verifier
* replanner

---

## 2. Tool Calling

* selection
* timing
* parameter
* chaining
* result parsing
* error handling
* retry

---

## 3. Context Management

* system prompt
* history
* filtering
* context reconstruction
* compression
* scratchpad
* memory
* irrelevant information

---

## 4. State Management

* state schema
* progress
* completed
* pending
* dependency
* intermediate result
* checkpoint
* termination condition

---

## 5. Reflection & Verification

重点区分：

### Reflection

LLM 判断：

> “我做得对不对？”

### Verification

系统判断：

> “客观条件是否已经满足？”

尽可能减少没有客观依据的 Reflection。

---

## 6. Fallback & Recovery

设计：

```text
Failure
↓
Classify
↓
Retry / Replan / Alternative Tool / Fallback
```

禁止：

```text
失败
↓
完全相同 Prompt
↓
完全相同 Retry
↓
再次失败
```

---

# 二十二、实验纪律

所有实验必须遵守：

## Rule 1

一次 Candidate 尽量验证一个主要 Hypothesis。

---

## Rule 2

不要同时修改十几个机制后再判断：

> “整体好像有效”。

---

## Rule 3

记录 negative result。

失败实验同样重要。

---

## Rule 4

不要隐藏 regression。

---

## Rule 5

测试结果优先于 Agent 自我评价。

---

# 二十三、优化目标函数

Leader 的首要评分指标：

```text
Primary Score = Passed Tests
```

如果多个 Candidate PASS 接近，则综合：

```text
Secondary Score =
Stability
- Regression
- Unnecessary Complexity
- Latency Cost
- Token Cost
```

可以参考：

```text
Utility =
Test Gain
× Confidence
× Generalization
÷
(Complexity + Regression Risk + Runtime Cost)
```

该公式只用于辅助决策。

最终仍以真实实验为准。

---

# 二十四、防止局部最优

如果连续两轮：

```text
PASS 提升 ≤ 1
```

Leader 必须触发：

> **Architecture Reassessment**

重新审视：

* 当前 Agent abstraction 是否合理
* 当前 workflow 是否本身错误
* 是否存在错误 completion model
* 是否 state architecture 不适合 chain task
* 是否 tool interface 限制 Agent
* 是否 Prompt 已经接近能力边界

此时允许创建：

> architecture-level candidate。

---

# 二十五、最终结果报告

三轮结束后，Leader 输出：

## 1. Optimization History

| Stage    | PASS | FAIL | ERROR | Success Rate | Net Gain |
| -------- | ---: | ---: | ----: | -----------: | -------: |
| Original |      |      |       |              |          |
| Round 1  |      |      |       |              |          |
| Round 2  |      |      |       |              |          |
| Round 3  |      |      |       |              |          |

---

# 二十六、实验记录

列出所有重要 Candidate：

| Round | Candidate | Hypothesis | PASS | Result |
| ----- | --------- | ---------- | ---: | ------ |
| 1     | A         |            |      |        |
| 1     | B         |            |      |        |
| 1     | C         |            |      |        |
| 2     | A         |            |      |        |
| ...   |           |            |      |        |

---

# 二十七、最有效修改

按照：

> **实际增加 PASS 的贡献**

排序。

```text
1.
2.
3.
4.
5.
```

而不是按照理论重要程度。

---

# 二十八、最终 Root Cause

区分：

## Primary Root Causes

真正造成大量失败的问题。

## Secondary Root Causes

影响部分测试的问题。

## Symptoms

只是表象的问题。

---

# 二十九、最终架构

描述最终实际系统。

例如：

```text
User Input
    ↓
Task Analyzer
    ↓
Planner
    ↓
Structured State
    ↓
Executor
    ↓
Tool Layer
    ↓
State Update
    ↓
Verifier
    ↓
 ┌── success → Final
 │
 └── failure
       ↓
   Failure Classifier
       ↓
 Retry / Replan / Fallback
```

这里只描述实际采用架构。

不要为了报告完整而虚构模块。

---

# 三十、剩余失败测试

对剩余测试按照 Cluster 汇总：

```text
Cluster:

Tests:

Failure Mode:

Current Root Cause:

Attempts:

Why Previous Attempts Failed:

Most Promising Next Experiment:
```

---
# 三十一、最终结论
必须明确回答：
```text
Original:
X / 83
Final:
Y / 83
Net Gain:
+N
```
然后回答：
### 最重要的三个优化
1.
2.
3.
### 最重要的三个 Root Cause
1.
2.
3.
### 是否接近当前架构能力上限？
是 / 否
依据：
### 如果允许 Round 4，只做哪三个实验？
按照：
`Expected Test Gain × Confidence ÷ Implementation Cost`
排序：
1.
2.
3.
---
# 三十二、执行要求
现在立即开始。
不要先输出泛泛而谈的 Agent 优化建议。
直接：
1. 探索 repository
2. 找到 `tests/question/redesign`
3. 建立 Agent execution path
4. 运行 Original Baseline
5. 分析失败测试
6. 创建 Failure Taxonomy
7. 启动 5 个 Engineer Sub-Agent
8. 让 Leader 设计 Round 1 Candidates
9. 创建隔离 Worktree / Branch
10. 实现 Candidate A/B/C
11. 分别运行测试
12. 选择 Winner
13. 进入 Round 2
14. 重复直到 Round 3 完成
在执行过程中：
* 不要因为任务规模较大而停止在“分析方案”
* 不要只生成文档而不修改代码
* 不要只修改代码而不跑测试
* 不要只跑部分测试后宣称成功
* 不要跳过失败实验记录
* 不要为了 PASS 数作弊
整个任务的核心不是：
> **模拟 6 个人开会。**
而是：
> **利用多 Agent 提出不同且可验证的根因假设，通过隔离的并行实现和真实测试进行竞争，让数据决定哪条优化路线进入下一轮。**
