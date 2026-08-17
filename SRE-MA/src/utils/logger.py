import threading
import builtins, zlib
from contextvars import ContextVar

inc_id = ContextVar("incident_id", default="")
print_lock = threading.Lock()
orig_print = builtins.print


COLORS = ["\033[36m", "\033[33m", "\033[35m", "\033[32m", "\033[34m", "\033[96m"]
RESET = "\033[0m"

def clean_print(*args, **kwargs):
    inc = inc_id.get()
    if inc:
        color = COLORS[zlib.crc32(inc.encode()) % len(COLORS)]
        prefix = f"{color}[{inc}]{RESET} "
    else:
        prefix = ""
    with print_lock:
        orig_print(prefix + " ".join(str(a) for a in args), **kwargs)

builtins.print = clean_print 