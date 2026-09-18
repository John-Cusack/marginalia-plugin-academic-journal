"""Tests for pipeline state machine transitions."""

from __future__ import annotations

import pytest

from acad.models import PipelineStage
from acad.pipeline.state_machine import (
    InvalidTransitionError,
    can_transition,
    validate_transition,
)


def test_valid_forward_transitions():
    """Test all valid forward transitions in the pipeline."""
    valid_pairs = [
        (PipelineStage.discovered, PipelineStage.resolved),
        (PipelineStage.discovered, PipelineStage.unresolvable),
        (PipelineStage.resolved, PipelineStage.acquired),
        (PipelineStage.acquired, PipelineStage.ingested),
        (PipelineStage.ingested, PipelineStage.citations_extracted),
        (PipelineStage.citations_extracted, PipelineStage.complete),
        (PipelineStage.unresolvable, PipelineStage.resolved),  # re-resolve
    ]
    for from_stage, to_stage in valid_pairs:
        validate_transition(from_stage, to_stage)  # Should not raise
        assert can_transition(from_stage, to_stage)


def test_invalid_transitions():
    """Test that invalid transitions raise."""
    invalid_pairs = [
        (PipelineStage.discovered, PipelineStage.acquired),  # skip resolved
        (PipelineStage.discovered, PipelineStage.ingested),   # skip stages
        (PipelineStage.resolved, PipelineStage.discovered),   # backward
        (PipelineStage.complete, PipelineStage.discovered),   # from terminal
        (PipelineStage.acquired, PipelineStage.resolved),     # backward
        (PipelineStage.ingested, PipelineStage.acquired),     # backward
    ]
    for from_stage, to_stage in invalid_pairs:
        with pytest.raises(InvalidTransitionError):
            validate_transition(from_stage, to_stage)
        assert not can_transition(from_stage, to_stage)


def test_complete_is_terminal():
    """Complete stage should have no valid transitions."""
    for stage in PipelineStage:
        assert not can_transition(PipelineStage.complete, stage)


def test_error_contains_stage_names():
    with pytest.raises(InvalidTransitionError) as exc_info:
        validate_transition(PipelineStage.discovered, PipelineStage.complete)
    assert "discovered" in str(exc_info.value)
    assert "complete" in str(exc_info.value)
