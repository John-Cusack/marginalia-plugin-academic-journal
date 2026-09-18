"""arXiv PDF acquisition module."""

from __future__ import annotations

import hashlib
import re
from typing import TYPE_CHECKING, ClassVar

from acad.acquisition_modules.base import AcquisitionModule
from acad.infra.http_client import ResilientHttpClient
from acad.models import AcquiredFile, Paper

if TYPE_CHECKING:
    from pathlib import Path

#: ``https://arxiv.org/pdf/2401.00001v2.pdf`` and ``/abs/hep-th/9901001`` alike.
_ARXIV_URL = re.compile(r"arxiv\.org/(?:pdf|abs)/(?P<id>[^?#]+?)(?:\.pdf)?/?$", re.IGNORECASE)


def _arxiv_id_from_url(url: str | None) -> str | None:
    match = _ARXIV_URL.search(url or "")
    return match.group("id") if match else None


class ArxivModule(AcquisitionModule):
    id: ClassVar[str] = "arxiv"
    display_name: ClassVar[str] = "arXiv PDF"
    priority: ClassVar[int] = 80

    async def can_acquire(self, paper: Paper) -> tuple[float, str]:
        for ext_id in paper.external_ids:
            if ext_id.source.value == "arxiv":
                return 0.95, "arXiv paper"
        # Check if OA URL points to arxiv
        if paper.open_access_url and "arxiv.org" in paper.open_access_url:
            return 0.9, "arXiv URL"
        return 0.0, "not an arXiv paper"

    async def acquire(self, paper: Paper, dest: Path) -> AcquiredFile:
        arxiv_id = None
        for ext_id in paper.external_ids:
            if ext_id.source.value == "arxiv":
                arxiv_id = ext_id.external_id
                break

        # can_acquire also accepts a paper whose only arXiv evidence is its
        # open-access URL, so the identifier has to be recoverable from it too.
        if not arxiv_id:
            arxiv_id = _arxiv_id_from_url(paper.open_access_url)

        if not arxiv_id:
            raise ValueError("No arXiv ID found in identifiers or open access URL")

        arxiv_id = arxiv_id.removeprefix("arXiv:").removeprefix("arxiv:")
        pdf_url = f"https://arxiv.org/pdf/{arxiv_id}.pdf"

        http = ResilientHttpClient(timeout=60.0)
        try:
            size, _ = await http.download(
                pdf_url, str(dest),
                source="arxiv",
                paper_id=paper.id,
            )
        finally:
            await http.close()

        content = dest.read_bytes()
        if content[:5] != b"%PDF-":
            dest.unlink(missing_ok=True)
            raise ValueError("Downloaded arXiv file is not a valid PDF")

        file_hash = hashlib.sha256(content).hexdigest()

        return AcquiredFile(
            file_path=str(dest),
            file_hash=file_hash,
            file_size=size,
            source_url=pdf_url,
            module_id=self.id,
        )
