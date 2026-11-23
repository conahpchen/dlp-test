"""Approved Docker runtime for an isolated, synthetic MySQL instance."""

import json
import os
import re
import secrets
import shutil
import subprocess
import time
from contextlib import contextmanager
from pathlib import Path


class RuntimeUnavailable(RuntimeError):
    pass


IMAGE_PATTERN = r"(?:docker\.io/library/|library/)?mysql@sha256:[a-f0-9]{64}"
MANAGED_LABEL = "org.honeynet.runtime=isolated-mysql"
SEED_SQL = """
CREATE TABLE collab_core.catalog (
    id INT PRIMARY KEY, name VARCHAR(512) NOT NULL, fact_id VARCHAR(80) NOT NULL
);
CREATE TABLE collab_core.projection (world_version BIGINT NOT NULL);
CREATE TABLE collab_core.catalog_records (
    id INT PRIMARY KEY, catalog_id INT NOT NULL, state VARCHAR(24) NOT NULL,
    FOREIGN KEY (catalog_id) REFERENCES collab_core.catalog(id)
);
INSERT INTO collab_core.catalog_records VALUES (1,1,'ready'),(2,1,'archived');
"""


def seed_sql(plan):
    documents = plan["documents"]
    version = int(plan.get("graph_world_version") or plan["world_version"])
    if not 1 <= len(documents) <= 3 or version < 1:
        raise RuntimeUnavailable("Invalid isolated DWS projection")
    values = []
    for index, document in enumerate(documents, 1):
        path = document["path"].encode()
        fact = document["fact_id"].encode()
        if not 1 <= len(path) <= 512 or not 1 <= len(fact) <= 80:
            raise RuntimeUnavailable("DWS projection exceeds the isolated schema budget")
        values.append(f"( {index}, CONVERT(X'{path.hex()}' USING utf8mb4), "
                      f"CONVERT(X'{fact.hex()}' USING utf8mb4))")
    schema, records = SEED_SQL.rsplit("INSERT INTO", 1)
    content = (schema + f"INSERT INTO collab_core.projection VALUES ({version});\n"
               + "INSERT INTO collab_core.catalog VALUES " + ",".join(values) + ";\n"
               + "INSERT INTO" + records)
    if plan.get("graph_world_version"):
        rows = []
        for node in plan["scene"]["nodes"]:
            texts = (node["id"], node["title"], node["body"])
            rows.append("(" + ",".join(f"CONVERT(X'{value.encode().hex()}' USING utf8mb4)" for value in texts) + ")")
        content += ("CREATE TABLE collab_core.generated_nodes (node_id VARCHAR(32) PRIMARY KEY, "
                    "title VARCHAR(120), body TEXT);\nINSERT INTO collab_core.generated_nodes VALUES "
                    + ",".join(rows) + ";\n")
    return content


def _docker(*args, timeout=15, check=True):
    try:
        return subprocess.run(["docker", *args], capture_output=True, text=True, timeout=timeout, check=check)
    except (OSError, subprocess.SubprocessError) as exc:
        raise RuntimeUnavailable("Docker operation failed") from exc


def configuration():
    image = os.environ.get("HONEYNET_L3_MYSQL_IMAGE", "")
    path = os.environ.get("HONEYNET_L3_STATE_DIR", "")
    if not re.fullmatch(IMAGE_PATTERN, image) or not path:
        raise RuntimeUnavailable("No approved isolated runtime is available")
    directory = Path(path).resolve()
    if len(os.fsencode(directory / ("0" * 32) / "socket" / "mysql.sock")) >= 107:
        raise RuntimeUnavailable("Isolated runtime path exceeds the Unix socket limit")
    return image, directory


