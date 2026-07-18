"""Config loading: config.yaml (tunables) + .env (secrets).

Never hardcode paths/thresholds/secrets elsewhere in the codebase --
everything that varies goes through this module.
"""
from __future__ import annotations

import os
import threading
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv


class ConfigError(RuntimeError):
    pass


class _DotDict(dict):
    """dict that also allows attribute access, recursively."""

    def __getattr__(self, item: str) -> Any:
        try:
            value = self[item]
        except KeyError as exc:
            raise AttributeError(item) from exc
        if isinstance(value, dict) and not isinstance(value, _DotDict):
            value = _DotDict(value)
            self[item] = value
        return value

    def __setattr__(self, key: str, value: Any) -> None:
        self[key] = value


class Config:
    """Loaded configuration, resolved against a project root.

    Usage:
        cfg = Config.load()               # searches upward for config.yaml
        cfg.paths.db_path                 # -> resolved absolute Path
        cfg.raw["batch"]["size"]          # raw dict access also works
    """

    def __init__(self, raw: dict, root: Path, env_path: Path | None):
        self.raw = _DotDict(raw)
        self.root = root
        self._env_path = env_path
        self._contact_email: str | None = None

    # ------------------------------------------------------------------
    # Construction
    # ------------------------------------------------------------------
    @classmethod
    def load(cls, start: Path | None = None, config_filename: str = "config.yaml") -> "Config":
        start = start or Path.cwd()
        root = _find_project_root(start, config_filename)
        config_path = root / config_filename
        with open(config_path, "r", encoding="utf-8") as fh:
            raw = yaml.safe_load(fh) or {}

        env_path = root / ".env"
        if env_path.exists():
            load_dotenv(env_path, override=False)
        else:
            env_path = None

        return cls(raw, root, env_path)

    # ------------------------------------------------------------------
    # Attribute-style access to the raw dict, with path resolution.
    # ------------------------------------------------------------------
    def __getattr__(self, item: str) -> Any:
        return getattr(self.raw, item)

    def path(self, *keys: str) -> Path:
        """Resolve a dotted `paths.*` config value to an absolute Path,
        relative to the project root, creating parent dirs on demand.
        """
        node: Any = self.raw
        for key in keys:
            node = node[key]
        p = Path(node)
        if not p.is_absolute():
            p = (self.root / p).resolve()
        return p

    def ensure_dirs(self) -> None:
        for key in ("data_dir", "download_tmp_dir", "staging_dir", "review_dir", "logs_dir", "status_dir"):
            self.path("paths", key).mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------
    # Secrets
    # ------------------------------------------------------------------
    @property
    def contact_email(self) -> str:
        if self._contact_email is None:
            env_var = self.raw["contact_email_env"]
            value = os.environ.get(env_var, "").strip()
            if not value or value == "you@example.com":
                raise ConfigError(
                    f"Environment variable {env_var} is not set (or is still the example "
                    "value). Copy .env.example to .env and set a real contact email -- "
                    "the pipeline refuses to identify itself to remote servers without one."
                )
            self._contact_email = value
        return self._contact_email

    @property
    def user_agent(self) -> str:
        template = self.raw["user_agent_template"]
        return template.format(contact_email=self.contact_email)

    def user_agent_for(self, template_key_path: tuple[str, ...]) -> str:
        """Resolve an override UA template (e.g. politeness.edgar.user_agent_template)."""
        node: Any = self.raw
        for key in template_key_path:
            node = node[key]
        return node.format(contact_email=self.contact_email)

    def rclone_remote(self) -> str:
        return os.environ.get("RCLONE_REMOTE", self.raw["rclone"]["remote_name"])

    def rclone_root_folder(self) -> str:
        """Delivery folder on Drive. Per-machine override via RCLONE_ROOT_FOLDER
        lets each VM deliver into its own folder on the SAME account (falls
        back to rclone.root_folder in config.yaml)."""
        return os.environ.get("RCLONE_ROOT_FOLDER", self.raw["rclone"]["root_folder"])


def _find_project_root(start: Path, config_filename: str) -> Path:
    cur = start.resolve()
    for candidate in [cur, *cur.parents]:
        if (candidate / config_filename).exists():
            return candidate
    raise ConfigError(f"Could not find {config_filename} starting from {start}")


_singleton_lock = threading.Lock()
_singleton: Config | None = None


def get_config() -> Config:
    """Process-wide cached config singleton (each CLI invocation is a fresh process)."""
    global _singleton
    with _singleton_lock:
        if _singleton is None:
            _singleton = Config.load()
        return _singleton
