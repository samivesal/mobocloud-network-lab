"""Terminal management of the installed local settings file."""
import argparse
import fcntl
import getpass
import os
from pathlib import Path
import re
import stat
import tempfile

from .policy import validate


FIELDS = (
    ("LAB_PORT", "Web panel port", "8765"),
    ("LAB_LIVE", "Live testing (0 or 1)", "0"),
    ("LAB_SPEED", "Speed testing (0 or 1)", "0"),
    ("LAB_SCHEDULE", "Subscription tests and scheduling (0 or 1)", "0"),
    ("LAB_CHECK_URL", "Ping HTTPS URL", ""),
    ("LAB_DOWNLOAD_URL", "Download HTTPS URL", ""),
    ("LAB_UPLOAD_URL", "Upload HTTPS URL", ""),
    ("LAB_TELEGRAM", "Telegram alerts (0 or 1)", "0"),
    ("LAB_BOT_TOKEN", "Telegram bot token", ""),
    ("LAB_CHAT_ID", "Telegram chat ID", ""),
    ("LAB_SEND_SUMMARIES", "Telegram summaries (0 or 1)", "0"),
)
PRIVATE = {"LAB_BOT_TOKEN", "LAB_CHAT_ID", "LAB_CHECK_URL", "LAB_DOWNLOAD_URL", "LAB_UPLOAD_URL"}
FLAGS = {"LAB_LIVE", "LAB_SPEED", "LAB_SCHEDULE", "LAB_TELEGRAM", "LAB_SEND_SUMMARIES"}
ASSIGNMENT = re.compile(r"^\s*([A-Za-z_][A-Za-z_0-9]*)\s*=(.*)$")


def replace_text(path, text, *, expected):
    """Replace an existing file atomically without widening its permissions."""
    with path.with_name(path.name + ".lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if path.read_text() != expected:
            raise ValueError("The file changed while it was open. Reopen the editor and try again.")
        current = path.stat()
        with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, delete=False) as staged:
            pending = Path(staged.name)
            try:
                os.fchown(staged.fileno(), current.st_uid, current.st_gid)
                os.fchmod(staged.fileno(), stat.S_IMODE(current.st_mode))
                staged.write(text)
                staged.flush()
                os.fsync(staged.fileno())
                os.replace(pending, path)
            finally:
                pending.unlink(missing_ok=True)


def read_settings(text):
    values, indices = {}, {}
    lines = text.splitlines(keepends=True)
    for index, line in enumerate(lines):
        match = ASSIGNMENT.fullmatch(line.rstrip("\r\n"))
        if not match:
            continue
        key, value = match.groups()
        value = value.strip()
        if value.startswith(("'", '"')):
            double = value.startswith('"')
            quoted = re.fullmatch(r'"((?:[^"\\]|\\.)*)"' if double else r"'([^']*)'", value)
            if not quoted:
                raise ValueError("The settings file contains an incomplete or multiline quoted value.")
            value = re.sub(r'\\([\\"$`])', r'\1', quoted[1]) if double else quoted[1]
        elif (len(value) - len(value.rstrip("\\"))) % 2:
            raise ValueError("The settings file contains a multiline value the form cannot safely edit.")
        else:
            value = re.sub(r"\\(.)", r"\1", value)
        values[key], indices[key] = value, index
    return values, lines, indices


def render_settings(lines, indices, changed):
    lines = list(lines)
    for key, value in changed.items():
        quoted = re.sub(r'([\\\"$`])', r'\\\1', value)
        line = f'{key}="{quoted}"\n'
        if key in indices:
            lines[indices[key]] = line
        else:
            if lines and not lines[-1].endswith("\n"):
                lines[-1] += "\n"
            lines.append(line)
    return "".join(lines)


def normalize_value(key, value, optional):
    if value == "-" and optional:
        return ""
    if key in FLAGS or key == "LAB_PORT":
        if not value.isdecimal():
            raise ValueError("Enter a valid number.")
        parsed = int(value)
        minimum, maximum = (0, 1) if key in FLAGS else (1, 65535)
        if not minimum <= parsed <= maximum:
            raise ValueError(f"Enter a number between {minimum} and {maximum}.")
        return str(parsed)
    return value


def edit_settings(path):
    original = path.read_text()
    current, lines, indices = read_settings(original)
    changed = {}
    while True:
        print("\nMobocloud Settings")
        for index, (key, label, default) in enumerate(FIELDS, 1):
            value = changed.get(key, current.get(key, default))
            display = ("set" if value else "empty") if key in PRIVATE else value
            print(f"{index}) {label} [{display}]")
        print("s) Save and apply  |  0) Exit without saving")
        choice = input("Select: ").strip().lower()
        if choice == "0":
            return False
        if choice == "s":
            if not changed:
                return False
            try:
                validate({**current, **changed})
                replace_text(path, render_settings(lines, indices, changed), expected=original)
            except ValueError as exc:
                print(f"Settings were not saved. {exc}")
                continue
            print("Settings saved.")
            return True
        if not choice.isdecimal() or not 1 <= int(choice) <= len(FIELDS):
            print("Enter a menu option.")
            continue
        key, label, default = FIELDS[int(choice) - 1]
        prompt = f"{label} (Enter to keep current"
        prompt += ", - to clear): " if not default else "): "
        read = getpass.getpass if key in {"LAB_BOT_TOKEN", "LAB_CHAT_ID"} else input
        entry = read(prompt).strip()
        if not entry:
            continue
        try:
            value = normalize_value(key, entry, not default)
        except ValueError as exc:
            print(exc)
            continue
        if value == current.get(key, default):
            changed.pop(key, None)
        else:
            changed[key] = value


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("settings", type=Path)
    try:
        raise SystemExit(0 if edit_settings(parser.parse_args().settings) else 3)
    except (EOFError, KeyboardInterrupt):
        raise SystemExit(3)
    except (OSError, ValueError) as exc:
        raise SystemExit(f"Could not read or write settings: {exc}") from None
