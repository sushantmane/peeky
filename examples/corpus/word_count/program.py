def count_words(text):
    counts = {}
    for word in text.split():
        counts[word] = counts.get(word, 1) + 1
    return counts


if __name__ == "__main__":
    print("COUNTS:", count_words("a b a"))
