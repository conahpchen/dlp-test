"""Correlate AWS VPC Flow Logs with directly observed TCP socket endpoints."""

import argparse
import hashlib
import hmac
import ipaddress
import json
import logging
import os
import re
import sqlite3
from collections import Counter
from contextlib import closing, contextmanager
from datetime import datetime
from pathlib import Path
from time import time

INTERFACE = re.compile(r"eni-[0-9a-f]{8,17}")
HEADER = "version account-id interface-id srcaddr dstaddr srcport dstport protocol packets bytes start end action log-status"
MAX_IMPORT_LINES = 20000
MAX_EVIDENCE_ROWS = 100000


@contextmanager
def _store(path):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.touch(mode=0o600, exist_ok=True)
    with closing(sqlite3.connect(path, timeout=3)) as db, db:
        db.executescript("""
        CREATE TABLE IF NOT EXISTS vpc_flows (
            id INTEGER PRIMARY KEY, flow_id TEXT UNIQUE, interface TEXT, signature TEXT,
            started INTEGER, ended INTEGER, packets INTEGER, bytes INTEGER
        );
        CREATE INDEX IF NOT EXISTS vpc_flows_tuple ON vpc_flows(interface,signature,started,ended);
        CREATE TABLE IF NOT EXISTS socket_observations (
            id INTEGER PRIMARY KEY, session TEXT UNIQUE, interface TEXT, signature TEXT, observed INTEGER
        );
        CREATE INDEX IF NOT EXISTS socket_tuple ON socket_observations(interface,signature,observed);
        CREATE TABLE IF NOT EXISTS direct_observations (
            id INTEGER PRIMARY KEY, session TEXT, interface TEXT, signature TEXT, observed INTEGER,
            UNIQUE(session,interface,signature,observed)
        );
        CREATE INDEX IF NOT EXISTS direct_tuple ON direct_observations(interface,signature,observed);
        """)
        yield db


def _signature(key, src, dst, sport, dport, proto):
    src = ipaddress.ip_address(src).compressed
    dst = ipaddress.ip_address(dst).compressed
    if not 0 <= sport <= 65535 or not 0 <= dport <= 65535 or not 0 <= proto <= 255:
        raise ValueError("Invalid flow tuple")
    return hmac.new(key.encode(), f"{src}|{dst}|{sport}|{dport}|{proto}".encode(), hashlib.sha256).hexdigest()


def ingest(path, key, source):
    if len(key.encode()) < 32:
        raise ValueError("Set HONEYNET_FLOW_HMAC_KEY to at least 32 bytes")
    stats = Counter()
    accepted = []
    for number, line in enumerate(source, 1):
        if number > MAX_IMPORT_LINES:
            raise ValueError(f"Import exceeds {MAX_IMPORT_LINES} lines")
        cols = line.split() if len(line) <= 1024 else ()
        if not line.strip() or " ".join(cols) == HEADER:
            continue
        try:
            if len(cols) != 14 or cols[0] != "2" or not INTERFACE.fullmatch(cols[2]):
                raise ValueError("Unsupported record")
            _, account, interface, src, dst, sport, dport, proto, packets, size, start, end, action, status = cols
            if status in ("NODATA", "SKIPDATA"):
                stats["unobserved"] += 1
                continue
            if status != "OK" or action not in ("ACCEPT", "REJECT") or not re.fullmatch(r"[0-9]{12}", account):
                raise ValueError("Unsupported record")
            start, end, packets, size = (int(n) for n in (start, end, packets, size))
            if not 0 < start <= end <= 253402300799 or not 0 <= packets < 2**63 or not 0 <= size < 2**63:
                raise ValueError("Invalid time or counters")
            sport, dport, proto = int(sport), int(dport), int(proto)
            signature = _signature(key, src, dst, sport, dport, proto)
        except ValueError as exc:
            raise ValueError(f"Invalid default VPC Flow Logs record at line {number}") from exc
        if action != "ACCEPT" or proto != 6:
            stats["not_eligible"] += 1
            continue
        material = f"{account}|{interface}|{signature}|{start}|{end}|{packets}|{size}"
        identifier = hashlib.sha256(material.encode()).hexdigest()
        accepted.append((identifier, interface, signature, start, end, packets, size))
    with _store(path) as db:
        stats["inserted"] = db.executemany(
            "INSERT OR IGNORE INTO vpc_flows(flow_id,interface,signature,started,ended,packets,bytes) "
            "VALUES(?,?,?,?,?,?,?)", accepted,
        ).rowcount
        db.execute("DELETE FROM vpc_flows WHERE id <= (SELECT MAX(id)-? FROM vpc_flows)", (MAX_EVIDENCE_ROWS,))
    return {"inserted": stats["inserted"], "duplicates": len(accepted) - stats["inserted"],
            "unobserved": stats["unobserved"], "not_eligible": stats["not_eligible"]}


