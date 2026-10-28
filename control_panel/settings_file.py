"""Read/write a KEY=VALUE env file without disturbing comments or unknown keys."""
import os
import tempfile

SETTINGS_PATH = os.environ.get("SETTINGS_PATH", "/config/settings.env")


def parse(path=SETTINGS_PATH):
    values = {}
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, val = line.partition("=")
                val = val.strip()
                if len(val) >= 2 and val[0] == val[-1] and val[0] in "'\"":
                    val = val[1:-1]
                values[key.strip()] = val
    except FileNotFoundError:
        pass
    return values


def update(changes, path=SETTINGS_PATH):
    """Set keys in place; append new ones. Values are single-line."""
    # every separator splitlines() honours, not just \n, or a value could become its own key
    changes = {k: " ".join(str(v).splitlines()) for k, v in changes.items()}
    try:
        with open(path, encoding="utf-8") as f:
            lines = f.read().splitlines()
    except FileNotFoundError:
        lines = []
    done = set()
    for i, line in enumerate(lines):
        key = line.partition("=")[0].strip()
        if not line.lstrip().startswith("#") and key in changes:
            lines[i] = f"{key}={changes[key]}"
            done.add(key)
    lines += [f"{k}={v}" for k, v in changes.items() if k not in done]
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path))
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


if __name__ == "__main__":
    # self-check
    p = os.path.join(tempfile.mkdtemp(), "t.env")
    open(p, "w").write("# c\nA=1\nB='two words'\nFLAGS=--playlist x\n")
    assert parse(p) == {"A": "1", "B": "two words", "FLAGS": "--playlist x"}
    update({"A": "9", "C": "new\nline"}, p)
    assert parse(p) == {"A": "9", "B": "two words", "FLAGS": "--playlist x", "C": "new line"}
    assert open(p).read().startswith("# c\nA=9\n")
    update({"A": "x EVIL=1"}, p)
    update({"B": "z"}, p)
    assert "EVIL" not in parse(p)
    print("settings_file ok")
