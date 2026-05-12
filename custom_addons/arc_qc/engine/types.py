"""Core types for the ARC QC engine. Zero Odoo imports."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Literal


class Severity(str, Enum):
    CRITICAL = 'critical'
    HIGH = 'high'
    MEDIUM = 'medium'
    LOW = 'low'


class Verdict(str, Enum):
    SHIP = 'ship'
    CONDITIONAL_SHIP = 'conditional_ship'
    BLOCK = 'block'


@dataclass(slots=True)
class Finding:
    severity: Severity
    phase: str
    code: str
    message: str
    file_path: str | None = None
    line_number: int | None = None
    field_name: str | None = None
    expected: str | None = None
    actual: str | None = None
    spec_ref: str | None = None
    game_id: str = ''


@dataclass(slots=True)
class GameInfo:
    """Discovered game directory."""
    game_id: str
    path: str
    model_dirs: list[ModelDirInfo] = field(default_factory=list)


@dataclass(slots=True)
class ModelDirInfo:
    """Discovered model directory within a game."""
    model_name: str
    path: str
    game_id: str
    has_runs: bool = False
    has_steps: bool = False


@dataclass(slots=True)
class QcConfig:
    """Configuration for a QC run."""
    expected_models: list[str] | None = None
    expected_runs_per_model: int = 3
    max_steps: int = 200
    # If set, skip content safety scan (for speed during dev)
    skip_content_safety: bool = False
    # If set, skip smell tests
    skip_smell_tests: bool = False


@dataclass(slots=True)
class GameResult:
    game_id: str
    game_path: str
    verdict: Verdict
    models_found: int = 0
    models_expected: int = 4
    runs_checked: int = 0
    steps_checked: int = 0
    critical_count: int = 0
    high_count: int = 0
    medium_count: int = 0
    low_count: int = 0
    findings: list[Finding] = field(default_factory=list)
    model_dirs: list[ModelDirInfo] = field(default_factory=list)


@dataclass(slots=True)
class QcResult:
    verdict: Verdict
    findings: list[Finding]
    game_results: list[GameResult] = field(default_factory=list)
    games_checked: int = 0
    models_checked: int = 0
    runs_checked: int = 0
    steps_checked: int = 0
    duration_seconds: float = 0.0
    critical_count: int = 0
    high_count: int = 0
    medium_count: int = 0
    low_count: int = 0
