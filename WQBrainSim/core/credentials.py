from getpass import getpass
import os
from pathlib import Path
import sys

KEYS = ("BRAIN_USERNAME", "BRAIN_PASSWORD")
ENV_FILE = Path(__file__).resolve().parent.parent / ".env"


def read_env_file(path: Path) -> dict[str, str]:
    """Parse KEY=value lines as plain text; never expand or execute them."""
    values = {}
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        key, sep, value = line.strip().removeprefix("export ").partition("=")
        key, value = key.strip(), value.strip()
        if not sep or key not in KEYS:
            continue
        if len(value) >= 2 and value[0] in "\"'" and value[-1] == value[0]:
            value = value[1:-1]
        values[key] = value
    return values


def credentials() -> tuple[str, str]:
    values = {key: os.getenv(key) for key in KEYS if os.getenv(key)}
    if len(values) < len(KEYS) and ENV_FILE.is_file():
        values = read_env_file(ENV_FILE) | values
    username, password = values.get("BRAIN_USERNAME"), values.get("BRAIN_PASSWORD")
    if not username or not password:
        if not sys.stdin.isatty():
            raise ValueError(f"Set BRAIN_USERNAME and BRAIN_PASSWORD in the environment or {ENV_FILE}.")
        username = username or input("BRAIN username: ")
        password = password or getpass("BRAIN password: ")
    if not username or not password:
        raise ValueError("Username and password must be nonempty.")
    return username, password
