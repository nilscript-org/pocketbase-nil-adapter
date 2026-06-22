"""System-client layer: the ONE module that performs I/O against your backend.

`SystemClient` is the protocol the edge/translation depend on, so the conformance proof can run
against `FakeSystem` with no live instance. `PocketBaseClient` talks to a real PocketBase
(https://pocketbase.io) over its REST API — records live in collections.
"""

from __future__ import annotations

from typing import Any, Protocol


class SystemError(RuntimeError):
    """A write the System rejected — its message is surfaced/logged by the edge."""


class SystemClient(Protocol):
    def create(self, target: str, doc: dict[str, Any]) -> dict[str, Any]: ...

    def list(self, target: str, filters: dict[str, Any] | None = None) -> list[dict[str, Any]]: ...

    def search(  # indexed lookup by a native domain (reference/option resolution)
        self,
        target: str,
        domain: list[list[Any]],
        *,
        fields: tuple[str, ...] | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]: ...

    def update(self, target: str, record_id: str, doc: dict[str, Any]) -> dict[str, Any]: ...

    def delete(self, target: str, record_id: str) -> None: ...

    def exists(self, target: str) -> bool: ...  # is this native target provisioned? (PROPOSE preflight)

    def schema(self, target: str) -> list[dict[str, Any]] | None: ...  # target shape (skeleton), or None

    def get(self, target: str, record_id: str) -> dict[str, Any] | None: ...  # one record (before-image)


