import time

def timing(func):
    """
        Print the execution time of a function in milliseconds.
        Usage: add @timing on the line after @classmethod.
    """
    def wrapper(*args, **kwargs):
        t0 = time.perf_counter()
        result = func(*args, **kwargs)
        t1 = time.perf_counter()
        print(f"[Timer] {func.__name__} time: {(t1 - t0)*1000:.4f} ms")
        return result
    return wrapper