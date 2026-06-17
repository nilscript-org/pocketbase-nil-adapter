# pocketbase-nil-adapter

[![conformance](https://github.com/nilscript-org/pocketbase-nil-adapter/actions/workflows/conformance.yml/badge.svg)](https://github.com/nilscript-org/pocketbase-nil-adapter/actions/workflows/conformance.yml)
[![Official Verified Adapter](https://img.shields.io/badge/nilscript-Official%20Verified%20Adapter-2ea44f)](https://github.com/nilscript-org/nilscript)

A **conformant NIL translation shim for [PocketBase](https://pocketbase.io/)** — the official
reference adapter for the [NIL standard](https://github.com/nilscript-org/nilscript). It speaks the
six NIL endpoints (+ `/nil/v0.1/describe`) at the edge and translates each verb into native
PocketBase records.

> Built from [`nil-adapter-template`](https://github.com/nilscript-org/nil-adapter-template).
> The edge / state / models / manifest loader are the generic, unmodified kernel output; only
> `system.py`, `translate.py`, and `compensation.py` are PocketBase-specific.

## New in 0.3.0

- **Skeleton discovery** — `GET /nil/v0.1/describe` reports `{system, verbs, targets:{name:{exists, fields[]}}}` by reading PocketBase collection definitions (`schema(target)`). Powers the kernel `handshake()`.
- **Generic `resource.*` CRUD** — `create / read / update / delete` against **any** collection, with synthesized reversibility (create→delete, update→restore before-image, delete→recreate). No per-collection verb needed — e.g. *update a coupon by its code*.
- **Identifier resolution** — `update`/`delete` accept a real record id **or** a human identifier (code/name/sku/…) resolved server-side; applies to generic and semantic verbs.
- **PROPOSE preflight** — writing to a missing collection refuses at PROPOSE (`UPSTREAM_UNAVAILABLE`), not after COMMIT.
- **Auth** — superusers via `/api/collections/_superusers/auth-with-password` (PocketBase ≥ 0.23) with legacy fallback; identity = email / username / record id.
- New system-client methods: `exists(target)`, `schema(target)`, `get(target, id)`.

## Status — conformant

- **Offline proof:** `17/17` green — every active write verb reaches `executed` against the
  in-memory `FakeSystem`, rollback-honesty holds, and `/describe` exposes a valid skeleton.
- **Live proof:** `nilscript conformance-test` → `11/11` incl. the mandatory `exposes_describe_skeleton`.
- **Conforms to:** `nilscript >= 0.3.0` (SEQRD-PC / ROLLBACK / describe / resource.*).

### Reversibility tiers (earned, not asserted)

| Verb | Tier | Compensation |
| --- | --- | --- |
| `commerce.create_product` | **REVERSIBLE** | `commerce.delete_product` |
| `commerce.record_payment` | **COMPENSABLE** | `commerce.process_refund` |
| `services.create_invoice` | **COMPENSABLE** | `commerce.process_refund` |
| everything else (coupons, clients, messages, fulfillments, proposals, …) | **IRREVERSIBLE** | — (safe default; `ROLLBACK` refuses honestly) |

`ROLLBACK` always *previews* a compensation — never a silent write. An unknown or expired
compensation token is refused.

## Run it

```
pip install -e ".[dev]"
pytest                      # offline conformance proof — 17/17 green (incl. describe skeleton)
python -m pocketbase_nil_adapter.run    # boot the shim (uvicorn)
```

## Prove it against a live shim

This adapter depends on the **published standard**, not a relative path. Install the kernel CLI and
point it at a running shim:

```
pip install nilscript                       # the kernel (CLI + conformance harness)
nilscript conformance-test --url https://your-shim.example --verb commerce.create_product
nilscript manifest validate requirements-manifest.json
```

CI (`.github/workflows/conformance.yml`) runs the offline proof + manifest validation on every push,
and an opt-in live gate (`workflow_dispatch` with a shim URL + verb) that exercises the live
conformance matrix including the rollback-honesty rows across all three tiers.

## What "conformant" means

See the standard's definition in
[`nil-adapter-template/CONTRIBUTING.md`](https://github.com/nilscript-org/nil-adapter-template/blob/main/CONTRIBUTING.md).
Conformance is the three gates — offline proof, live proof, manifest honesty — not "passes the
kernel's own test suite".

## License

Tracks the core standard — see [github.com/nilscript-org/nilscript](https://github.com/nilscript-org/nilscript).
