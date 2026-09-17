"""Pipeline stage transitions and validation."""

from __future__ import annotations

from acad.models import PipelineStage

# Valid state transitions: {from_stage: [to_stages]}
_TRANSITIONS: dict[PipelineStage, list[PipelineStage]] = {
    PipelineStage.discovered: [PipelineStage.resolved, PipelineStage.unresolvable],
    PipelineStage.resolved: [PipelineStage.acquired],
    PipelineStage.acquired: [PipelineStage.ingested],
    PipelineStage.ingested: [PipelineStage.citations_extracted],
    PipelineStage.citations_extracted: [PipelineStage.complete],
    PipelineStage.unresolvable: [PipelineStage.resolved],  # can re-resolve later
    PipelineStage.complete: [],
}

# Which stage's job should be enqueued when entering a given stage
NEXT_JOB_STAGE: dict[PipelineStage, PipelineStage | None] = {
    PipelineStage.discovered: PipelineStage.resolved,
    PipelineStage.resolved: PipelineStage.acquired,
    PipelineStage.acquired: PipelineStage.ingested,
    PipelineStage.ingested: PipelineStage.citations_extracted,
    PipelineStage.citations_extracted: None,
    PipelineStage.complete: None,
    PipelineStage.unresolvable: None,
}


class InvalidTransitionError(Exception):
    def __init__(self, from_stage: PipelineStage, to_stage: PipelineStage) -> None:
        self.from_stage = from_stage
        self.to_stage = to_stage
        super().__init__(
            f"Invalid transition: {from_stage.value} → {to_stage.value}"
        )


def validate_transition(from_stage: PipelineStage, to_stage: PipelineStage) -> None:
    """Raise InvalidTransitionError if the transition is not allowed."""
    allowed = _TRANSITIONS.get(from_stage, [])
    if to_stage not in allowed:
        raise InvalidTransitionError(from_stage, to_stage)


def can_transition(from_stage: PipelineStage, to_stage: PipelineStage) -> bool:
    """Check if a transition is valid without raising."""
    return to_stage in _TRANSITIONS.get(from_stage, [])
