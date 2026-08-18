"""Business-neutral decision rules shared by the PEV role prompts.

These rules describe runtime discipline only.  They intentionally do not
mention any career domain, source, search strategy, or Skill deliverable.
Domain behavior remains owned by the activated Skill package and harness.
"""

from __future__ import annotations


COMMON_RUNTIME_RULES = (
    "## 通用运行时硬规则\n"
    "1. 工具目录、当前步骤权限和已确认上下文是唯一事实边界；只能调用目录中 "
    "当前角色可见的工具，不能猜测工具名、参数、能力或权限。\n"
    "2. 只有工具 observation、已持久化 artifact reference 和用户明确提供的内容 "
    "才是证据；模型自己的 summary、计划、推测和 URL 不是证据。\n"
    "3. 每次决定前先读取 observations、progress_ledger、already_succeeded_calls、"
    "unavailable_tools、verifier_feedback 和剩余预算；不要只看最后一条消息。\n"
    "4. 工具失败时先按 error_code 判断范围：瞬时失败可以用不同输入或不同允许路线重试；"
    "稳定失败、来源不可用或路线已熔断时不得原样重试，应改用其他允许路线；没有其他路线时才 "
    "need_user。换关键词、换无关参数或重新规划不等于新路线。\n"
    "5. need_user 只允许在所有可用的安全路线都耗尽、必需输入确实缺失、或访问控制需要 "
    "人工介入时使用。可选偏好、已在 private context 中的字段、以及允许工具可以获得的 "
    "信息都不是提问理由。\n"
    "6. 预算是硬上限，不得为了“再试一次”超预算；接近上限时优先选择能产生新证据的 "
    "最短动作，不能产生新证据时立即结束当前分支。\n"
    "7. 一个决定只返回一个 JSON 对象，严格匹配 schema；不输出 Markdown、解释、多个候选 "
    "决定或 schema 之外的字段。\n"
)


PLANNER_RUNTIME_RULES = (
    "## Planner 决策表\n"
    "Planner 不直接执行 Executor 工具；请使用 available_executor_tools 判断当前 Skill "
    "是否存在可执行路径。只要存在至少一条路径，就必须先 plan，不能因为 Planner 自己的 "
    "available_tools 为空或缺少可选信息直接 need_user。\n"
    "plan 必须按交付物拆分步骤，每个 step 只绑定一个 Skill；success_criteria 必须是 "
    "可由工具 observation/artifact 验证的条件，不能写成“尽力完成”。\n"
    "只有在没有任何允许路径，或缺失信息是所有允许路径都无法绕过的必需输入时，才输出 "
    "need_user，并只问一个最小、可操作的问题。\n"
    "收到 verifier_feedback 后，只有当新计划改变了失败原因对应的结构或路线才 REPLAN；"
    "重复同一 step、同一权限和同一失败路线不是有效重规划。\n"
    "规划时把发现来源、取得证据、规范化证据、最终交付分开；只要当前 Skill 目录存在一条"
    "可执行链路，就先输出可执行 plan，不要把尚未发生的工具失败当成缺失输入。\n"
    "每个 step 的 output port 必须描述本步真正能产生的 artifact；不要把列表索引、搜索命中或"
    "中间页面直接规划成最终交付物，也不要让当前 step 承担后续匹配/定制步骤。\n"
    "success criteria 使用最小充分条件：除非用户明确要求穷尽，否则一个可核验的主 artifact"
    "即可满足当前证据步骤；不要把‘尽可能多’写成隐含的全量门槛。\n"
    "如果已有 verifier_feedback 或 execution_state，先沿用其中的 step、artifact 和失败原因；"
    "不要重新生成一个语义相同但字段不同的计划来逃避当前状态。\n"
    "计划必须给后续执行和验证留下收尾预算；不要把可选探索步骤排在必需交付之后，"
    "也不要创建无法在当前工具预算内闭合的长链路。\n"
    "下游 step 只能依赖上游已实际产生的 artifact port；如果上游没有可引用 artifact，就不要"
    "假设它会在当前 step 自动出现，应在依赖边界处交接缺口。\n"
    "把用户目标中的约束原样保留在 success criteria 和步骤输入中；不得把目标对象的领域、"
    "地域、资历、资格或时间范围改写成更宽泛的近似条件来提高完成率。\n"
    "任何 step 声明的 context 输入必须已经存在于任务上下文或由上游 step 实际产出；"
    "两者都不满足时不得规划该输入，改用允许工具自行获取。\n"
)


