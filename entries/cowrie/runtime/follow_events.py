"""Follow complete Cowrie JSON records across rotation."""

import json
import os
import sys
import time
from pathlib import Path


def follow(path, cursor=None):
    identity = None
    offset = 0
    if cursor and cursor.exists():
        dev, ino, offset = json.loads(cursor.read_text())
        identity = (dev, ino)

    while True:
        try:
            stream = path.open("rb")
        except FileNotFoundError:
            time.sleep(.5)
            continue

        with stream:
            opened = os.fstat(stream.fileno())
            current = (opened.st_dev, opened.st_ino)
            if identity != current or opened.st_size < offset:
                identity, offset = current, 0
            stream.seek(offset)
            while True:
                line = stream.readline()
                if line.endswith(b"\n"):
                    yield line
                    offset = stream.tell()
                    if cursor:
                        pending = cursor.with_suffix(".tmp")
                        pending.write_text(json.dumps([*identity, offset]))
                        pending.replace(cursor)
                    continue
                stream.seek(offset)
                try:
                    state = path.stat()
                except FileNotFoundError:
                    break
                if (state.st_dev, state.st_ino) != identity or state.st_size < offset:
                    break
                time.sleep(.5)


if __name__ == "__main__":
    for line in follow(Path(sys.argv[1])):
        sys.stdout.buffer.write(line)
        sys.stdout.buffer.flush()
