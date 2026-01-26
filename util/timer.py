import time

def timing(func):
    """
        输出某个函数的执行时间，ms。
        用法，在@classmethod后一行添加@timing
    """
    def wrapper(*args, **kwargs):
        t0 = time.perf_counter()
        result = func(*args, **kwargs)
        t1 = time.perf_counter()
        print(f"[Timer] {func.__name__} time: {(t1 - t0)*1000:.4f} ms")
        return result
    return wrapper