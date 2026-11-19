"""Forward Cowrie's JSON log from images that ship without tail."""

import os
import sys
import time
from pathlib import Path

path = Path(sys.argv[1])
while True:
    try:
        stream = path.open("rb")
    except FileNotFoundError:
        time.sleep(.5)
        continue

    with stream:
        inode = os.fstat(stream.fileno()).st_ino
        while True:
            offset = stream.tell()
            line = stream.readline()
            if line.endswith(b"\n"):
                sys.stdout.buffer.write(line)
                sys.stdout.buffer.flush()
                continue
            stream.seek(offset)
            try:
                state = path.stat()
            except FileNotFoundError:
                break
            if state.st_ino != inode or state.st_size < offset:
                break
            time.sleep(.5)
