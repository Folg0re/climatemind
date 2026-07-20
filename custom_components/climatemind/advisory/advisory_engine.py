class AdvisoryEngine:
    """Engine che traduce la potenza MPC in una posizione di valvola consigliata.

    L'input `mpc_power` è atteso normalizzato tra 0.0 e 1.0. Il risultato è un
    intero nella gamma 0..max_position.
    """

    def __init__(self, max_position: int = 5, central_heating: object | None = None) -> None:
        self.max_position = max_position
        # central_heating: optional manager exposing is_heat_available_now()
        self._central_heating = central_heating

    def recommend_position(self, mpc_power: float | None) -> int:
        if mpc_power is None:
            return 0
        # If central heating manager exists and reports no heat available,
        # avoid recommending any opening.
        try:
            if self._central_heating is not None and not self._central_heating.is_heat_available_now():
                return 0
        except Exception:
            # On any error querying central heating, fall back to safe behaviour
            pass
        power = max(0.0, min(1.0, float(mpc_power)))
        pos = round(power * self.max_position)
        return max(0, min(self.max_position, pos))
