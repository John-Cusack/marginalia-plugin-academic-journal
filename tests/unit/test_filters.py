"""Tests for AcademicPaperFilter."""

from __future__ import annotations

import sqlalchemy as sa

from acad.filters import AcademicPaperFilter


class TestAcademicPaperFilter:
    def test_properties(self):
        f = AcademicPaperFilter()
        assert f.filter_id == "academic_paper"
        assert "year_min" in f.input_schema["properties"]
        assert "venue" in f.input_schema["properties"]
        assert "min_citations" in f.input_schema["properties"]
        assert len(f.description) > 0

    def test_build_clause_year_range(self):
        f = AcademicPaperFilter()
        clause = f.build_clause({"year_min": 2020, "year_max": 2024})
        assert isinstance(clause, sa.sql.expression.SelectBase)
        compiled = str(clause.compile())
        assert "acad_papers" in compiled
        assert "year_min" in compiled
        assert "year_max" in compiled

    def test_build_clause_venue(self):
        f = AcademicPaperFilter()
        clause = f.build_clause({"venue": "Nature"})
        compiled = str(clause.compile())
        assert "venue_pattern" in compiled

    def test_build_clause_citations(self):
        f = AcademicPaperFilter()
        clause = f.build_clause({"min_citations": 100})
        compiled = str(clause.compile())
        assert "citation_count" in compiled

    def test_build_clause_influential_citations(self):
        f = AcademicPaperFilter()
        clause = f.build_clause({"min_influential_citations": 10})
        compiled = str(clause.compile())
        assert "influential_citation_count" in compiled

    def test_build_clause_open_access(self):
        f = AcademicPaperFilter()
        clause = f.build_clause({"open_access": True})
        compiled = str(clause.compile())
        assert "open_access_url" in compiled

    def test_build_clause_combined(self):
        f = AcademicPaperFilter()
        clause = f.build_clause({
            "year_min": 2020,
            "venue": "Nature",
            "min_citations": 50,
            "open_access": True,
        })
        compiled = str(clause.compile())
        assert "year_min" in compiled
        assert "venue_pattern" in compiled
        assert "citation_count" in compiled
        assert "open_access_url" in compiled

    def test_build_clause_empty_filters(self):
        f = AcademicPaperFilter()
        clause = f.build_clause({})
        assert isinstance(clause, sa.sql.expression.SelectBase)
        compiled = str(clause.compile())
        assert "acad_papers" in compiled
