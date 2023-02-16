import os
import sys

for stream in [sys.stdin, sys.stdout, sys.stderr]:
    print(f"{stream.name:8s} " + ("tty" if os.isatty(stream.fileno()) else "not tty"))

print(f"args: {sys.argv}")