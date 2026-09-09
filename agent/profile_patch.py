import io
import shutil
import tarfile
from pathlib import Path

from agent import local_worker

# Keep only state that can matter for a persistent authenticated Chromium profile.
PROFILE_PATHS = (
    "Local State",
    "Default/Network/Cookies",
    "Default/Cookies",
    "Default/Local Storage",
    "Default/Session Storage",
    "Default/IndexedDB",
    "Default/Preferences",
    "Default/Secure Preferences",
    "Default/Web Data",
    "Default/SharedStorage",
)


def pack_profile() -> bytes:
    root = local_worker.PROFILE_DIR
    root.mkdir(parents=True, exist_ok=True)
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w:gz", compresslevel=6) as tar:
        for relative in PROFILE_PATHS:
            item = root / relative
            if item.exists():
                tar.add(item, arcname=relative, recursive=True)
    return local_worker.derive_fernet().encrypt(raw.getvalue())


def unpack_profile(data: bytes) -> None:
    decrypted = local_worker.derive_fernet().decrypt(data)
    root = local_worker.PROFILE_DIR
    if root.exists():
        shutil.rmtree(root)
    root.mkdir(parents=True, exist_ok=True)
    with tarfile.open(fileobj=io.BytesIO(decrypted), mode="r:gz") as tar:
        tar.extractall(root, filter="data")
