"""Local single-instance lock and durable emergency stop for runtime failures."""
from contextlib import contextmanager
import fcntl
import os
from pathlib import Path

_fault_latched = False


def execution_fault_latched():
    return _fault_latched


@contextmanager
def runtime_instance_lock(path="var/sm-copy.instance.lock", pid_path=None):
    """Hold one local inode for the lifetime of a run; never unlink a held lock."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    pid_path = Path(pid_path or ("var/sm-copy.pid" if str(path) == "var/sm-copy.instance.lock"
                                 else str(target) + ".pid"))
    descriptor = os.open(target, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError("another local sm-copy process holds the runtime lock") from None
        if pid_path.is_symlink():
            raise RuntimeError("runtime PID path is a symlink")
        if pid_path.exists():
            value = pid_path.read_text().strip()
            if not value.isascii() or not value.isdecimal() or not 0 < int(value) < 2 ** 31:
                raise RuntimeError("runtime PID file is invalid; operator review required")
            pid = int(value)
            if pid != os.getpid():
                try:
                    os.kill(pid, 0)
                except ProcessLookupError:
                    pass
                except PermissionError:
                    raise RuntimeError("runtime PID is active but cannot be inspected") from None
                else:
                    raise RuntimeError("existing PID is alive; stop the original process before starting")
        pid_fd = os.open(pid_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
        try:
            os.write(pid_fd, str(os.getpid()).encode())
            os.fsync(pid_fd)
        finally:
            os.close(pid_fd)
        yield
    finally:
        os.close(descriptor)


def trip_execution_stop():
    """Only called by the running service on a critical failure; no auto clearing."""
    global _fault_latched
    _fault_latched = True
    path = Path(os.environ.get("SMART_MONEY_EMERGENCY_STOP_FILE", "var/EXECUTION_STOP"))
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    except FileExistsError:
        return
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
