"""Pydantic models for academic paper pipeline."""

from __future__ import annotations

import enum
from datetime import datetime
from uuid import UUID, uuid4

from pydantic import BaseModel, Field


class PipelineStage(str, enum.Enum):
    discovered = "discovered"
    resolved = "resolved"
    acquired = "acquired"
    ingested = "ingested"
    citations_extracted = "citations_extracted"
    complete = "complete"
    unresolvable = "unresolvable"


class StageStatus(str, enum.Enum):
    pending = "pending"
    in_progress = "in_progress"
    succeeded = "succeeded"
    failed = "failed"
    blocked_manual = "blocked_manual"


class ExternalIdSource(str, enum.Enum):
    doi = "doi"
    openalex = "openalex"
    semantic_scholar = "semantic_scholar"
    arxiv = "arxiv"
    pmid = "pmid"
    crossref = "crossref"


class ExternalId(BaseModel):
    source: ExternalIdSource
    external_id: str


class Author(BaseModel):
    name: str
    position: int
    openalex_author_id: str | None = None
    s2_author_id: str | None = None
    entity_id: UUID | None = None


class Paper(BaseModel):
    id: UUID = Field(default_factory=uuid4)
    title: str
    abstract: str | None = None
    year: int | None = None
    venue: str | None = None
    citation_count: int = 0
    influential_citation_count: int = 0
    open_access_url: str | None = None
    file_path: str | None = None
    file_hash: str | None = None
    document_id: UUID | None = None
    pipeline_stage: PipelineStage = PipelineStage.discovered
    stage_status: StageStatus = StageStatus.pending
    stage_error: str | None = None
    external_ids: list[ExternalId] = Field(default_factory=list)
    authors: list[Author] = Field(default_factory=list)
    created_at: datetime | None = None
    updated_at: datetime | None = None


class DiscoveryRun(BaseModel):
    id: UUID = Field(default_factory=uuid4)
    query_text: str
    source: str
    papers_found: int = 0
    papers_new: int = 0
    status: str = "running"
    error: str | None = None
    created_at: datetime | None = None
    completed_at: datetime | None = None


class Job(BaseModel):
    id: UUID = Field(default_factory=uuid4)
    paper_id: UUID
    stage: PipelineStage
    status: StageStatus = StageStatus.pending
    priority: int = 0
    attempts: int = 0
    max_attempts: int = 5
    last_error: str | None = None
    locked_by: str | None = None
    locked_at: datetime | None = None
    scheduled_after: datetime | None = None
    created_at: datetime | None = None
    completed_at: datetime | None = None


class AcquiredFile(BaseModel):
    """Result of an acquisition module download."""
    file_path: str
    file_hash: str
    file_size: int
    source_url: str | None = None
    module_id: str | None = None
