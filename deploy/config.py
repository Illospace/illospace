#!/usr/bin/env python3
"""Read literal environment values. Never execute an environment file."""

import os
import sys
from pathlib import Path


def read(path: Path) -> dict[str, str]:
    values = {}
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        key, sep, value = line.partition("=")
        if not sep:
            raise ValueError("Use KEY=value in the environment file")
        value = value.strip()
        if value.startswith(("'", '"')):
            if len(value) < 2 or value[-1] != value[0]:
                raise ValueError("Use one line for each environment value")
            value = value[1:-1]
        values[key.strip()] = value
    return values


if __name__ == "__main__":
    try:
        values = read(Path(sys.argv[1]))
        print(os.environ.get(sys.argv[2], values.get(sys.argv[2], "")))
    except (OSError, ValueError):
        sys.exit("Cannot read the environment file. Use literal KEY=value lines.")
