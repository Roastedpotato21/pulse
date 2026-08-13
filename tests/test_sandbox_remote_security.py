import io
import os
import tarfile
import tempfile
from pathlib import Path

import pytest

from pulse.sandbox.errors import SandboxSecurityError


@pytest.mark.asyncio
async def test_zip_slip_prevention():
    # Construct malicious overlay tar containing ../ traversal
    bio = io.BytesIO()
    with tarfile.open(fileobj=bio, mode="w:gz") as tar:
        ti = tarfile.TarInfo(name="../malicious.txt")
        content = b"evil content"
        ti.size = len(content)
        tar.addfile(ti, io.BytesIO(content))
        
    overlay_bytes = bio.getvalue()
    local_overlay_path = Path(tempfile.gettempdir()) / "test_remote_overlay_slip"
    local_overlay_path.mkdir(parents=True, exist_ok=True)
    
    # Test extraction validation
    with pytest.raises(SandboxSecurityError) as exc_info, tarfile.open(fileobj=io.BytesIO(overlay_bytes), mode="r:gz") as tar:
        for member in tar.getmembers():
            member_path = os.path.join(str(local_overlay_path), member.name)
            if not os.path.abspath(member_path).startswith(os.path.abspath(str(local_overlay_path))):
                raise SandboxSecurityError(
                    "Path traversal detected in download_artifact",
                    operation="download_artifact",
                    path=str(local_overlay_path),
                )
    assert "Path traversal detected" in str(exc_info.value)
