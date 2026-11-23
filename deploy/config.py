#!/usr/bin/env python3
"""Read literal environment values. Never execute an environment file."""

import os
import re
import sys
from pathlib import Path


def parse_value(raw: str) -> str:
    value = raw.strip()
    if value.startswith(("'", '"')):
        quote = value[0]
        end = -1
        i = 1
        while i < len(value):
            if quote == '"' and value[i] == "\\":
                i += 2
                continue
            if value[i] == quote:
                end = i
                break
            i += 1
        if end < 0:
            raise ValueError("Use one line for each environment value")
        trailing = value[end + 1 :].strip()
        if trailing and not trailing.startswith("#"):
            raise ValueError("Use one line for each environment value")
        return value[1:end]
    # Docker Compose strips inline comments preceded by whitespace in unquoted values.
    return re.split(r"\s+#", value, maxsplit=1)[0].strip()


def read(path: Path) -> dict[str, str]:
    values = {}
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        key, sep, value = line.partition("=")
        if not sep:
            raise ValueError("Use KEY=value in the environment file")
        values[key.strip()] = parse_value(value)
    return values


if __name__ == "__main__":
    try:
        values = read(Path(sys.argv[1]))
        print(os.environ.get(sys.argv[2], values.get(sys.argv[2], "")))
    except (OSError, ValueError):
        sys.exit("Cannot read the environment file. Use literal KEY=value lines.")
