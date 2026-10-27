from contextlib import contextmanager
import fcntl
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import shutil
import stat
import time


METHODS = {"server/discover", "tools/list", "prompts/list", "resources/list", "resources/templates/list"}
MAX_BYTES = 1024 * 1024
MAX_ENTRIES = 128
MAX_TTL = 300000
FILES = re.compile(r"v1-[0-9a-f]{64}(?:-[0-9a-f]{64}\.json|\.native)\Z")
TEMP_FILES = re.compile(r"v1-[0-9a-f]{32}\.tmp\Z")
SECRET_NAMES = re.compile(r"token|secret|password|passwd|credential|auth|header|cookie|key|dsn", re.I)
SECRET_FIELDS = re.compile(
    r"(?:x[_-]?)?(?:(?:access|refresh|id|session|auth)[_-]?)?token|(?:client[_-]?)?secret|password|passwd"
    r"|credentials?|(?:proxy[_-]?)?authorization|(?:x[_-]?)?api[_-]?key|private[_-]?key|(?:set[_-]?)?cookie", re.I)
CREDENTIALS = re.compile(
    r"\b(?:Bearer|Basic)\s+\S+|\b(?:sk-|gh[pousr]_|github_pat_|xox[baprs]-)[A-Za-z0-9_-]{8,}"
    r"|\bAKIA[A-Z0-9]{16}\b|\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+"
    r"|-----BEGIN [A-Z ]*PRIVATE KEY-----|\b[a-z][a-z0-9+.-]{0,30}://[^\s/@:]+:[^\s/@]+@"
    r"|\b(?:(?:(?:access|refresh|id|auth)[_-]?)?token|password|passwd|(?:client[_-]?)?secret"
    r"|api[_-]?key|authorization)\s*[=:]\s*\S+", re.I)
FAILURES = (OSError, ValueError, TypeError, KeyError, OverflowError, RecursionError)


