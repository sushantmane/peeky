"""Independent supplied C verifier: argv[1] is a prepared launch descriptor.

The controller copies/checks this file; candidates never edit these expectations.
Returns normally after checking every case so the trusted completion wrapper can
record completion. Uses no third-party packages or model calls.
"""

import json
import subprocess
import sys
from pathlib import Path

spec = json.loads(Path(sys.argv[1]).read_text())
assert spec["schema"] == 1 and spec["language"] == "c"
command = spec["command"]
assert isinstance(command, list) and command
cases = [
    (1, 3, 8),
    (2, 3, 8),
    (3, 3, 8),
    (4, 3, 8),
    (1, 1, 8),
    (4, 1, 8),
    (1, 5, 2),
    (2, 5, 2),
    (1, 3, 0),
    (1, 5, 12),
    (2, 5, 12),
    (3, 5, 12),
]
for page, size, total in cases:
    result = subprocess.run(
        [*command, str(page), str(size), str(total)],
        capture_output=True,
        text=True,
        timeout=5,
        check=False,
        env=spec["environment"],
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.startswith("PAGE:"), result.stdout
    actual = json.loads(result.stdout.removeprefix("PAGE:").strip())
    all_items = [(index + 1) * 10 for index in range(total)]
    expected = all_items[(page - 1) * size : page * size]
    assert actual == expected, (page, size, total, actual, expected)
print(f"PASS: {len(cases)} independent C pagination cases")
invalid_cases = [
    ["999999999999999999999999999999999999"],
    ["129"],
    ["1", "129"],
    ["1", "3", "129"],
    ["0"],
    ["1", "0"],
    ["1", "3", "-1"],
    ["1junk"],
    [""],
    ["1", "3", "8", "extra"],
]
for arguments in invalid_cases:
    result = subprocess.run(
        [*command, *arguments],
        capture_output=True,
        text=True,
        timeout=5,
        check=False,
        env=spec["environment"],
    )
    assert result.returncode == 2, (arguments, result.returncode, result.stderr)
    assert "PAGE:" not in result.stdout, (arguments, result.stdout)
print(f"PASS: {len(invalid_cases)} invalid argument cases rejected")
