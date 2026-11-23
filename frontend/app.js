const { createApp } = Vue;

const COLORS = ['#7db4ed', '#f7b66e', '#a3d680', '#e894a0', '#baa0e8', '#6ecfc8', '#f0d666', '#d6a078',
  '#96c3a0', '#e6a0dc', '#a0afcd', '#fa9678', '#82c8eb', '#c8d26e', '#bebebe'];

async function api(path, opts = {}) {
  const o = { ...opts };
  if (o.body && !(o.body instanceof FormData)) {
    o.headers = { 'Content-Type': 'application/json' };
    o.body = JSON.stringify(o.body);
  }
  const r = await fetch(path, o);
  let data = null;
  try { data = await r.json(); } catch (_) { /* sin cuerpo */ }
  if (!r.ok) {
    let d = data && data.detail !== undefined ? data.detail : r.statusText;
    if (d && d.errores) d = d.errores.join('\n');
    else if (Array.isArray(d)) d = d.map(e => (e.loc ? e.loc.slice(1).join('.') + ': ' : '') + (e.msg || e)).join('\n');
    throw new Error(d);
  }
  return data;
}

const emptyOrder = () => ({ id: null, order_number: '', client: '', blind_type: 'vertical', width: null, height: null,
  quantity: 1, color: '', texture: '', thickness: 3, priority: 'Normal', manual_pieces: [{ nombre: '', ancho: null, alto: null, cantidad: 1 }] });
const emptyLot = () => ({ id: null, code: '', height: 2440, width: 1220, thickness: 3, color: '', texture: '',
  directional: false, quantity: 1, unit_cost: null, is_offcut: false });

