"""Write a workspace file atomically. Refuse links and paths outside its folder."""

import os
import tempfile
from pathlib import Path


def file_path(root: Path, folder_name: str, file_name: str, *, create=True) -> Path:
    folder = root / folder_name
    if folder.is_symlink() or Path(file_name).name != file_name:
        raise ValueError("The workspace path is invalid")
    if create:
        folder.mkdir(exist_ok=True)
    path = folder / file_name
    if path.is_symlink() or path.resolve().parent != folder.resolve():
        raise ValueError("The workspace path is invalid")
    return path


def write(root: Path, folder_name: str, file_name: str, text: str) -> Path:
    path = file_path(root, folder_name, file_name)
    fd, name = tempfile.mkstemp(prefix=".illo-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)
    return path
