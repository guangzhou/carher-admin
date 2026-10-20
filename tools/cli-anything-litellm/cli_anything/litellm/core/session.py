from __future__ import annotations

import json
import os
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path
from urllib.parse import urlparse


@dataclass(frozen=True, slots=True)
class Session:
    base_url: str = "http://localhost:4000"
    model: str | None = None


def normalize_base_url(value: str) -> str:
    parsed = urlparse(value)
    if parsed.scheme not in ("http", "https") or not parsed.netloc or parsed.query or parsed.fragment:
        raise ValueError("base_url must be a full http:// or https:// URL without query or fragment")
    return value.rstrip("/")


def session_from_mapping(value: object) -> Session:
    if not isinstance(value, dict):
        raise ValueError("session must be a JSON object")
    unknown = set(value) - {"base_url", "model"}
    if unknown:
        raise ValueError(f"unsupported or secret session fields: {', '.join(sorted(unknown))}")
    base_url = value.get("base_url", "http://localhost:4000")
    model = value.get("model")
    if not isinstance(base_url, str) or (model is not None and not isinstance(model, str)):
        raise ValueError("session fields have invalid types")
    return Session(base_url=normalize_base_url(base_url), model=model or None)


def load_session(path: Path) -> Session:
    if not path.exists():
        return Session()
    try:
        return session_from_mapping(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot load session {path}: {exc}") from exc


def save_session(path: Path, session: Session) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_suffix(f"{path.suffix}.lock")
    with lock_path.open("a+", encoding="utf-8") as lock_file:
        try:
            import fcntl

            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        except (ImportError, OSError):
            fcntl = None
        temp_fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent, text=True)
        try:
            with os.fdopen(temp_fd, "w", encoding="utf-8") as output:
                json.dump(asdict(session), output, indent=2, sort_keys=True)
                output.write("\n")
                output.flush()
                os.fsync(output.fileno())
            os.chmod(temp_name, 0o600)
            os.replace(temp_name, path)
        finally:
            if os.path.exists(temp_name):
                os.unlink(temp_name)
            if fcntl is not None:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
