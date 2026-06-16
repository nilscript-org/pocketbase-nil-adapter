"""Compensation handlers (ROLLBACK / Saga) for the PocketBase shim.

Every write verb is IRREVERSIBLE until mapped here AND declared in requirements-manifest.json. A
reversal is a *governed* compensation: the edge previews it (PROPOSE) and executes it (COMMIT) like
any other action — never a silent write. Unmapped verbs REFUSE a ROLLBACK with code IRREVERSIBLE,
which is the honest default.
"""

from __future__ import annotations

from typing import Any

# verb -> {"reversibility": "REVERSIBLE" | "COMPENSABLE", "verb": "<compensating verb>"}
# Leave a verb OUT to keep it IRREVERSIBLE (the safe, zero-touch default).
COMPENSATIONS: dict[str, dict[str, Any]] = {
    # A created product has a clean inverse: delete it.
    "commerce.create_product": {"reversibility": "REVERSIBLE", "verb": "commerce.delete_product"},
    # A recorded payment has no inverse, but a refund offsets it forward.
    "commerce.record_payment": {"reversibility": "COMPENSABLE", "verb": "commerce.process_refund"},
    # An issued invoice is offset by a refund against that invoice (a credit).
    "services.create_invoice": {"reversibility": "COMPENSABLE", "verb": "commerce.process_refund"},
    # Everything else (coupons, clients, messages, fulfillments, proposals, …) is IRREVERSIBLE
    # by omission — no PocketBase-safe reversal exists, and the shim refuses to pretend one does.
}


def _entity_id(result: dict[str, Any]) -> str:
    """The committed record's id, from the EVENT result envelope (entity.id) or a raw record."""
    entity = result.get("entity") if isinstance(result, dict) else None
    if isinstance(entity, dict) and entity.get("id"):
        return str(entity["id"])
    return str(result.get("id") or result.get("name") or "")


def compensate(verb: str, result: dict[str, Any]) -> dict[str, Any]:
    """Return the compensating-proposal args for `verb` given its committed `result`.

    Raises NotImplementedError for an unmapped (IRREVERSIBLE) verb — the conformance proof then
    verifies that ROLLBACK of such an effect is REFUSED, not silently executed.
    """
    mapping = COMPENSATIONS.get(verb)
    if mapping is None:
        raise NotImplementedError(f"{verb} is IRREVERSIBLE — no compensation mapped")

    target = mapping["verb"]
    if target == "commerce.delete_product":
        return {"product_id": _entity_id(result)}
    if target == "commerce.process_refund":
        # Offset the committed effect by refunding against the record it produced.
        return {"refund_target": _entity_id(result)}
    raise NotImplementedError(f"map the compensation args for {verb} -> {target}")
