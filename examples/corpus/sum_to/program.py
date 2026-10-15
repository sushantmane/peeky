def sum_to(n):
    total = 0
    for i in range(n):
        total += i
    return total


if __name__ == "__main__":
    print("SUM:", sum_to(5))
