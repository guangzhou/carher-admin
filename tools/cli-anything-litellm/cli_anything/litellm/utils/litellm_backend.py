from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass
from importlib import metadata


@dataclass(frozen=True, slots=True)
class BackendError(Exception):
    message: str
    status_code: int | None = None
    response: object | None = None

    def __str__(self) -> str:
        return self.message


class LiteLLMBackend:
    @staticmethod
    def _executable(name: str) -> str | None:
        return shutil.which(name) or next(
            (
                candidate
                for candidate in ((os.path.dirname(sys.executable) + "/" + name),)
                if os.path.isfile(candidate)
            ),
            None,
        )

    def status(self) -> dict[str, object]:
        try:
            version = metadata.version("litellm")
        except metadata.PackageNotFoundError:
            version = None
        return {
            "installed": version is not None,
            "version": version,
            "executables": {name: self._executable(name) for name in ("litellm", "lite", "litellm-proxy")},
        }

    def run_native(self, executable: str, args: tuple[str, ...], timeout: float) -> dict[str, object]:
        path = self._executable(executable)
        if path is None:
            raise BackendError(f"{executable} is not installed; install LiteLLM with proxy extras")
        try:
            result = subprocess.run((path, *args), capture_output=True, text=True, timeout=timeout, check=False)
        except subprocess.TimeoutExpired as exc:
            raise BackendError(f"{executable} timed out after {timeout:g}s") from exc
        return {
            "command": [path, *args],
            "returncode": result.returncode,
            "stdout": result.stdout,
            "stderr": result.stderr,
        }

    def request(
        self,
        method: str,
        base_url: str,
        path: str,
        api_key: str | None,
        payload: dict[str, object] | None,
        timeout: float,
    ) -> object:
        body = None if payload is None else json.dumps(payload).encode("utf-8")
        headers = {"Accept": "application/json", "Content-Type": "application/json"}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        request = urllib.request.Request(
            f"{base_url.rstrip('/')}/{path.lstrip('/')}",
            data=body,
            headers=headers,
            method=method,
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                raw = response.read().decode("utf-8")
                return json.loads(raw) if raw else {"status": response.status}
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode("utf-8", errors="replace")
            try:
                response = json.loads(raw)
            except json.JSONDecodeError:
                response = raw
            raise BackendError(f"LiteLLM proxy returned HTTP {exc.code}", exc.code, response) from exc
        except (urllib.error.URLError, TimeoutError) as exc:
            raise BackendError(f"cannot reach LiteLLM proxy at {base_url}: {exc}") from exc
