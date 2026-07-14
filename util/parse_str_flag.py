def parse_stream_flag(stream_val) -> bool:
    """
    Parse the stream field from the frontend / Scheduler / Proxy,
    Accepted input types:
      - bool: True / False
      - str: "true", "false", "1", "0", "yes", "no"(case-insensitive)
      - int: 1 / 0
    All other cases are treated as False.
    """
    if isinstance(stream_val, bool):
        return stream_val

    if isinstance(stream_val, int):
        return stream_val == 1

    if isinstance(stream_val, str):
        val = stream_val.strip().lower()
        if val in ("true", "1", "yes", "y"):
            return True
        if val in ("false", "0", "no", "n"):
            return False

    # Unknown type or value: disable streaming by default
    return False