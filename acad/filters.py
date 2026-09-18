"""FilterExtension for academic paper metadata queries."""

from __future__ import annotations

from typing import Any

import sqlalchemy as sa


class AcademicPaperFilter:
    """Filter passages by academic paper metadata.

    Joins through ``acad_papers.document_id`` → ``core.documents.id`` →
    ``core.passages.document_id`` to resolve passage IDs from paper-level
    filters like year range, venue, and citation count.
    """

    @property
    def filter_id(self) -> str:
        return "academic_paper"

    @property
    def description(self) -> str:
        return (
            "Filter passages from academic papers by year range, venue, "
            "citation count, or open access status. Use for queries like "
            "'highly-cited papers from Nature published after 2020'."
        )

    @property
    def input_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "year_min": {
                    "type": "integer",
                    "description": "Minimum publication year (inclusive).",
                },
                "year_max": {
                    "type": "integer",
                    "description": "Maximum publication year (inclusive).",
                },
                "venue": {
                    "type": "string",
                    "description": "Journal or conference name (case-insensitive substring match).",
                },
                "min_citations": {
                    "type": "integer",
                    "description": "Minimum total citation count.",
                },
                "min_influential_citations": {
                    "type": "integer",
                    "description": "Minimum influential citation count (Semantic Scholar metric).",
                },
                "open_access": {
                    "type": "boolean",
                    "description": "If true, only include papers with an open access URL.",
                },
            },
        }

    def build_clause(self, value: Any) -> sa.sql.expression.SelectBase:
        # Join acad_papers → core.documents → core.passages
        # to get passage_ids matching paper-level filters.
        conditions: list[sa.sql.expression.ColumnElement] = [
            sa.literal_column("ap.document_id").isnot(None),
        ]

        if value.get("year_min") is not None:
            conditions.append(
                sa.literal_column("ap.year") >= sa.bindparam("year_min", value=value["year_min"])
            )
        if value.get("year_max") is not None:
            conditions.append(
                sa.literal_column("ap.year") <= sa.bindparam("year_max", value=value["year_max"])
            )
        if value.get("venue"):
            conditions.append(
                sa.literal_column("ap.venue").ilike(
                    sa.bindparam("venue_pattern", value=f"%{value['venue']}%")
                )
            )
        if value.get("min_citations") is not None:
            conditions.append(
                sa.literal_column("ap.citation_count") >= sa.bindparam(
                    "min_citations", value=value["min_citations"]
                )
            )
        if value.get("min_influential_citations") is not None:
            conditions.append(
                sa.literal_column("ap.influential_citation_count") >= sa.bindparam(
                    "min_influential", value=value["min_influential_citations"]
                )
            )
        if value.get("open_access"):
            conditions.append(sa.literal_column("ap.open_access_url").isnot(None))

        return sa.select(
            sa.literal_column("p.id").label("passage_id")
        ).select_from(
            sa.text(
                "acad_papers ap "
                "JOIN core.passages p ON p.document_id = ap.document_id"
            )
        ).where(
            sa.and_(*conditions)
        )