class PocketBaseClient:
    """PocketBase backend: every NIL entity is a collection of records.

    Auth: pass an admin token, or admin_email + admin_password to authenticate on construction.
    PocketBase REST surface used:
      - POST   /api/collections/{collection}/records          (create)
      - GET    /api/collections/{collection}/records           (list, with optional filter)
      - DELETE /api/collections/{collection}/records/{id}      (delete — used by ROLLBACK)
    """

    def __init__(
        self,
        base_url: str = "http://127.0.0.1:8090",
        *,
        token: str | None = None,
        admin_email: str | None = None,
        admin_password: str | None = None,
    ) -> None:
        import httpx  # imported lazily so the conformance proof (FakeSystem) needs no httpx

        self._http = httpx.Client(base_url=base_url.rstrip("/"), timeout=20.0)
        if token:
            self._http.headers["Authorization"] = token
        elif admin_email and admin_password:
            self._http.headers["Authorization"] = self._authenticate(admin_email, admin_password)

    def _authenticate(self, identity: str, password: str) -> str:
        """Return a superuser auth token.

        PocketBase >= 0.23 authenticates superusers via the `_superusers` auth collection;
        the legacy `/api/admins/auth-with-password` path was removed. Try the current path
        first and fall back to the legacy one so this client works against either server.
        """
        creds = {"identity": identity, "password": password}
        for path in (
            "/api/collections/_superusers/auth-with-password",  # PocketBase >= 0.23
            "/api/admins/auth-with-password",                   # PocketBase < 0.23 (legacy)
        ):
            resp = self._http.post(path, json=creds)
            if resp.status_code < 400:
                return resp.json()["token"]
            if resp.status_code != 404:  # 404 = wrong PB version; keep trying. else: real failure.
                raise SystemError(f"PocketBase auth failed ({path}): {resp.text}")
        raise SystemError("PocketBase auth failed: no known auth endpoint responded")

    def create(self, target: str, doc: dict[str, Any]) -> dict[str, Any]:
        resp = self._http.post(f"/api/collections/{target}/records", json=doc)
        if resp.status_code >= 400:
            raise SystemError(f"{target}: {resp.text}")
        record = resp.json()
        # The generic edge/entity_ref keys off `name`; PocketBase's primary key is `id`.
        record.setdefault("name", record.get("id"))
        return record

    def list(self, target: str, filters: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        params: dict[str, Any] = {"perPage": 200}
        if filters:
            clause = " && ".join(f"{field}~'{value}'" for field, value in filters.items())
            params["filter"] = f"({clause})"
        resp = self._http.get(f"/api/collections/{target}/records", params=params)
        if resp.status_code >= 400:
            raise SystemError(f"{target}: {resp.text}")
        return resp.json().get("items", [])

    def search(
        self,
        target: str,
        domain: list[list[Any]],
        *,
        fields: tuple[str, ...] | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        """Indexed lookup by a native domain (used by the reference/option resolver). Translates the
        AND-of-triples domain to a PocketBase filter: `=` → `=`, `ilike` → `~` (contains)."""
        op_map = {"=": "=", "ilike": "~", "!=": "!="}
        clause = " && ".join(f"{f}{op_map.get(op, '~')}'{v}'" for f, op, v in (domain or []))
        params: dict[str, Any] = {"perPage": limit}
        if clause:
            params["filter"] = f"({clause})"
        if fields:
            params["fields"] = ",".join(fields)
        resp = self._http.get(f"/api/collections/{target}/records", params=params)
        if resp.status_code >= 400:
            raise SystemError(f"{target}: {resp.text}")
        return resp.json().get("items", [])

    def update(self, target: str, record_id: str, doc: dict[str, Any]) -> dict[str, Any]:
        resp = self._http.patch(f"/api/collections/{target}/records/{record_id}", json=doc)
        if resp.status_code >= 400:
            raise SystemError(f"{target}/{record_id}: {resp.text}")
        record = resp.json()
        record.setdefault("name", record.get("id"))
        return record

    def delete(self, target: str, record_id: str) -> None:
        resp = self._http.delete(f"/api/collections/{target}/records/{record_id}")
        if resp.status_code >= 400:
            raise SystemError(f"{target}/{record_id}: {resp.text}")

    def schema(self, target: str) -> list[dict[str, Any]] | None:
        # The native target's shape (skeleton). None means not provisioned. PocketBase returns the
        # collection definition on GET; we surface its non-system fields {name, type, required}.
        resp = self._http.get(f"/api/collections/{target}")
        if resp.status_code != 200:
            return None
        body = resp.json()
        fields = body.get("fields") or body.get("schema") or []
        out: list[dict[str, Any]] = []
        for f in fields:
            if f.get("system"):
                continue
            opts = f.get("options") or {}  # PocketBase <0.23 nests under "options"; >=0.23 flattens
            meta: dict[str, Any] = {"name": f.get("name"), "type": f.get("type"),
                                    "required": bool(f.get("required"))}
            relation = f.get("collectionId") or opts.get("collectionId")  # relation field → target collection
            if relation:
                meta["relation"] = relation
            values = f.get("values") or opts.get("values")  # select field → allowed values (B)
            if values:
                meta["options"] = [{"value": v, "label": v} for v in values]
            out.append(meta)
        return out

    def exists(self, target: str) -> bool:
        return self.schema(target) is not None

    def get(self, target: str, record_id: str) -> dict[str, Any] | None:
        resp = self._http.get(f"/api/collections/{target}/records/{record_id}")
        return resp.json() if resp.status_code == 200 else None


# Backwards-friendly alias: the scaffold refers to a generic "RealSystemClient".
RealSystemClient = PocketBaseClient


class FakeSystem:
    """In-memory backend for the conformance proof — no live instance needed."""

    def __init__(self) -> None:
        self.docs: dict[str, list[dict[str, Any]]] = {}
        self.schemas: dict[str, list[dict[str, Any]]] = {}  # optional per-target field_meta (tests)
        self._counter = 0

    def create(self, target: str, doc: dict[str, Any]) -> dict[str, Any]:
        self._counter += 1
        name = str(doc.get("name") or f"{target}-{self._counter:05d}")
        record = {**doc, "name": name, "target": target}
        self.docs.setdefault(target, []).append(record)
        return record

    def list(self, target: str, filters: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        rows = list(self.docs.get(target, []))
        for field, value in (filters or {}).items():
            rows = [r for r in rows if str(value).lower() in str(r.get(field, "")).lower()]
        return rows

    def search(
        self,
        target: str,
        domain: list[list[Any]],
        *,
        fields: tuple[str, ...] | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        rows = list(self.docs.get(target, []))
        for field, op, value in domain or []:
            if op == "ilike":
                rows = [r for r in rows if str(value).lower() in str(r.get(field, "")).lower()]
            else:  # "=" exact, with str fallback
                rows = [r for r in rows if r.get(field) == value or str(r.get(field, "")) == str(value)]
        return rows[:limit]

    def update(self, target: str, record_id: str, doc: dict[str, Any]) -> dict[str, Any]:
        for record in self.docs.get(target, []):
            if record.get("name") == record_id:
                record.update(doc)
                return record
        record = {**doc, "name": record_id, "target": target}  # upsert keeps the proof deterministic
        self.docs.setdefault(target, []).append(record)
        return record

    def delete(self, target: str, record_id: str) -> None:
        rows = self.docs.get(target, [])
        self.docs[target] = [r for r in rows if r.get("name") != record_id]

    def exists(self, target: str) -> bool:
        return True  # in-memory backend is always ready (creates targets on demand)

    def schema(self, target: str) -> list[dict[str, Any]] | None:
        return self.schemas.get(target, [])  # seeded field_meta if a test set it, else provisioned/empty

    def get(self, target: str, record_id: str) -> dict[str, Any] | None:
        return next((r for r in self.docs.get(target, []) if r.get("name") == record_id), None)
