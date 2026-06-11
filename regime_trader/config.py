"""Centralized configuration: YAML tunables + environment credentials."""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG_PATH = PROJECT_ROOT / "config" / "config.yaml"


class ConfigSection:
    """Dot-access wrapper over a nested dict. Read-only by convention."""

    def __init__(self, data: dict):
        self._data = data

    def __getattr__(self, key: str) -> Any:
        if key.startswith("_"):
            raise AttributeError(key)
        try:
            value = self._data[key]
        except KeyError:
            raise AttributeError(f"missing config key: {key}") from None
        if isinstance(value, dict):
            return ConfigSection(value)
        return value

    def get(self, key: str, default: Any = None) -> Any:
        value = self._data.get(key, default)
        if isinstance(value, dict):
            return ConfigSection(value)
        return value

    def as_dict(self) -> dict:
        return self._data

    def __contains__(self, key: str) -> bool:
        return key in self._data


class Config(ConfigSection):
    """Full system config. Credentials come from the environment only."""

    def __init__(self, path: str | Path | None = None):
        load_dotenv(PROJECT_ROOT / ".env")
        path = Path(path) if path else DEFAULT_CONFIG_PATH
        with open(path, "r", encoding="utf-8") as fh:
            data = yaml.safe_load(fh)
        super().__init__(data)
        self._path = path

    # --- credentials (never stored in config files) ---
    @property
    def alpaca_api_key(self) -> str:
        return _require_env("ALPACA_API_KEY")

    @property
    def alpaca_secret_key(self) -> str:
        return _require_env("ALPACA_SECRET_KEY")

    @property
    def alpaca_paper(self) -> bool:
        return os.environ.get("ALPACA_PAPER", "true").strip().lower() != "false"

    @property
    def exa_api_key(self) -> str | None:
        return os.environ.get("EXA_API_KEY")

    @property
    def firecrawl_api_key(self) -> str | None:
        return os.environ.get("FIRECRAWL_API_KEY")

    def resolve_path(self, rel: str) -> Path:
        p = Path(rel)
        return p if p.is_absolute() else PROJECT_ROOT / p


def _require_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(
            f"{name} is not set. Put it in {PROJECT_ROOT / '.env'} or the environment. "
            "Credentials are never read from config files."
        )
    return value


_config: Config | None = None


def get_config(path: str | Path | None = None, reload: bool = False) -> Config:
    global _config
    if _config is None or reload or path is not None:
        _config = Config(path)
    return _config
