"""docs/openapi.yaml must not drift from the routes the server actually serves.

Two directions:

* every path+method the spec documents dispatches on a live server — a
  documented operation must never answer 404 (no such route) or 405 (wrong
  method). POSTs are probed with an unknown-field body: every mutation
  handler validates its field set before acting, so a 400 proves the route
  dispatched without mutating anything. DELETEs are probed with ``?dry_run=1``
  (plan-only) and dummy ids, so a 400 "no such thing" equally proves dispatch.
* every ``/api/v1`` resource the server knows (``_API_RESOURCE_METHODS``)
  appears in the spec with exactly the methods the server accepts — and vice
  versa. ``login``/``logout`` are excluded: browser-only, deliberately out of
  contract.

Granularity note: the reverse check works at the first-path-segment level, so
a brand-new sub-path under an existing resource still needs a human to add it
to the spec; the forward check then guards it forever after.
"""

from __future__ import annotations

import json
import pathlib
import urllib.error
import urllib.request

import pytest
import yaml

from alle.api import server
from alle.state import Store
from conftest import start_test_server, stop_test_server

SPEC_PATH = pathlib.Path(__file__).resolve().parent.parent / "docs" / "openapi.yaml"
SPEC = yaml.safe_load(SPEC_PATH.read_text())

# Dummy values for path templates. They need not exist — a 400 "no such
# provider/channel/rule" still proves the route dispatched.
_PARAMS = {
    "{name}": "nordvpn",
    "{provider}": "nordvpn",
    "{channel}": "drift_probe",
    "{id}": "rs_drift_probe",
}

_METHODS = ("get", "post", "delete", "put", "patch")


def _spec_operations() -> list[tuple[str, str]]:
    return [
        (path, method)
        for path, item in SPEC["paths"].items()
        for method in _METHODS
        if method in item
    ]


@pytest.fixture
def live():
    Store.load().add_provider("nordvpn")
    httpd = server.build_server()
    thread = start_test_server(httpd)
    api = server.control_api()
    try:
        yield f"http://{api['address']}", api["secret"]
    finally:
        stop_test_server(httpd, thread)


def test_every_documented_operation_dispatches(live):
    base, secret = live
    for path, method in _spec_operations():
        url_path = path
        for template, value in _PARAMS.items():
            url_path = url_path.replace(template, value)
        url = base + url_path
        headers = {"Authorization": f"Bearer {secret}"}
        data = None
        if method == "post":
            # unknown field: rejected by every handler's field validation,
            # after dispatch and before any state change
            data = json.dumps({"__drift_probe__": True}).encode()
            headers["Content-Type"] = "application/json"
        if method == "delete":
            url += "?dry_run=1"  # plan-only: state is never touched
        if path == "/health":
            url += "?nonce=drift"
            headers = {}
        req = urllib.request.Request(
            url, method=method.upper(), headers=headers, data=data
        )
        try:
            status = urllib.request.urlopen(req, timeout=10).status
        except urllib.error.HTTPError as e:
            status = e.code
        assert status not in (404, 405), (
            f"{method.upper()} {path} answered {status}: documented in "
            f"openapi.yaml but not served (spec drift)"
        )


def test_served_resources_match_the_spec():
    spec_methods: dict[str, set[str]] = {}
    for path, method in _spec_operations():
        if not path.startswith("/api/v1/"):
            continue  # /health is documented but lives outside /api/v1
        resource = path[len("/api/v1/") :].split("/")[0]
        spec_methods.setdefault(resource, set()).add(method.upper())

    served = {
        resource: set(methods)
        for resource, methods in server._API_RESOURCE_METHODS.items()
        if resource not in ("login", "logout")  # browser-only, out of contract
    }
    assert spec_methods == served, (
        "openapi.yaml resources/methods differ from _API_RESOURCE_METHODS: "
        f"only in spec: { {k: v for k, v in spec_methods.items() if served.get(k) != v} }; "
        f"only served: { {k: v for k, v in served.items() if spec_methods.get(k) != v} }"
    )
