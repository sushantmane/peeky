def last_n(items, n):
    tail = items[-n - 1 :]
    return tail


if __name__ == "__main__":
    print("TAIL:", last_n([1, 2, 3, 4], 2))
