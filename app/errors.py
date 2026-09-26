"""研究链路的显式失败。

窗口没选、标的不认识、日历不够——这些都必须让调用方看见，
不能在编排器里悄悄换成默认值。
"""

from __future__ import annotations

from typing import List, Optional


class ResearchError(Exception):
    def __init__(self, code: str, message: str, hint: str = "") -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.hint = hint


class NeedsWindowChoice(ResearchError):
    """只给了股票名、没给研究窗口时，由前端提示用户选择。"""


class NeedsUserInput(ResearchError):
    """研究中途需要用户补一句，才能继续。"""

    def __init__(
        self,
        code: str,
        message: str,
        hint: str = "",
        *,
        field: str = "",
        choices: Optional[List[str]] = None,
    ) -> None:
        super().__init__(code, message, hint)
        self.field = field
        self.choices = list(choices or [])
