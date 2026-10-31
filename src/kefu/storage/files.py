import hashlib
import io
import os
import re
import stat
import warnings
from pathlib import Path

from PIL import Image, UnidentifiedImageError

from kefu.storage.repository import DomainError

Image.MAX_IMAGE_PIXELS = 20_000_000


def validate_image(content):
    if len(content) > 10 * 1024 * 1024:
        raise DomainError("UPLOAD_TOO_LARGE", "每张图片最多 10 MiB", 413)
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(content)) as picture:
                if (
                    picture.format not in ("JPEG", "PNG", "WEBP")
                    or picture.width * picture.height > 20_000_000
                ):
                    raise ValueError("image format or pixels")
                mime = Image.MIME[picture.format]
                picture.verify()
            with Image.open(io.BytesIO(content)) as picture:
                picture.load()
                if getattr(picture, "n_frames", 1) > 1:
                    raise ValueError("animated images are unsupported")
        return mime
    except (
        UnidentifiedImageError,
        ValueError,
        OSError,
        Image.DecompressionBombError,
        Image.DecompressionBombWarning,
    ):
        raise DomainError(
            "INVALID_IMAGE", "仅支持有效的 JPEG、PNG 或 WebP，且不超过 2000 万像素", 422
        ) from None


class FileStore:
    def __init__(self, root: Path):
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def _name(self, key):
        if not re.fullmatch(r"[a-f0-9-]{36}", key):
            raise DomainError("NOT_FOUND", "材料不可访问", 404)
        return key

    def put(self, key, content):
        key = self._name(key)
        path = self.root / key
        flags = (
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_BINARY", 0)
        )
        with os.fdopen(os.open(path, flags, 0o600), "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        return hashlib.sha256(content).hexdigest()

    def unlink(self, key):
        """Remove a registered server key only after its database tombstone."""
        key = self._name(key)
        try:
            if os.name == "posix":
                directory = self._directory_fd()
                try:
                    os.unlink(key, dir_fd=directory)
                finally:
                    os.close(directory)
            else:
                (self.root / key).unlink()
        except FileNotFoundError:
            pass

    def _directory_fd(self):
        flags = os.O_RDONLY
        for capability in ("O_DIRECTORY", "O_NOFOLLOW"):
            flags |= getattr(os, capability)
        return os.open(self.root, flags)

    def read(self, key, expected_hash):
        key = self._name(key)
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
        try:
            if os.name == "posix":
                directory = self._directory_fd()
                try:
                    fd = os.open(key, flags, dir_fd=directory)
                finally:
                    os.close(directory)
            else:
                if (self.root / key).is_symlink():
                    raise OSError("symlink")
                fd = os.open(self.root / key, flags)
            with os.fdopen(fd, "rb") as stream:
                if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                    raise OSError("not regular")
                content = stream.read(10 * 1024 * 1024 + 1)
            if hashlib.sha256(content).hexdigest() != expected_hash:
                raise OSError("hash mismatch")
            return content
        except OSError:
            raise DomainError(
                "MATERIAL_UNAVAILABLE", "原始材料缺失或已变更，请重新上传", 404
            ) from None
