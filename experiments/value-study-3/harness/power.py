#!/usr/bin/env python3
"""Round 3's power estimate: the smallest improvement the main run would detect.

    harness/power.py [--sims 400] [--resamples 1000] [--priors uniform,hard,easy]

A simulated study draws n bugs. Each bug's true control rate p comes from a prior over the
candidate pool, kept with the screening's probability of keeping it, (1 - p)^2 (1 + p) (README,
"Screening"). The `peeky-first` rate is p + d + e, clipped to [0, 1], where d is the improvement
and e, the bug's own deviation, is normal with sd tau. Rounds 1 and 2 put tau between 0 and 0.08
(README, "Power"); the estimate uses 0.08. Each arm has k fresh attempts per bug. The analysis is
round 1's: the mean over bugs of the paired difference, with a percentile bootstrap interval that
resamples bugs. An improvement is detected when the interval's lower end is above zero
(two-sided 95%).

Prints, for each prior and design (n bugs, k attempts per arm), the smallest d on a 2.5-point
grid with power 0.8. Python 3.9+, standard library only."""

import argparse
import random
import statistics

PRIORS = {
    # Uniform over [0, 1]: no knowledge of the pool's rates.
    "uniform": lambda rng: rng.random(),
    # Beta(1, 2), mean 1/3: a pool that is mostly hard for this model.
    "hard": lambda rng: rng.betavariate(1, 2),
    # Beta(2, 1), mean 2/3: a pool this model mostly solves, as rounds 1 and 2 suggest
    # (README, "Selection").
    "easy": lambda rng: rng.betavariate(2, 1),
}


def screened_rate(prior, rng):
    """A true control rate the screening kept: drawn from the prior, kept with probability
    (1 - p)^2 (1 + p), the chance that attempt 1 fails and attempts 2 and 3 solve at most once."""
    while True:
        p = prior(rng)
        if rng.random() < (1 - p) ** 2 * (1 + p):
            return p


def one_study(prior, n, k, d, tau, resamples, rng):
    diffs = []
    for _ in range(n):
        p = screened_rate(prior, rng)
        q = min(1.0, max(0.0, p + d + rng.gauss(0, tau)))
        control = sum(rng.random() < p for _ in range(k)) / k
        treated = sum(rng.random() < q for _ in range(k)) / k
        diffs.append(treated - control)
    means = sorted(
        statistics.mean(diffs[rng.randrange(n)] for _ in range(n)) for _ in range(resamples)
    )
    return means[int(0.025 * resamples)] > 0


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--sims", type=int, default=400)
    parser.add_argument("--resamples", type=int, default=1000)
    parser.add_argument("--tau", type=float, default=0.08)
    parser.add_argument("--seed", type=int, default=20261102)
    parser.add_argument("--priors", default=",".join(PRIORS))
    parser.add_argument(
        "--designs",
        default="15x10,20x6,20x8,20x10,25x6,25x8,30x6,30x8",
        help="bugs x attempts per arm, comma-separated",
    )
    args = parser.parse_args()
    rng = random.Random(args.seed)
    steps = [round(0.025 * i, 3) for i in range(2, 13)]
    print("| Prior | Bugs | Attempts per arm | Smallest improvement with power 0.8 |")
    print("| --- | --- | --- | --- |")
    for prior in args.priors.split(","):
        for design in args.designs.split(","):
            n, k = (int(x) for x in design.split("x"))
            found = None
            for d in steps:
                power = statistics.mean(
                    one_study(PRIORS[prior], n, k, d, args.tau, args.resamples, rng)
                    for _ in range(args.sims)
                )
                if power >= 0.8:
                    found = d
                    break
            shown = "more than 30 pp" if found is None else "{:.1f} pp".format(found * 100)
            print("| {} | {} | {} | {} |".format(prior, n, k, shown), flush=True)


if __name__ == "__main__":
    main()
