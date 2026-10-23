"""`--set-at LINE@all` on real C builds (#265): a change at every stop of a line, each made
and checked as one `--set-at LINE@N` is. Tenth evaluation, debounce: testing "reset the
count whenever the input is back at the accepted level" took six hand-computed hits."""

import json
import os

import pytest

from peeky.cli import main

# The tenth evaluation's debounce case, as it was given (debounce/debounce.c).
DEBOUNCE = r"""/* debounce: turns a push-button's noisy contact signal into clean presses
 * and releases. The signal has one character per millisecond sample: '1'
 * (contact closed) or '0' (open). The button starts released. A new level
 * is accepted only once it has held for THRESHOLD samples in a row; a
 * shorter blip is contact bounce, and is ignored. Prints the number of
 * accepted changes, then each one as P@ms (press) or R@ms (release), at
 * the sample that completed it. */
#include <stdio.h>
#include <stdlib.h>

struct debounce {
    int stable;    /* the accepted level: 0 released, 1 pressed */
    int candidate; /* the level the input is moving to */
    int count;     /* samples in a row at the candidate level */
    int threshold; /* samples in a row needed to accept a change */
};

struct events {
    char text[8192];
    size_t used;
};

static void record(struct events *ev, char kind, int ms) {
    size_t room = sizeof ev->text - ev->used;
    int n = snprintf(ev->text + ev->used, room, "%s%c@%d", ev->used ? "," : "", kind, ms);
    if (n < 0 || (size_t)n >= room) {
        fprintf(stderr, "debounce: too many changes\n");
        exit(1);
    }
    ev->used += (size_t)n;
}

/* Takes one sample; returns 1 if it completes a change of the stable level. */
static int settle(struct debounce *db, int level) {
    if (level == db->stable) {
        return 0; /* the input agrees with the accepted level */
    }
    if (level != db->candidate) {
        db->candidate = level;
        db->count = 0;
    }
    db->count++;
    if (db->count < db->threshold) {
        return 0;
    }
    db->stable = level;
    db->count = 0;
    return 1;
}

/* Feeds sample ms to the debouncer, and records the change it completes. */
static int feed(struct debounce *db, char sample, int ms, struct events *ev) {
    if (sample != '0' && sample != '1') {
        fprintf(stderr, "debounce: bad sample '%c' at %d ms\n", sample, ms);
        exit(2);
    }
    if (!settle(db, sample - '0')) {
        return 0;
    }
    record(ev, db->stable ? 'P' : 'R', ms);
    return 1;
}

int main(int argc, char **argv) {
    if (argc != 3) {
        fprintf(stderr, "usage: debounce THRESHOLD SIGNAL\n");
        return 2;
    }
    char *end;
    long threshold = strtol(argv[1], &end, 10);
    if (*argv[1] == '\0' || *end != '\0' || threshold < 1 || threshold > 1000) {
        fprintf(stderr, "debounce: bad threshold: %s\n", argv[1]);
        return 2;
    }
    static struct events ev;
    struct debounce db = {0, 0, 0, (int)threshold};
    const char *signal = argv[2];
    int changes = 0;
    for (int ms = 0; signal[ms] != '\0'; ms++) {
        changes += feed(&db, signal[ms], ms, &ev);
    }
    printf("DEBOUNCE:%d:%s\n", changes, ev.text);
    return 0;
}
"""
# The issue's input, its buggy output and the expected one.
ARGS = ("3", "00101101111110100100001101100000")
BUGGY, EXPECTED = "DEBOUNCE:4:P@5,R@15,P@23,R@28", "DEBOUNCE:2:P@9,R@20"
# `settle`'s `return 0;` when the sample agrees with the accepted level; `main`'s call.
AGREES, CALL = 36, 80

# The same debouncer with its loop in `main`, which owns the struct: the line reached only
# when the sample agrees with the accepted level is in the frame that owns `db.count`.
INLINE = r"""#include <stdio.h>

struct debounce {
    int stable;
    int candidate;
    int count;
    int threshold;
};

int main(int argc, char **argv) {
    struct debounce db = {0, 0, 0, 3};
    const char *signal = argc > 1 ? argv[1] : "";
    int quiet = 0;
    printf("DEBOUNCE:");
    for (int ms = 0; signal[ms] != '\0'; ms++) {
        int level = signal[ms] - '0';
        if (level == db.stable) {
            quiet++; /* AGREES */
            continue;
        }
        if (level != db.candidate) {
            db.candidate = level;
            db.count = 0;
        }
        db.count++;
        if (db.count >= db.threshold) {
            db.stable = level;
            db.count = 0;
            printf("%c@%d,", level ? 'P' : 'R', ms);
        }
    }
    printf(" quiet %d\n", quiet);
    return 0;
}
"""
# A one-line loop: each pass stops part-way through the line, where nothing is changed.
SQUARES = """#include <stdio.h>

int main(void) {
    int total = 0;
    for (int i = 0; i < 3; i++) { int sq = i * i; total += sq; }
    printf("TOTAL:%d\\n", total);
    return 0;
}
"""


@pytest.fixture
def native(tmp_path, monkeypatch):
    if not os.environ.get("PEEKY_NATIVE_DAP"):
        pytest.skip("set PEEKY_NATIVE_DAP and native tool paths to enable native conformance")
    monkeypatch.setenv("PEEKY_STATE_DIR", str(tmp_path / "state"))
    return tmp_path


def write(root, name, text):
    path = root / name
    path.write_text(text)
    return path


