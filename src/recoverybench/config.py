"""Configuration management for RecoveryBench."""

from pathlib import Path

from pydantic import BaseModel, Field

from recoverybench.sandbox.config import SandboxConfig


class Settings(BaseModel):
    """Runtime configuration settings."""

    data_dir: Path = Field(default_factory=lambda: Path(".recoverybench"))
    scenarios_dir: Path = Field(default_factory=lambda: Path("scenarios"))
    db_path: Path = Field(default_factory=lambda: Path(".recoverybench/ledger.db"))
    reports_dir: Path = Field(default_factory=lambda: Path(".recoverybench/reports"))
    max_attempts: int = Field(default=5, description="Max allowed tool attempts before circuit breaker trips")
    default_timeout_seconds: float = Field(default=10.0, description="Max wall-clock seconds for a run")
    isolation_mode: str = Field(
        default="isolated",
        description="Default agent execution isolation mode: 'isolated' (Docker container) or 'trusted' (in-process)",
    )
    sandbox: SandboxConfig = Field(
        default_factory=SandboxConfig,
        description="Configuration for container sandbox execution.",
    )

    def ensure_directories(self) -> None:
        """Create necessary directories if they do not exist."""
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.scenarios_dir.mkdir(parents=True, exist_ok=True)
        self.reports_dir.mkdir(parents=True, exist_ok=True)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)


_global_settings: Settings | None = None


def get_settings() -> Settings:
    """Retrieve or initialize the active Settings singleton."""
    global _global_settings
    if _global_settings is None:
        _global_settings = Settings()
    return _global_settings


def set_settings(settings: Settings) -> None:
    """Override active settings (useful for testing)."""
    global _global_settings
    _global_settings = settings
