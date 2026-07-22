/**
 * rs-settings-valve – Valve protection and manual valve settings.
 */
import { html, nothing } from "lit";
import { RsSettingsBase } from "./rs-settings-base";
import { customElement, property } from "lit/decorators.js";
import type { HomeAssistant } from "../../types";
import { localize } from "../../utils/localize";

@customElement("rs-settings-valve")
export class RsSettingsValve extends RsSettingsBase {
  @property({ attribute: false }) public hass!: HomeAssistant;
  @property({ type: Boolean }) public valveProtectionEnabled = false;
  @property({ type: Number }) public valveProtectionInterval = 7;
  @property({ type: String }) public manualValveEntity = "";

  render() {
    const l = this.hass.language;

    return html`
      <div class="toggle-row">
        <div class="toggle-text">
          <span class="toggle-label">${localize("valve_protection.title", l)}</span>
          <span class="toggle-hint">${localize("valve_protection.hint", l)}</span>
        </div>
        <ha-switch
          .checked=${this.valveProtectionEnabled}
          @change=${(e: Event) =>
            this._fire("valveProtectionEnabled", (e.target as HTMLInputElement).checked)}
        ></ha-switch>
      </div>

      ${this.valveProtectionEnabled
        ? html`
            <div class="threshold-grid" style="margin-top: 12px">
              <div class="threshold-field">
                <ha-textfield
                  .value=${String(this.valveProtectionInterval)}
                  .label=${localize("valve_protection.interval_label", l)}
                  .suffix=${localize("valve_protection.interval_suffix", l)}
                  type="number"
                  step="1"
                  min="1"
                  max="90"
                  @change=${(e: Event) => {
                    const v = parseInt((e.target as HTMLInputElement).value, 10);
                    if (!isNaN(v) && v >= 1 && v <= 90) this._fire("valveProtectionInterval", v);
                  }}
                ></ha-textfield>
                <span class="field-hint">${localize("valve_protection.interval_hint", l)}</span>
              </div>
            </div>
          `
        : nothing}

      <div class="settings-section" style="margin-top: 24px; border-top: 1px solid var(--divider-color); padding-top: 16px;">
        <div class="toggle-text" style="margin-bottom: 12px;">
          <span class="toggle-label" style="font-weight: 500; display: block;">Testa Termostatica Manuale (0 - 5)</span>
          <span class="toggle-hint" style="font-size: 12px; color: var(--secondary-text-color);">
            Associa un helper numerico di Home Assistant (input_number) per simulare la posizione fisica della valvola manuale nella stanza.
          </span>
        </div>
        <ha-entity-picker
          .hass=${this.hass}
          .value=${this.manualValveEntity}
          .includeDomains=${["input_number"]}
          @value-changed=${(e: CustomEvent) =>
            this._fire("manualValveEntity", e.detail.value)}
        ></ha-entity-picker>
      </div>
    `;
  }

  static styles = [RsSettingsBase.settingsBaseStyles];
}

declare global {
  interface HTMLElementTagNameMap {
    "rs-settings-valve": RsSettingsValve;
  }
}
