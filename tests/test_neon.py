import httpx
import pytest

from pocbench import neon


def _client(statuses: dict[str, str]) -> neon.Neon:
    def handler(request: httpx.Request) -> httpx.Response:
        op_id = request.url.path.rsplit("/", 1)[-1]
        return httpx.Response(200, json={"operation": {"id": op_id, "status": statuses[op_id]}})
    http = httpx.Client(base_url=neon.API, transport=httpx.MockTransport(handler))
    return neon.Neon(api_key="k", project_id="p", client=http)


def test_skipped_operations_count_as_done():
    _client({"a": "finished", "b": "skipped"}).wait([{"id": "a"}, {"id": "b"}])


def test_failed_operation_raises():
    with pytest.raises(neon.NeonError, match="failed"):
        _client({"a": "failed"}).wait([{"id": "a"}])
