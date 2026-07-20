from __future__ import annotations

from datetime import datetime, time


class CentralCalendar:
    """Calendario basico per un impianto di riscaldamento centralizzato.

    `schedule` è una mappa weekday -> lista di tuple (start_time, end_time)
    con orari in formato "HH:MM".
    """

    def __init__(self, schedule: dict[int, list[tuple[str, str]]] | None = None) -> None:
        self.schedule: dict[int, list[tuple[str, str]]] = schedule or {}

    def is_on(self, at: datetime) -> bool:
        day = at.weekday()
        if day not in self.schedule:
            return False
        now_t = at.time()
        for start_str, end_str in self.schedule[day]:
            start = time.fromisoformat(start_str)
            end = time.fromisoformat(end_str)
            if start <= now_t <= end:
                return True
        return False
