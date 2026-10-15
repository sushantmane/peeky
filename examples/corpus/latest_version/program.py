def latest(versions):
    ordered = sorted(versions)
    return ordered[-1]


if __name__ == "__main__":
    print("LATEST:", latest(["9", "10"]))
