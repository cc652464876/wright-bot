"""
Partial stubs for pywebview — minimal surface used by Prism bridge / net_monitor.
"""

from __future__ import annotations

from typing import Any, Callable, List, Optional, Tuple, Union

FOLDER_DIALOG: int
OPEN_DIALOG: int

class Window:
    events: Any
    def evaluate_js(self, code: str) -> Any: ...
    def create_file_dialog(
        self,
        dialog_type: int,
        directory: str = ...,
        allow_multiple: bool = ...,
        save_filename: str = ...,
        file_types: Tuple[str, ...] = ...,
        **kwargs: Any,
    ) -> Optional[List[str]]: ...
    def restore(self) -> None: ...
    def show(self) -> None: ...
    def destroy(self) -> None: ...

windows: List[Window]

def create_window(
    title: str = ...,
    url: Optional[str] = None,
    html: Optional[str] = None,
    js_api: Any = None,
    width: int = ...,
    height: int = ...,
    min_size: Optional[Tuple[int, int]] = None,
    **kwargs: Any,
) -> Optional[Window]: ...