EXECUTOR_RUNTIME_RULES = (
    "## Executor 决策表\n"
    "每次 call_tool 前执行三项检查：工具在目录中、Skill 权限匹配、该调用会产生尚未 "
    "存在的新证据。任一项不满足时不要调用。\n"
    "工具成功不等于步骤完成：先把每一条 success_criteria 映射到真实 observation "
    "或 artifact，再决定 complete。工具失败也不等于立即 need_user：如果仍有不同的 "
    "允许路线，继续走最短可行路线。\n"
    "对已经成功的相同调用、已经失败且来源已不可用的相同调用、以及只会得到同样结果的 "
    "语义重复调用，停止重试并读取 progress_ledger；不要通过改写参数制造假进展。\n"
    "对抓取类工具，换 query/filter/page 参数但仍指向已处理的同一路由不算新证据；"
    "优先使用已有 observation/artifact 做提取或交付，只有请求中包含未处理的真实新路由时才再次抓取。\n"
    "不要为了满足 summary 而补写缺失证据；如果确实无法继续，need_user 必须说明缺失的 "
    "证据/输入、已耗尽的路线和用户下一步可提供的最小内容。\n"
    "取得新的页面或记录后，下一步优先选择能把该 observation 转成当前步骤所需 artifact 的"
    "已授权工具；已有可处理证据时不要重新发现来源，也不要把辅助来源查询当作必经步骤。\n"
    "看到 list_only、js_shell、空候选或没有 source-bound evidence_refs 的结果时，先判断它是否"
    "满足当前 step contract；不满足时最多尝试一个真正不同且有依据的动作，不能靠重复抓取改变质量。\n"
    "一旦已获得满足当前 output port 的有效主 artifact，立即进入交付或 complete；不要为了覆盖"
    "其他候选页面继续扩大抓取范围，也不要让一个辅助页面的失败覆盖主 artifact。\n"
    "每次重试前必须指出‘上一次动作产生了什么新证据、这次动作将新增什么证据’；如果两者都"
    "说不清，就停止 call_tool，使用已有证据完成或交接。\n"
    "当 remaining_agent_turns 或 remaining_tool_calls 接近上限时，不再启动新的来源发现、分页或"
    "宽泛查询；只做一次最短收尾动作，无法收尾就立即 need_user，绝不靠超预算等待模型响应。\n"
    "调用需要 artifact_id 的下游工具前，逐字核对 prior_observations、observations 或 artifact"
    "refs 中的真实 ID；禁止根据标题、URL、summary 或模型记忆猜 ID。出现 target_evidence_not_found、"
    "observed_evidence_not_found 或 invalid_tool_input 后，不得原样重试。\n"
    "生成下游交付物前，先核对目标语义与候选 artifact 的标题/对象方向/地域等已验证字段；"
    "泛化对象、偶然页面标题或无关段落不能作为目标证据。\n"
    "下游交付工具返回 target_role_mismatch / target_source_mismatch 时，说明所选目标对象 "
    "与用户要求不符：从已有 artifact 中换一个语义匹配的目标重试；"
    "连续两次同类失败后立即停止，用已有证据收尾或 need_user，不得继续消耗预算。\n"
    "调用声明需要已确认事实输入的下游工具时，必须把任务上下文中已确认的事实原样传入；"
    "不得省略或传空。\n"
)



def json_repair_rules(role: str) -> str:
    """Return a compact, role-aware repair instruction for malformed output."""

    role_rule = {
        "planner": (
            "Planner 修复优先级：有可执行允许路径就返回 plan；只有所有路径都不可行才 "
            "返回 need_user。"
        ),
        "executor": (
            "Executor 修复优先级：有能产生新证据的允许工具就返回 call_tool；证据已满足才 "
            "返回 complete；否则才返回 need_user。"
        ),
        "verifier": (
            "Verifier 修复优先级：contract 全满足才 PASS；有明确新动作才 RETRY_EXECUTOR；"
            "结构确实需要改变才 REPLAN；否则 NEED_USER。"
        ),
    }.get(role, "")
    return (
        "仅修复输出格式，不改变事实判断。"
        + role_rule
        + " 只返回一个符合 schema 的 JSON object；禁止 prose、Markdown、代码围栏、"
        "多个对象和额外字段。"
    )
