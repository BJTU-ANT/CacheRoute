def parse_stream_flag(stream_val) -> bool:
    """
    解析来自前端 / Scheduler / Proxy 的 stream 字段，
    允许输入类型：
      - bool: True / False
      - str: "true", "false", "1", "0", "yes", "no"（大小写不敏感）
      - int: 1 / 0
    其他情况一律视为 False。
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

    # 未知类型或值 —— 默认关闭流式
    return False