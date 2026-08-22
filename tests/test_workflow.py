#!/usr/bin/env python3
import json
import sys
from pathlib import Path


# JSON is valid YAML and keeps this semantic check portable to hosts without Ruby.
workflow = json.loads((Path(sys.argv[1]) / ".github/workflows/check.yml").read_text())
triggers = workflow["on"]
assert set(triggers) == {"pull_request", "push", "workflow_dispatch"}
assert triggers["pull_request"] == {}
assert triggers["push"] == {"branches": ["main"]}
assert triggers["workflow_dispatch"] == {}
assert workflow["permissions"] == {"contents": "read"}

jobs = workflow["jobs"]
assert set(jobs) == {"check", "promote"}
job = jobs["check"]
assert "permissions" not in job
assert sorted(job["strategy"]["matrix"]["os"]) == ["macos-latest", "ubuntu-latest"]
assert job["timeout-minutes"] > 0

steps = job["steps"]
checkout = "actions/checkout@d23441a48e516b6c34aea4fa41551a30e30af803"
uses = [step["uses"] for step in steps if "uses" in step]
assert uses == [checkout]
assert next(step for step in steps if step.get("uses") == checkout)["with"]["persist-credentials"] is False
assert sorted(step["run"] for step in steps if "run" in step) == [
    "bash tests/test_fleet.sh",
    "bin/fleet check",
]

promote = jobs["promote"]
assert promote["needs"] == "check"
assert promote["if"] == "github.event_name == 'push' && github.ref == 'refs/heads/main'"
assert promote["runs-on"] == "ubuntu-latest"
assert promote["permissions"] == {"contents": "write"}
assert promote["timeout-minutes"] > 0
assert promote["steps"] == [
    {
        "uses": checkout,
        "with": {"fetch-depth": 0},
    },
    {
        "name": "Promote tested commit",
        "run": 'git push origin "${GITHUB_SHA}:refs/heads/live"',
    },
]

print("Fleet workflow test passed.")