createApp({
  data() {
    return {
      tabs: [{ id: 'plan', label: 'Planeación' }, { id: 'orders', label: 'Cola de órdenes' },
        { id: 'inventory', label: 'Inventario' }, { id: 'runs', label: 'Corridas' }, { id: 'settings', label: 'Configuración' }],
      tab: 'plan', lots: [], orders: [], runs: [], settings: null, blindTypes: {},
      run: null, runId: null, running: false, progress: {}, toasts: [],
      orderForm: emptyOrder(), lotForm: emptyLot(), despiece: [], despieceErr: '',
      impact: null, dragIdx: null, dragOver: null, lotFilter: '', showLabels: true, modalSheet: null,
    };
  },
  computed: {
    activeOrders() { return this.orders.filter(o => o.status === 'pendiente' || o.status === 'parcial'); },
    doneOrders() { return this.orders.filter(o => o.status === 'cortada'); },
    totalSheets() { return this.lots.filter(l => !l.is_offcut).reduce((a, l) => a + l.quantity, 0); },
    offcutCount() { return this.lots.filter(l => l.is_offcut).reduce((a, l) => a + l.quantity, 0); },
    M() { return this.run.result.metricas; },
    B() { return this.run.result.linea_base; },
    knownColors() { return [...new Set(this.lots.map(l => l.color))]; },
    knownTextures() { return [...new Set(this.lots.map(l => l.texture))]; },
    filteredLots() {
      const f = this.lotFilter.toLowerCase();
      return f ? this.lots.filter(l => JSON.stringify(l).toLowerCase().includes(f)) : this.lots;
    },
    orderIndex() {
      const idx = {};
      if (this.run && this.run.result) this.run.result.ordenes.forEach((o, i) => { idx[o.order_id] = i; });
      return idx;
    },
    groupedPending() {
      const m = {};
      for (const p of (this.run?.result?.pendientes || [])) {
        const k = p.order_number + p.piece_key + p.w + p.h;
        (m[k] ||= { ...p, n: 0 }).n++;
      }
      return Object.values(m);
    },
  },
  watch: {
    orderForm: { deep: true, handler() { this.debouncedDespiece(); } },
  },
  async mounted() {
    this.blindTypes = await api('/api/blind-types');
    await Promise.all([this.loadLots(), this.loadOrders(), this.loadSettings(), this.loadRuns()]);
    const latest = this.runs.find(r => r.status === 'simulada' || r.status === 'confirmada');
    if (latest) await this.viewRun(latest.id, false);
    const running = this.runs.find(r => r.status === 'ejecutando');
    if (running) { this.runId = running.id; this.running = true; this.poll(); }
  },
  methods: {
    toast(msg, kind = 'ok') {
      const id = Math.random();
      this.toasts.push({ id, msg, kind });
      setTimeout(() => { this.toasts = this.toasts.filter(t => t.id !== id); }, kind === 'err' ? 8000 : 3500);
    },
    go(t) { this.tab = t; if (t === 'runs') this.loadRuns(); if (t === 'orders') this.refreshImpact(); },
    pct(v) { return v == null ? '—' : (v * 100).toFixed(1) + ' %'; },
    money(v) { return (v || 0).toLocaleString('es-MX', { minimumFractionDigits: 2, maximumFractionDigits: 2 }); },
    orderColor(id) { return COLORS[(this.orderIndex[id] ?? 0) % COLORS.length]; },
    labelSize(p) { const s = Math.min(Math.max(p.w, p.h) / 7, Math.min(p.w, p.h) * 0.55); return Math.max(20, Math.min(110, s)); },
    spark(h) {
      if (!h || h.length < 2) return '0,18 160,18';
      const mn = Math.min(...h), mx = Math.max(...h), r = (mx - mn) || 1;
      return h.map((v, i) => `${(i / (h.length - 1)) * 160},${4 + (1 - (v - mn) / r) * 28}`).join(' ');
    },

    async loadLots() { this.lots = await api('/api/lots'); },
    async loadOrders() { this.orders = await api('/api/orders?all=true'); },
    async loadSettings() { this.settings = await api('/api/settings'); },
    async loadRuns() { this.runs = await api('/api/runs'); },
    async loadDemo() {
      const r = await api('/api/demo', { method: 'POST' });
      this.toast(`Demostración cargada: ${r.lotes} lotes, ${r.ordenes} órdenes`);
      await Promise.all([this.loadLots(), this.loadOrders()]);
    },

    // ------------------------------------------------------------ órdenes
    debouncedDespiece() {
      clearTimeout(this._dt);
      this._dt = setTimeout(this.previewDespiece, 250);
    },
    async previewDespiece() {
      const f = this.orderForm;
      if (f.blind_type !== 'manual' && !(f.width > 0 && f.height > 0)) { this.despiece = []; this.despieceErr = ''; return; }
      try {
        this.despiece = await api('/api/despiece', { method: 'POST', body: {
          blind_type: f.blind_type, width: f.width, height: f.height, quantity: f.quantity || 1,
          manual_pieces: f.blind_type === 'manual' ? f.manual_pieces.filter(m => m.ancho > 0 && m.alto > 0) : null } });
        this.despieceErr = '';
      } catch (e) { this.despiece = []; this.despieceErr = e.message; }
    },
    async saveOrder() {
      const f = { ...this.orderForm };
      if (f.blind_type !== 'manual') f.manual_pieces = null;
      try {
        if (f.id) await api('/api/orders/' + f.id, { method: 'PUT', body: f });
        else await api('/api/orders', { method: 'POST', body: f });
        this.toast(f.id ? 'Orden actualizada' : `Orden ${f.order_number} agregada a la cola`);
        const keep = { color: f.color, texture: f.texture, thickness: f.thickness, blind_type: f.blind_type };
        this.orderForm = { ...emptyOrder(), ...keep };
        await this.loadOrders();
        this.refreshImpact();
      } catch (e) { this.toast(e.message, 'err'); }
    },
    editOrder(o) {
      this.orderForm = { ...emptyOrder(), ...JSON.parse(JSON.stringify(o)),
        manual_pieces: o.manual_pieces || [{ nombre: '', ancho: null, alto: null, cantidad: 1 }] };
      window.scrollTo({ top: 0, behavior: 'smooth' });
    },
    resetOrder() { this.orderForm = emptyOrder(); },
    async deleteOrder(o) {
      if (!confirm(`¿Eliminar la orden ${o.order_number}?`)) return;
      await api('/api/orders/' + o.id, { method: 'DELETE' });
      await this.loadOrders(); this.refreshImpact();
    },
    async move(o, dir) {
      await api(`/api/orders/${o.id}/move?direction=${dir}`, { method: 'POST' });
      await this.loadOrders(); this.refreshImpact();
    },
    async dropAt(i) {
      this.dragOver = null;
      if (this.dragIdx === null || this.dragIdx === i) return;
      const ids = this.activeOrders.map(o => o.id);
      const [m] = ids.splice(this.dragIdx, 1);
      ids.splice(i, 0, m);
      this.dragIdx = null;
      await api('/api/orders/reorder', { method: 'POST', body: { ids } });
      await this.loadOrders(); this.refreshImpact();
    },
    async refreshImpact() {
      if (!this.activeOrders.length) { this.impact = null; return; }
      try { this.impact = await api('/api/plan/preview', { method: 'POST' }); } catch (_) { this.impact = null; }
    },
    impactOf(id) { return this.impact ? this.impact.ordenes.find(o => o.order_id === id) : null; },

    // --------------------------------------------------------- inventario
    async saveLot() {
      const f = { ...this.lotForm };
      if (f.unit_cost === '' ) f.unit_cost = null;
      try {
        if (f.id) await api('/api/lots/' + f.id, { method: 'PUT', body: f });
        else await api('/api/lots', { method: 'POST', body: f });
        this.toast(f.id ? 'Lote actualizado' : `Lote ${f.code} registrado`);
        this.lotForm = emptyLot();
        await this.loadLots();
      } catch (e) { this.toast(e.message, 'err'); }
    },
    editLot(l) { this.lotForm = { ...l }; window.scrollTo({ top: 0, behavior: 'smooth' }); },
    resetLot() { this.lotForm = emptyLot(); },
    async deleteLot(l) {
      if (!confirm(`¿Eliminar el lote ${l.code}?`)) return;
      await api('/api/lots/' + l.id, { method: 'DELETE' });
      await this.loadLots();
    },
    async importFile(ev, kind) {
      const file = ev.target.files[0];
      ev.target.value = '';
      if (!file) return;
      const fd = new FormData();
      fd.append('file', file);
      try {
        const r = await api(`/api/${kind}/import`, { method: 'POST', body: fd });
        this.toast(`${r.importados} registros importados`);
        await (kind === 'lots' ? this.loadLots() : this.loadOrders());
      } catch (e) {
        this.toast('Importación rechazada (no se cargó ningún registro):\n' + e.message, 'err');
      }
    },

    // ----------------------------------------------------- configuración
    async saveSettings() {
      try {
        const s = JSON.parse(JSON.stringify(this.settings));
        if (s.ga.semilla === '') s.ga.semilla = null;
        this.settings = await api('/api/settings', { method: 'PUT', body: s });
        this.toast('Parámetros guardados');
      } catch (e) { this.toast(e.message, 'err'); }
    },
    async resetSettings() {
      if (!confirm('¿Restaurar los parámetros por defecto?')) return;
      this.settings = await api('/api/settings/reset', { method: 'POST' });
      this.toast('Parámetros restaurados');
    },

    // --------------------------------------------------------- corridas
    async startRun() {
      try {
        const r = await api('/api/runs', { method: 'POST' });
        this.runId = r.run_id; this.running = true; this.progress = {}; this.tab = 'plan';
        this.poll();
      } catch (e) { this.toast(e.message, 'err'); }
    },
    async poll() {
      const r = await api('/api/runs/' + this.runId);
      if (r.status === 'ejecutando') {
        this.progress = r.progreso || {};
        setTimeout(this.poll, 500);
        return;
      }
      this.running = false;
      if (r.status === 'error') { this.toast('Error en la optimización: ' + r.error, 'err'); return; }
      this.run = r;
      this.toast(`Optimización ${r.id} lista (${r.result.tiempo_total_s} s)`);
      this.loadRuns();
    },
    async cancelRun() { await api(`/api/runs/${this.runId}/cancel`, { method: 'POST' }); },
    async viewRun(id, switchTab = true) {
      this.run = await api('/api/runs/' + id);
      if (switchTab) this.tab = 'plan';
    },
    async confirmRun() {
      if (!confirm('Se descontarán las hojas del inventario, se registrarán los retazos recuperables y las órdenes cubiertas pasarán a "cortada". ¿Confirmar?')) return;
      try {
        const r = await api(`/api/runs/${this.run.id}/confirm`, { method: 'POST' });
        this.toast(`Confirmada: ${r.hojas_descontadas} hojas descontadas, ${r.retazos_registrados} retazos dados de alta`);
        await Promise.all([this.viewRun(this.run.id, false), this.loadLots(), this.loadOrders(), this.loadRuns()]);
      } catch (e) { this.toast(e.message, 'err'); }
    },
    async discardRun() {
      await api(`/api/runs/${this.run.id}/discard`, { method: 'POST' });
      await this.viewRun(this.run.id, false); this.loadRuns();
    },
    openSheet(s) { this.modalSheet = s; },
  },
}).mount('#app');
