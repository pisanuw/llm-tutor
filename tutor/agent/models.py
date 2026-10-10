"""Single registry of Claude model ids, tiers, and prices (S21).

Every module that names a model imports from here: the actor (turn.py via
config's CLAUDE_MODEL default), the supervisors (preclassify, judge, the
mastery grader), and the cost estimator (usage). One place to upgrade a tier
or correct a price.

Pure data: no imports from the rest of tutor (config imports THIS module for
its default, so this module must not import config).
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ModelSpec:
    id: str
    tier: str                 # actor | supervisor | auditor
    in_price: float           # USD per 1M input tokens
    out_price: float          # USD per 1M output tokens
    cache_read_price: float   # USD per 1M cache-read tokens
    # USD per 1M cache-write tokens: 1.25x input for the default 5-minute
    # ephemeral cache. Billed on every cold-cache turn (plan step 2.1, A3).
    cache_write_price: float = 0.0


# The three tiers. The auditor (nightly re-judge) uses
# the actor-class model at supervisor temperature; registered now so its cost
# is priced from day one.
ACTOR = ModelSpec("claude-sonnet-4-6", "actor", 3.0, 15.0, 0.30, 3.75)
SUPERVISOR = ModelSpec("claude-haiku-4-5-20251001", "supervisor", 1.0, 5.0, 0.10, 1.25)
AUDITOR = ModelSpec("claude-sonnet-4-6", "auditor", 3.0, 15.0, 0.30, 3.75)

REGISTRY: dict[str, ModelSpec] = {spec.id: spec for spec in (ACTOR, SUPERVISOR, AUDITOR)}

# Fallback pricing for an unknown id: assume actor-class prices (conservative).
DEFAULT_PRICING: tuple[float, float, float] = (ACTOR.in_price, ACTOR.out_price, ACTOR.cache_read_price)


def pricing(model_id: str) -> tuple[float, float, float]:
    """(input, output, cache-read) USD per 1M tokens for a model id."""
    spec = REGISTRY.get(model_id)
    if spec is None:
        return DEFAULT_PRICING
    return (spec.in_price, spec.out_price, spec.cache_read_price)


def cache_write_price(model_id: str) -> float:
    """USD per 1M cache-write tokens for a model id (actor-class if unknown)."""
    spec = REGISTRY.get(model_id)
    return spec.cache_write_price if spec is not None else ACTOR.cache_write_price
