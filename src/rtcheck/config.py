"""Configuration: what counts as an entry point, and what counts as forbidden."""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path

# The default policy targets the classic real-time audio contract:
# no allocation, no locking, no I/O, no sleeping, no termination.
# Every entry here is a *known* leaf. rtcheck does not guess.
DEFAULT_EFFECTS: dict[str, dict] = {
    "alloc": {
        "message": "allocate or free memory",
        "symbols": [
            "malloc", "calloc", "realloc", "reallocarray", "free",
            "aligned_alloc", "posix_memalign", "memalign", "valloc",
            "strdup", "strndup", "asprintf", "vasprintf",
            "operator new", "operator new[]", "operator delete", "operator delete[]",
        ],
    },
    "lock": {
        "message": "take a lock, which may block",
        "symbols": [
            "pthread_mutex_lock", "pthread_mutex_timedlock",
            "pthread_rwlock_rdlock", "pthread_rwlock_wrlock",
            "pthread_cond_wait", "pthread_cond_timedwait",
            "pthread_join", "pthread_barrier_wait",
            "sem_wait", "sem_timedwait",
        ],
    },
    "io": {
        "message": "perform I/O",
        "symbols": [
            "printf", "fprintf", "vprintf", "vfprintf", "puts", "fputs", "putchar",
            "perror", "scanf", "fscanf",
            "open", "openat", "close", "read", "write", "pread", "pwrite",
            "fopen", "fclose", "fread", "fwrite", "fflush", "fseek",
            "socket", "send", "recv", "connect", "accept",
        ],
    },
    "syscall": {
        "message": "enter the kernel",
        "symbols": [
            "mmap", "munmap", "mprotect", "brk", "sbrk",
            "fork", "execve", "waitpid", "kill",
            "dlopen", "dlclose", "dlsym",
            "getenv", "setenv",
        ],
    },
    "block": {
        "message": "sleep or wait on a timer",
        "symbols": [
            "sleep", "usleep", "nanosleep", "poll", "select", "epoll_wait",
            "clock_nanosleep", "sched_yield",
        ],
    },
    "terminate": {
        "message": "terminate the process",
        "symbols": ["abort", "exit", "_Exit", "quick_exit", "__assert_fail"],
    },
}


@dataclass
class Config:
    entrypoints: list[str] = field(default_factory=list)
    effects: dict[str, dict] = field(default_factory=lambda: dict(DEFAULT_EFFECTS))
    allow: list[str] = field(default_factory=list)
    enabled: list[str] | None = None
    flag_recursion: bool = True
    indirect: str = "warn"  # "warn" | "address-taken" | "ignore"
    clang_args: list[str] = field(default_factory=list)

    @property
    def symbol_to_effect(self) -> dict[str, str]:
        """Flat lookup: leaf symbol name -> effect category."""
        out: dict[str, str] = {}
        for name, spec in self.effects.items():
            if self.enabled is not None and name not in self.enabled:
                continue
            for sym in spec.get("symbols", []):
                out[sym] = name
        for allowed in self.allow:
            out.pop(allowed, None)
        return out

    def message_for(self, effect: str) -> str:
        return self.effects.get(effect, {}).get("message", effect)


def load(path: Path | None) -> Config:
    """Load a config file, layering it over the built-in defaults."""
    cfg = Config()
    if path is None:
        return cfg

    with open(path, "rb") as fh:
        raw = tomllib.load(fh)

    cfg.entrypoints = list(raw.get("entrypoints", {}).get("functions", []))
    cfg.allow = list(raw.get("allow", {}).get("functions", []))

    opts = raw.get("options", {})
    cfg.flag_recursion = bool(opts.get("flag_recursion", True))
    cfg.indirect = str(opts.get("indirect", "warn"))
    cfg.clang_args = list(opts.get("clang_args", []))
    if "effects" in opts:
        cfg.enabled = list(opts["effects"])

    # User effect tables extend or override the defaults, per category.
    for name, spec in raw.get("forbidden", {}).items():
        base = dict(cfg.effects.get(name, {"symbols": [], "message": name}))
        if spec.get("replace"):
            base["symbols"] = list(spec.get("symbols", []))
        else:
            base["symbols"] = sorted(set(base.get("symbols", [])) | set(spec.get("symbols", [])))
        if "message" in spec:
            base["message"] = spec["message"]
        cfg.effects[name] = base

    return cfg
