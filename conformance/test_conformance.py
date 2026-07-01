"""Conformance proof for this shim — drives the edge with PROPOSE -> COMMIT per active write verb.

Runs against the in-memory FakeSystem (no live backend). With empty translation stubs every verb
FAILS (the stub raises NotImplementedError) — that is the point: the harness must detect
non-conformance. As you fill `translate.py`, verbs flip to passing.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from pocketbase_nil_adapter.compensation import COMPENSATIONS
from pocketbase_nil_adapter.edge import CapturingEmitter, create_app
from pocketbase_nil_adapter.system import FakeSystem
from pocketbase_nil_adapter.translate import WRITE_VERBS


def _env(verb: str, args: dict) -> dict:
    return {"nil": "0.1", "grant": "g", "workspace": "w", "body": {"verb": verb, "args": args}}


def _commit(client, verb_name: str) -> dict:
    args = {field: "x" for field in WRITE_VERBS[verb_name].required}
    pid = client.post("/nil/v0.1/propose", json=_env(verb_name, args)).json()["body"]["id"]
    return client.post(
        "/nil/v0.1/commit",
        json={"nil": "0.1", "grant": "g", "workspace": "w",
               "body": {"proposal": pid, "idempotency_key": pid}},
    ).json()["body"]


def test_rollback_honesty() -> None:
    """A reversible verb emits a compensation token and ROLLBACK previews (never silently writes);
    an unknown token is refused. Skips only if no verb is mapped reversible in compensation.py."""
    reversible = next((v for v in sorted(WRITE_VERBS) if v in COMPENSATIONS), None)
    if reversible is None:
        pytest.skip("no reversible verb mapped in compensation.py")
    client = TestClient(create_app(FakeSystem(), CapturingEmitter(), bearer=None), raise_server_exceptions=False)

    committed = _commit(client, reversible)
    token = committed.get("compensation", {}).get("token")
    assert token, f"{reversible} is mapped reversible but COMMIT emitted no compensation token"

    rolled = client.post("/nil/v0.1/rollback", json={"nil": "0.1", "grant": "g", "workspace": "w",
        "body": {"compensation_token": token, "reason": "owner_cancel"}}).json()["body"]
    assert rolled["outcome"] == "proposal", "ROLLBACK must PREVIEW a compensation, never silently write"

    bogus = client.post("/nil/v0.1/rollback", json={"nil": "0.1", "grant": "g", "workspace": "w",
        "body": {"compensation_token": "__no_such_token__", "reason": "owner_cancel"}}).json()["body"]
    assert bogus["outcome"] == "refusal", "an unknown compensation token must be refused, never reversed"


@pytest.mark.parametrize("verb_name", sorted(WRITE_VERBS))
def test_write_verb_reaches_executed(verb_name: str) -> None:
    verb = WRITE_VERBS[verb_name]
    args = {field: "x" for field in verb.required}  # placeholder valid-shaped args
    # Seed any DECLARED references so the prerequisite gate (which now verifies referenced records
    # exist at PROPOSE) is satisfied — the happy path presumes its premises are met.
    system = FakeSystem()
    for field_name, target in verb.references.items():
        system.docs.setdefault(target, []).append({"id": args[field_name], "name": args[field_name], "target": target})
    client = TestClient(create_app(system, CapturingEmitter(), bearer=None), raise_server_exceptions=False)

    proposed = client.post("/nil/v0.1/propose", json=_env(verb_name, args)).json()
    proposal_id = proposed.get("body", {}).get("id")
    assert proposal_id, f"{verb_name}: PROPOSE did not yield a proposal: {proposed}"

    committed = client.post(
        "/nil/v0.1/commit",
        json={"nil": "0.1", "grant": "g", "workspace": "w",
               "body": {"proposal": proposal_id, "idempotency_key": proposal_id}},
    )
    state = committed.json().get("body", {}).get("state")
    assert state == "executed", f"{verb_name}: not conformant yet (state={state}) — fill translate.py"


def test_prerequisites_refused_at_propose() -> None:
    """A create whose declared premise is unmet is refused HONESTLY at PROPOSE — never a late commit
    failure. Universal: driven by the verb's `required` + `references`, enforced on the generic spine.
      - an invoice with no party_id → refused (required),
      - an invoice referencing a non-existent client → refused (broken reference),
      - once the client exists → it proposes.
    """
    system = FakeSystem()
    client = TestClient(create_app(system, CapturingEmitter(), bearer=None), raise_server_exceptions=False)
    mk = lambda a: client.post("/nil/v0.1/propose", json=_env("resource.create", a)).json()["body"]

    no_client = mk({"target": "invoices", "amount": 10, "currency": "SAR"})
    assert no_client["outcome"] == "refusal" and no_client["field"] == "party_id"

    bad_ref = mk({"target": "invoices", "party_id": 99999, "amount": 10, "currency": "SAR"})
    assert bad_ref["outcome"] == "refusal" and "does not exist" in bad_ref["message"]

    system.docs.setdefault("clients", []).append({"id": "7", "name": "7", "target": "clients"})
    ok = mk({"target": "invoices", "party_id": "7", "amount": 10, "currency": "SAR"})
    assert ok["outcome"] == "proposal" and ok["tier"] == "HIGH"


def test_describe_exposes_skeleton() -> None:
    """MANDATORY: /nil/v0.1/describe exposes a valid skeleton — nil version, a verb catalog, and
    per native target {exists, fields}. This is the universal connect handshake the kernel uses."""
    client = TestClient(create_app(FakeSystem(), CapturingEmitter(), bearer=None), raise_server_exceptions=False)
    d = client.get("/nil/v0.1/describe").json()
    assert d.get("nil") == "0.1", "describe must report the NIL version"
    assert d.get("verbs"), "describe must list the verb catalog"
    targets = d.get("targets", {})
    assert isinstance(targets, dict) and targets, "describe must report native targets"
    for name, t in targets.items():
        assert isinstance(t, dict) and "exists" in t and "fields" in t, f"{name}: target needs {{exists, fields}}"
    assert all(t["exists"] for t in targets.values()), "FakeSystem targets are always provisioned"


def test_choice_gate_returns_candidates_on_ambiguous_match() -> None:
    # The Choice Gate: a value matching several members refuses AMBIGUOUS with up to 8 candidates that
    # obey the kernel Candidate schema ({id: str, name}) — the regression guard for the crash bug.
    sys = FakeSystem()
    sys.docs["countries"] = [{"id": "us1", "name": "United States", "code": "US"},
                             {"id": "gb1", "name": "United Kingdom", "code": "GB"}]
    sys.schemas["clients"] = [{"name": "country", "type": "relation", "relation": "countries"}]
    sys.create("clients", {"name": "Badr"})
    client = TestClient(create_app(sys, CapturingEmitter(), bearer=None), raise_server_exceptions=False)

    proposed = client.post("/nil/v0.1/propose", json=_env("resource.update",
        {"target": "clients", "id": "Badr", "data": {"country": "United"}})).json()["body"]

    assert proposed["outcome"] == "refusal" and proposed["code"] == "AMBIGUOUS"
    assert {c["id"] for c in proposed["candidates"]} == {"us1", "gb1"}
    sentences = pytest.importorskip("nilscript.sdk.sentences")
    sentences.ProposalBody.model_validate(proposed)  # would have caught the candidate-schema crash


def test_choice_gate_points_to_full_list_when_nothing_matches() -> None:
    # 0 matches → refuse INVALID_ARGS and point the agent at the full list to query and pick from.
    sys = FakeSystem()
    sys.docs["countries"] = [{"id": "qa1", "name": "Qatar", "code": "QA"}]
    sys.schemas["clients"] = [{"name": "country", "type": "relation", "relation": "countries"}]
    sys.create("clients", {"name": "Badr"})
    client = TestClient(create_app(sys, CapturingEmitter(), bearer=None), raise_server_exceptions=False)

    proposed = client.post("/nil/v0.1/propose", json=_env("resource.update",
        {"target": "clients", "id": "Badr", "data": {"country": "Atlantis"}})).json()["body"]

    assert proposed["outcome"] == "refusal" and proposed["code"] == "INVALID_ARGS"
    assert "resource.read" in proposed["message"]
    pytest.importorskip("nilscript.sdk.sentences").ProposalBody.model_validate(proposed)


def test_resource_update_resolves_selection_and_relation_from_schema() -> None:
    # The universal resolver on PocketBase: a select value resolves to its stored key (B), a relation
    # value to the referenced record id (C) — schema-driven, no per-field declaration. The same
    # capability the Odoo adapter has, now on the PB-backed live MCP path.
    sys = FakeSystem()
    sys.docs["countries"] = [{"id": "qa1", "name": "Qatar", "code": "QA"},
                             {"id": "sa1", "name": "Saudi Arabia", "code": "SA"}]
    sys.schemas["clients"] = [
        {"name": "status", "type": "select",
         "options": [{"value": "available", "label": "available"}, {"value": "sold", "label": "sold"}]},
        {"name": "country", "type": "relation", "relation": "countries"},
    ]
    sys.create("clients", {"name": "Badr"})
    client = TestClient(create_app(sys, CapturingEmitter(), bearer=None), raise_server_exceptions=False)

    pid = client.post("/nil/v0.1/propose", json=_env("resource.update", {"target": "clients",
        "id": "Badr", "data": {"status": "available", "country": "Qatar"}})).json()["body"]["id"]
    client.post("/nil/v0.1/commit", json={"nil": "0.1", "grant": "g", "workspace": "w",
        "body": {"proposal": pid, "idempotency_key": pid}})

    rec = sys.get("clients", "Badr")
    assert rec["status"] == "available"   # select value → stored key (B)
    assert rec["country"] == "qa1"        # relation value → referenced id (C)


