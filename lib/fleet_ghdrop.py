#!/usr/bin/env python3
import argparse
import fcntl
import importlib.util
import json
import os
import re
import shlex
import subprocess
import sys
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlencode


spec = importlib.util.spec_from_file_location("fleet_apply", Path(__file__).with_name("fleet_apply.py"))
fleet = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fleet)
REFERENCE = re.compile(r"https://github\.com/([A-Za-z0-9][A-Za-z0-9-]*)/([A-Za-z0-9_.-]+)/(pull|issues)/([1-9][0-9]*)")
WAIT = timedelta(days=10)


def reference_endpoint(url):
    match = REFERENCE.fullmatch(url)
    if not match or match[2] in {".", ".."}:
        raise fleet.FleetError("Expected a GitHub PR or issue URL.")
    return f"repos/{match[1]}/{match[2]}/issues/{match[4]}"


def image_url(url, service):
    if not isinstance(url, str) or not re.fullmatch(re.escape(service) + r"/f/[0-9a-f]{16}/[A-Za-z0-9._-]+\.(?i:png|jpe?g|gif|webp|svg)", url):
        raise fleet.FleetError("Only images on the configured ghdrop service can be tracked.")


def timestamp(value):
    if not isinstance(value, str):
        raise ValueError("Invalid timestamp.")
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if result.tzinfo is None:
        raise ValueError("Timestamp has no timezone.")
    return result


def command(home, arguments):
    try:
        result = subprocess.run(arguments, env=fleet.mcp.child_environment(home=home), capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired) as error:
        raise fleet.FleetError(f"Could not run {arguments[0]}.") from error
    if result.returncode:
        raise fleet.FleetError(f"{arguments[0]} failed; nothing else will be deleted.")
    return result.stdout


def github(home, endpoint):
    try:
        return json.loads(command(home, ["gh", "api", "--hostname", "github.com", endpoint]))
    except json.JSONDecodeError as error:
        raise fleet.FleetError("GitHub returned invalid JSON.") from error


