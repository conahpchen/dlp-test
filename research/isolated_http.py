"""A full Nginx runtime serving a bounded, immutable DWS document projection."""

import html
import os
import re
import shutil
import time
from pathlib import Path

import httpx

from research.isolated_mysql import RuntimeUnavailable, _docker, _inspect, _remove, locked

LABEL = "org.honeynet.runtime=isolated-http"
CONFIG = """
pid /tmp/nginx.pid;
error_log stderr warn;
events { worker_connections 32; }
http {
  access_log off;
  client_body_temp_path /tmp/client;
  proxy_temp_path /tmp/proxy;
  fastcgi_temp_path /tmp/fastcgi;
  uwsgi_temp_path /tmp/uwsgi;
  scgi_temp_path /tmp/scgi;
  server {
    listen unix:/socket/http.sock;
    root /projection;
    location = /index.html { default_type text/html; }
    location = /healthz { default_type text/plain; return 200 "honeynet-l3-http"; }
    location / { return 404; }
  }
}
"""


def _instance(run):
    image = os.environ.get("HONEYNET_L3_HTTP_IMAGE", "")
    state = os.environ.get("HONEYNET_L3_STATE_DIR", "")
    if not re.fullmatch(r"(?:docker\.io/library/|library/)?nginx@sha256:[a-f0-9]{64}", image) or not state:
        raise RuntimeUnavailable("No approved isolated HTTP runtime is available")
    if not re.fullmatch(r"[a-f0-9]{32}", run):
        raise RuntimeUnavailable("Invalid release identifier")
    directory = Path(state).resolve()
    instance = directory / run
    if len(os.fsencode(instance / "socket" / "http.sock")) >= 107:
        raise RuntimeUnavailable("Isolated runtime path exceeds the Unix socket limit")
    return image, directory, instance, "honeynet-l3-" + run


def _ready(instance, name):
    status = _inspect(name, label="isolated-http")
    if (not status or not status["State"]["Running"] or status["HostConfig"]["NetworkMode"] != "none"
            or status["HostConfig"]["PortBindings"]):
        return False
    transport = httpx.HTTPTransport(uds=str(instance / "socket" / "http.sock"))
    try:
        with httpx.Client(transport=transport, timeout=2) as client:
            health = client.get("http://local/healthz")
            page = client.head("http://local/index.html")
            return health.text == "honeynet-l3-http" and page.status_code == 200
    except (OSError, httpx.HTTPError):
        return False


def ready(run):
    try:
        _, _, instance, name = _instance(run)
        return _ready(instance, name)
    except RuntimeUnavailable:
        return False


def start(plan):
    image, directory, instance, name = _instance(plan["run_id"])
    documents = plan["documents"]
    if not 1 <= len(documents) <= 3 or sum(len(doc["content"].encode()) for doc in documents) > 20000:
        raise RuntimeUnavailable("Isolated HTTP document budget exceeded")
    projection = "<!doctype html><meta charset=utf-8><title>知识库</title>"
    projection += "<h1>知识库</h1>"
    for doc in documents:
        projection += "<h2>" + html.escape(doc["path"]) + "</h2><pre>" + html.escape(doc["content"]) + "</pre>"
    if plan.get("graph_world_version"):
        projection += f'<p data-world-version="{plan["graph_world_version"]}">DWS 版本</p>'
        for node in plan["scene"]["nodes"]:
            projection += "<h2>" + html.escape(node["title"]) + "</h2><pre>" + html.escape(node["body"]) + "</pre>"
    with locked(directory):
        if plan.get("graph_world_version") and _ready(instance, name):
            if (instance / "files" / "index.html").read_text(encoding="utf-8") != projection:
                raise RuntimeUnavailable("Isolated HTTP projection differs from the committed release")
            return
        if _docker("ps", "-aq", "--filter", "label=org.honeynet.runtime").stdout.strip():
            raise RuntimeUnavailable("Isolated runtime capacity reached")
        if instance.exists():
            shutil.rmtree(instance)
        instance.mkdir(mode=0o700)
        try:
            config = instance / "files"
            config.mkdir()
            config.chmod(0o755)
            socket = instance / "socket"
            socket.mkdir()
            socket.chmod(0o777)
            for name_in_dir, text in (("nginx.conf", CONFIG), ("index.html", projection)):
                path = config / name_in_dir
                path.write_text(text, encoding="utf-8")
                path.chmod(0o644)
            _docker("create", "--pull=never", "--name", name, "--label", LABEL,
                    "--network", "none", "--cpus", "0.5", "--memory", "64m", "--pids-limit", "32",
                    "--security-opt", "no-new-privileges", "--read-only", "--cap-drop", "ALL",
                    "--user", "101:101", "--tmpfs", "/tmp:rw,nosuid,nodev,noexec,size=8m",
                    "--mount", f"type=bind,source={config},target=/projection,readonly",
                    "--mount", f"type=bind,source={socket},target=/socket",
                    "--entrypoint", "/usr/sbin/nginx", image, "-c", "/projection/nginx.conf", "-g",
                    "daemon off;", timeout=45)
            _docker("start", name, timeout=45)
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline:
                if _ready(instance, name):
                    return
                time.sleep(.25)
            raise RuntimeUnavailable("Isolated HTTP failed readiness")
        except BaseException:
            _remove(instance, name, label="isolated-http")
            raise


def stop(run):
    _, directory, instance, name = _instance(run)
    with locked(directory):
        _remove(instance, name, label="isolated-http")
