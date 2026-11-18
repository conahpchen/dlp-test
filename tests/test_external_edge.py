import os
import socket
import ssl
import subprocess
import time
from pathlib import Path

import httpx
import pytest

from tests.test_v2_loopback import listener


@pytest.mark.skipif(not os.environ.get("HONEYNET_TEST_L3_HTTP_IMAGE"), reason="requires Nginx image")
def test_https_edge_public_allowlist_and_private_routes(tmp_path):
    image = os.environ["HONEYNET_TEST_L3_HTTP_IMAGE"]
    env = dict(os.environ, HONEYNET_VISITOR_ONLY="1", HONEYNET_PG_DSN="",
               BUSINESS_STATE_FILE=str(tmp_path / "world.sqlite"),
               RESEARCH_STATE_FILE=str(tmp_path / "research.sqlite"))
    for key in ("RESEARCH_MODEL_API_KEY", "HONEYNET_CONTROL_TOKEN", "HONEYNET_DASHBOARD_PASSWORD"):
        env.pop(key, None)
    with listener("business.app:app", env, ingress_only=True) as public:
        files = tmp_path / "edge"
        files.mkdir()
        cert, key = files / "tls.crt", files / "tls.key"
        subprocess.run(["openssl", "req", "-new", "-x509", "-nodes", "-days", "1",
                        "-subj", "/CN=localhost", "-addext", "subjectAltName=IP:127.0.0.1",
                        "-newkey", "rsa:2048", "-keyout", str(key), "-out", str(cert)],
                       check=True, capture_output=True, timeout=10)
        template = (Path(__file__).resolve().parents[1] / "deploy/harden/nginx-external.conf").read_text()
        with socket.socket() as temporary:
            temporary.bind(("127.0.0.1", 0))
            port = temporary.getsockname()[1]
        (files / "nginx.conf").write_text(template.replace("127.0.0.1:8443", f"127.0.0.1:{port}")
                                         .replace("127.0.0.1:8080", f"127.0.0.1:{public.base_url.port}"))
        container = subprocess.check_output([
            "docker", "run", "--detach", "--pull=never", "--network", "host", "--read-only",
            "--tmpfs", "/tmp:rw,nosuid,nodev,noexec,size=16m", "--entrypoint", "/usr/sbin/nginx",
            "--mount", f"type=bind,source={files},target=/edge,readonly",
            image, "-c", "/edge/nginx.conf", "-g", "daemon off;",
        ], text=True, timeout=45).strip()
        try:
            context = ssl.create_default_context(cafile=str(cert))
            with httpx.Client(base_url=f"https://127.0.0.1:{port}", verify=context, timeout=2) as edge:
                end = time.monotonic() + 8
                while True:
                    try:
                        assert edge.get("/").status_code == 200
                        break
                    except httpx.ConnectError:
                        if time.monotonic() >= end:
                            raise
                        time.sleep(.2)
                assert edge.get("/v1/models").status_code == 200
                completion = edge.post("/v1/chat/completions",
                                       headers={"Authorization": "Bearer sk-demo-internal-2026"},
                                       json={"model": "corp-llm-7b"})
                assert completion.status_code == 200
                assert "x-deception" not in completion.headers
                for path in ("/api/research/status", "/api/runtime", "/api/events", "/api/publications",
                             "/api/evidence/runs", "/api/audit", "/evidence", "/publications", "/healthz"):
                    assert edge.get(path, headers={"x-control-token": "test-control"}).status_code == 404
        finally:
            subprocess.run(["docker", "rm", "-f", "-v", container], check=False, capture_output=True, timeout=20)