@contextmanager
def registry(home):
    path = fleet.safe_target(home, ".local/state/ghdrop/images.json")
    fleet.ensure_parent(home, path)
    path.parent.chmod(0o700)
    lock = path.with_suffix(".lock")
    descriptor = os.open(lock, os.O_WRONLY | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
    with os.fdopen(descriptor, "w") as stream:
        fcntl.flock(stream, fcntl.LOCK_EX)
        config = fleet.safe_target(home, ".config/ghdrop/config.json", allow_missing=False)
        service = json.loads(fleet.read_existing(config, "{}"))["url"].rstrip("/")
        if not re.fullmatch(r"https://[A-Za-z0-9.-]+", service):
            raise fleet.FleetError("Invalid ghdrop service URL.")
        data = json.loads(fleet.read_existing(path, '{"version":1,"images":{}}'))
        if data.get("version") != 1 or not isinstance(data.get("images"), dict):
            raise fleet.FleetError("Invalid image registry; no images deleted.")
        for url, entry in data["images"].items():
            image_url(url, service)
            if not isinstance(entry, dict):
                raise fleet.FleetError("Invalid image registry; no images deleted.")
            timestamp(entry["seen_at"])
            if type(entry["keep"]) is not bool or not isinstance(entry["refs"], list):
                raise fleet.FleetError("Invalid image registry; no images deleted.")
            for ref in entry["refs"]:
                reference_endpoint(ref)
            if entry["deleted_at"] is not None:
                timestamp(entry["deleted_at"])
        yield path, service, data


def save(home, path, data):
    fleet.atomic_write(home, path, (json.dumps(data, indent=2) + "\n").encode())
    path.chmod(0o600)


def track(home, reference, urls, *, now=None):
    reference_endpoint(reference)
    now = now or datetime.now(timezone.utc)
    with registry(home) as (path, service, data):
        for url in urls:
            image_url(url, service)
            entry = data["images"].setdefault(url, {"refs": [], "seen_at": now.isoformat(), "keep": False, "deleted_at": None})
            if entry["deleted_at"]:
                raise fleet.FleetError("This image was already deleted.")
            if reference not in entry["refs"]:
                entry["refs"].append(reference)
            entry["seen_at"] = now.isoformat()
        save(home, path, data)
    print(f"Tracked {len(urls)} image(s).")


def keep(home, urls):
    with registry(home) as (path, service, data):
        for url in urls:
            image_url(url, service)
            if url not in data["images"] or data["images"][url]["deleted_at"]:
                raise fleet.FleetError("Track this image before marking it to keep.")
            data["images"][url]["keep"] = True
        save(home, path, data)
    print(f"Kept {len(urls)} image(s) permanently.")


def closed_long_enough(home, reference, now):
    item = github(home, reference_endpoint(reference))
    if not isinstance(item, dict) or item.get("html_url") != reference or not isinstance(item.get("labels"), list):
        raise fleet.FleetError("GitHub reference could not be verified.")
    if any(label["name"].casefold() == "keep-media" for label in item["labels"]):
        return False
    return item.get("state") == "closed" and bool(item.get("closed_at")) and now - timestamp(item["closed_at"]) >= WAIT


def cleanup(home, *, dry_run=False, now=None):
    now = now or datetime.now(timezone.utc)
    deleted = 0
    candidates = 0
    with registry(home) as (path, service, data):
        for url, entry in data["images"].items():
            if entry["deleted_at"] or entry["keep"] or not entry["refs"] or now - timestamp(entry["seen_at"]) < WAIT:
                continue
            if not all(closed_long_enough(home, ref, now) for ref in entry["refs"]):
                continue
            query = urlencode({"q": f'"{url}" in:body,comments', "per_page": 100})
            found = github(home, "search/issues?" + query)
            if found.get("incomplete_results") is not False or found.get("total_count") != len(found.get("items", [])):
                raise fleet.FleetError("GitHub search was incomplete; no further images deleted.")
            references = set(entry["refs"]) | {item["html_url"] for item in found["items"]}
            for reference in references:
                reference_endpoint(reference)
            if references != set(entry["refs"]):
                entry["refs"] = sorted(references)
                save(home, path, data)
            if not all(closed_long_enough(home, ref, now) for ref in sorted(references)):
                continue
            candidates += 1
            if dry_run:
                print("eligible: " + url)
                continue
            command(home, ["ghdrop", "delete", "--url", service, url])
            entry["deleted_at"] = now.isoformat()
            save(home, path, data)
            deleted += 1
            print("deleted: " + url)
    print(f"Eligible: {candidates}; deleted: {deleted}; wait: 10 days.")


def main(root, arguments=None):
    parser = argparse.ArgumentParser(prog="fleet media", description="Track PR images and delete them automatically after 10 days closed. Untracked images stay. Use keep or a keep-media label to protect important images.")
    actions = parser.add_subparsers(dest="action", required=True)
    add = actions.add_parser("track", help="Register images after posting them in a PR or issue")
    add.add_argument("reference")
    add.add_argument("urls", nargs="+")
    pin = actions.add_parser("keep", help="Permanently protect tracked images")
    pin.add_argument("urls", nargs="+")
    clean = actions.add_parser("cleanup", help="Delete eligible images without a prompt")
    clean.add_argument("--dry-run", action="store_true", help="List eligible images and remember discovered references, without deleting files")
    args = parser.parse_args(arguments)
    home = fleet.fixed_home()
    config = Path(root) / "hosts" / fleet.host_id(Path(root)) / "ghdrop-cleanup.json"
    if not config.exists():
        raise fleet.FleetError("Image cleanup is not configured on this host.")
    options = json.loads(config.read_text())
    target = options.get("ssh")
    if not isinstance(target, list) or any(not isinstance(value, str) or not value or "\x00" in value for value in target):
        raise fleet.FleetError("Invalid cleanup host configuration.")
    if target:
        remote = 'exec "$HOME/.local/bin/fleet" media ' + shlex.join(sys.argv[2:] if arguments is None else arguments)
        result = subprocess.run(["ssh", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=yes", "-o", "ConnectTimeout=10", *target, remote])
        return result.returncode
    if args.action == "track":
        track(home, args.reference, args.urls)
    elif args.action == "keep":
        keep(home, args.urls)
    else:
        cleanup(home, dry_run=args.dry_run)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main(Path(sys.argv[1]), sys.argv[2:]))
    except (fleet.FleetError, OSError, ValueError, KeyError, TypeError) as error:
        print(f"media: {error}", file=sys.stderr)
        raise SystemExit(1)
