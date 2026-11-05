/**
 * Flaschenpost Interactive Shopping List Panel & Lovelace Card
 * Custom Web Component for Home Assistant
 * Version: 2.0.4
 */

class FlaschenpostView extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: "open" });
    this._domCreated = false;
    this._hass = null;
    this._config = {};
    this._activeList = "Einkaufsliste";
    this._lists = { Einkaufsliste: [] };
    this._listConfigs = {};
    this._listNames = ["Einkaufsliste"];
    this._categories = [];
    this._cart = null;

    // Search builder state
    this._query = "";
    this._amount = 1;
    this._unit = "x";
    this._category = "";
    this._sort = "price";
    this._positiveKeywords = [];
    this._negativeKeywords = [];
    this._preview = null;
    this._isSearching = false;
    this._searchDebounceTimer = null;
    this._editingUid = null;

    // Modals
    this._showDryRunModal = false;
    this._dryRunResult = null;
    this._showBuyModal = false;
    this._buyConfirmChecked = false;
    this._buyLoading = false;
    this._coupons = [];
    this._showCouponModal = false;
    this._actionMessage = null;
    this._nextUpdate = null;
    this._lastUpdated = null;
    this._updateIntervalSeconds = 300;
    this._countdownInterval = null;
    this._isRefreshing = false;
  }

  connectedCallback() {
    this._ensureDom();
    if (!this._countdownInterval) {
      this._countdownInterval = setInterval(() => this._tickCountdown(), 1000);
    }
  }

  disconnectedCallback() {
    if (this._countdownInterval) {
      clearInterval(this._countdownInterval);
      this._countdownInterval = null;
    }
  }

  set hass(hass) {
    const firstSet = !this._hass;
    this._hass = hass;
    this._ensureDom();
    if (firstSet) {
      this._loadInitialData();
    }
  }

  setConfig(config) {
    this._config = config || {};
  }

  getCardSize() {
    return 6;
  }

  async _loadInitialData() {
    if (!this._hass) return;
    await Promise.all([
      this._fetchLists(),
      this._fetchCategories(),
      this._fetchCart(),
      this._fetchCoupons(),
    ]);
    this._updateCartUi();
    this._updateTabsUi();
    this._updateListItemsUi();
    this._updateCategoryOptionsUi();
  }

  async _fetchLists() {
    if (!this._hass) return;
    try {
      const resp = await this._hass.callWS({ type: "flaschenpost/get_lists" });
      if (resp && resp.lists) {
        this._lists = resp.lists;
        this._listNames = resp.names && resp.names.length > 0 ? resp.names : Object.keys(resp.lists);
        if (resp.configs) {
          this._listConfigs = resp.configs;
        }
        if (!this._listNames.includes(this._activeList)) {
          this._activeList = this._listNames[0] || "Einkaufsliste";
        }
      }
    } catch (err) {
      console.error("Failed to fetch shopping lists:", err);
    }
  }

  async _fetchCategories() {
    if (!this._hass) return;
    try {
      const resp = await this._hass.callWS({ type: "flaschenpost/get_categories" });
      if (resp && resp.options) {
        this._categories = resp.options;
      }
    } catch (err) {
      console.warn("Failed to fetch category options:", err);
    }
  }

  async _fetchCart() {
    if (!this._hass) return;
    try {
      const resp = await this._hass.callWS({ type: "flaschenpost/get_cart" });
      if (resp) {
        this._cart = resp;
        if (resp.next_update) this._nextUpdate = resp.next_update;
        if (resp.last_updated) this._lastUpdated = resp.last_updated;
        if (resp.update_interval_seconds) this._updateIntervalSeconds = resp.update_interval_seconds;
        if (resp.active_coupons) this._coupons = resp.active_coupons;
        this._updateCartUi();
      }
    } catch (err) {
      console.warn("Failed to fetch cart:", err);
    }
  }

  async _fetchCoupons() {
    if (!this._hass) return;
    try {
      const resp = await this._hass.callWS({ type: "flaschenpost/get_coupons" });
      if (resp && resp.coupons) {
        this._coupons = resp.coupons;
        this._updateCartUi();
      }
    } catch (err) {
      console.warn("Failed to fetch coupons:", err);
    }
  }

  async _addCoupon(code, description = "") {
    if (!this._hass || !code || !code.trim()) return;
    try {
      const resp = await this._hass.callWS({
        type: "flaschenpost/add_coupon",
        code: code.trim(),
        description: description.trim(),
      });
      if (resp && resp.coupons) {
        this._coupons = resp.coupons;
      }
      this._showToast(`Gutschein '${code.trim().toUpperCase()}' gespeichert! Wird bei nächster Bestellung eingelöst.`);
      await this._fetchCart();
      this._updateCartUi();
      this._updateModalsUi();
    } catch (err) {
      this._showToast(`Fehler beim Speichern: ${err.message || err}`, true);
    }
  }

  async _deleteCoupon(code) {
    if (!this._hass || !code) return;
    try {
      const resp = await this._hass.callWS({
        type: "flaschenpost/delete_coupon",
        code: code,
      });
      if (resp && resp.coupons) {
        this._coupons = resp.coupons;
      }
      this._showToast(`Gutschein '${code}' entfernt.`);
      this._updateCartUi();
      this._updateModalsUi();
    } catch (err) {
      this._showToast(`Fehler beim Löschen: ${err.message || err}`, true);
    }
  }

  _getInitialCountdownText() {
    if (this._isRefreshing) return "Aktualisiere...";
    if (!this._nextUpdate) return "Update...";
    const now = new Date().getTime();
    const target = new Date(this._nextUpdate).getTime();
    const diffSec = Math.max(0, Math.round((target - now) / 1000));
    if (diffSec <= 0) return "Aktualisiere...";
    const m = Math.floor(diffSec / 60);
    const s = diffSec % 60;
    return `Update in ${m}:${s < 10 ? "0" : ""}${s}`;
  }

  _tickCountdown() {
    if (!this._nextUpdate) return;
    const now = new Date().getTime();
    const target = new Date(this._nextUpdate).getTime();
    const diffSec = Math.max(0, Math.round((target - now) / 1000));
    const countdownEl = this.shadowRoot ? this.shadowRoot.getElementById("update-countdown-text") : null;
    if (countdownEl) {
      if (this._isRefreshing) {
        countdownEl.textContent = "Aktualisiere...";
      } else if (diffSec <= 0) {
        countdownEl.textContent = "Aktualisiere...";
        if (!this._isRefreshing && now - target < 4000) {
          this._manualRefresh(false);
        }
      } else {
        const m = Math.floor(diffSec / 60);
        const s = diffSec % 60;
        countdownEl.textContent = `Update in ${m}:${s < 10 ? "0" : ""}${s}`;
      }
    }
  }

  async _manualRefresh(showToast = true) {
    if (!this._hass || this._isRefreshing) return;
    this._isRefreshing = true;
    const iconEl = this.shadowRoot ? this.shadowRoot.getElementById("update-refresh-icon") : null;
    if (iconEl) iconEl.style.transform = "rotate(360deg)";
    if (showToast) this._showToast("Aktualisiere Flaschenpost-Daten...");
    try {
      await this._hass.callWS({ type: "flaschenpost/refresh" });
      await this._fetchCart();
      if (showToast) this._showToast("Daten aktualisiert! ✓");
    } catch (err) {
      console.warn("Refresh failed:", err);
      if (showToast) this._showToast(`Fehler: ${err.message || err}`, true);
    } finally {
      this._isRefreshing = false;
      this._tickCountdown();
      if (iconEl) iconEl.style.transform = "none";
    }
  }

  _triggerSearchDebounced(delay = 1000) {
    if (this._searchDebounceTimer) {
      clearTimeout(this._searchDebounceTimer);
    }
    this._searchDebounceTimer = setTimeout(() => {
      this._executeSearch();
    }, delay);
  }

  _buildCurrentQueryString() {
    const rawQ = (this._query || "").trim();
    if (!rawQ) return "";

    const parts = [rawQ];
    for (const pos of this._positiveKeywords) {
      const clean = pos.trim();
      if (clean && !rawQ.toLowerCase().includes(clean.toLowerCase())) {
        parts.push(clean);
      }
    }
    for (const neg of this._negativeKeywords) {
      const clean = neg.trim().replace(/^!+/, "");
      if (clean) {
        parts.push(`!${clean}`);
      }
    }
    return parts.join(" ");
  }

  async _executeSearch() {
    const queryWithKws = this._buildCurrentQueryString();
    if (!queryWithKws || !this._hass) {
      this._preview = null;
      this._isSearching = false;
      this._updatePreview();
      return;
    }

    this._isSearching = true;
    this._updatePreview();

    try {
      let amountPrefix = null;
      if (this._unit !== "x" || (this._amount && Number(this._amount) > 1)) {
        amountPrefix = `${this._amount}${this._unit === "x" ? "x" : this._unit}`;
      }

      const resp = await this._hass.callWS({
        type: "flaschenpost/preview_search",
        query: queryWithKws,
        amount_prefix: amountPrefix,
        category: this._category || undefined,
        sort_by: this._sort === "price" ? undefined : this._sort,
        limit: 8,
      });

      this._preview = resp;
    } catch (err) {
      console.error("Preview search failed:", err);
      this._preview = null;
    } finally {
      this._isSearching = false;
      this._updatePreview();
    }
  }

  _toggleNegativeKeyword(kw) {
    const clean = kw.trim().replace(/^!+/, "");
    const idx = this._negativeKeywords.findIndex((k) => k.toLowerCase() === clean.toLowerCase());
    if (idx >= 0) {
      this._negativeKeywords.splice(idx, 1);
    } else {
      this._negativeKeywords.push(clean);
      this._positiveKeywords = this._positiveKeywords.filter((k) => k.toLowerCase() !== clean.toLowerCase());
    }
    this._updateChips();
    this._triggerSearchDebounced();
  }

  _togglePositiveKeyword(kw) {
    const clean = kw.trim().replace(/^!+/, "");
    const idx = this._positiveKeywords.findIndex((k) => k.toLowerCase() === clean.toLowerCase());
    if (idx >= 0) {
      this._positiveKeywords.splice(idx, 1);
    } else {
      this._positiveKeywords.push(clean);
      this._negativeKeywords = this._negativeKeywords.filter((k) => k.toLowerCase() !== clean.toLowerCase());
    }
    this._updateChips();
    this._triggerSearchDebounced();
  }

  _addCustomKeyword(kw, isNegative) {
    const clean = (kw || "").trim().replace(/^[!+]+/, "");
    if (!clean) return;
    if (isNegative) {
      if (!this._negativeKeywords.some((k) => k.toLowerCase() === clean.toLowerCase())) {
        this._negativeKeywords.push(clean);
        this._positiveKeywords = this._positiveKeywords.filter((k) => k.toLowerCase() !== clean.toLowerCase());
      }
    } else {
      if (!this._positiveKeywords.some((k) => k.toLowerCase() === clean.toLowerCase())) {
        this._positiveKeywords.push(clean);
        this._negativeKeywords = this._negativeKeywords.filter((k) => k.toLowerCase() !== clean.toLowerCase());
      }
    }
    this._updateChips();
    this._triggerSearchDebounced();
  }

  async _addItemToList(summary) {
    if (!summary || !this._hass) return;
    try {
      if (this._editingUid) {
        await this._hass.callWS({
          type: "flaschenpost/update_item",
          list_name: this._activeList,
          uid: this._editingUid,
          summary: summary,
        });
        this._editingUid = null;
        this._showToast(`Eintrag in '${this._activeList}' aktualisiert!`);
      } else {
        await this._hass.callWS({
          type: "flaschenpost/add_item",
          list_name: this._activeList,
          summary: summary,
        });
        this._showToast(`Zu '${this._activeList}' hinzugefügt!`);
      }
      this._clearBuilder();
      await this._fetchLists();
      this._updateBuilderUi();
      this._updateListItemsUi();
      this._updateTabsUi();
      this._updatePreview();
    } catch (err) {
      this._showToast(`Fehler: ${err.message || err}`, true);
    }
  }

  _clearBuilder() {
    this._query = "";
    this._amount = 1;
    this._unit = "x";
    this._category = "";
    this._sort = "price";
    this._positiveKeywords = [];
    this._negativeKeywords = [];
    this._preview = null;
    this._editingUid = null;
  }

  async _toggleItemCompleted(item) {
    if (!this._hass) return;
    try {
      await this._hass.callWS({
        type: "flaschenpost/update_item",
        list_name: this._activeList,
        uid: item.uid,
        completed: !item.completed,
      });
      await this._fetchLists();
      this._updateListItemsUi();
      this._updateTabsUi();
    } catch (err) {
      this._showToast(`Fehler: ${err.message || err}`, true);
    }
  }

  async _deleteItem(item) {
    if (!this._hass) return;
    try {
      await this._hass.callWS({
        type: "flaschenpost/delete_item",
        list_name: this._activeList,
        uids: [item.uid],
      });
      this._showToast("Eintrag gelöscht");
      await this._fetchLists();
      this._updateListItemsUi();
      this._updateTabsUi();
    } catch (err) {
      this._showToast(`Fehler: ${err.message || err}`, true);
    }
  }

  _loadItemIntoBuilder(item) {
    this._clearBuilder();
    this._editingUid = item.uid;
    this._query = item.query || item.summary;
    this._category = item.category || "";
    this._sort = item.sort_by || "price";
    this._negativeKeywords = [...(item.negative_keywords || [])];

    if (item.unit_type === "weight_g") {
      if (item.target_amount_base >= 1000) {
        this._amount = item.target_amount_base / 1000;
        this._unit = "kg";
      } else {
        this._amount = item.target_amount_base;
        this._unit = "g";
      }
    } else if (item.unit_type === "volume_ml") {
      if (item.target_amount_base >= 1000) {
        this._amount = item.target_amount_base / 1000;
        this._unit = "L";
      } else {
        this._amount = item.target_amount_base;
        this._unit = "ml";
      }
    } else if (item.unit_type === "crate") {
      this._amount = Math.round(item.target_amount_base);
      this._unit = "Kiste";
    } else {
      this._amount = item.quantity || 1;
      this._unit = "x";
    }

    this._updateBuilderUi();
    this._triggerSearchDebounced();
    const searchInput = this.shadowRoot.getElementById("search-input");
    if (searchInput) {
      searchInput.focus();
    }
  }

  async _importCartToNewList() {
    const defaultName = `Warenkorb ${new Date().toLocaleDateString("de-DE")}`;
    const name = window.prompt("Name der neuen Einkaufsliste für den Warenkorb-Import:", defaultName);
    if (!name || !name.trim()) return;
    const clean = name.trim();
    try {
      this._showToast("Importiere aktuellen Warenkorb...");
      const resp = await this._hass.callWS({
        type: "flaschenpost/import_cart_to_list",
        name: clean,
      });
      this._activeList = clean;
      await this._fetchLists();
      this._updateTabsUi();
      this._updateListItemsUi();
      this._updateBuilderUi();
      this._showToast(`Liste '${clean}' mit ${resp.imported_count || 0} Artikeln importiert!`);
    } catch (err) {
      this._showToast(`Import fehlgeschlagen: ${err.message || err}`, true);
    }
  }

  async _saveListConfig(listName, patch) {
    try {
      const current = (this._listConfigs && this._listConfigs[listName]) || {
        random_enabled: false,
        random_count: 1,
        consume_on_order: false,
        consume_action: "complete",
      };
      const merged = { ...current, ...patch };
      const resp = await this._hass.callWS({
        type: "flaschenpost/set_list_config",
        name: listName,
        random_enabled: merged.random_enabled,
        random_count: merged.random_count,
        consume_on_order: merged.consume_on_order,
        consume_action: merged.consume_action,
      });
      if (!this._listConfigs) this._listConfigs = {};
      this._listConfigs[listName] = resp.config || merged;
      this._updateTabsUi();
      this._updateListItemsUi();
      this._showToast(`Einstellungen für '${listName}' gespeichert!`);
    } catch (err) {
      this._showToast(`Fehler: ${err.message || err}`, true);
    }
  }

  async _resetListWeights(listName) {
    try {
      await this._hass.callWS({
        type: "flaschenpost/reset_list_weights",
        name: listName,
      });
      await this._fetchLists();
      this._updateListItemsUi();
      this._showToast("Gewichte auf 1.0 zurückgesetzt!");
    } catch (err) {
      this._showToast(`Fehler: ${err.message || err}`, true);
    }
  }

  async _createNewList() {
    const name = window.prompt("Name der neuen Einkaufsliste:", "Vorrat");
    if (!name || !name.trim()) return;
    const clean = name.trim();
    try {
      await this._hass.callWS({
        type: "flaschenpost/save_list",
        name: clean,
        items: [],
      });
      this._activeList = clean;
      await this._fetchLists();
      this._updateTabsUi();
      this._updateListItemsUi();
      this._updateBuilderUi();
      this._showToast(`Liste '${clean}' erstellt!`);
    } catch (err) {
      this._showToast(`Fehler: ${err.message || err}`, true);
    }
  }

  async _deleteCurrentList() {
    if (this._listNames.length <= 1) {
      this._showToast("Die letzte Liste kann nicht gelöscht werden.", true);
      return;
    }
    if (!window.confirm(`Möchtest du die Liste '${this._activeList}' wirklich löschen?`)) {
      return;
    }
    try {
      await this._hass.callWS({
        type: "flaschenpost/delete_list",
        name: this._activeList,
      });
      this._activeList = this._listNames.find((n) => n !== this._activeList) || "Einkaufsliste";
      await this._fetchLists();
      this._updateTabsUi();
      this._updateListItemsUi();
      this._updateBuilderUi();
      this._showToast("Liste gelöscht");
    } catch (err) {
      this._showToast(`Fehler: ${err.message || err}`, true);
    }
  }

  async _runDryRunPreview() {
    if (!this._hass) return;
    try {
      this._showToast("Berechne Warenkorb-Auflösung...");
      const resp = await this._hass.callWS({
        type: "flaschenpost/preview_lists",
        shopping_lists: [this._activeList],
      });
      this._dryRunResult = resp;
      this._showDryRunModal = true;
      this._updateModalsUi();
    } catch (err) {
      this._showToast(`Fehler: ${err.message || err}`, true);
    }
  }

  async _syncToCart(clearFirst = false) {
    if (!this._hass) return;
    try {
      this._showToast(clearFirst ? "Warenkorb wird ersetzt..." : "Artikel werden hinzugefügt...");
      const resp = await this._hass.callWS({
        type: "flaschenpost/sync_to_cart",
        shopping_lists: [this._activeList],
        clear_cart_first: clearFirst,
        mark_completed: false,
      });
      await this._fetchCart();
      this._showDryRunModal = false;
      this._updateCartUi();
      this._updateModalsUi();
      this._showToast(`${resp.synced_count || 0} Artikel in Warenkorb übertragen!`);
    } catch (err) {
      this._showToast(`Fehler: ${err.message || err}`, true);
    }
  }

  async _executeBuyCart() {
    if (!this._buyConfirmChecked) {
      this._showToast("Bitte bestätige die Bestellung über das Kontrollkästchen.", true);
      return;
    }
    this._buyLoading = true;
    this._updateModalsUi();
    try {
      const resp = await this._hass.callWS({
        type: "flaschenpost/place_order",
        confirm: true,
      });
      this._showBuyModal = false;
      this._buyConfirmChecked = false;
      this._showToast(`Bestellung #${resp.order_number || resp.order_id} erfolgreich aufgegeben! 🎉`);
      await this._fetchCart();
      this._updateCartUi();
    } catch (err) {
      this._showToast(`Bestellfehler: ${err.message || err}`, true);
    } finally {
      this._buyLoading = false;
      this._updateModalsUi();
    }
  }

  _showToast(msg, isError = false) {
    this._actionMessage = { text: msg, isError };
    const container = this.shadowRoot ? this.shadowRoot.getElementById("toast-container") : null;
    if (container) {
      container.innerHTML = `<div class="toast ${isError ? "error" : "success"}">${msg}</div>`;
    }
    setTimeout(() => {
      if (this._actionMessage && this._actionMessage.text === msg) {
        this._actionMessage = null;
        if (container) {
          container.innerHTML = "";
        }
      }
    }, 4000);
  }

  _renderNegativeChipsHtml() {
    const commonNegatives = ["zucker", "bio", "alkoholfrei", "laktose", "weizen", "gezuckert"];
    return `
      ${this._negativeKeywords
        .map(
          (kw) => `
        <span class="chip chip-neg-active" data-neg-toggle="${kw}">
          🚫 !${kw} <span class="chip-close">×</span>
        </span>
      `
        )
        .join("")}
      ${commonNegatives
        .filter((ck) => !this._negativeKeywords.some((k) => k.toLowerCase() === ck.toLowerCase()))
        .map(
          (ck) => `
        <span class="chip chip-neg-avail" data-neg-toggle="${ck}">
          + !${ck}
        </span>
      `
        )
        .join("")}
    `;
  }

  _renderPositiveChipsHtml() {
    const commonPositives = ["bio", "vegan", "zero", "light", "3,5%", "1,5%"];
    return `
      ${this._positiveKeywords
        .map(
          (kw) => `
        <span class="chip chip-pos-active" data-pos-toggle="${kw}">
          ✓ ${kw} <span class="chip-close">×</span>
        </span>
      `
        )
        .join("")}
      ${commonPositives
        .filter((cp) => !this._positiveKeywords.some((k) => k.toLowerCase() === cp.toLowerCase()))
        .map(
          (cp) => `
        <span class="chip chip-pos-avail" data-pos-toggle="${cp}">
          + ${cp}
        </span>
      `
        )
        .join("")}
    `;
  }

  _bindChipsEvents() {
    const root = this.shadowRoot;
    if (!root) return;

    root.querySelectorAll("#chips-neg-container [data-neg-toggle]").forEach((el) => {
      el.addEventListener("click", () => {
        this._toggleNegativeKeyword(el.dataset.negToggle);
      });
    });

    root.querySelectorAll("#chips-pos-container [data-pos-toggle]").forEach((el) => {
      el.addEventListener("click", () => {
        this._togglePositiveKeyword(el.dataset.posToggle);
      });
    });
  }

  _updateChips() {
    const root = this.shadowRoot;
    if (!root) return;
    const negContainer = root.getElementById("chips-neg-container");
    if (negContainer) {
      negContainer.innerHTML = this._renderNegativeChipsHtml();
    }
    const posContainer = root.getElementById("chips-pos-container");
    if (posContainer) {
      posContainer.innerHTML = this._renderPositiveChipsHtml();
    }
    this._bindChipsEvents();
  }

  _renderPreviewContent() {
    const queryWithKws = this._buildCurrentQueryString();
    if (!this._query || !this._query.trim()) {
      return "";
    }

    const activeCat = this._category || (this._preview && this._preview.spec && this._preview.spec.category) || "";
    const catBadge = activeCat ? `<span class="badge-cat" title="Kategorie-Filter aktiv">📁 ${activeCat}</span>` : "";
    const previewTitle = this._preview && this._preview.lazy_summary
      ? this._preview.lazy_summary
      : (activeCat ? `${activeCat}::${queryWithKws}` : queryWithKws);

    let winnerHtml = "";
    if (this._preview && this._preview.winner && this._preview.winner.product) {
      const w = this._preview.winner;
      const priceMetric = w.product.price_per_kg_eur
        ? `${Number(w.product.price_per_kg_eur).toFixed(2)} €/kg`
        : `${Number(w.product.price_eur || 0).toFixed(2)} €`;

      const isMengenrabatt = w.tier_applied || (w.product && w.product.tier_applied);
      const winnerCats = (w.product && w.product.categories ? w.product.categories.join(" > ") : "") || "—";
      winnerHtml = `
        <div class="winner-banner">
          <div style="display:flex; align-items:center; gap:6px; flex-wrap:wrap;">
            <span class="winner-badge">★ Order-Time Winner</span>
            ${isMengenrabatt ? `<span class="badge-mengenrabatt">Mengenrabatt</span>` : ""}
            ${activeCat ? `<span class="badge-cat-sm">📁 ${activeCat}</span>` : ""}
          </div>
          <div style="font-weight: 700; font-size: 1.05rem; margin-top: 4px;">
            ${w.quantity}x ${w.product ? w.product.name : ""} (<a href="${w.product_url || (w.product && w.product.product_url) || (w.slug ? `https://www.flaschenpost.de/p/${w.slug}` : (w.product && w.product.slug ? `https://www.flaschenpost.de/p/${w.product.slug}` : `https://www.flaschenpost.de/p/${w.article_id}`))}" target="_blank" rel="noopener noreferrer" class="sku-link" title="Auf flaschenpost.de öffnen">#${w.article_id} ↗</a>)
          </div>
          <div style="font-size: 0.88rem; color: #555; margin-top: 2px;">
            Geliefert: <strong>${w.delivered_amount_base || ""}</strong> für <strong>${(w.line_total_eur || 0).toFixed(2)} €</strong>
            (${priceMetric}${winnerCats !== "—" ? `, Kategorie: <em>${winnerCats}</em>` : ""})
          </div>
        </div>
      `;
    } else if (this._isSearching) {
      winnerHtml = `<div style="padding: 12px; color: #666;">Warehouse-Katalog wird durchsucht...</div>`;
    } else {
      winnerHtml = `<div style="padding: 12px; color: #d32f2f;">Kein lieferbares Produkt passt zu diesen Suchbegriffen & Filtern.</div>`;
    }

    let candidatesHtml = "";
    if (this._preview && this._preview.candidates && this._preview.candidates.length > 0) {
      candidatesHtml = `
        <div style="overflow-x: auto;">
          <table class="candidate-table">
            <thead>
              <tr>
                <th>#SKU</th>
                <th>Produkt</th>
                <th>Kategorie</th>
                <th>Packung</th>
                <th>Grundpreis</th>
                <th>Bedarf -> Summe</th>
                <th>Status</th>
                <th>Aktionen</th>
              </tr>
            </thead>
            <tbody>
              ${this._preview.candidates
                .map((c) => {
                  const isWinner = this._preview.winner && this._preview.winner.article_id === c.article_id;
                  const candCats = (c.categories || []).join(" > ") || "—";
                  return `
                  <tr>
                    <td>
                      <a href="${c.product_url || (c.slug ? `https://www.flaschenpost.de/p/${c.slug}` : `https://www.flaschenpost.de/p/${c.article_id}`)}" target="_blank" rel="noopener noreferrer" class="sku-link" title="Auf flaschenpost.de öffnen">
                        <code>#${c.article_id} ↗</code>
                      </a>
                    </td>
                    <td>
                      <strong>${c.name}</strong>
                      ${c.tier_applied ? `<span class="badge-mengenrabatt" title="Günstigerer Mengenrabatt greift">Mengenrabatt</span>` : ""}
                    </td>
                    <td><span style="font-size:0.78rem; color:#666;">${candCats}</span></td>
                    <td>${c.description || "1x"} (${Number(c.price_eur || 0).toFixed(2)} €)</td>
                    <td><strong>${c.base_metric || "-"}</strong></td>
                    <td>${c.packs}x (${c.delivered_amount}) = <strong>${Number(c.line_total_eur || 0).toFixed(2)} €</strong></td>
                    <td>
                      ${
                        isWinner
                          ? `<span class="badge-winner">Winner</span>`
                          : c.is_available
                          ? `<span class="badge-instock">Lagernd</span>`
                          : `<span class="badge-nostock">Ausverkauft</span>`
                      }
                    </td>
                    <td>
                      <div style="display:flex; flex-direction:column; gap:4px;">
                        <button class="btn-outline btn-pin-sku" data-sku-summary="${c.pinned_summary}" style="font-size:0.75rem; padding:4px 8px;" title="Genau diese SKU pinnen">
                          📌 SKU #${c.article_id}
                        </button>
                      </div>
                    </td>
                  </tr>
                `;
                })
                .join("")}
            </tbody>
          </table>
        </div>
      `;
    }

    return `
      <div class="panel-card">
        <div class="card-title" style="display:flex; justify-content:space-between; align-items:center; flex-wrap:wrap; gap:8px;">
          <div style="display:flex; align-items:center; gap:8px; flex-wrap:wrap;">
            <span>Warehouse Live Preview für: <em>${previewTitle}</em></span>
            ${catBadge}
          </div>
          ${this._isSearching ? `<span style="font-size:0.8rem; color:#888;">Suche läuft...</span>` : ""}
        </div>
        ${winnerHtml}
        ${candidatesHtml}
      </div>
    `;
  }

  _bindPreviewEvents() {
    const root = this.shadowRoot;
    if (!root) return;
    root.querySelectorAll("#preview-container .btn-pin-sku[data-sku-summary]").forEach((btn) => {
      btn.addEventListener("click", () => {
        this._addItemToList(btn.dataset.skuSummary);
      });
    });
  }

  _updatePreview() {
    const root = this.shadowRoot;
    if (!root) return;
    const container = root.getElementById("preview-container");
    if (!container) return;
    container.innerHTML = this._renderPreviewContent();
    this._bindPreviewEvents();
  }

  _updateCartUi() {
    const root = this.shadowRoot;
    if (!root) return;
    const container = root.getElementById("cart-status-card");
    if (!container) return;

    const hasItems = this._cart && this._cart.items && this._cart.items.length > 0;
    const cartTotal = hasItems ? (this._cart.total_eur || 0) : 0;
    const minOrder = this._cart ? (this._cart.min_order_value_eur || 25.0) : 25.0;
    const minOrderPercent = hasItems
      ? Math.min(100, Math.round(((cartTotal || 0) / (minOrder || 25.0)) * 100))
      : 0;
    const isReached = hasItems && Boolean(this._cart && this._cart.min_order_reached);
    const remainingToMin = hasItems
      ? (this._cart ? (this._cart.remaining_to_min_eur || 0) : minOrder)
      : minOrder;

    const lastUpdateTitle = this._lastUpdated
      ? `Letztes Update: ${new Date(this._lastUpdated).toLocaleTimeString()} (Klicken für sofortige Aktualisierung)`
      : "Klicken für sofortige Aktualisierung";

    const activeCoupons = (this._coupons || []).filter((c) => c.status === "active");
    const activeCouponLabel = activeCoupons.length > 0
      ? `🎟️ Gutschein (${activeCoupons.length})`
      : "🎟️ Gutschein";

    container.innerHTML = `
      <div class="cart-pill" id="btn-manual-refresh" title="${lastUpdateTitle}" style="cursor: pointer; display: flex; align-items: center; gap: 6px; user-select: none;">
        <span id="update-refresh-icon" style="display: inline-block; transition: transform 0.5s ease;">🔄</span>
        <span id="update-countdown-text">${this._getInitialCountdownText()}</span>
      </div>
      <div class="cart-pill">
        Warenkorb: <strong>${(cartTotal || 0).toFixed(2)} €</strong>
      </div>
      <div title="Mindestbestellwert: ${minOrder.toFixed(2)} €">
        <div style="font-size:0.75rem; color:var(--secondary-text-color,#666); margin-bottom:2px;">
          ${isReached ? "Mindestbestellwert erreicht! ✓" : `Noch ${(remainingToMin || 0).toFixed(2)} €`}
        </div>
        <div class="min-order-bar">
          <div class="min-order-fill" style="width: ${minOrderPercent}%; background: ${isReached ? "#4caf50" : "#ff9800"};"></div>
        </div>
      </div>
      <div class="cart-actions">
        <button class="btn-outline" id="btn-coupon-modal" title="Gutscheincode eingeben">${activeCouponLabel}</button>
        <button class="btn-outline" id="btn-dryrun-cart">🔍 Live Vorschau</button>
        <button class="btn-primary" id="btn-sync-cart">📥 In Warenkorb</button>
        <button class="btn-success" id="btn-buy-modal">⚡ Bestellen</button>
      </div>
    `;

    const btnRefresh = container.querySelector("#btn-manual-refresh");
    if (btnRefresh) {
      btnRefresh.addEventListener("click", () => this._manualRefresh(true));
    }

    const btnDryRun = container.querySelector("#btn-dryrun-cart");
    if (btnDryRun) {
      btnDryRun.addEventListener("click", () => this._runDryRunPreview());
    }

    const btnSyncCart = container.querySelector("#btn-sync-cart");
    if (btnSyncCart) {
      btnSyncCart.addEventListener("click", () => this._syncToCart(false));
    }

    const btnCoupon = container.querySelector("#btn-coupon-modal");
    if (btnCoupon) {
      btnCoupon.addEventListener("click", () => {
        this._showCouponModal = true;
        this._updateModalsUi();
      });
    }

    const btnBuyModal = container.querySelector("#btn-buy-modal");
    if (btnBuyModal) {
      btnBuyModal.addEventListener("click", () => {
        this._showBuyModal = true;
        this._updateModalsUi();
      });
    }
  }

  _updateTabsUi() {
    const root = this.shadowRoot;
    if (!root) return;
    const tabsContainer = root.getElementById("tabs-row");
    if (!tabsContainer) return;

    tabsContainer.innerHTML = `
      ${this._listNames
        .map((lname) => {
          const count = (this._lists[lname] || []).filter((i) => !i.completed).length;
          const activeCls = lname === this._activeList ? "active" : "";
          const lcfg = (this._listConfigs && this._listConfigs[lname]) || {};
          const rndBadge = lcfg.random_enabled ? ` 🎲${lcfg.random_count || 1}` : "";
          return `
            <button class="tab-btn ${activeCls}" data-list-name="${lname}">
              ${lname} (${count}${rndBadge})
            </button>
          `;
        })
        .join("")}
      <button class="btn-outline" id="btn-add-list">+ Neue Liste</button>
      <button class="btn-outline" id="btn-import-cart" title="Warenkorb in eine neue Einkaufsliste importieren">📥 Warenkorb importieren</button>
      ${
        this._listNames.length > 1
          ? `<button class="btn-outline btn-danger" id="btn-delete-list" title="Aktive Liste löschen">🗑</button>`
          : ""
      }
    `;

    tabsContainer.querySelectorAll(".tab-btn[data-list-name]").forEach((btn) => {
      btn.addEventListener("click", () => {
        this._activeList = btn.dataset.listName;
        this._updateTabsUi();
        this._updateListItemsUi();
        this._updateBuilderUi();
      });
    });

    const btnAddList = tabsContainer.querySelector("#btn-add-list");
    if (btnAddList) {
      btnAddList.addEventListener("click", () => this._createNewList());
    }

    const btnImportCart = tabsContainer.querySelector("#btn-import-cart");
    if (btnImportCart) {
      btnImportCart.addEventListener("click", () => this._importCartToNewList());
    }

    const btnDelList = tabsContainer.querySelector("#btn-delete-list");
    if (btnDelList) {
      btnDelList.addEventListener("click", () => this._deleteCurrentList());
    }
  }

  _bindConfigBarEvents(container, cfg) {
    const chkRandom = container.querySelector("#chk-random-enabled");
    if (chkRandom) {
      chkRandom.addEventListener("change", async (e) => {
        const enabled = e.target.checked;
        const count = cfg.random_count || 1;
        await this._saveListConfig(this._activeList, { random_enabled: enabled, random_count: count });
      });
    }

    const inputRandomCount = container.querySelector("#input-random-count");
    if (inputRandomCount) {
      inputRandomCount.addEventListener("change", async (e) => {
        const count = Math.max(1, parseInt(e.target.value, 10) || 1);
        await this._saveListConfig(this._activeList, { random_enabled: true, random_count: count });
      });
    }

    const btnResetWeights = container.querySelector("#btn-reset-weights");
    if (btnResetWeights) {
      btnResetWeights.addEventListener("click", async () => {
        await this._resetListWeights(this._activeList);
      });
    }

    const chkConsume = container.querySelector("#chk-consume-on-order");
    if (chkConsume) {
      chkConsume.addEventListener("change", async (e) => {
        await this._saveListConfig(this._activeList, { consume_on_order: e.target.checked });
      });
    }

    const selConsumeAction = container.querySelector("#sel-consume-action");
    if (selConsumeAction) {
      selConsumeAction.addEventListener("change", async (e) => {
        await this._saveListConfig(this._activeList, { consume_action: e.target.value });
      });
    }
  }

  _updateListItemsUi() {
    const root = this.shadowRoot;
    if (!root) return;
    const cardTitleSpan = root.querySelector("#active-list-card .card-title span");
    const container = root.getElementById("list-items-container");
    if (!container) return;

    const listItems = (this._lists && this._lists[this._activeList]) || [];
    const openItems = listItems.filter((it) => !it.completed);
    const completedItems = listItems.filter((it) => it.completed);

    const cfg = (this._listConfigs && this._listConfigs[this._activeList]) || {
      random_enabled: false,
      random_count: 1,
      consume_on_order: false,
      consume_action: "complete",
    };

    if (cardTitleSpan) {
      const rndInfo = cfg.random_enabled ? ` • 🎲 ${cfg.random_count || 1} zufällig` : "";
      const consumeInfo = cfg.consume_on_order
        ? ` • ⚡ Auto-Consume (${cfg.consume_action === "delete" ? "Löschen" : "Erledigt"})`
        : "";
      cardTitleSpan.textContent = `📋 ${this._activeList} (${openItems.length} offen, ${completedItems.length} erledigt${rndInfo}${consumeInfo})`;
    }

    const configBarHtml = `
      <div class="list-config-bar" style="display:flex; flex-direction:column; gap:8px;">
        <div style="display:flex; align-items:center; gap:8px; flex-wrap:wrap;">
          <label style="display:flex; align-items:center; gap:6px; cursor:pointer; font-weight:600; font-size:0.88rem;">
            <input type="checkbox" id="chk-random-enabled" ${cfg.random_enabled ? "checked" : ""} />
            <span>🎲 Zufallsauswahl (Randomizer)</span>
          </label>
          ${cfg.random_enabled ? `
            <div style="display:flex; align-items:center; gap:6px; font-size:0.85rem; margin-left:8px;">
              <span style="color:var(--secondary-text-color, #666);">Auswahl:</span>
              <input type="number" id="input-random-count" min="1" max="50" value="${cfg.random_count || 1}" style="width:55px; padding:3px 6px; border:1px solid var(--divider-color, #ccc); border-radius:4px; font-size:0.85rem;" />
              <span style="color:var(--secondary-text-color, #666);">Artikel</span>
            </div>
            <span class="badge-random-info" style="font-size:0.75rem; background:#ede7f6; color:#512da8; padding:2px 8px; border-radius:12px; font-weight:600;">
              Gewichtet (${cfg.random_count || 1} aus ${openItems.length})
            </span>
            <button class="btn-outline" id="btn-reset-weights" style="font-size:0.75rem; padding:2px 8px;" title="Alle Artikel-Gewichte auf 1.0 zurücksetzen">↺ Gewichte reset</button>
          ` : ""}
        </div>
        <div style="display:flex; align-items:center; gap:8px; flex-wrap:wrap; border-top:1px dashed var(--divider-color, #e0e0e0); padding-top:6px;">
          <label style="display:flex; align-items:center; gap:6px; cursor:pointer; font-weight:600; font-size:0.88rem;">
            <input type="checkbox" id="chk-consume-on-order" ${cfg.consume_on_order ? "checked" : ""} />
            <span>⚡ Bei Bestellung verbrauchen (Auto-Consume)</span>
          </label>
          ${cfg.consume_on_order ? `
            <div style="display:flex; align-items:center; gap:6px; font-size:0.85rem; margin-left:8px;">
              <span style="color:var(--secondary-text-color, #666);">Aktion:</span>
              <select id="sel-consume-action" style="padding:3px 6px; border:1px solid var(--divider-color, #ccc); border-radius:4px; font-size:0.85rem; background:var(--card-background-color, #fff); color:var(--primary-text-color, #333);">
                <option value="complete" ${cfg.consume_action !== "delete" ? "selected" : ""}>Als erledigt markieren</option>
                <option value="delete" ${cfg.consume_action === "delete" ? "selected" : ""}>Vollständig löschen</option>
              </select>
            </div>
          ` : ""}
        </div>
      </div>
    `;
    if (listItems.length === 0) {
      container.innerHTML = `
        ${configBarHtml}
        <div style="padding: 24px; text-align: center; color: #888;">Diese Liste ist noch leer. Nutze die Suche links, um Artikel hinzuzufügen!</div>
      `;
      this._bindConfigBarEvents(container, cfg);
      return;
    }

    container.innerHTML = `
      ${configBarHtml}
      <table class="list-items-table">
        <tbody>
          ${openItems
            .map(
              (item) => `
            <tr>
              <td style="width: 32px;">
                <input type="checkbox" class="chk-item" data-uid="${item.uid}" />
              </td>
              <td>
                <div class="item-text" style="display:inline-flex; align-items:center; flex-wrap:wrap; gap:4px;">
                  <span>${item.summary}</span>
                  ${cfg.random_enabled ? `<span class="badge-weight" title="Gewichtung: ${item.weight || 1} (Erhöht sich, solange dieser Artikel nicht gezogen wird)">🎲 ${item.weight || 1}x</span>` : ""}
                </div>
                ${item.last_resolved_note ? `<div class="item-note">💡 ${item.last_resolved_note}</div>` : ""}
              </td>
              <td style="text-align: right; white-space: nowrap;">
                <button class="btn-outline btn-edit-item" data-uid="${item.uid}" style="padding: 4px 8px; font-size: 0.8rem;">✏️</button>
                <button class="btn-outline btn-del-item" data-uid="${item.uid}" style="padding: 4px 8px; font-size: 0.8rem; color: #d32f2f;">✕</button>
              </td>
            </tr>
          `
            )
            .join("")}

          ${
            completedItems.length > 0
              ? `
            <tr>
              <td colspan="3" style="padding-top: 16px; font-size: 0.8rem; font-weight: 700; color: #888; text-transform: uppercase;">
                Erledigt (${completedItems.length})
              </td>
            </tr>
            ${completedItems
              .map(
                (item) => `
              <tr>
                <td style="width: 32px;">
                  <input type="checkbox" checked class="chk-item" data-uid="${item.uid}" />
                </td>
                <td>
                  <div class="item-text completed">${item.summary}</div>
                </td>
                <td style="text-align: right; white-space: nowrap;">
                  <button class="btn-outline btn-del-item" data-uid="${item.uid}" style="padding: 4px 8px; font-size: 0.8rem; color: #d32f2f;">✕</button>
                </td>
              </tr>
            `
              )
              .join("")}
          `
              : ""
          }
        </tbody>
      </table>
    `;

    this._bindConfigBarEvents(container, cfg);

    container.querySelectorAll(".chk-item[data-uid]").forEach((chk) => {
      chk.addEventListener("change", () => {
        const item = (this._lists[this._activeList] || []).find((it) => it.uid === chk.dataset.uid);
        if (item) this._toggleItemCompleted(item);
      });
    });

    container.querySelectorAll(".btn-edit-item[data-uid]").forEach((btn) => {
      btn.addEventListener("click", () => {
        const item = (this._lists[this._activeList] || []).find((it) => it.uid === btn.dataset.uid);
        if (item) this._loadItemIntoBuilder(item);
      });
    });

    container.querySelectorAll(".btn-del-item[data-uid]").forEach((btn) => {
      btn.addEventListener("click", () => {
        const item = (this._lists[this._activeList] || []).find((it) => it.uid === btn.dataset.uid);
        if (item) this._deleteItem(item);
      });
    });
  }

  _updateCategoryOptionsUi() {
    const root = this.shadowRoot;
    if (!root) return;
    const catSelect = root.getElementById("category-select");
    if (!catSelect) return;

    let optsHtml = '<option value="">Alle Kategorien</option>';
    if (this._categories && this._categories.length > 0) {
      optsHtml += this._categories
        .map(
          (c) => `<option value="${c.value}" ${this._category === c.value ? "selected" : ""}>${c.label}</option>`
        )
        .join("");
    }
    catSelect.innerHTML = optsHtml;
    catSelect.value = this._category || "";
  }

  _updateBuilderUi() {
    const root = this.shadowRoot;
    if (!root) return;

    const searchInput = root.getElementById("search-input");
    if (searchInput && searchInput.value !== (this._query || "")) {
      searchInput.value = this._query || "";
    }

    const amountInput = root.getElementById("amount-input");
    if (amountInput && Number(amountInput.value) !== Number(this._amount || 1)) {
      amountInput.value = this._amount || 1;
    }

    const unitSelect = root.getElementById("unit-select");
    if (unitSelect && unitSelect.value !== (this._unit || "x")) {
      unitSelect.value = this._unit || "x";
    }

    const sortSelect = root.getElementById("sort-select");
    if (sortSelect && sortSelect.value !== (this._sort || "price")) {
      sortSelect.value = this._sort || "price";
    }

    const catSelect = root.getElementById("category-select");
    if (catSelect && catSelect.value !== (this._category || "")) {
      catSelect.value = this._category || "";
    }

    const builderTitle = root.getElementById("builder-title");
    if (builderTitle) {
      builderTitle.textContent = this._editingUid ? "✏️ Eintrag bearbeiten" : "🔍 Artikel suchen & konfigurieren";
    }

    const btnCancel = root.getElementById("btn-cancel-edit");
    if (btnCancel) {
      btnCancel.style.display = this._editingUid ? "inline-flex" : "none";
    }

    const btnSave = root.getElementById("btn-save-builder");
    if (btnSave) {
      btnSave.textContent = this._editingUid ? "💾 Eintrag aktualisieren" : `➕ Zu '${this._activeList}' hinzufügen`;
    }

    this._updateChips();
  }

  _updateModalsUi() {
    const root = this.shadowRoot;
    if (!root) return;
    const container = root.getElementById("modals-container");
    if (!container) return;

    const hasItems = this._cart && this._cart.items && this._cart.items.length > 0;
    const cartTotal = hasItems ? (this._cart.total_eur || 0) : 0;

    let dryRunHtml = "";
    if (this._showDryRunModal && this._dryRunResult) {
      dryRunHtml = `
        <div class="modal-overlay" id="modal-overlay-dryrun">
          <div class="modal-box">
            <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 16px;">
              <h2 style="margin: 0; font-size: 1.25rem;">Live Resolution Vorschau (${this._dryRunResult.lists_merged ? this._dryRunResult.lists_merged.join(", ") : ""})</h2>
              <button class="btn-outline" id="btn-close-dryrun">✕</button>
            </div>

            <div style="margin-bottom: 16px; font-size: 1.05rem;">
              Geschätzter Warenwert: <strong>${(this._dryRunResult.estimated_goods_eur || 0).toFixed(2)} €</strong>
            </div>

            <div style="overflow-x: auto; max-height: 45vh;">
              <table class="candidate-table">
                <thead>
                  <tr>
                    <th>Anfrage</th>
                    <th>Gewähltes Produkt</th>
                    <th>Menge</th>
                    <th>Grundpreis</th>
                    <th>Gesamt</th>
                  </tr>
                </thead>
                <tbody>
                  ${(this._dryRunResult.resolved_items || [])
                    .map(
                      (r) => `
                    <tr>
                      <td><code>${r.requested}</code></td>
                      <td>
                        <strong>${r.name}</strong><br />
                        <a href="${r.product_url || (r.slug ? `https://www.flaschenpost.de/p/${r.slug}` : `https://www.flaschenpost.de/p/${r.article_id}`)}" target="_blank" rel="noopener noreferrer" class="sku-link" style="font-size:0.75rem;" title="Auf flaschenpost.de öffnen">#${r.article_id} ↗</a>
                      </td>
                      <td>${r.quantity_added}x (${r.delivered_amount})</td>
                      <td>${r.price_per_kg_eur ? `${Number(r.price_per_kg_eur).toFixed(2)} €/kg` : `${Number(r.unit_price_eur || 0).toFixed(2)} €`}</td>
                      <td><strong>${Number(r.line_total_eur || 0).toFixed(2)} €</strong></td>
                    </tr>
                  `
                    )
                    .join("")}
                </tbody>
              </table>
            </div>

            ${
              this._dryRunResult.unresolved && this._dryRunResult.unresolved.length > 0
                ? `
              <div style="margin-top: 14px; padding: 10px 14px; background: #ffebee; border-radius: 8px; color: #c62828; font-size: 0.85rem;">
                <strong>Nicht auflösbar / Ausverkauft:</strong> ${this._dryRunResult.unresolved.join(", ")}
              </div>
            `
                : ""
            }

            <div style="display: flex; justify-content: flex-end; gap: 10px; margin-top: 20px;">
              <button class="btn-secondary" id="btn-sync-replace">Warenkorb leeren & neu füllen</button>
              <button class="btn-primary" id="btn-sync-append">Zu aktuellem Warenkorb hinzufügen</button>
            </div>
          </div>
        </div>
      `;
    }

    const activeCoupons = (this._coupons || []).filter((c) => c.status === "active");
    const nextCoupon = activeCoupons.length > 0 ? activeCoupons[0] : null;

    let buyHtml = "";
    if (this._showBuyModal) {
      buyHtml = `
        <div class="modal-overlay" id="modal-overlay-buy">
          <div class="modal-box">
            <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 16px;">
              <h2 style="margin: 0; font-size: 1.25rem; color: #2e7d32;">⚡ Flaschenpost Bestellung aufgeben</h2>
              <button class="btn-outline" id="btn-close-buy">✕</button>
            </div>

            <div style="background: var(--secondary-background-color, #f9f9f9); padding: 16px; border-radius: 8px; margin-bottom: 16px;">
              <div style="font-size: 1.1rem; font-weight: 700; margin-bottom: 8px;">
                Warenkorb Gesamtsumme: ${(cartTotal || 0).toFixed(2)} €
              </div>
              <div style="font-size: 0.9rem; color: #666;">
                Enthaltene Artikel: ${this._cart && this._cart.items ? this._cart.items.length : 0}
              </div>
              ${nextCoupon ? `
                <div style="margin-top: 10px; padding: 8px 12px; background: #e8f5e9; border: 1px solid #c8e6c9; border-radius: 6px; color: #2e7d32; font-size: 0.9rem;">
                  🎟️ <strong>Gutscheincode:</strong> <code>${nextCoupon.code}</code> (${nextCoupon.description || "Hinterlegter Gutschein"}) wird bei dieser Bestellung eingelöst!
                </div>
              ` : ""}
            </div>

            <div style="margin: 16px 0;">
              <label style="display: flex; align-items: center; gap: 8px; font-size: 0.95rem; cursor: pointer;">
                <input type="checkbox" id="chk-buy-confirm" ${this._buyConfirmChecked ? "checked" : ""} />
                <strong>Ich bestätige die kostenpflichtige Bestellung bei Flaschenpost.</strong>
              </label>
            </div>

            <div style="display: flex; justify-content: flex-end; gap: 10px; margin-top: 20px;">
              <button class="btn-secondary" id="btn-cancel-buy">Abbrechen</button>
              <button class="btn-success" id="btn-submit-buy" ${!this._buyConfirmChecked || this._buyLoading ? "disabled" : ""}>
                ${this._buyLoading ? "Bestellung wird gesendet..." : "Jetzt kostenpflichtig bestellen"}
              </button>
            </div>
          </div>
        </div>
      `;
    }

    let couponHtml = "";
    if (this._showCouponModal) {
      const activeList = (this._coupons || []).filter((c) => c.status === "active");
      const usedList = (this._coupons || []).filter((c) => c.status === "used");

      couponHtml = `
        <div class="modal-overlay" id="modal-overlay-coupon">
          <div class="modal-box">
            <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 16px;">
              <h2 style="margin: 0; font-size: 1.25rem; color: var(--primary-text-color, #212121);">🎟️ Flaschenpost Gutscheincodes</h2>
              <button class="btn-outline" id="btn-close-coupon">✕</button>
            </div>

            <p style="font-size: 0.9rem; color: var(--secondary-text-color, #666); margin-top: 0; margin-bottom: 16px;">
              Gutscheincodes werden in der Datenbank gespeichert und automatisch bei deiner nächsten Flaschenpost-Bestellung eingelöst.
            </p>

            <div style="display: flex; gap: 8px; margin-bottom: 20px; flex-wrap: wrap;">
              <input type="text" id="input-coupon-code" placeholder="Gutscheincode (z. B. FLASCHE10)" style="flex: 2; min-width: 160px; padding: 8px 12px; border: 1px solid var(--divider-color, #ddd); border-radius: 6px; font-weight: bold; text-transform: uppercase;" />
              <input type="text" id="input-coupon-desc" placeholder="Beschreibung (optional)" style="flex: 3; min-width: 160px; padding: 8px 12px; border: 1px solid var(--divider-color, #ddd); border-radius: 6px;" />
              <button class="btn-primary" id="btn-save-coupon">Hinzufügen</button>
            </div>

            <div style="font-size: 1rem; font-weight: 600; margin-bottom: 8px;">
              Aktive Gutscheine (${activeList.length})
            </div>
            ${activeList.length === 0 ? `
              <div style="padding: 12px; background: var(--secondary-background-color, #f9f9f9); border-radius: 6px; color: #888; font-size: 0.9rem; margin-bottom: 16px;">
                Keine aktiven Gutscheincodes hinterlegt.
              </div>
            ` : `
              <div style="display: flex; flex-direction: column; gap: 8px; margin-bottom: 16px; max-height: 25vh; overflow-y: auto;">
                ${activeList.map((c) => `
                  <div style="display: flex; justify-content: space-between; align-items: center; padding: 10px 14px; background: #e8f5e9; border: 1px solid #c8e6c9; border-radius: 8px;">
                    <div>
                      <strong style="font-family: monospace; font-size: 1.05rem; color: #1b5e20;">${c.code}</strong>
                      ${c.description ? `<span style="margin-left: 8px; color: #555; font-size: 0.85rem;">— ${c.description}</span>` : ""}
                      <div style="font-size: 0.75rem; color: #2e7d32; margin-top: 2px;">✓ Aktiv • Wird bei nächster Bestellung eingelöst</div>
                    </div>
                    <button class="btn-outline btn-delete-coupon" data-code="${c.code}" style="padding: 4px 8px; color: #c62828;" title="Gutschein entfernen">🗑️</button>
                  </div>
                `).join("")}
              </div>
            `}

            ${usedList.length > 0 ? `
              <div style="font-size: 0.9rem; font-weight: 600; margin-bottom: 8px; color: #777;">
                Bereits eingelöste Gutscheine (${usedList.length})
              </div>
              <div style="display: flex; flex-direction: column; gap: 6px; max-height: 15vh; overflow-y: auto;">
                ${usedList.map((c) => `
                  <div style="display: flex; justify-content: space-between; align-items: center; padding: 8px 12px; background: var(--secondary-background-color, #f5f5f5); border-radius: 6px; font-size: 0.85rem; color: #777;">
                    <div>
                      <span style="font-family: monospace; text-decoration: line-through;">${c.code}</span>
                      <span style="margin-left: 8px;">${c.order_id ? `(Bestellung #${c.order_id})` : "(Eingelöst)"}</span>
                    </div>
                    <button class="btn-outline btn-delete-coupon" data-code="${c.code}" style="padding: 2px 6px; font-size: 0.75rem;" title="Aus Verlauf entfernen">✕</button>
                  </div>
                `).join("")}
              </div>
            ` : ""}

            <div style="display: flex; justify-content: flex-end; margin-top: 20px;">
              <button class="btn-secondary" id="btn-close-coupon-footer">Schließen</button>
            </div>
          </div>
        </div>
      `;
    }

    container.innerHTML = `${dryRunHtml}${buyHtml}${couponHtml}`;

    const btnCloseCoupon = container.querySelector("#btn-close-coupon");
    const btnCloseCouponFooter = container.querySelector("#btn-close-coupon-footer");
    if (btnCloseCoupon) {
      btnCloseCoupon.addEventListener("click", () => {
        this._showCouponModal = false;
        this._updateModalsUi();
      });
    }
    if (btnCloseCouponFooter) {
      btnCloseCouponFooter.addEventListener("click", () => {
        this._showCouponModal = false;
        this._updateModalsUi();
      });
    }

    const btnSaveCoupon = container.querySelector("#btn-save-coupon");
    if (btnSaveCoupon) {
      btnSaveCoupon.addEventListener("click", () => {
        const codeInput = container.querySelector("#input-coupon-code");
        const descInput = container.querySelector("#input-coupon-desc");
        const code = codeInput ? codeInput.value : "";
        const desc = descInput ? descInput.value : "";
        if (code && code.trim()) {
          this._addCoupon(code.trim(), desc ? desc.trim() : "");
        }
      });
    }

    container.querySelectorAll(".btn-delete-coupon").forEach((btn) => {
      btn.addEventListener("click", (e) => {
        const code = e.currentTarget.getAttribute("data-code");
        if (code) {
          this._deleteCoupon(code);
        }
      });
    });

    const btnCloseDryRun = container.querySelector("#btn-close-dryrun");
    if (btnCloseDryRun) {
      btnCloseDryRun.addEventListener("click", () => {
        this._showDryRunModal = false;
        this._updateModalsUi();
      });
    }

    const btnSyncReplace = container.querySelector("#btn-sync-replace");
    if (btnSyncReplace) {
      btnSyncReplace.addEventListener("click", () => this._syncToCart(true));
    }

    const btnSyncAppend = container.querySelector("#btn-sync-append");
    if (btnSyncAppend) {
      btnSyncAppend.addEventListener("click", () => this._syncToCart(false));
    }

    const btnCloseBuy = container.querySelector("#btn-close-buy");
    const btnCancelBuy = container.querySelector("#btn-cancel-buy");
    if (btnCloseBuy) {
      btnCloseBuy.addEventListener("click", () => {
        this._showBuyModal = false;
        this._updateModalsUi();
      });
    }
    if (btnCancelBuy) {
      btnCancelBuy.addEventListener("click", () => {
        this._showBuyModal = false;
        this._updateModalsUi();
      });
    }

    const chkBuyConfirm = container.querySelector("#chk-buy-confirm");
    if (chkBuyConfirm) {
      chkBuyConfirm.addEventListener("change", (e) => {
        this._buyConfirmChecked = e.target.checked;
        const btnSubmit = container.querySelector("#btn-submit-buy");
        if (btnSubmit) {
          btnSubmit.disabled = !this._buyConfirmChecked || this._buyLoading;
        }
      });
    }

    const btnSubmitBuy = container.querySelector("#btn-submit-buy");
    if (btnSubmitBuy) {
      btnSubmitBuy.addEventListener("click", () => this._executeBuyCart());
    }
  }

  _ensureDom() {
    if (this._domCreated) return;
    this._domCreated = true;

    this.shadowRoot.innerHTML = `
      <style>
        :host {
          display: block;
          font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
          color: var(--primary-text-color, #212121);
          background-color: var(--primary-background-color, #fafafa);
          box-sizing: border-box;
          padding: 16px;
          min-height: 100%;
        }

        * {
          box-sizing: border-box;
        }

        .header-bar {
          display: flex;
          flex-wrap: wrap;
          align-items: center;
          justify-content: space-between;
          gap: 12px;
          background: var(--card-background-color, #ffffff);
          border-radius: 12px;
          padding: 16px 20px;
          box-shadow: 0 2px 8px rgba(0,0,0,0.06);
          margin-bottom: 16px;
        }

        .brand-title {
          display: flex;
          align-items: center;
          gap: 10px;
          font-size: 1.35rem;
          font-weight: 700;
          color: var(--primary-color, #e53935);
        }

        .cart-status-card {
          display: flex;
          align-items: center;
          gap: 16px;
          flex-wrap: wrap;
        }

        .cart-pill {
          background: var(--secondary-background-color, #f0f2f5);
          padding: 6px 14px;
          border-radius: 20px;
          font-size: 0.9rem;
          font-weight: 600;
        }

        .min-order-bar {
          width: 140px;
          height: 10px;
          background: #e0e0e0;
          border-radius: 5px;
          overflow: hidden;
          position: relative;
        }

        .min-order-fill {
          height: 100%;
          transition: width 0.3s ease;
        }

        .cart-actions {
          display: flex;
          gap: 8px;
        }

        button {
          font-family: inherit;
          font-size: 0.88rem;
          font-weight: 600;
          padding: 8px 14px;
          border-radius: 8px;
          border: none;
          cursor: pointer;
          transition: all 0.2s ease;
          display: inline-flex;
          align-items: center;
          gap: 6px;
        }

        .btn-primary {
          background: var(--primary-color, #e53935);
          color: #ffffff;
        }
        .btn-primary:hover {
          opacity: 0.9;
        }

        .btn-secondary {
          background: var(--secondary-background-color, #e0e0e0);
          color: var(--primary-text-color, #212121);
        }
        .btn-secondary:hover {
          background: #d5d5d5;
        }

        .btn-success {
          background: #4caf50;
          color: white;
        }
        .btn-success:hover {
          background: #43a047;
        }

        .btn-danger {
          background: #f44336;
          color: white;
        }

        .btn-outline {
          background: transparent;
          border: 1px solid var(--divider-color, #e0e0e0);
          color: var(--primary-text-color, #212121);
        }
        .btn-outline:hover {
          background: rgba(0,0,0,0.04);
        }

        .tabs-row {
          display: flex;
          align-items: center;
          gap: 8px;
          overflow-x: auto;
          margin-bottom: 16px;
          padding-bottom: 4px;
        }

        .tab-btn {
          background: var(--card-background-color, #ffffff);
          border: 1px solid var(--divider-color, #e0e0e0);
          color: var(--primary-text-color, #212121);
          padding: 8px 16px;
          border-radius: 20px;
          font-weight: 600;
          white-space: nowrap;
        }
        .tab-btn.active {
          background: var(--primary-color, #e53935);
          color: #ffffff;
          border-color: var(--primary-color, #e53935);
        }

        .layout-grid {
          display: grid;
          grid-template-columns: 1fr;
          gap: 16px;
        }

        @media (min-width: 900px) {
          .layout-grid {
            grid-template-columns: 420px 1fr;
          }
        }

        .panel-card {
          background: var(--card-background-color, #ffffff);
          border-radius: 12px;
          padding: 16px 20px;
          box-shadow: 0 2px 8px rgba(0,0,0,0.06);
        }

        .card-title {
          font-size: 1.1rem;
          font-weight: 700;
          margin-bottom: 14px;
          display: flex;
          justify-content: space-between;
          align-items: center;
        }

        .form-group {
          margin-bottom: 12px;
        }

        label {
          display: block;
          font-size: 0.82rem;
          font-weight: 600;
          color: var(--secondary-text-color, #757575);
          margin-bottom: 5px;
        }

        input[type="text"], input[type="number"], select {
          width: 100%;
          padding: 9px 12px;
          border-radius: 8px;
          border: 1px solid var(--divider-color, #cccccc);
          background: var(--primary-background-color, #ffffff);
          color: var(--primary-text-color, #212121);
          font-size: 0.95rem;
          outline: none;
        }
        input:focus, select:focus {
          border-color: var(--primary-color, #e53935);
        }

        .amount-unit-row {
          display: grid;
          grid-template-columns: 110px 1fr;
          gap: 8px;
        }

        .chips-container {
          display: flex;
          flex-wrap: wrap;
          gap: 6px;
          margin-top: 6px;
        }

        .chip {
          display: inline-flex;
          align-items: center;
          gap: 4px;
          padding: 4px 10px;
          border-radius: 16px;
          font-size: 0.82rem;
          font-weight: 600;
          cursor: pointer;
          user-select: none;
          transition: all 0.15s ease;
        }

        .chip-neg-avail {
          background: #ffebee;
          color: #c62828;
          border: 1px dashed #ef9a9a;
        }
        .chip-neg-avail:hover {
          background: #ffcdd2;
        }

        .chip-neg-active {
          background: #d32f2f;
          color: #ffffff;
          border: 1px solid #b71c1c;
        }

        .chip-pos-avail {
          background: #e8f5e9;
          color: #2e7d32;
          border: 1px dashed #a5d6a7;
        }
        .chip-pos-avail:hover {
          background: #c8e6c9;
        }

        .chip-pos-active {
          background: #388e3c;
          color: #ffffff;
          border: 1px solid #1b5e20;
        }

        .chip-close {
          font-size: 1.05rem;
          line-height: 1;
          margin-left: 2px;
        }

        .custom-kw-adder {
          display: flex;
          gap: 6px;
          margin-top: 8px;
        }

        .winner-banner {
          background: #fff8e1;
          border: 2px solid #ffe082;
          border-radius: 10px;
          padding: 12px 16px;
          margin: 14px 0;
          display: flex;
          flex-direction: column;
          gap: 6px;
        }

        .winner-badge {
          display: inline-flex;
          align-items: center;
          gap: 4px;
          background: #ffa000;
          color: #ffffff;
          font-size: 0.75rem;
          font-weight: 800;
          padding: 2px 8px;
          border-radius: 12px;
          width: fit-content;
          text-transform: uppercase;
        }

        .badge-cat {
          background: #e0f2fe;
          color: #0284c7;
          font-size: 0.75rem;
          font-weight: 700;
          padding: 3px 8px;
          border-radius: 6px;
          border: 1px solid rgba(2, 132, 199, 0.3);
          display: inline-flex;
          align-items: center;
          gap: 4px;
        }
        .badge-cat-sm {
          background: #e0f2fe;
          color: #0284c7;
          font-size: 0.72rem;
          font-weight: 700;
          padding: 1px 6px;
          border-radius: 4px;
        }
        .badge-mengenrabatt {
          background: #e8f5e9;
          color: #2e7d32;
          font-size: 0.72rem;
          font-weight: 700;
          padding: 1px 6px;
          border-radius: 4px;
          margin-left: 4px;
          text-transform: uppercase;
        }

        .candidate-table {
          width: 100%;
          border-collapse: collapse;
          font-size: 0.85rem;
          margin-top: 10px;
        }

        .candidate-table th {
          background: var(--secondary-background-color, #f5f5f5);
          text-align: left;
          padding: 8px 10px;
          font-weight: 700;
          color: var(--secondary-text-color, #616161);
        }

        .candidate-table td {
          padding: 8px 10px;
          border-top: 1px solid var(--divider-color, #eeeeee);
          vertical-align: middle;
        }

        .sku-link {
          color: var(--primary-color, #e53935);
          text-decoration: none;
          font-weight: 700;
          display: inline-flex;
          align-items: center;
          gap: 2px;
        }
        .sku-link:hover {
          text-decoration: underline;
          opacity: 0.85;
        }
        .sku-link code {
          color: inherit;
          cursor: pointer;
        }

        .badge-winner {
          background: #4caf50;
          color: white;
          font-weight: 700;
          padding: 2px 6px;
          border-radius: 4px;
          font-size: 0.75rem;
        }
        .badge-instock {
          background: #e3f2fd;
          color: #1565c0;
          font-weight: 600;
          padding: 2px 6px;
          border-radius: 4px;
          font-size: 0.75rem;
        }
        .badge-nostock {
          background: #ffebee;
          color: #c62828;
          font-weight: 600;
          padding: 2px 6px;
          border-radius: 4px;
          font-size: 0.75rem;
        }

        .list-config-bar {
          display: flex;
          align-items: center;
          justify-content: space-between;
          background: var(--secondary-background-color, #f8f9fa);
          border: 1px solid var(--divider-color, #e0e0e0);
          border-radius: 8px;
          padding: 8px 12px;
          margin-bottom: 12px;
          flex-wrap: wrap;
          gap: 8px;
        }

        .badge-weight {
          display: inline-flex;
          align-items: center;
          font-size: 0.75rem;
          font-weight: 700;
          background: #f3e5f5;
          color: #7b1fa2;
          padding: 1px 6px;
          border-radius: 4px;
        }

        .badge-random-info {
          display: inline-flex;
          align-items: center;
          font-size: 0.75rem;
          font-weight: 600;
          background: #ede7f6;
          color: #512da8;
          padding: 2px 8px;
          border-radius: 12px;
        }

        .list-items-table {
          width: 100%;
          border-collapse: collapse;
          margin-top: 8px;
        }

        .list-items-table td {
          padding: 10px 8px;
          border-bottom: 1px solid var(--divider-color, #f0f0f0);
          vertical-align: middle;
        }

        .item-text {
          font-size: 0.95rem;
          font-weight: 600;
        }
        .item-text.completed {
          text-decoration: line-through;
          color: var(--secondary-text-color, #9e9e9e);
        }

        .item-note {
          font-size: 0.8rem;
          color: var(--secondary-text-color, #757575);
          margin-top: 2px;
        }

        .modal-overlay {
          position: fixed;
          top: 0; left: 0; right: 0; bottom: 0;
          background: rgba(0,0,0,0.5);
          display: flex;
          align-items: center;
          justify-content: center;
          z-index: 1000;
          padding: 20px;
        }

        .modal-box {
          background: var(--card-background-color, #ffffff);
          border-radius: 12px;
          max-width: 720px;
          width: 100%;
          max-height: 85vh;
          overflow-y: auto;
          padding: 20px 24px;
          box-shadow: 0 10px 30px rgba(0,0,0,0.25);
        }

        .toast {
          position: fixed;
          bottom: 24px;
          right: 24px;
          padding: 12px 20px;
          border-radius: 8px;
          color: white;
          font-weight: 600;
          box-shadow: 0 4px 12px rgba(0,0,0,0.2);
          z-index: 1200;
          animation: slideUp 0.25s ease;
        }
        .toast.success { background: #323232; }
        .toast.error { background: #d32f2f; }

        @keyframes slideUp {
          from { transform: translateY(20px); opacity: 0; }
          to { transform: translateY(0); opacity: 1; }
        }
      </style>

      <!-- Toast container -->
      <div id="toast-container"></div>

      <!-- Top Header & Cart summary -->
      <div class="header-bar">
        <div class="brand-title">
          <span>🛒</span> Flaschenpost Einkaufslisten
        </div>
        <div class="cart-status-card" id="cart-status-card"></div>
      </div>

      <!-- Named Lists Tabs -->
      <div class="tabs-row" id="tabs-row"></div>

      <!-- Main Layout: Left = Builder, Right = List items & Preview -->
      <div class="layout-grid">
        <!-- Search & Interactive Builder Panel (DOM Nodes NEVER Rebuilt) -->
        <div class="panel-card">
          <div class="card-title">
            <span id="builder-title">🔍 Artikel suchen & konfigurieren</span>
            <button class="btn-outline" style="font-size:0.75rem; padding:4px 8px; display:none;" id="btn-cancel-edit">Abbrechen</button>
          </div>

          <div class="form-group">
            <label for="search-input">Produktbegriff</label>
            <input type="text" id="search-input" placeholder="z.B. Heidelbeeren, Hafermilch, Cola..." />
          </div>

          <div class="form-group amount-unit-row">
            <div>
              <label for="amount-input">Menge</label>
              <input type="number" id="amount-input" step="any" min="0.1" value="1" />
            </div>
            <div>
              <label for="unit-select">Einheit</label>
              <select id="unit-select">
                <option value="x" selected>x (Stück / Packung)</option>
                <option value="g">g (Gramm)</option>
                <option value="kg">kg (Kilogramm)</option>
                <option value="ml">ml (Milliliter)</option>
                <option value="L">L (Liter)</option>
                <option value="Kiste">Kiste(n)</option>
              </select>
            </div>
          </div>

          <div class="form-group">
            <label for="sort-select">Sortierkriterium</label>
            <select id="sort-select">
              <option value="price" selected>Günstigster Gesamtpreis (Standard)</option>
              <option value="price_per_kg">Günstigster Grundpreis (€/kg)</option>
              <option value="price_per_liter">Günstigster Grundpreis (€/L)</option>
              <option value="price_per_unit">Günstigster Stückpreis (€/Einheit)</option>
              <option value="relevance">Relevanz (Flaschenpost)</option>
            </select>
          </div>

          <div class="form-group" id="category-group">
            <label for="category-select">Kategorie-Filter (Optional)</label>
            <select id="category-select">
              <option value="">Alle Kategorien</option>
            </select>
          </div>

          <!-- Negative Keywords Builder Section -->
          <div class="form-group" style="margin-top: 14px;">
            <label style="color: #c62828;">🚫 Negative Keywords (Ausschließen mit !)</label>
            <div id="chips-neg-container" class="chips-container"></div>
          </div>

          <!-- Positive Keywords Builder Section -->
          <div class="form-group">
            <label style="color: #2e7d32;">✓ Positive Keywords (Qualifizierer)</label>
            <div id="chips-pos-container" class="chips-container"></div>
          </div>

          <!-- Custom Keyword Adder Input -->
          <div class="custom-kw-adder">
            <input type="text" id="custom-kw-input" placeholder="Eigenes Keyword (z.B. laktose, bio)..." />
            <button class="btn-secondary" id="btn-add-pos-kw" title="Als positives Keyword">+</button>
            <button class="btn-danger" id="btn-add-neg-kw" title="Als negatives Keyword (Ausschluss)">!</button>
          </div>

          <!-- Add to List Action -->
          <div style="margin-top: 18px;">
            <button class="btn-primary" style="width: 100%; justify-content: center; padding: 12px;" id="btn-save-builder">
              ➕ Zu 'Einkaufsliste' hinzufügen
            </button>
          </div>
        </div>

        <!-- Right Side: Items in Active List & Live Search Candidates -->
        <div style="display: flex; flex-direction: column; gap: 16px;">
          <!-- Live Preview Search Results Card -->
          <div id="preview-container"></div>

          <!-- Active Shopping List Items Card -->
          <div class="panel-card" id="active-list-card">
            <div class="card-title">
              <span>📋 Einkaufsliste</span>
              <div style="display: flex; gap: 8px;">
                <button class="btn-outline" id="btn-sync-active-list">In Warenkorb</button>
              </div>
            </div>
            <div id="list-items-container"></div>
          </div>
        </div>
      </div>

      <!-- Modals Container -->
      <div id="modals-container"></div>
    `;

    this._bindBuilderEvents();
    this._updateCartUi();
    this._updateTabsUi();
    this._updateListItemsUi();
    this._updateCategoryOptionsUi();
    this._updateBuilderUi();
    this._updatePreview();
    this._updateModalsUi();
  }

  _bindBuilderEvents() {
    const root = this.shadowRoot;
    if (!root) return;

    const searchInput = root.getElementById("search-input");
    if (searchInput) {
      searchInput.addEventListener("input", (e) => {
        this._query = e.target.value;
        this._triggerSearchDebounced(1000);
      });
      searchInput.addEventListener("keydown", (e) => {
        if (e.key === "Enter") {
          e.preventDefault();
          this._handleSaveFromBuilder();
        }
      });
    }

    const amountInput = root.getElementById("amount-input");
    if (amountInput) {
      amountInput.addEventListener("input", (e) => {
        this._amount = parseFloat(e.target.value) || 1;
        this._triggerSearchDebounced(1000);
      });
    }

    const unitSelect = root.getElementById("unit-select");
    if (unitSelect) {
      unitSelect.addEventListener("change", (e) => {
        this._unit = e.target.value;
        this._triggerSearchDebounced(250);
      });
    }

    const sortSelect = root.getElementById("sort-select");
    if (sortSelect) {
      sortSelect.addEventListener("change", (e) => {
        this._sort = e.target.value;
        this._triggerSearchDebounced(250);
      });
    }

    const catSelect = root.getElementById("category-select");
    if (catSelect) {
      catSelect.addEventListener("change", (e) => {
        this._category = e.target.value;
        this._triggerSearchDebounced(250);
      });
    }

    const customKwInput = root.getElementById("custom-kw-input");
    const btnAddPos = root.getElementById("btn-add-pos-kw");
    const btnAddNeg = root.getElementById("btn-add-neg-kw");

    if (btnAddPos && customKwInput) {
      btnAddPos.addEventListener("click", () => {
        this._addCustomKeyword(customKwInput.value, false);
        customKwInput.value = "";
      });
    }
    if (btnAddNeg && customKwInput) {
      btnAddNeg.addEventListener("click", () => {
        this._addCustomKeyword(customKwInput.value, true);
        customKwInput.value = "";
      });
    }
    if (customKwInput) {
      customKwInput.addEventListener("keydown", (e) => {
        if (e.key === "Enter") {
          e.preventDefault();
          this._addCustomKeyword(customKwInput.value, false);
          customKwInput.value = "";
        }
      });
    }

    const btnSaveBuilder = root.getElementById("btn-save-builder");
    if (btnSaveBuilder) {
      btnSaveBuilder.addEventListener("click", () => this._handleSaveFromBuilder());
    }

    const btnCancelEdit = root.getElementById("btn-cancel-edit");
    if (btnCancelEdit) {
      btnCancelEdit.addEventListener("click", () => {
        this._clearBuilder();
        this._updateBuilderUi();
        this._updatePreview();
      });
    }

    const btnSyncActive = root.getElementById("btn-sync-active-list");
    if (btnSyncActive) {
      btnSyncActive.addEventListener("click", () => this._syncToCart(false));
    }
  }

  render() {
    this._ensureDom();
    this._updateCartUi();
    this._updateTabsUi();
    this._updateListItemsUi();
    this._updateCategoryOptionsUi();
    this._updateBuilderUi();
    this._updatePreview();
    this._updateModalsUi();
  }

  _handleSaveFromBuilder() {
    let summary = "";
    if (this._preview && this._preview.lazy_summary) {
      summary = this._preview.lazy_summary;
    } else {
      const q = this._buildCurrentQueryString();
      if (!q) return;
      const amtPart = this._unit === "x" && Number(this._amount) <= 1 ? "" : `${this._amount}${this._unit === "x" ? "x" : this._unit} `;
      const catPart = this._category ? `${this._category}::` : "";
      const sortPart = this._sort && this._sort !== "price" ? ` [${this._sort}]` : "";
      summary = `${amtPart}${catPart}${q}${sortPart}`;
    }
    this._addItemToList(summary);
  }
}

customElements.define("flaschenpost-view", FlaschenpostView);

class FlaschenpostPanel extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: "open" });
    this._view = document.createElement("flaschenpost-view");
    this.shadowRoot.appendChild(this._view);
  }

  set hass(hass) {
    this._view.hass = hass;
  }

  set panel(panel) {
    this._view.setConfig(panel && panel.config ? panel.config : {});
  }
}

customElements.define("flaschenpost-panel", FlaschenpostPanel);

class FlaschenpostCard extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: "open" });
    this._view = document.createElement("flaschenpost-view");
    this.shadowRoot.appendChild(this._view);
  }

  set hass(hass) {
    this._view.hass = hass;
  }

  setConfig(config) {
    this._view.setConfig(config || {});
  }

  getCardSize() {
    return 6;
  }
}

customElements.define("flaschenpost-card", FlaschenpostCard);

window.customCards = window.customCards || [];
window.customCards.push({
  type: "flaschenpost-card",
  name: "Flaschenpost Shopping List Card",
  description: "Interactive Flaschenpost shopping list with live product search, keyword chips, and cart actions",
  preview: true,
});
