"""Small standalone program with one-based page numbers."""


def get_page(items, page, page_size):
    if page < 1 or page_size < 1:
        raise ValueError("page and page_size must be positive")
    start = page * page_size
    end = start + page_size
    return items[start:end]


if __name__ == "__main__":
    print("PAGE:", get_page([10, 20, 30, 40, 50, 60], 1, 3))
