from __future__ import annotations

import threading
from pathlib import Path
from typing import Protocol


class DirectoryPickerError(RuntimeError):
    """The host could not open or complete the native directory picker."""


class DirectoryPicker(Protocol):
    def choose(self, initial_path: Path | None = None) -> Path | None: ...


class NativeDirectoryPicker:
    """Open a native directory picker on the machine hosting Management API."""

    def __init__(self, *, title: str = "选择允许的工作目录根路径") -> None:
        self._title = title
        self._lock = threading.Lock()

    def choose(self, initial_path: Path | None = None) -> Path | None:
        with self._lock:
            return self._choose_locked(initial_path)

    def _choose_locked(self, initial_path: Path | None) -> Path | None:
        try:
            import tkinter as tk
            from tkinter import filedialog
        except ImportError as exc:
            raise DirectoryPickerError(
                "Python tkinter is unavailable on the Management API host"
            ) from exc

        root: tk.Tk | None = None
        try:
            initial_directory = _usable_initial_path(initial_path)
            root = tk.Tk()
            root.withdraw()
            root.attributes("-topmost", True)  # pyright: ignore[reportUnknownMemberType]
            root.update()
            selected = filedialog.askdirectory(
                parent=root,
                title=self._title,
                initialdir=str(initial_directory) if initial_directory is not None else None,
                mustexist=True,
            )
        except tk.TclError as exc:
            raise DirectoryPickerError(
                "the native directory picker could not be opened on the Management API host"
            ) from exc
        finally:
            if root is not None:
                try:
                    root.destroy()
                except tk.TclError:
                    pass

        if not selected:
            return None
        try:
            path = Path(selected).expanduser().resolve(strict=True)
        except (OSError, RuntimeError) as exc:
            raise DirectoryPickerError(f"selected directory is unavailable: {selected}") from exc
        if not path.is_dir():
            raise DirectoryPickerError(f"selected path is not a directory: {selected}")
        return path


def _usable_initial_path(path: Path | None) -> Path | None:
    if path is None:
        return None
    try:
        resolved = path.expanduser().resolve(strict=True)
    except (OSError, RuntimeError):
        return None
    return resolved if resolved.is_dir() else None
