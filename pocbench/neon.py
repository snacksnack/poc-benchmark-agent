"""Minimal Neon management API client for configuration variants.

A variant is a child branch of the dataset branch with its own fixed-size
compute (autoscaling min = max). Copy-on-write means the dataset is identical
across variants; an index built on a variant is diverged pages, billed as
storage (R8), and disappears with the branch.

The API key stays in-process. Connection strings are never logged.
"""

import os
import time
from dataclasses import dataclass

import httpx

API = "https://console.neon.tech/api/v2"
OP_POLL_SECONDS = 2.0
OP_TIMEOUT_SECONDS = 600.0


@dataclass(frozen=True)
class Variant:
    branch_id: str
    endpoint_id: str
    cu: float
    name: str


class NeonError(RuntimeError):
    pass


class Neon:
    def __init__(self, api_key: str | None = None, project_id: str | None = None,
                 client: httpx.Client | None = None):
        key = api_key or os.environ.get("NEON_API_KEY")
        self.project_id = project_id or os.environ.get("NEON_PROJECT_ID")
        if not key or not self.project_id:
            raise NeonError("NEON_API_KEY and NEON_PROJECT_ID are required")
        self._http = client or httpx.Client(
            base_url=API, timeout=60,
            headers={"Authorization": f"Bearer {key}", "Accept": "application/json"},
        )

    def _req(self, method: str, path: str, **kw) -> dict:
        resp = self._http.request(method, f"/projects/{self.project_id}{path}", **kw)
        if resp.status_code >= 400:
            # The body carries Neon's message; it never echoes the key.
            raise NeonError(f"{method} {path} -> {resp.status_code}: {resp.text[:300]}")
        return resp.json() if resp.content else {}

    def wait(self, operations: list[dict]) -> None:
        pending = {op["id"] for op in operations}
        deadline = time.monotonic() + OP_TIMEOUT_SECONDS
        while pending:
            if time.monotonic() > deadline:
                raise NeonError(f"operations still pending after {OP_TIMEOUT_SECONDS}s: {pending}")
            for op_id in list(pending):
                status = self._req("GET", f"/operations/{op_id}")["operation"]["status"]
                if status == "finished":
                    pending.discard(op_id)
                elif status in ("failed", "error", "cancelled"):
                    raise NeonError(f"operation {op_id} {status}")
            if pending:
                time.sleep(OP_POLL_SECONDS)

    def default_branch_id(self) -> str:
        for b in self._req("GET", "/branches")["branches"]:
            if b.get("default"):
                return b["id"]
        raise NeonError("project has no default branch")

    def branch_id(self, name: str) -> str | None:
        for b in self._req("GET", "/branches")["branches"]:
            if b["name"] == name:
                return b["id"]
        return None

    def endpoint_id(self, branch_id: str) -> str:
        for e in self._req("GET", f"/branches/{branch_id}/endpoints")["endpoints"]:
            if e["type"] == "read_write":
                return e["id"]
        raise NeonError(f"branch {branch_id} has no read-write compute")

    def resize(self, endpoint_id: str, cu: float) -> None:
        """Fixed size (min = max), then restart so the new size is in effect
        and the cache starts empty."""
        data = self._req("PATCH", f"/endpoints/{endpoint_id}", json={"endpoint": {
            "autoscaling_limit_min_cu": cu, "autoscaling_limit_max_cu": cu}})
        self.wait(data.get("operations", []))
        self.restart(endpoint_id)

    def create_variant(self, name: str, cu: float, parent_id: str | None = None) -> Variant:
        body = {
            "branch": {"name": name, "parent_id": parent_id or self.default_branch_id()},
            "endpoints": [{
                "type": "read_write",
                "autoscaling_limit_min_cu": cu,
                "autoscaling_limit_max_cu": cu,
            }],
        }
        data = self._req("POST", "/branches", json=body)
        self.wait(data.get("operations", []))
        return Variant(data["branch"]["id"], data["endpoints"][0]["id"], cu, name)

    def delete_variant(self, variant: Variant) -> None:
        data = self._req("DELETE", f"/branches/{variant.branch_id}")
        self.wait(data.get("operations", []))

    def connection_uri(self, branch_id: str, database: str, role: str) -> str:
        params = {"branch_id": branch_id, "database_name": database, "role_name": role,
                  "pooled": "false"}
        return self._req("GET", "/connection_uri", params=params)["uri"]

    def restart(self, endpoint_id: str) -> None:
        data = self._req("POST", f"/endpoints/{endpoint_id}/restart")
        self.wait(data.get("operations", []))

    def set_suspend_timeout(self, endpoint_id: str, seconds: int) -> None:
        """-1 disables scale-to-zero (Launch and up). Callers restore it in a
        finally block: per-session only, never left as a project setting."""
        data = self._req("PATCH", f"/endpoints/{endpoint_id}",
                         json={"endpoint": {"suspend_timeout_seconds": seconds}})
        self.wait(data.get("operations", []))
