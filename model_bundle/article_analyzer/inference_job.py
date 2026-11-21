"""Keep the Windows CLI's workers inside the lifetime of its process."""
import os


def contain_workers():
    if os.name != "nt":
        return
    import ctypes
    from ctypes import wintypes as win

    ptr = ctypes.c_size_t

    class Limits(ctypes.Structure):
        _fields_ = [
            ("process_time", ctypes.c_int64), ("job_time", ctypes.c_int64),
            ("flags", win.DWORD), ("min_working_set", ptr), ("max_working_set", ptr),
            ("active_process_limit", win.DWORD), ("affinity", ptr),
            ("priority", win.DWORD), ("scheduling", win.DWORD),
        ]

    class ExtendedLimits(ctypes.Structure):
        _fields_ = [
            ("basic", Limits), ("io_counters", ctypes.c_uint64 * 6),
            ("process_memory", ptr), ("job_memory", ptr), ("peak_process", ptr), ("peak_job", ptr),
        ]

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateJobObjectW.argtypes = [win.LPVOID, win.LPCWSTR]
    kernel.CreateJobObjectW.restype = win.HANDLE
    kernel.SetInformationJobObject.argtypes = [win.HANDLE, ctypes.c_int, win.LPVOID, win.DWORD]
    kernel.AssignProcessToJobObject.argtypes = [win.HANDLE, win.HANDLE]
    kernel.GetCurrentProcess.restype = win.HANDLE
    kernel.CloseHandle.argtypes = [win.HANDLE]
    handle = kernel.CreateJobObjectW(None, None)
    if not handle:
        raise ctypes.WinError(ctypes.get_last_error())
    limits = ExtendedLimits()
    limits.basic.flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    if not (kernel.SetInformationJobObject(handle, 9, ctypes.byref(limits), ctypes.sizeof(limits))
            and kernel.AssignProcessToJobObject(handle, kernel.GetCurrentProcess())):
        error = ctypes.get_last_error()
        kernel.CloseHandle(handle)
        raise ctypes.WinError(error)
    # Non-inheritable handle, owned until OS process teardown (including hard cancellation).

