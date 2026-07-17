from langgraph.graph import StateGraph, END
from .schemas import EvidenceMatchingState
from .agents import extract_requirements, assess_match


class EvidenceMatchingGraph:
    """Production graph for evidence-based job-candidate matching.

    This graph accepts frozen snapshots and returns structured MatchComputationOutput.
    It does NOT access databases, files, or object storage.
    """

    def __init__(self, model):
        self.model = model
        self._graph = self._build()

    def _build(self):
        builder = StateGraph(EvidenceMatchingState)
        builder.add_node("extract_requirements", self._extract)
        builder.add_node("assess_match", self._assess)
        builder.add_node("fail", self._fail)
        builder.set_entry_point("extract_requirements")
        builder.add_conditional_edges("extract_requirements", self._route_after_extract)
        builder.add_conditional_edges("assess_match", self._route_after_assess)
        builder.add_edge("fail", END)
        return builder.compile()

    async def _extract(self, state: EvidenceMatchingState) -> dict:
        return await extract_requirements(state.model_dump(), self.model)

    async def _assess(self, state: EvidenceMatchingState) -> dict:
        return await assess_match(state.model_dump(), self.model)

    def _route_after_extract(self, state: EvidenceMatchingState) -> str:
        return state.next_step

    def _route_after_assess(self, state: EvidenceMatchingState) -> str:
        return state.next_step if state.next_step != "finish" else END

    async def _fail(self, state: EvidenceMatchingState) -> dict:
        return {"next_step": END}

    async def arun(self, job_snapshot: dict, profile_snapshot: dict) -> dict:
        initial = EvidenceMatchingState(
            job_snapshot=job_snapshot,
            profile_snapshot=profile_snapshot,
        )
        result = await self._graph.ainvoke(initial)
        return result
