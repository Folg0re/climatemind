# Report modifiche upstream v1.7.6

Confronto eseguito tra i tag upstream `v1.7.5` e `v1.7.6`.

## Riepilogo

- File modificati: 32
- Righe aggiunte: 1.360
- Righe rimosse: 654
- File aggiunti, rimossi o rinominati: nessuno
- Il percorso upstream dell'integrazione e `custom_components/roommind/`.

## File modificati

### CI e automazione

- `.github/workflows/ci.yml`
- `.github/workflows/mutation.yml`
- `.github/workflows/release.yml`

### Integrazione Home Assistant

- `custom_components/roommind/const.py`
- `custom_components/roommind/control/mpc_controller.py`
- `custom_components/roommind/coordinator.py`
- `custom_components/roommind/manifest.json`
- `custom_components/roommind/utils/schedule_utils.py`
- `custom_components/roommind/websocket_api.py`

### Documentazione

- `docs/control-and-devices.md`

### Frontend

- `frontend/package-lock.json`
- `frontend/package.json`
- `frontend/src/components/analytics/rs-analytics-chart.ts`
- `frontend/src/components/rs-hero-status.ts`
- `frontend/src/components/rs-presence-section.ts`
- `frontend/src/components/rs-room-detail.ts`
- `frontend/src/locales/de.json`
- `frontend/src/locales/en.json`
- `frontend/src/locales/fr.json`
- `frontend/src/types/index.ts`
- `frontend/src/utils/chart-builder.ts`

### Test

- `tests/conftest.py`
- `tests/control/test_fan_mode.py`
- `tests/coordinator/test_climate_control_disabled.py`
- `tests/coordinator/test_entity_cleanup.py`
- `tests/coordinator/test_heat_source.py`
- `tests/coordinator/test_presence.py`
- `tests/coordinator/test_process_room_snapshot.py`
- `tests/coordinator/test_schedule.py`
- `tests/integration/test_mpc_behavior.py`
- `tests/test_websocket_api.py`
- `tests/utils/test_schedule_utils.py`

## Commit funzionali principali

- `9d3fd29`: consente di rimuovere dalla configurazione le entita di presenza orfane.
- `590c884`: assegna priorita all'azione globale di spegnimento rispetto a `idle_action` per singolo dispositivo.
- `4fb3eda`: usa il suffisso esatto dell'entita, evitando collisioni tra ID di stanze con prefissi comuni.
- `accd836`: rileva le modalita `heat_cool` e `auto` durante l'osservazione in modalita di solo apprendimento.
- `4fed6d4`: valuta i blocchi di pianificazione nel fuso orario di Home Assistant.
- `856fc5a`: espone attraverso WebSocket lo stato e il motivo della protezione del compressore.
- `cc6244a`: corregge la visualizzazione del setpoint in stanze con modalita diretta e proporzionale miste.
- `d9d3cd8`: aggiunge `deviceTarget` al grafico analytics, disabilitato per impostazione predefinita.

Sono inoltre inclusi aggiornamenti delle dipendenze frontend e della configurazione CI.