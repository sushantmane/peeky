def is_adult(age, limit=18):
    allowed = age > limit
    return allowed


if __name__ == "__main__":
    print("ADULT:", is_adult(18))
