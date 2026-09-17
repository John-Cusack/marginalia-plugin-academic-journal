"""Tests for Pydantic models."""

from __future__ import annotations

from uuid import UUID

from acad.models import (
    AcquiredFile,
    Author,
    DiscoveryRun,
    ExternalId,
    ExternalIdSource,
    Job,
    Paper,
    PipelineStage,
    StageStatus,
)


def test_paper_defaults():
    paper = Paper(title="Test Paper")
    assert paper.title == "Test Paper"
    assert paper.pipeline_stage == PipelineStage.discovered
    assert paper.stage_status == StageStatus.pending
    assert paper.citation_count == 0
    assert paper.external_ids == []
    assert paper.authors == []
    assert isinstance(paper.id, UUID)


def test_paper_with_external_ids():
    paper = Paper(
        title="Test",
        external_ids=[
            ExternalId(source=ExternalIdSource.doi, external_id="10.1234/test"),
            ExternalId(source=ExternalIdSource.arxiv, external_id="2301.12345"),
        ],
    )
    assert len(paper.external_ids) == 2
    assert paper.external_ids[0].source == ExternalIdSource.doi


def test_external_id_source_values():
    assert ExternalIdSource.doi.value == "doi"
    assert ExternalIdSource.openalex.value == "openalex"
    assert ExternalIdSource.semantic_scholar.value == "semantic_scholar"
    assert ExternalIdSource.arxiv.value == "arxiv"
    assert ExternalIdSource.pmid.value == "pmid"


def test_pipeline_stage_values():
    assert PipelineStage.discovered.value == "discovered"
    assert PipelineStage.unresolvable.value == "unresolvable"
    assert PipelineStage.complete.value == "complete"


def test_author_model():
    author = Author(name="John Doe", position=0)
    assert author.name == "John Doe"
    assert author.entity_id is None


def test_job_defaults():
    from uuid import uuid4
    job = Job(paper_id=uuid4(), stage=PipelineStage.resolved)
    assert job.status == StageStatus.pending
    assert job.attempts == 0
    assert job.max_attempts == 5


def test_acquired_file():
    af = AcquiredFile(
        file_path="/tmp/test.pdf",
        file_hash="abc123",
        file_size=1024,
        source_url="https://example.com/paper.pdf",
        module_id="direct_pdf",
    )
    assert af.file_size == 1024
    assert af.module_id == "direct_pdf"


def test_discovery_run_defaults():
    run = DiscoveryRun(query_text="test query", source="openalex")
    assert run.status == "running"
    assert run.papers_found == 0
