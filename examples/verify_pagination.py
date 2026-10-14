"""Independent verifier: takes candidate source path; never edited by the agent."""

import runpy
import sys


def verify(path):
    get_page = runpy.run_path(path)["get_page"]
    items = [10, 20, 30, 40, 50, 60, 70]
    cases = [
        (1, 3, [10, 20, 30]),
        (2, 3, [40, 50, 60]),
        (3, 3, [70]),
        (4, 3, []),
        (1, 20, items),
        (2, 20, []),
        (1, 1, [10]),
    ]
    for page, size, expected in cases:
        actual = get_page(items, page, size)
        assert actual == expected, (page, size, expected, actual)
    assert get_page([], 1, 3) == []
    for page, size in [(0, 3), (-1, 3), (1, 0), (1, -1)]:
        try:
            get_page(items, page, size)
        except ValueError:
            pass
        else:
            raise AssertionError("Invalid pagination was accepted")
    print("PASS: 12 independent pagination checks")


if __name__ == "__main__":
    verify(sys.argv[1])
