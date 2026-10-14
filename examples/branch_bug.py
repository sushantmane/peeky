"""A reproducible branch bug. The demo never edits this file."""


def handle_request():
    timeout_ms = 0  # Simulates a bad configuration value.
    if timeout_ms <= 0:  # BREAKPOINT
        result = "fallback"
    else:
        result = "success"
    print(f"RESULT={result}; timeout_ms={timeout_ms}", flush=True)
    return result


if __name__ == "__main__":
    handle_request()