def probe(capsys, path, at, *argv, args=()):
    code = main(
        ["probe", str(path), "--at", f"{path.name}:{at}", *argv, "--json", "--quiet", "--no-save"]
        + [f"--arg={arg}" for arg in args]
    )
    return code, json.loads(capsys.readouterr().out)


def line_of(text, marker):
    return next(i for i, line in enumerate(text.splitlines(), 1) if marker in line)


@pytest.mark.timeout(300)
def test_the_debounce_recipe_end_to_end(native, capsys):
    path = write(native, "debounce.c", DEBOUNCE)
    assert DEBOUNCE.splitlines()[AGREES - 1].strip().startswith("return 0; /* the input agrees")
    assert DEBOUNCE.splitlines()[CALL - 1].strip().startswith("changes += feed(&db")
    # The natural form: every stop of the line reached only in the case to test. There
    # `db` is settle's pointer, so it is refused before the run, as one change would be.
    code, refused = probe(
        capsys, path, AGREES, "--set-at", f"{AGREES}@all", "db.count=0", args=ARGS
    )
    assert code == 2 and refused["refused_before_launch"], refused
    message = refused["error"]["message"]
    assert f"debounce.c:{CALL} in main, which passes `&db`, where `db.count` is settable" in message
    # The owner's line runs once per sample, not only where line 36 does: saying so keeps
    # `80@all` from passing for the same experiment.
    assert (
        f"`--set-at {CALL}@all` would make the change at every stop of line {CALL}, whether "
        f"or not the call made there reaches line {AGREES}: to make it only where line "
        f"{AGREES} runs, try it in a candidate edit (`--old`/`--new`)" in message
    ), message
    # So it is: the count reset before every sample never reaches the threshold.
    code, every = probe(capsys, path, CALL, "--set-at", f"{CALL}@all", "db.count=0", args=ARGS)
    assert code == 0 and every["output"].strip() == "DEBOUNCE:0:", every
    assert [(c["hit"], c["at"], c["function"]) for c in every["interventions"]] == [
        (hit, f"{CALL}@{hit}", "main") for hit in range(1, len(ARGS[1]) + 1)
    ]
    # The change only where line 36 runs, in one run: a candidate edit there (probed at
    # line 35, outside the edit).
    code, candidate = probe(
        capsys,
        path,
        AGREES - 1,
        "--capture",
        "level,db.count",
        "--hits",
        "all",
        "--old",
        "return 0; /* the input agrees with the accepted level */",
        "--new",
        "db->count = 0; /* a blip, if any, is over */\n        return 0;",
        args=ARGS,
    )
    assert code == 0 and candidate["output"].strip() == EXPECTED, candidate
    # Without either, the issue's output.
    code, plain = probe(capsys, path, AGREES, args=ARGS)
    assert code == 0 and plain["output"].strip() == BUGGY, plain


@pytest.mark.timeout(300)
@pytest.mark.parametrize("name", ["inline.c", "inline.cpp"])
def test_an_all_in_the_frame_that_owns_the_value_tests_the_hypothesis(native, capsys, name):
    if name.endswith(".cpp") and not os.environ.get("PEEKY_NATIVE_CXX"):
        pytest.skip("set PEEKY_NATIVE_CXX to build C++")
    path = write(native, name, INLINE)
    agrees = line_of(INLINE, "/* AGREES */")
    code, plain = probe(capsys, path, agrees, args=ARGS[1:])
    assert code == 0 and plain["output"].strip() == "DEBOUNCE:P@5,R@15,P@23,R@28, quiet 20"
    code, result = probe(
        capsys,
        path,
        agrees,
        "--capture",
        "ms,db.count",
        "--hits",
        "all",
        "--set-at",
        f"{agrees}@all",
        "db.count=0",
        args=ARGS[1:],
    )
    assert code == 0, result
    assert result["output"].strip() == "DEBOUNCE:P@9,R@20, quiet 16", result["output"]
    changes = result["interventions"]
    # One change at each of the line's stops, each read back, with its own hit.
    assert [change["hit"] for change in changes] == list(range(1, result["hits"] + 1))
    assert all(change.get("all") and change["after"] == "0" for change in changes), changes
    assert {change["function"] for change in changes} == {"main"}
    reset = [(c["hit"], c["before"]) for c in changes if c["before"] != "0"]
    assert reset and all(before in ("1", "2") for _, before in reset), reset
    # An after-set row at each captured hit.
    after = [row for row in result["rows"] if row["phase"] == "after-set"]
    assert {row["hit"] for row in after} == set(range(1, result["hits"] + 1))
    assert "unapplied" not in result


@pytest.mark.timeout(300)
def test_a_stop_where_the_change_is_refused_ends_the_probe(native, capsys):
    # Line 5's first stop is before the loop; each pass then stops part-way through it,
    # where a statement may hold a copy: the second stop refuses, as `5@2` would.
    path = write(native, "squares.c", SQUARES)
    code, result = probe(capsys, path, 5, "--set-at", "5@all", "total=100")
    assert code == 1 and result["status"] == "failed", result
    error = result["error"]
    assert error["code"] == "MUTATION_REJECTED", error
    assert error["message"].startswith(
        "--set-at squares.c:5@all total=100 was refused at hit 2 of line 5 (in main): 'total' "
        "cannot be changed here."
    ), error["message"]
    assert error["message"].endswith(
        "(1 earlier intervention(s) applied; the run is not the experiment asked for)"
    ), error["message"]
    assert [(c["hit"], c["before"], c["after"]) for c in result["interventions"]] == [
        (1, "0", "100")
    ]
