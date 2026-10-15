def add_tag(tag, tags=[]):
    tags.append(tag)
    return tags


if __name__ == "__main__":
    print("FIRST:", add_tag("a"))
    print("SECOND:", add_tag("b"))