@contextmanager
def locked(directory):
    import fcntl

    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    if directory.stat().st_mode & 0o077:
        raise RuntimeUnavailable("Isolated runtime state directory must be private")
    with (directory / ".lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield


def _instance(run):
    if not re.fullmatch(r"[a-f0-9]{32}", run):
        raise RuntimeUnavailable("Invalid release identifier")
    image, directory = configuration()
    return image, directory, directory / run, "honeynet-l3-" + run


def _inspect(name, *, label="isolated-mysql"):
    result = _docker("container", "inspect", name, check=False)
    if result.returncode:
        if "No such" in result.stderr:
            return None
        raise RuntimeUnavailable("Cannot inspect isolated runtime")
    row = json.loads(result.stdout)[0]
    labels = row["Config"]["Labels"] or {}
    if labels.get("org.honeynet.runtime") != label:
        raise RuntimeUnavailable("Container ownership does not match")
    return row


def _ready(instance, name):
    import pymysql

    status = _inspect(name)
    if (not status or not status["State"]["Running"] or status["HostConfig"]["NetworkMode"] != "none"
            or status["HostConfig"]["PortBindings"]):
        return False
    # The official entrypoint runs a transient SQL server during initialization.
    # The container's main process must have switched to mysqld before publication.
    process = _docker("exec", name, "/bin/cat", "/proc/1/comm", check=False)
    if process.returncode or process.stdout.strip() != "mysqld":
        return False
    try:
        password = (instance / "catalog.password").read_text()
        with pymysql.connect(unix_socket=str(instance / "socket" / "mysql.sock"), user="catalog_reader",
                             password=password, database="collab_core", connect_timeout=1,
                             read_timeout=2, write_timeout=2) as db, db.cursor() as cursor:
            cursor.execute("SELECT EXISTS(SELECT 1 FROM catalog c JOIN catalog_records r "
                           "ON c.id=r.catalog_id WHERE c.id=1 AND r.id=1)")
            return cursor.fetchone() == (1,)
    except (OSError, pymysql.MySQLError):
        return False


def ready(run):
    try:
        _, _, instance, name = _instance(run)
        return _ready(instance, name)
    except RuntimeUnavailable:
        return False


def start(plan):
    run = plan["run_id"]
    contents = seed_sql(plan)
    image, directory, instance, name = _instance(run)
    with locked(directory):
        if _inspect(name):
            if (plan.get("graph_world_version") and _ready(instance, name)
                    and (instance / "seed.sql").read_text(encoding="utf-8") == contents):
                return
            raise RuntimeUnavailable("This isolated release already has an instance")
        existing = _docker("ps", "-aq", "--filter", "label=org.honeynet.runtime").stdout.strip()
        if existing:
            raise RuntimeUnavailable("Isolated runtime capacity reached")
        if instance.exists():
            shutil.rmtree(instance)
        instance.mkdir(mode=0o700)
        try:
            socket_dir = instance / "socket"
            socket_dir.mkdir()
            socket_dir.chmod(0o777)  # Container mysql UID; private parent restricts host access.
            root_password, catalog_password = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
            (instance / "catalog.password").write_text(catalog_password)
            (instance / "catalog.password").chmod(0o600)
            env = instance / "runtime.env"
            env.write_text(
                f"MYSQL_ROOT_PASSWORD={root_password}\nMYSQL_USER=catalog_reader\n"
                f"MYSQL_PASSWORD={catalog_password}\nMYSQL_DATABASE=collab_core\n"
            )
            env.chmod(0o600)
            seed = instance / "seed.sql"
            seed.write_text(contents)
            seed.chmod(0o644)
            _docker("create", "--pull=never", "--name", name, "--label", MANAGED_LABEL,
                    "--network", "none", "--cpus", "1", "--memory", "512m", "--pids-limit", "128",
                    "--security-opt", "no-new-privileges", "--read-only",
                    "--cap-drop", "ALL", "--cap-add", "CHOWN", "--cap-add", "DAC_OVERRIDE",
                    "--cap-add", "SETGID", "--cap-add", "SETUID",
                    "--tmpfs", "/var/lib/mysql:rw,nosuid,nodev,noexec,size=512m",
                    "--tmpfs", "/tmp:rw,nosuid,nodev,noexec,size=64m",
                    "--env-file", str(env),
                    "--mount", f"type=bind,source={socket_dir},target=/socket",
                    "--mount", f"type=bind,source={seed},target=/docker-entrypoint-initdb.d/10-seed.sql,readonly",
                    image, "--skip-networking", "--mysqlx=OFF", "--socket=/socket/mysql.sock",
                    "--pid-file=/tmp/mysqld.pid", "--innodb-buffer-pool-size=64M", timeout=45)
            env.unlink()
            _docker("start", name, timeout=45)
            deadline = time.monotonic() + 60
            while time.monotonic() < deadline:
                if _ready(instance, name):
                    return
                status = _inspect(name)
                if status and not status["State"]["Running"]:
                    break
                time.sleep(.5)
            raise RuntimeUnavailable("Isolated MySQL failed readiness")
        except BaseException:
            _remove(instance, name)
            raise


def _remove(instance, name, *, label="isolated-mysql"):
    if _inspect(name, label=label):
        _docker("rm", "--force", "--volumes", name)
    if instance.exists():
        shutil.rmtree(instance)


def stop(run):
    _, directory, instance, name = _instance(run)
    with locked(directory):
        _remove(instance, name)
