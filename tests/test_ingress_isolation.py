import ctypes.util
import os
import socket
import subprocess
import sys
from contextlib import contextmanager

import pytest

from tests.test_v2_loopback import listener


pytestmark = pytest.mark.skipif(sys.platform != "linux" or not ctypes.util.find_library("seccomp"),
                                reason="requires Linux libseccomp")


def test_public_listener_replies_under_linux_ingress_policy(tmp_path):
    env = dict(os.environ, HONEYNET_VISITOR_ONLY="1", HONEYNET_PG_DSN="",
               BUSINESS_STATE_FILE=str(tmp_path / "world.json"),
               RESEARCH_STATE_FILE=str(tmp_path / "research.sqlite"))
    with listener("business.app:app", env, ingress_only=True) as public:
        assert public.get("/v1/models").status_code == 200
        assert public.get("/api/research/status").status_code == 404
        assert public.get("/api/runtime").status_code == 404


def test_ingress_policy_blocks_tcp_udp_and_unix_connect(tmp_path):
    target = tmp_path / "private.sock"
    with socket.socket(socket.AF_INET) as tcp, socket.socket(socket.AF_UNIX) as private:
        tcp.bind(("127.0.0.1", 0))
        tcp.listen()
        private.bind(str(target))
        private.listen()
        port = tcp.getsockname()[1]
        script = tmp_path / "probe.py"
        script.write_text("""
import errno
import socket
import sys

def denied(operation):
    try:
        operation()
    except OSError as error:
        assert error.errno == errno.EPERM
    else:
        raise AssertionError("outbound operation unexpectedly allowed")

with socket.socket() as remote:
    denied(lambda: remote.connect(("127.0.0.1", int(sys.argv[1]))))
    denied(lambda: remote.sendto(b"probe", ("127.0.0.1", int(sys.argv[1]))))
with socket.socket(socket.AF_UNIX) as control:
    denied(lambda: control.connect(sys.argv[2]))
denied(lambda: socket.socket(socket.AF_INET, socket.SOCK_DGRAM))
""")
        subprocess.run([sys.executable, "-m", "deploy.harden.network", str(script), str(port), str(target)],
                       check=True, timeout=5)


@contextmanager
def mysql_listener(checkpoint, research):
    import time

    import pymysql

    with socket.socket() as temporary:
        temporary.bind(("127.0.0.1", 0))
        port = temporary.getsockname()[1]
    env = dict(os.environ, HONEYNET_MYSQL_PORT=str(port), HONEYNET_MYSQL_PASSWORD="synthetic-catalog",
               BUSINESS_STATE_FILE=str(checkpoint), RESEARCH_STATE_FILE=str(research))
    for name in ("RESEARCH_MODEL_API_KEY", "HONEYNET_CONTROL_TOKEN", "HONEYNET_DASHBOARD_PASSWORD"):
        env.pop(name, None)
    process = subprocess.Popen([sys.executable, "-m", "deploy.harden.network", "entries.mysql.app"], env=env)

    def connect(username="catalog_reader"):
        return pymysql.connect(host="127.0.0.1", port=port, user=username,
                               password="synthetic-catalog", connect_timeout=1, read_timeout=2)

    try:
        until = time.monotonic() + 5
        while True:
            try:
                with connect():
                    break
            except pymysql.OperationalError:
                if process.poll() is not None or time.monotonic() >= until:
                    raise
                time.sleep(.15)
        yield connect
    finally:
        process.terminate()
        process.wait(timeout=5)


def test_mysql_listener_replies_under_linux_ingress_policy(demo_seed, tmp_path, monkeypatch):
    from business.enterprise import enrich_seed
    from business.storage import persist
    from dws.world import World
    from replay.engine import Engine

    checkpoint = tmp_path / "world.sqlite"
    monkeypatch.setenv("BUSINESS_STATE_FILE", str(checkpoint))
    persist(Engine(World(enrich_seed(demo_seed))), [], [])
    with mysql_listener(checkpoint, tmp_path / "research.sqlite") as connect:
        with connect() as db, db.cursor() as cursor:
            cursor.execute("SELECT 1")
            assert cursor.fetchone() == (1,)
