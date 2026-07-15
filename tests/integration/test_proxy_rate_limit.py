from __future__ import annotations

import os
import subprocess
import uuid

import pytest


pytestmark = pytest.mark.skipif(
    "TEST_PROXY_RATE_LIMIT" not in os.environ,
    reason="requires the Compose Nginx-to-Uvicorn proxy chain",
)


REQUEST_SCRIPT = r"""
import json, sys, urllib.error, urllib.request
body = json.dumps({'account': sys.argv[1], 'password': 'incorrect-password'}).encode()
request = urllib.request.Request(
    'http://frontend/api/auth/login', data=body,
    headers={'Content-Type': 'application/json', 'X-Forwarded-For': sys.argv[2]},
)
try:
    urllib.request.urlopen(request, timeout=10)
except urllib.error.HTTPError as exc:
    print(exc.code)
else:
    print(200)
"""


def _run(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, check=True, capture_output=True, text=True, timeout=30)


def _status(container: str, account: str, spoofed_xff: str) -> int:
    result = _run(
        "docker", "exec", container, "python", "-c", REQUEST_SCRIPT,
        account, spoofed_xff,
    )
    return int(result.stdout.strip())


def test_real_nginx_proxy_separates_accounts_and_ignores_spoofed_xff() -> None:
    suffix = uuid.uuid4().hex[:10]
    account_a = f"proxy-a-{suffix}"
    account_b = f"proxy-b-{suffix}"
    clients = [f"rate-client-a-{suffix}", f"rate-client-b-{suffix}"]
    network = os.environ.get("TEST_COMPOSE_NETWORK", "platform-foundation_proxy")
    image = os.environ.get("TEST_BACKEND_IMAGE", "platform-foundation-backend:latest")
    try:
        for client in clients:
            _run(
                "docker", "run", "-d", "--name", client, "--network", network,
                "--entrypoint", "python", image,
                "-c", "import time; time.sleep(120)",
            )
        assert [_status(clients[0], account_a, f"198.51.100.{index}") for index in range(1, 9)] == [401] * 8
        assert _status(clients[0], account_a, "203.0.113.250") == 429
        assert _status(clients[1], account_b, "203.0.113.250") == 401
    finally:
        subprocess.run(
            ["docker", "rm", "-f", *clients],
            check=False, capture_output=True, text=True, timeout=30,
        )
