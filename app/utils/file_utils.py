import hashlib
import logging
import mimetypes
import os
from pathlib import Path
import shutil

from regex import B

from langchain_classic.embeddings import awa

from fastapi import UploadFile

logger = logging.getLogger(__name__)


def get_file_hash(file_content: bytes) -> str:
    return hashlib.sha256(file_content).hexdigest()


def get_file_mime_type(filename: str) -> str:
    mime_type, _ = mimetypes.guess_type(filename)
    return mime_type or "application/octet-stream"


def ensure_directory(diretory: Path) -> None:
    diretory.mkdir(parents=True, exist_ok=True)


async def save_uploaded_file(
    upload_file: UploadFile, destination: Path, max_size: int = 10 * 1024 * 1024
) -> tuple[str, int]:
    """save upload file to destination
    returns:
    tuple:[file_hash,file_size]
    """
    ensure_directory(destination)
    content = await upload_file.read()
    file_size = len(content)
    if file_size > max_size:
        raise ValueError(f"File size {file_size} exceeds maximum {max_size}")

    file_hash = get_file_hash(content)

    with open(destination, "wb") as f:
        f.write(content)

    logger.info(f"File saved:{destination}(size:{file_size},has:{file_hash})")
    return file_hash, file_size


def delete_file_safe(file_path: Path) -> bool:
    """safely delete file, return True if sucessful"""

    try:
        if file_path.exists():
            file_path.unlink()
            logger.info(f"File deleted:{file_path}")
            return True
        else:
            logger.warning(f"File not fund for deletion:{file_path}")
            return False
    except Exception as e:
        logger.error(f"Error deleting file {file_path}:{e}")
        return False


def copy_file_safe(source: Path, destination: Path) -> bool:
    try:
        ensure_directory(destination)
        shutil.copy2(source, destination)
        logger.info(f"File copied:{source}->{destination}")
        return True
    except Exception as e:
        logger.error(f"Error copying file {source} to {destination}:{e}")
        return False


def get_file_size(file_path: Path):
    return file_path.stat().st_size


def is_file_type_allowed(filename: str, allowed_types: list[str]) -> bool:
    if not filename:
        return False
    extension = Path(filename).suffix.lower()
    return extension in [ex.lower() for ex in allowed_types]


def senitize_filename(filename: str) -> str:
    dangerous_chars = '<>:"/\\|?*'
    for char in dangerous_chars:
        filename = filename.replace(char, "_")
    filename = filename.strip(" .")
    # Ensure filename is not empty
    if not filename:
        filename = "unnamed_file"

    # Limit length
    if len(filename) > 255:
        name, ext = os.path.splitext(filename)
        filename = name[: 255 - len(ext)] + ext

    return filename


def get_unique_filename(directory: Path, filename: str) -> str:
    base_path = directory / filename
    if not base_path.exists():
        return filename
    name, ext = os.path.splitext(filename)
    counter = 1
    while True:
        new_filename = f"{name}_{counter}{ext}"
        new_path = directory / new_filename
        if not new_path.exists():
            return new_filename
        counter += 1


class FileManager:
    def __init__(self, base_directory: Path) -> None:
        self.base_directory = base_directory
        ensure_directory(base_directory)

    def get_file_path(self, relative_path: str) -> Path:
        return self.base_directory / relative_path

    def save_file(self, content: bytes, relativate_path: str) -> Path:
        file_path = self.get_file_path(relative_path=relativate_path)
        ensure_directory(file_path)
        with open(file_path, "wb") as f:
            f.write(content)
        return file_path

    def readfile(self, relative_path: str) -> bytes:
        file_path = self.get_file_path(relative_path)
        with open(file_path, "rb") as f:
            return f.read()

    def delete_file(self, relative_path: str) -> bool:
        file_path = self.get_file_path(relative_path)
        return delete_file_safe(file_path)

    def file_exists(self, relative_path: str) -> bool:
        """Check if file exists at relative path"""
        file_path = self.get_file_path(relative_path)
        return file_path.exists()

    def list_files(self, relative_directory: str = "") -> list[str]:
        directory = self.get_file_path(relative_directory)

        if not directory.exists() or not directory.is_dir():
            return []
        files = []
        for item in directory.iterdir():
            if item.is_file():
                files.append(str(item.relative_to(self.base_directory)))

        return sorted(files)
