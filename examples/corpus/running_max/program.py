def running_max(values):
    best = 0
    result = []
    for value in values:
        best = max(best, value)
        result.append(best)
    return result


if __name__ == "__main__":
    print("MAX:", running_max([-3, -1, -2]))
