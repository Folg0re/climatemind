class AdvisoryEngine:
    """Engine che traduce la potenza MPC in una posizione di valvola consigliata.

    L'input `mpc_power` è atteso normalizzato tra 0.0 e 1.0. Il risultato è un
    intero nella gamma 0..max_position.
    """

    def __init__(self, max_position: int = 5) -> None:
        self.max_position = max_position

    def recommend_position(self, mpc_power: float | None) -> int:
        if mpc_power is None:
            return 0
        power = max(0.0, min(1.0, float(mpc_power)))
        pos = round(power * self.max_position)
        return max(0, min(self.max_position, pos))
