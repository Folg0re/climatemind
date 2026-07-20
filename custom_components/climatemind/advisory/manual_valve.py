from dataclasses import dataclass


@dataclass
class ManualValve:
    """Modello semplice per una valvola manuale con posizioni discrete 0–5.

    La classe fornisce metodi per impostare la posizione e per convertire una
    richiesta di potenza normalizzata (0.0–1.0) in una posizione della valvola.
    """

    position: int = 0
    max_position: int = 5

    def set_position(self, pos: int) -> None:
        if pos < 0:
            pos = 0
        if pos > self.max_position:
            pos = self.max_position
        self.position = pos

    def position_from_power(self, power: float) -> int:
        """Converti una potenza normalizzata (0.0-1.0) in posizione intera."""
        if power is None:
            return 0
        normalized = max(0.0, min(1.0, float(power)))
        pos = round(normalized * self.max_position)
        return max(0, min(self.max_position, pos))