def observe_cowrie(path, key, interface, node, record):
    if not path or len(key.encode()) < 32 or not INTERFACE.fullmatch(interface or ""):
        return False
    try:
        session = record["session"]
        if not isinstance(session, str) or not 0 < len(session) <= 128:
            return False
        signature = _signature(key, record["src_ip"], record["dst_ip"],
                               int(record["src_port"]), int(record["dst_port"]), 6)
        stamp = datetime.fromisoformat(record["timestamp"])
        if stamp.tzinfo is None:
            return False
        observed = int(stamp.timestamp())
    except (KeyError, ValueError, TypeError):
        return False
    with _store(path) as db:
        db.execute("INSERT OR IGNORE INTO socket_observations(session,interface,signature,observed) VALUES(?,?,?,?)",
                   (f"{node}/{session}", interface, signature, observed))
        db.execute("DELETE FROM socket_observations WHERE id <= (SELECT MAX(id)-? FROM socket_observations)",
                   (MAX_EVIDENCE_ROWS,))
    return True


def observe_direct(entry, session, peer, local):
    if os.environ.get("HONEYNET_FLOW_DIRECT") != "1" or not peer or not local:
        return False
    path = os.environ.get("HONEYNET_FLOW_STATE_FILE")
    key = os.environ.get("HONEYNET_FLOW_HMAC_KEY", "")
    interface = os.environ.get(f"HONEYNET_FLOW_INTERFACE_{entry.upper()}", "")
    if not path or len(key.encode()) < 32 or not INTERFACE.fullmatch(interface):
        return False
    try:
        signature = _signature(key, peer[0], local[0], peer[1], local[1], 6)
    except (ValueError, TypeError, IndexError):
        return False
    try:
        with _store(path) as db:
            db.execute("INSERT OR IGNORE INTO direct_observations(session,interface,signature,observed) "
                       "VALUES(?,?,?,?)", (session, interface, signature, int(time())))
            db.execute("DELETE FROM direct_observations WHERE id <= (SELECT MAX(id)-? FROM direct_observations)",
                       (MAX_EVIDENCE_ROWS,))
    except (OSError, sqlite3.Error):
        logging.getLogger(__name__).warning("Direct flow observation could not be stored")
        return False
    return True


def observe_http(entry, session, request):
    # A forwarded address does not contain the original server socket and source port.
    if any(header in request.headers for header in ("forwarded", "x-forwarded-for", "x-real-ip")):
        return False
    return observe_direct(entry, session, request.scope.get("client"), request.scope.get("server"))


def matches(path, sessions):
    if not path or not sessions or not Path(path).is_file():
        return {}
    uri = Path(path).resolve().as_uri() + "?mode=ro"
    placeholders = ",".join("?" for _ in sessions)
    with closing(sqlite3.connect(uri, uri=True, timeout=3)) as db:
        tables = {name for name, in db.execute(
            "SELECT name FROM sqlite_master WHERE name IN ('vpc_flows','socket_observations','direct_observations')"
        )}
        if not {"vpc_flows", "socket_observations"} <= tables:
            return {}
        direct = (" UNION ALL SELECT session,interface,signature,observed FROM direct_observations"
                  if "direct_observations" in tables else "")
        rows = db.execute(
            "WITH sockets AS NOT MATERIALIZED (SELECT session,interface,signature,observed "
            f"FROM socket_observations{direct}) "
            "SELECT DISTINCT s.session,f.flow_id,f.interface,f.started,f.ended,f.packets,f.bytes "
            "FROM sockets s JOIN vpc_flows f ON s.interface=f.interface AND s.signature=f.signature "
            "AND s.observed BETWEEN f.started AND f.ended "
            f"WHERE s.session IN ({placeholders}) "
            "AND NOT EXISTS (SELECT 1 FROM sockets other WHERE other.session<>s.session "
            "AND other.interface=f.interface AND other.signature=f.signature "
            "AND other.observed BETWEEN f.started AND f.ended) ORDER BY f.id DESC LIMIT 800",
            tuple(sessions),
        ).fetchall()
    indexed = {}
    for session, *fields in rows:
        group = indexed.setdefault(session, [])
        if len(group) < 8:
            group.append(dict(zip(("flow_id", "interface", "started", "ended", "packets", "bytes"), fields, strict=True)))
    return indexed


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("file", type=Path)
    args = parser.parse_args()
    path = os.environ.get("HONEYNET_FLOW_STATE_FILE")
    if not path:
        parser.error("Set HONEYNET_FLOW_STATE_FILE")
    try:
        with args.file.open(encoding="utf-8") as source:
            result = ingest(path, os.environ.get("HONEYNET_FLOW_HMAC_KEY", ""), source)
    except (ValueError, OSError, sqlite3.Error) as exc:
        parser.exit(2, str(exc) + "\n")
    print(json.dumps(result))


if __name__ == "__main__":
    main()
