"""Cross-platform file locking. Wraps fcntl (POSIX) and msvcrt (Windows).

Uses BLOCKING locks by default — concurrent processes wait instead of crashing.
Pass blocking=False to raise on contention (cron-style "another instance is running").
"""
import sys

if sys.platform == 'win32':
    import msvcrt
    def lock(f, blocking=True):
        f.seek(0)
        mode = msvcrt.LK_LOCK if blocking else msvcrt.LK_NBLCK
        msvcrt.locking(f.fileno(), mode, 1)
    def unlock(f):
        try:
            f.seek(0)
            msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
        except OSError:
            pass
else:
    import fcntl
    def lock(f, blocking=True):
        flags = fcntl.LOCK_EX if blocking else (fcntl.LOCK_EX | fcntl.LOCK_NB)
        fcntl.flock(f, flags)
    def unlock(f):
        fcntl.flock(f, fcntl.LOCK_UN)


class FileLock:
    """Context manager for cross-platform file locking.

    Default is blocking. Pass blocking=False to raise BlockingIOError on contention.
    """
    def __init__(self, path, blocking=True):
        self.path = str(path)
        self.blocking = blocking
        self.f = None

    def __enter__(self):
        self.f = open(self.path, 'a')
        lock(self.f, blocking=self.blocking)
        return self

    def __exit__(self, *args):
        if self.f:
            unlock(self.f)
            self.f.close()
            self.f = None
