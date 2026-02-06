import time
import functools

def clock(func):
    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        # Use perf_counter for the highest available resolution
        start_time = time.perf_counter()
        try:
            return func(*args, **kwargs)
        finally:
            end_time = time.perf_counter()
            total_time = end_time - start_time
            print(f"\n[{func.__name__}] execution time: {total_time:.6f} seconds")
            
    return wrapper