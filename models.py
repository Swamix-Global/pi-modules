"""Pydantic shapes for the ctx engine: usage snapshots, minification telemetry, health."""
from __future__ import annotations

from pydantic import BaseModel, Field


class Snapshot(BaseModel):
    """Usage snapshot Pi appends to every raw body (_pi_ctx::{...})."""

    usage: int = Field(ge=1, description="inference host reported context tokens")
    start: int = Field(ge=0)
    end: int = Field(ge=0)


class CondenseInfo(BaseModel):
    """Per-request minification telemetry: what was cut, what was condensed."""

    dropped_blocks: int = 0
    condensed_entries: int = 0
    summary_chars: int = 0
    duration_ms: float = 0.0
    trigger: str = "passthrough"  # passthrough | strategy | strategy+condenser


class Stats(BaseModel):
    """`/stats` counters, mutated once per request."""

    requests: int = 0
    passthrough: int = 0
    strategy_passes: int = 0
    condenses: int = 0
    dropped_blocks: int = 0
    trims: int = 0            # requests whose drop frontier moved (each one rebuilds the cache)
    rebuild_tokens: int = 0   # estimated tokens re-billed by those frontier moves
    usage_tokens: int = 0
    status: dict[str, int] = Field(default_factory=dict)
    last_condense: CondenseInfo | None = None
    active: bool = False
    model: str = ""


class Health(BaseModel):
    """`/health` view: condenser liveness plus lifetime counters."""

    ok: bool
    usage_tokens: int
    compactions: int
    active: bool
    model: str


class CodexResponse(BaseModel):
    """Minimal /codex/responses shape when mapped onto chat/completions hosts."""

    id: str
    object: str = "response"
    created_at: int
    status: str = "completed"
    model: str
    output: list[dict]
    usage: dict
