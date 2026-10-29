"""A locked, encrypted credential file for a single-host container deployment."""

import fcntl
import json
import os
import tempfile
import threading
from contextlib import contextmanager
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken

from .core import Problem


class FileVault:
    def __init__(self, directory):
        self.directory = Path(directory)
        self.lock = threading.RLock()

    @contextmanager
    def locked(self):
        with self.lock:
            try:
                if self.directory.is_symlink():
                    raise Problem("凭据目录不能是符号链接", 503)
                self.directory.mkdir(parents=True, mode=0o700, exist_ok=True)
                self.directory.chmod(0o700)
                flags = os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW
                with os.fdopen(os.open(self.directory / ".lock", flags, 0o600), "r+b") as handle:
                    os.fchmod(handle.fileno(), 0o600)
                    fcntl.flock(handle, fcntl.LOCK_EX)
                    try:
                        yield
                    finally:
                        fcntl.flock(handle, fcntl.LOCK_UN)
            except OSError:
                raise Problem("无法访问凭据文件，请检查持久化目录权限", 503) from None

    def read_file(self, name):
        with os.fdopen(os.open(self.directory / name, os.O_RDONLY | os.O_NOFOLLOW), "rb") as handle:
            os.fchmod(handle.fileno(), 0o600)
            return handle.read()

    def load(self):
        key_path, values_path = self.directory / "master.key", self.directory / "credentials.enc"
        new_key = not key_path.exists()
        if new_key:
            if values_path.exists():
                raise Problem("凭据主密钥丢失，请恢复完整凭据目录", 503)
            descriptor = os.open(key_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(Fernet.generate_key())
                handle.flush()
                os.fsync(handle.fileno())
        try:
            cipher = Fernet(self.read_file("master.key"))
            if values_path.exists():
                values = json.loads(cipher.decrypt(self.read_file("credentials.enc")))
            elif new_key:
                values = {}
                self.save(cipher, values)
            else:
                raise Problem("凭据文件丢失，请恢复完整凭据目录", 503)
            if not isinstance(values, dict) or any(
                not isinstance(service, str) or not isinstance(items, dict)
                or any(not isinstance(k, str) or not isinstance(v, str) for k, v in items.items())
                for service, items in values.items()
            ):
                raise ValueError
        except (ValueError, InvalidToken, UnicodeError):
            raise Problem("凭据文件无法解密或格式损坏，请恢复完整备份", 503) from None
        return cipher, values

    def save(self, cipher, values):
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(dir=self.directory, prefix=".credentials-", delete=False) as handle:
                temporary = Path(handle.name)
                os.fchmod(handle.fileno(), 0o600)
                handle.write(cipher.encrypt(json.dumps(values, ensure_ascii=False).encode()))
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.directory / "credentials.enc")
            descriptor = os.open(self.directory, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
        finally:
            if temporary and temporary.exists():
                temporary.unlink()

    def get(self, service, name):
        with self.locked():
            _, values = self.load()
            return values.get(service, {}).get(name)

    def set(self, service, name, value):
        if not isinstance(value, str):
            raise Problem("凭据必须为文本", 422)
        with self.locked():
            cipher, values = self.load()
            values.setdefault(service, {})[name] = value
            self.save(cipher, values)

    def delete(self, service, name):
        with self.locked():
            cipher, values = self.load()
            if name in values.get(service, {}):
                del values[service][name]
                self.save(cipher, values)
