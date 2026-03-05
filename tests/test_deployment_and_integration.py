from __future__ import annotations

import importlib
import json
import os
import shutil
import socket
import subprocess
import sys
import time
from contextlib import contextmanager
from pathlib import Path
from urllib import error, request

import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
FRONTEND_API_TOKEN = "frontend-test-token"


def _find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        try:
            sock.bind(("127.0.0.1", 0))
        except PermissionError:
            pytest.skip("skipping integration smoke test: local socket bind is not permitted")
        return int(sock.getsockname()[1])


def _http_get(url: str, *, headers: dict[str, str] | None = None) -> tuple[int, str]:
    request_headers = headers or {}
    req = request.Request(url, headers=request_headers, method="GET")
    try:
        with request.urlopen(req, timeout=2) as response:  # noqa: S310
            return response.status, response.read().decode("utf-8")
    except error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8")


def _wait_for_backend_ready(
    base_url: str,
    *,
    timeout_seconds: int = 45,
    process: subprocess.Popen[str] | None = None,
) -> None:
    deadline = time.time() + timeout_seconds
    last_status: int | None = None
    last_body = ""

    while time.time() < deadline:
        if process is not None and process.poll() is not None:
            stdout, stderr = process.communicate(timeout=2)
            raise AssertionError(
                "backend process exited before startup completed.\n"
                f"stdout:\n{stdout}\n"
                f"stderr:\n{stderr}"
            )
        try:
            status, body = _http_get(f"{base_url}/health")
            last_status = status
            last_body = body
            if status == 200:
                return
        except OSError:
            pass
        time.sleep(0.25)

    raise AssertionError(
        "backend did not become ready before timeout.\n"
        f"last_status={last_status}\n"
        f"last_body={last_body}"
    )


def _docker_runtime_or_skip() -> str:
    docker_bin = shutil.which("docker")
    if docker_bin is None:
        pytest.skip("skipping docker smoke test: Docker CLI is not installed")

    probe = subprocess.run(
        [docker_bin, "info"],
        capture_output=True,
        text=True,
        timeout=10,
    )
    if probe.returncode != 0:
        stderr = probe.stderr.strip() or probe.stdout.strip() or "unknown docker daemon error"
        pytest.skip(f"skipping docker smoke test: Docker daemon unavailable ({stderr})")

    return docker_bin


@contextmanager
def _running_backend_server(database_path: Path):
    port = _find_free_port()
    base_url = f"http://127.0.0.1:{port}"
    env = os.environ.copy()
    env.update(
        {
            "DATABASE_PATH": str(database_path),
            "SALT": "test-salt",
            "TRUST_PROXY_HEADERS": "false",
            "FRONTEND_API_TOKEN": FRONTEND_API_TOKEN,
        }
    )

    process: subprocess.Popen[str] | None = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "uvicorn",
            "backend.main:create_app",
            "--factory",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
        ],
        cwd=str(PROJECT_ROOT),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )

    try:
        _wait_for_backend_ready(base_url, process=process)
        yield base_url
    finally:
        if process is not None and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)


def test_deployment_smoke_dockerized_backend_startup_and_events_auth() -> None:
    docker_bin = _docker_runtime_or_skip()
    image_tag = f"agentspotter-smoke:{os.getpid()}-{int(time.time())}"
    host_port = _find_free_port()

    build = subprocess.run(
        [docker_bin, "build", "-t", image_tag, "."],
        cwd=str(PROJECT_ROOT),
        capture_output=True,
        text=True,
        timeout=600,
    )
    assert build.returncode == 0, (
        "docker build failed for deployment smoke test.\n"
        f"stdout:\n{build.stdout}\n"
        f"stderr:\n{build.stderr}"
    )

    run = subprocess.run(
        [
            docker_bin,
            "run",
            "--rm",
            "-d",
            "-p",
            f"{host_port}:8000",
            "-e",
            "DATABASE_PATH=/tmp/events.db",
            "-e",
            "SALT=test-salt",
            "-e",
            "TRUST_PROXY_HEADERS=false",
            "-e",
            f"FRONTEND_API_TOKEN={FRONTEND_API_TOKEN}",
            image_tag,
        ],
        cwd=str(PROJECT_ROOT),
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert run.returncode == 0, (
        "docker run failed for deployment smoke test.\n"
        f"stdout:\n{run.stdout}\n"
        f"stderr:\n{run.stderr}"
    )
    container_id = run.stdout.strip()

    try:
        base_url = f"http://127.0.0.1:{host_port}"
        try:
            _wait_for_backend_ready(base_url)
        except AssertionError as exc:
            logs = subprocess.run(
                [docker_bin, "logs", container_id],
                capture_output=True,
                text=True,
                timeout=10,
            )
            raise AssertionError(
                f"{exc}\ncontainer logs:\n{logs.stdout}\n{logs.stderr}"
            ) from exc

        unauthorized_status, unauthorized_body = _http_get(f"{base_url}/events?limit=5")
        assert unauthorized_status == 401
        assert json.loads(unauthorized_body) == {"detail": "unauthorized"}

        authorized_status, authorized_body = _http_get(
            f"{base_url}/events?limit=5",
            headers={"Authorization": f"Bearer {FRONTEND_API_TOKEN}"},
        )
        assert authorized_status == 200
        payload = json.loads(authorized_body)
        assert set(payload.keys()) == {"refresh", "counters", "events", "has_more"}
    finally:
        subprocess.run(
            [docker_bin, "rm", "-f", container_id],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
        subprocess.run(
            [docker_bin, "rmi", "-f", image_tag],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )


def test_frontend_fetches_authenticated_events_from_running_backend(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    app = importlib.import_module("frontend.app")
    if app.requests is None:
        pytest.skip("requests dependency is unavailable; cannot run frontend/backend integration test")

    monkeypatch.setenv("FRONTEND_API_TOKEN", FRONTEND_API_TOKEN)

    with _running_backend_server(tmp_path / "events.db") as backend_url:
        agent_txt_status, _ = _http_get(f"{backend_url}/agent.txt")
        assert agent_txt_status == 200

        payload = app._fetch_events_page(
            backend_url,
            event_type="all",
            source="all",
            hide_likely_crawlers=False,
            q="",
            limit=25,
        )

    assert isinstance(payload.get("events"), list)
    assert any(event.get("event_type") == "fetch" for event in payload["events"])
    assert int(payload["counters"]["fetch"]) >= 1