def encode(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


class MetadataCache:
    def __init__(self, home: Path, command: list[str], environment: dict, cwd: Path):
        self.enabled = False
        try:
            self.home = Path(home).absolute()
            self.command = list(command)
            self.environment = dict(environment)
            self.cwd = Path(cwd).absolute()
            self.secrets = tuple(value for name, value in self.environment.items()
                                 if value and SECRET_NAMES.search(name))
            with self._session(create=True) as (directory, key):
                self._context(key)
            self.enabled = True
        except FAILURES:
            pass

    def _check_file(self, details):
        if (not stat.S_ISREG(details.st_mode) or details.st_uid != os.getuid()
                or details.st_nlink != 1 or stat.S_IMODE(details.st_mode) != 0o600):
            raise OSError("Unsafe cache file")

    def _check_directory(self, descriptor, private=False):
        details = os.fstat(descriptor)
        if (not stat.S_ISDIR(details.st_mode) or details.st_uid != os.getuid()
                or details.st_mode & 0o022
                or (private and stat.S_IMODE(details.st_mode) != 0o700)):
            raise OSError("Unsafe cache directory")

    def _directory(self):
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        descriptor = os.open(self.home, flags)
        try:
            self._check_directory(descriptor)
            for part in (".local", "state", "fleet", "mcp-cache"):
                try:
                    os.mkdir(part, mode=0o700, dir_fd=descriptor)
                except FileExistsError:
                    pass
                child = os.open(part, flags, dir_fd=descriptor)
                os.close(descriptor)
                descriptor = child
                self._check_directory(descriptor, private=part == "mcp-cache")
            return descriptor
        except BaseException:
            os.close(descriptor)
            raise

    def _open(self, directory, name, flags=os.O_RDONLY):
        descriptor = os.open(name, flags | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600, dir_fd=directory)
        try:
            self._check_file(os.fstat(descriptor))
            return descriptor
        except BaseException:
            os.close(descriptor)
            raise

    def _read(self, directory, name, limit=MAX_BYTES):
        with os.fdopen(self._open(directory, name), "rb") as stream:
            if os.fstat(stream.fileno()).st_size > limit:
                raise ValueError("Oversized cache file")
            data = stream.read(limit + 1)
        if len(data) > limit:
            raise ValueError("Oversized cache file")
        return data

    def _existing(self, directory, name):
        try:
            details = os.stat(name, dir_fd=directory, follow_symlinks=False)
        except FileNotFoundError:
            return None
        self._check_file(details)
        return details

    def _atomic(self, directory, name, data):
        if len(data) > MAX_BYTES:
            raise ValueError("Oversized cache file")
        self._existing(directory, name)
        temporary = "v1-" + os.urandom(16).hex() + ".tmp"
        descriptor = self._open(directory, temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL)
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            self._existing(directory, name)
            os.replace(temporary, name, src_dir_fd=directory, dst_dir_fd=directory)
        finally:
            try:
                os.unlink(temporary, dir_fd=directory)
            except FileNotFoundError:
                pass

    @contextmanager
    def _session(self, create=False):
        directory = self._directory()
        try:
            lock = self._open(directory, "lock", os.O_RDWR | os.O_CREAT)
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                try:
                    key = self._read(directory, "key", 32)
                except FileNotFoundError:
                    if not create:
                        raise
                    key = os.urandom(32)
                    self._atomic(directory, "key", key)
                if len(key) != 32:
                    raise ValueError("Invalid cache key")
                try:
                    generation = self._read(directory, "generation", 32)
                except FileNotFoundError:
                    generation = None
                if generation is not None:
                    if len(generation) != 32:
                        raise ValueError("Invalid cache generation")
                    key = hmac.new(key, generation, hashlib.sha256).digest()
                yield directory, key
            finally:
                os.close(lock)
        finally:
            os.close(directory)

    def _identity(self, path):
        path = path.resolve()
        try:
            details = path.stat()
        except FileNotFoundError:
            return [str(path), None]
        return [str(path), details.st_dev, details.st_ino, details.st_size,
                details.st_mtime_ns, details.st_ctime_ns, details.st_mode]

    def _context(self, key, *, native=False):
        if not self.command:
            raise ValueError("Missing command")
        executable = self.command[0]
        if "/" not in executable:
            search = os.pathsep.join(str(self.cwd / part) for part in
                                     self.environment.get("PATH", os.defpath).split(os.pathsep))
            executable = shutil.which(executable, path=search)
            if executable is None:
                raise ValueError("Missing executable")
        inputs = [self._identity(self.cwd / executable)]
        for argument in self.command[1:]:
            value = argument.split("=", 1)[1] if argument.startswith("-") and "=" in argument else argument
            inputs.append(self._identity(self.cwd / value))
        directory = self.cwd.resolve()
        details = directory.stat()
        context = [self.command, {} if native else self.environment,
                   str(directory), details.st_dev, details.st_ino, inputs]
        return hmac.new(key, encode(context), hashlib.sha256).hexdigest()

    def _continuation(self, value):
        if isinstance(value, dict):
            return any(name in {"inputResponses", "requestState"} or self._continuation(item)
                       for name, item in value.items())
        if isinstance(value, list):
            return any(self._continuation(item) for item in value)
        return False

    def _request(self, request):
        if not isinstance(request, dict) or request.get("method") not in METHODS or self._continuation(request):
            return None
        params = request.get("params")
        if not isinstance(params, dict) or not isinstance(params.get("_meta"), dict):
            return None
        if params["_meta"].get("io.modelcontextprotocol/protocolVersion") != "2026-07-28":
            return None
        params = {**params, "_meta": {name: value for name, value in params["_meta"].items()
                                     if name != "progressToken"}}
        return encode([request["method"], params])

    def _private(self, value):
        if isinstance(value, str):
            return bool(CREDENTIALS.search(value)) or any(secret in value for secret in self.secrets)
        if isinstance(value, dict):
            return any(SECRET_FIELDS.fullmatch(name) or self._private(name) or self._private(item)
                       for name, item in value.items())
        if isinstance(value, list):
            return any(self._private(item) for item in value)
        return False

    def _cacheable(self, result):
        return (isinstance(result, dict) and result.get("resultType") == "complete"
                and result.get("cacheScope") == "public" and type(result.get("ttlMs")) is int
                and result["ttlMs"] > 0 and "error" not in result and not result.get("isError")
                and not self._continuation(result) and not self._private(result))

    def _name(self, key, request):
        return "v1-" + self._context(key) + "-" + hmac.new(key, request, hashlib.sha256).hexdigest() + ".json"

    def _remaining(self, payload, maximum):
        if not isinstance(payload, dict):
            return 0
        created, expires = payload.get("created"), payload.get("expires")
        if type(created) is not int or type(expires) is not int:
            return 0
        now = int(time.time() * 1000)
        if not created <= now < expires or not 0 < expires - created <= maximum:
            return 0
        return expires - now

    def _prune(self, directory, target):
        entries = []
        for name in os.listdir(directory):
            if not FILES.fullmatch(name) and not TEMP_FILES.fullmatch(name):
                continue
            details = self._existing(directory, name)
            if details is not None:
                if TEMP_FILES.fullmatch(name):
                    os.unlink(name, dir_fd=directory)
                elif name != target:
                    entries.append((details.st_mtime_ns, name))
        for _, name in sorted(entries)[:max(0, len(entries) - MAX_ENTRIES + 1)]:
            self._existing(directory, name)
            os.unlink(name, dir_fd=directory)

    def get(self, request_dict) -> dict | None:
        if not self.enabled:
            return None
        try:
            request = self._request(request_dict)
            if request is None:
                return None
            with self._session() as (directory, key):
                try:
                    payload = json.loads(self._read(directory, self._name(key, request)))
                except (FileNotFoundError, ValueError):
                    return None
                if not isinstance(payload, dict) or set(payload) != {"result", "created", "expires"}:
                    return None
                result = payload["result"]
                if not self._cacheable(result):
                    return None
                remaining = self._remaining(payload, min(result["ttlMs"], MAX_TTL))
                if remaining:
                    return {**result, "ttlMs": remaining}
        except FAILURES:
            self.enabled = False
        return None

    def put(self, request_dict, result_dict) -> None:
        if not self.enabled:
            return
        try:
            request = self._request(request_dict)
            if request is None or len(encode(result_dict)) > MAX_BYTES or not self._cacheable(result_dict):
                return
            created = int(time.time() * 1000)
            data = encode({"result": result_dict, "created": created,
                           "expires": created + min(result_dict["ttlMs"], MAX_TTL)})
            if len(data) > MAX_BYTES:
                return
            with self._session() as (directory, key):
                name = self._name(key, request)
                self._existing(directory, name)
                self._prune(directory, name)
                self._atomic(directory, name, data)
        except FAILURES:
            self.enabled = False

    def invalidate(self) -> None:
        try:
            with self._session() as (directory, key):
                prefix = "v1-" + self._context(key) + "-"
                for name in os.listdir(directory):
                    if FILES.fullmatch(name) and name.startswith(prefix):
                        self._existing(directory, name)
                        os.unlink(name, dir_fd=directory)
        except FAILURES:
            self.enabled = False
            try:
                directory = self._directory()
                try:
                    descriptor = self._open(directory, "generation", os.O_RDWR | os.O_CREAT)
                    try:
                        os.ftruncate(descriptor, 0)
                        if os.write(descriptor, os.urandom(32)) != 32:
                            raise OSError("Incomplete cache generation")
                    finally:
                        os.close(descriptor)
                finally:
                    os.close(directory)
            except FAILURES:
                pass

    def prefer_native(self) -> bool:
        if not self.enabled:
            return False
        try:
            with self._session() as (directory, key):
                try:
                    payload = json.loads(self._read(directory, "v1-" + self._context(key, native=True) + ".native"))
                except (FileNotFoundError, ValueError):
                    return False
                return (isinstance(payload, dict) and set(payload) == {"created", "expires"}
                        and self._remaining(payload, MAX_TTL) > 0)
        except FAILURES:
            self.enabled = False
        return False

    def mark_native(self) -> None:
        if not self.enabled:
            return
        try:
            with self._session() as (directory, key):
                name = "v1-" + self._context(key, native=True) + ".native"
                created = int(time.time() * 1000)
                self._existing(directory, name)
                self._prune(directory, name)
                self._atomic(directory, name, encode({"created": created, "expires": created + MAX_TTL}))
        except FAILURES:
            self.enabled = False
