def dedupe(emails):
    seen = []
    for email in emails:
        key = email.strip()
        if key not in seen:
            seen.append(key)
    return seen


if __name__ == "__main__":
    print("UNIQUE:", dedupe(["A@x.com", "a@x.com"]))
