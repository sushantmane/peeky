def find_index(items, target):
    for index, item in enumerate(items):
        if item == target:
            return index
        return -1


if __name__ == "__main__":
    print("INDEX:", find_index(["a", "b", "c"], "c"))
