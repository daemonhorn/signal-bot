"""Persist the Claude session id for each Signal user."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path


class SessionStore:
    def __init__(self, state_dir: Path):
        self.path = Path(state_dir) / "sessions.json"
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._data: dict[str, str] = {}
        if self.path.exists():
            try:
                loaded = json.loads(self.path.read_text())
                if isinstance(loaded, dict):
                    self._data = {str(k): str(v) for k, v in loaded.items()}
            except (OSError, json.JSONDecodeError):
                self._data = {}

    def get(self, user: str) -> str | None:
        return self._data.get(user)

    def set(self, user: str, session_id: str) -> None:
        if self._data.get(user) == session_id:
            return
        self._data[user] = session_id
        self._save()

    def clear(self, user: str) -> bool:
        existed = self._data.pop(user, None) is not None
        if existed:
            self._save()
        return existed

    def _save(self) -> None:
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=".sessions.")
        try:
            with os.fdopen(fd, "w") as fh:
                json.dump(self._data, fh, indent=2, sort_keys=True)
            os.chmod(tmp, 0o600)
            os.replace(tmp, self.path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
