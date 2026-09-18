"""Unpaywall API acquisition module."""

from __future__ import annotations

import hashlib
import os
from typing import TYPE_CHECKING, ClassVar

from acad.acquisition_modules.base import AcquisitionModule
from acad.infra.http_client import ResilientHttpClient
from acad.models import AcquiredFile, Paper

if TYPE_CHECKING:
    from pathlib import Path


class UnpaywallModule(AcquisitionModule):
    id: ClassVar[str] = "unpaywall"
    display_name: ClassVar[str] = "Unpaywall"
    priority: ClassVar[int] = 60

    async def can_acquire(self, paper: Paper) -> tuple[float, str]:
        for ext_id in paper.external_ids:
            if ext_id.source.value == "doi":
                return 0.7, "DOI present, can query Unpaywall"
        return 0.0, "no DOI"

    async def acquire(self, paper: Paper, dest: Path) -> AcquiredFile:
        doi = None
        for ext_id in paper.external_ids:
            if ext_id.source.value == "doi":
                doi = ext_id.external_id
                break

        if not doi:
            raise ValueError("No DOI found")

        email = os.environ.get("UNPAYWALL_EMAIL", "user@example.com")

        http = ResilientHttpClient()
        try:
            data = await http.get_json(
                "unpaywall",
                f"https://api.unpaywall.org/v2/{doi}",
                params={"email": email},
                paper_id=paper.id,
            )

            best_oa = data.get("best_oa_location") or {}
            pdf_url = best_oa.get("url_for_pdf") or best_oa.get("url")
            if not pdf_url:
                raise ValueError(f"No PDF URL from Unpaywall for DOI {doi}")

            size, _ = await http.download(
                pdf_url, str(dest),
                paper_id=paper.id,
            )
        finally:
            await http.close()

        content = dest.read_bytes()
        if content[:5] != b"%PDF-":
            dest.unlink(missing_ok=True)
            raise ValueError("Downloaded file is not a valid PDF")

        file_hash = hashlib.sha256(content).hexdigest()

        return AcquiredFile(
            file_path=str(dest),
            file_hash=file_hash,
            file_size=size,
            source_url=pdf_url,
            module_id=self.id,
        )
