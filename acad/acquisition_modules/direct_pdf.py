"""Generic HTTP download acquisition module."""

from __future__ import annotations

import hashlib
from typing import TYPE_CHECKING, ClassVar

from acad.acquisition_modules.base import AcquisitionModule
from acad.infra.http_client import ResilientHttpClient
from acad.models import AcquiredFile, Paper

if TYPE_CHECKING:
    from pathlib import Path


class DirectPDFModule(AcquisitionModule):
    id: ClassVar[str] = "direct_pdf"
    display_name: ClassVar[str] = "Direct PDF Download"
    priority: ClassVar[int] = 40

    async def can_acquire(self, paper: Paper) -> tuple[float, str]:
        if paper.open_access_url:
            return 0.5, "has open access URL"
        return 0.0, "no open access URL"

    async def acquire(self, paper: Paper, dest: Path) -> AcquiredFile:
        if not paper.open_access_url:
            raise ValueError("No open access URL")

        http = ResilientHttpClient(timeout=60.0)
        try:
            size, content_type = await http.download(
                paper.open_access_url, str(dest),
                paper_id=paper.id,
            )
        finally:
            await http.close()

        content = dest.read_bytes()
        if content[:5] != b"%PDF-":
            dest.unlink(missing_ok=True)
            raise ValueError("Downloaded file is not a valid PDF (missing %PDF- header)")

        file_hash = hashlib.sha256(content).hexdigest()

        return AcquiredFile(
            file_path=str(dest),
            file_hash=file_hash,
            file_size=size,
            source_url=paper.open_access_url,
            module_id=self.id,
        )
