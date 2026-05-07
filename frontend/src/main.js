import maplibregl from 'maplibre-gl';
import 'maplibre-gl/dist/maplibre-gl.css';
import { Protocol } from 'pmtiles';

// ---------------------------------------------------------------------------
// PMTiles protocol registration
// ---------------------------------------------------------------------------
const protocol = new Protocol();
maplibregl.addProtocol('pmtiles', protocol.tile);

// ---------------------------------------------------------------------------
// App state — Develop mode + tech filters + Acquire mode REPD filters
// ---------------------------------------------------------------------------
const TECH_DEFAULTS = {
  solar: {
    pvoutMin: 920,
    distMaxKm: 10,
    areaMin: 5,
    excludes: {
      aonb: true,
      national_park: true,
      sssi: true,
      green_belt: false,
      flood: true
    }
  },
  wind: {
    windMin: 7.0,
    distMaxKm: 10,
    areaMin: 5,
    excludes: {
      aonb: true,
      national_park: true,
      sssi: true,
      green_belt: false,
      flood: false
    }
  },
  battery: {
    distMaxKm: 5,
    areaMin: 5,
    excludes: {
      aonb: false,
      national_park: true,
      sssi: true,
      green_belt: false,
      flood: false
    }
  }
};

const ACQUIRE_DEFAULTS = {
  techs: new Set(['Solar', 'Wind', 'Battery', 'Hydro', 'Other']),
  statuses: new Set([
    'Operational',
    'Under Construction',
    'Planning Permission Granted'
  ]),
  capacityMin: 0,
  capacityMax: 500
};

const state = {
  mode: 'develop', // 'develop' | 'acquire'
  tech: 'solar', // 'solar' | 'wind' | 'battery'
  develop: structuredClone(TECH_DEFAULTS),
  acquire: {
    techs: new Set(ACQUIRE_DEFAULTS.techs),
    statuses: new Set(ACQUIRE_DEFAULTS.statuses),
    capacityMin: ACQUIRE_DEFAULTS.capacityMin,
    capacityMax: ACQUIRE_DEFAULTS.capacityMax
  },
  layerVis: {
    'constraint-aonb': true,
    'constraint-national-park': true,
    'constraint-green-belt': true,
    'constraint-sssi': true,
    'constraint-flood': true,
    'constraint-listed-building': false,
    'constraint-scheduled-monument': false,
    'solar-raster': false,
    'wind-raster': false,
    'substation-fill': true,
    'repd-circle': true
  }
};

// ---------------------------------------------------------------------------
// Boot
// ---------------------------------------------------------------------------
async function boot() {
  const urls = await fetch('/tile_urls.json').then((r) => r.json());

  const map = new maplibregl.Map({
    container: 'map',
    style: buildStyle(urls),
    center: [-1.7, 55.15],
    zoom: 8.5,
    hash: true,
    attributionControl: { compact: true }
  });

  map.addControl(new maplibregl.NavigationControl({ visualizePitch: false }), 'top-right');
  map.addControl(new maplibregl.ScaleControl({ maxWidth: 120, unit: 'metric' }), 'bottom-left');

  map.on('load', () => {
    wireInteractions(map);
    wireTopbar(map);
    renderFilterPanel(map);
    applyParcelStyling(map);
    wireMethodologyModal();
  });

  window._map = map;
  window._state = state;
}

// ---------------------------------------------------------------------------
// Style builder
// ---------------------------------------------------------------------------
function buildStyle(urls) {
  return {
    version: 8,
    glyphs: 'https://demotiles.maplibre.org/font/{fontstack}/{range}.pbf',
    sources: {
      basemap: {
        type: 'raster',
        tiles: ['https://basemaps.cartocdn.com/light_all/{z}/{x}/{y}@2x.png'],
        tileSize: 256,
        attribution: '&copy; CARTO &copy; OpenStreetMap contributors'
      },
      parcels: { type: 'vector', url: `pmtiles://${urls.tiles.parcels}` },
      substations: { type: 'vector', url: `pmtiles://${urls.tiles.substations}` },
      repd: { type: 'vector', url: `pmtiles://${urls.tiles.repd}` },
      constraints: { type: 'vector', url: `pmtiles://${urls.tiles.constraints}` },
      ne_polygon: { type: 'vector', url: `pmtiles://${urls.tiles.ne_polygon}` },
      solar_raster: {
        type: 'raster',
        url: `pmtiles://${urls.tiles.solar}`,
        tileSize: 256,
        attribution: 'Global Solar Atlas'
      },
      wind_raster: {
        type: 'raster',
        url: `pmtiles://${urls.tiles.wind}`,
        tileSize: 256,
        attribution: 'Global Wind Atlas'
      }
    },
    layers: [
      { id: 'background', type: 'background', paint: { 'background-color': '#eef1f4' } },
      { id: 'basemap', type: 'raster', source: 'basemap', paint: { 'raster-opacity': 0.6 } },

      {
        id: 'solar-raster',
        type: 'raster',
        source: 'solar_raster',
        layout: { visibility: 'none' },
        paint: { 'raster-opacity': 0.7 }
      },
      {
        id: 'wind-raster',
        type: 'raster',
        source: 'wind_raster',
        layout: { visibility: 'none' },
        paint: { 'raster-opacity': 0.7 }
      },

      {
        id: 'ne-outline',
        type: 'line',
        source: 'ne_polygon',
        'source-layer': 'ne_polygon',
        paint: { 'line-color': '#1a3552', 'line-width': 1.2, 'line-opacity': 0.5 }
      },

      {
        id: 'parcels-fill',
        type: 'fill',
        source: 'parcels',
        'source-layer': 'parcels',
        paint: { 'fill-color': '#4a90e2', 'fill-opacity': 0.2 }
      },
      {
        id: 'parcels-line',
        type: 'line',
        source: 'parcels',
        'source-layer': 'parcels',
        paint: { 'line-color': '#4a90e2', 'line-width': 0.5, 'line-opacity': 0.4 }
      },

      {
        id: 'constraint-aonb',
        type: 'fill',
        source: 'constraints',
        'source-layer': 'national_landscape',
        paint: { 'fill-color': '#88dd88', 'fill-opacity': 0.25 }
      },
      {
        id: 'constraint-national-park',
        type: 'fill',
        source: 'constraints',
        'source-layer': 'national_park',
        paint: { 'fill-color': '#66cc66', 'fill-opacity': 0.3 }
      },
      {
        id: 'constraint-green-belt',
        type: 'fill',
        source: 'constraints',
        'source-layer': 'green_belt',
        paint: { 'fill-color': '#aacc88', 'fill-opacity': 0.2 }
      },
      {
        id: 'constraint-sssi',
        type: 'fill',
        source: 'constraints',
        'source-layer': 'sssi',
        paint: { 'fill-color': '#cc8866', 'fill-opacity': 0.25 }
      },
      {
        id: 'constraint-flood',
        type: 'fill',
        source: 'constraints',
        'source-layer': 'flood_zones',
        paint: { 'fill-color': '#5588cc', 'fill-opacity': 0.2 }
      },
      {
        id: 'constraint-scheduled-monument',
        type: 'fill',
        source: 'constraints',
        'source-layer': 'scheduled_monument',
        layout: { visibility: 'none' },
        paint: { 'fill-color': '#aa6688', 'fill-opacity': 0.4 }
      },
      {
        id: 'constraint-listed-building',
        type: 'fill',
        source: 'constraints',
        'source-layer': 'listed_building',
        layout: { visibility: 'none' },
        paint: { 'fill-color': '#bb6688', 'fill-opacity': 0.3 }
      },

      {
        id: 'substation-fill',
        type: 'fill',
        source: 'substations',
        'source-layer': 'substations',
        paint: {
          'fill-color': '#ff6633',
          'fill-opacity': 0.4,
          'fill-outline-color': '#cc4422'
        }
      },
      {
        id: 'substation-line',
        type: 'line',
        source: 'substations',
        'source-layer': 'substations',
        paint: { 'line-color': '#cc4422', 'line-width': 1 }
      },

      {
        id: 'repd-circle',
        type: 'circle',
        source: 'repd',
        'source-layer': 'repd',
        paint: {
          'circle-color': [
            'match',
            ['get', 'Development Status'],
            'Operational', '#2ca02c',
            'Under Construction', '#1f77b4',
            'Planning Permission Granted', '#ff7f0e',
            '#888888'
          ],
          'circle-radius': [
            'interpolate',
            ['linear'],
            ['coalesce', ['to-number', ['get', 'Installed Capacity (MWelec)']], 1],
            0, 3,
            10, 6,
            50, 10,
            200, 14
          ],
          'circle-stroke-color': '#ffffff',
          'circle-stroke-width': 1,
          'circle-opacity': 0.85
        }
      }
    ]
  };
}

// ---------------------------------------------------------------------------
// Topbar wiring (mode + tech + filters button)
// ---------------------------------------------------------------------------
function wireTopbar(map) {
  // Mode toggle
  document.querySelectorAll('#mode-toggle .seg-btn').forEach((btn) => {
    btn.addEventListener('click', () => {
      const newMode = btn.dataset.mode;
      if (newMode === state.mode) return;
      state.mode = newMode;
      document.querySelectorAll('#mode-toggle .seg-btn').forEach((b) => {
        b.classList.toggle('active', b.dataset.mode === newMode);
        b.setAttribute('aria-selected', b.dataset.mode === newMode);
      });
      // Hide/show tech selector
      const techToggle = document.getElementById('tech-toggle');
      techToggle.classList.toggle('hidden', newMode !== 'develop');
      // Toggle parcel visibility
      const vis = newMode === 'develop' ? 'visible' : 'none';
      ['parcels-fill', 'parcels-line'].forEach((id) => {
        if (map.getLayer(id)) map.setLayoutProperty(id, 'visibility', vis);
      });
      renderFilterPanel(map);
      if (newMode === 'develop') {
        applyParcelStyling(map);
        // Clear REPD filter
        if (map.getLayer('repd-circle')) map.setFilter('repd-circle', null);
      } else {
        applyAcquireFilters(map);
      }
      updatePanelTitle();
    });
  });

  // Tech toggle
  document.querySelectorAll('#tech-toggle .seg-btn').forEach((btn) => {
    btn.addEventListener('click', () => {
      const newTech = btn.dataset.tech;
      if (newTech === state.tech) return;
      state.tech = newTech;
      document.querySelectorAll('#tech-toggle .seg-btn').forEach((b) => {
        b.classList.toggle('active', b.dataset.tech === newTech);
        b.setAttribute('aria-selected', b.dataset.tech === newTech);
      });
      renderFilterPanel(map);
      applyParcelStyling(map);
      updatePanelTitle();
    });
  });

  // Filters button
  const filtersBtn = document.getElementById('filters-btn');
  const filterPanel = document.getElementById('filter-panel');
  const closeBtn = document.getElementById('filter-panel-close');

  filtersBtn.addEventListener('click', () => {
    const open = filterPanel.classList.toggle('visible');
    filtersBtn.classList.toggle('active', open);
    document.body.classList.toggle('filter-open', open);
    filterPanel.setAttribute('aria-hidden', open ? 'false' : 'true');
  });
  closeBtn.addEventListener('click', () => {
    filterPanel.classList.remove('visible');
    filtersBtn.classList.remove('active');
    document.body.classList.remove('filter-open');
    filterPanel.setAttribute('aria-hidden', 'true');
  });
}

function updatePanelTitle() {
  const t = document.getElementById('filter-panel-title');
  if (state.mode === 'develop') {
    const techLabel = state.tech.charAt(0).toUpperCase() + state.tech.slice(1);
    t.textContent = `Develop · ${techLabel}`;
  } else {
    t.textContent = 'Acquire · REPD';
  }
}

// ---------------------------------------------------------------------------
// Filter panel (renders the contents based on mode + tech)
// ---------------------------------------------------------------------------
function renderFilterPanel(map) {
  updatePanelTitle();
  const body = document.getElementById('filter-panel-body');
  body.innerHTML = '';

  if (state.mode === 'develop') {
    body.appendChild(buildDevelopFilters(map));
  } else {
    body.appendChild(buildAcquireFilters(map));
  }
  body.appendChild(buildLayerToggles(map));
}

function buildDevelopFilters(map) {
  const frag = document.createDocumentFragment();
  const tech = state.tech;
  const cfg = state.develop[tech];

  // A. Resource threshold (solar / wind only)
  if (tech === 'solar' || tech === 'wind') {
    const sec = section('Resource threshold');
    if (tech === 'solar') {
      sec.appendChild(
        slider({
          id: 'pvoutMin',
          label: 'Min. PVOUT (kWh/kWp/yr)',
          min: 800,
          max: 1000,
          step: 1,
          value: cfg.pvoutMin,
          onInput: (v) => {
            cfg.pvoutMin = v;
            scheduleParcelUpdate(map);
          }
        })
      );
    } else {
      sec.appendChild(
        slider({
          id: 'windMin',
          label: 'Min. wind speed @100m (m/s)',
          min: 5,
          max: 13,
          step: 0.1,
          value: cfg.windMin,
          formatter: (v) => v.toFixed(1),
          onInput: (v) => {
            cfg.windMin = v;
            scheduleParcelUpdate(map);
          }
        })
      );
    }
    frag.appendChild(sec);
  }

  // B. Substation distance
  {
    const sec = section('Substation distance');
    const isBattery = tech === 'battery';
    sec.appendChild(
      slider({
        id: 'distMaxKm',
        label: isBattery
          ? 'Max. dist. to substation w/ any headroom (km)'
          : 'Max. dist. to substation w/ gen headroom (km)',
        min: 1,
        max: isBattery ? 50 : 80,
        step: 1,
        value: cfg.distMaxKm,
        onInput: (v) => {
          cfg.distMaxKm = v;
          scheduleParcelUpdate(map);
        }
      })
    );
    frag.appendChild(sec);
  }

  // C. Min parcel area
  {
    const sec = section('Parcel size');
    sec.appendChild(
      slider({
        id: 'areaMin',
        label: 'Min parcel area (ha)',
        min: 2,
        max: 100,
        step: 1,
        value: cfg.areaMin,
        onInput: (v) => {
          cfg.areaMin = v;
          scheduleParcelUpdate(map);
        }
      })
    );
    frag.appendChild(sec);
  }

  // D. Constraint exclusions
  {
    const sec = section('Constraint exclusions');
    const items = [
      { key: 'aonb', label: 'AONB / National Landscape', swatch: '#88dd88' },
      { key: 'national_park', label: 'National Park', swatch: '#66cc66' },
      { key: 'sssi', label: 'SSSI', swatch: '#cc8866' },
      { key: 'green_belt', label: 'Green Belt', swatch: '#aacc88' },
      { key: 'flood', label: 'Flood Zone', swatch: '#5588cc' }
    ];
    for (const item of items) {
      sec.appendChild(
        checkbox({
          label: `Exclude: ${item.label}`,
          checked: !!cfg.excludes[item.key],
          swatch: item.swatch,
          onChange: (checked) => {
            cfg.excludes[item.key] = checked;
            scheduleParcelUpdate(map);
          }
        })
      );
    }
    frag.appendChild(sec);
  }

  return frag;
}

function buildAcquireFilters(map) {
  const frag = document.createDocumentFragment();
  const cfg = state.acquire;

  // Tech filter chips
  {
    const sec = section('Technology');
    const techs = ['Solar', 'Wind', 'Battery', 'Hydro', 'Other'];
    const chipGroup = document.createElement('div');
    chipGroup.className = 'chip-group';
    for (const t of techs) {
      const chip = document.createElement('button');
      chip.type = 'button';
      chip.className = 'chip' + (cfg.techs.has(t) ? ' active' : '');
      chip.textContent = t;
      chip.addEventListener('click', () => {
        if (cfg.techs.has(t)) cfg.techs.delete(t);
        else cfg.techs.add(t);
        chip.classList.toggle('active');
        scheduleAcquireUpdate(map);
      });
      chipGroup.appendChild(chip);
    }
    sec.appendChild(chipGroup);
    frag.appendChild(sec);
  }

  // Status filter chips
  {
    const sec = section('Development status');
    const statuses = [
      'Operational',
      'Under Construction',
      'Planning Permission Granted',
      'Planning Application Submitted',
      'Other'
    ];
    const chipGroup = document.createElement('div');
    chipGroup.className = 'chip-group';
    for (const s of statuses) {
      const chip = document.createElement('button');
      chip.type = 'button';
      chip.className = 'chip' + (cfg.statuses.has(s) ? ' active' : '');
      chip.textContent = s;
      chip.addEventListener('click', () => {
        if (cfg.statuses.has(s)) cfg.statuses.delete(s);
        else cfg.statuses.add(s);
        chip.classList.toggle('active');
        scheduleAcquireUpdate(map);
      });
      chipGroup.appendChild(chip);
    }
    sec.appendChild(chipGroup);
    frag.appendChild(sec);
  }

  // Capacity range (dual slider)
  {
    const sec = section('Capacity (MWelec)');
    const wrapper = document.createElement('div');
    wrapper.className = 'slider-row';

    const labelEl = document.createElement('label');
    labelEl.innerHTML = `<span>Range</span><span class="value" id="cap-value">${cfg.capacityMin}–${cfg.capacityMax} MW</span>`;
    wrapper.appendChild(labelEl);

    const dualWrap = document.createElement('div');
    dualWrap.className = 'dual-slider';

    const minSlider = document.createElement('input');
    minSlider.type = 'range';
    minSlider.min = '0';
    minSlider.max = '500';
    minSlider.step = '5';
    minSlider.value = String(cfg.capacityMin);

    const maxSlider = document.createElement('input');
    maxSlider.type = 'range';
    maxSlider.min = '0';
    maxSlider.max = '500';
    maxSlider.step = '5';
    maxSlider.value = String(cfg.capacityMax);

    const updateLabel = () => {
      document.getElementById('cap-value').textContent = `${cfg.capacityMin}–${cfg.capacityMax} MW`;
    };

    minSlider.addEventListener('input', () => {
      let v = Number(minSlider.value);
      if (v > cfg.capacityMax) v = cfg.capacityMax;
      cfg.capacityMin = v;
      minSlider.value = String(v);
      updateLabel();
      scheduleAcquireUpdate(map);
    });
    maxSlider.addEventListener('input', () => {
      let v = Number(maxSlider.value);
      if (v < cfg.capacityMin) v = cfg.capacityMin;
      cfg.capacityMax = v;
      maxSlider.value = String(v);
      updateLabel();
      scheduleAcquireUpdate(map);
    });

    dualWrap.appendChild(minSlider);
    dualWrap.appendChild(maxSlider);
    wrapper.appendChild(dualWrap);
    sec.appendChild(wrapper);
    frag.appendChild(sec);
  }

  return frag;
}

function buildLayerToggles(map) {
  const sec = section('Layers', { collapsible: true, collapsed: true });

  const items = [
    { id: 'constraint-aonb', label: 'AONB / National Landscape', swatch: '#88dd88' },
    { id: 'constraint-national-park', label: 'National Park', swatch: '#66cc66' },
    { id: 'constraint-green-belt', label: 'Green Belt', swatch: '#aacc88' },
    { id: 'constraint-sssi', label: 'SSSI', swatch: '#cc8866' },
    { id: 'constraint-flood', label: 'Flood Zone', swatch: '#5588cc' },
    { id: 'constraint-listed-building', label: 'Listed Buildings', swatch: '#bb6688' },
    { id: 'constraint-scheduled-monument', label: 'Scheduled Monuments', swatch: '#aa6688' },
    { id: 'solar-raster', label: 'Solar PVOUT raster', swatch: '#f4c542' },
    { id: 'wind-raster', label: 'Wind speed raster', swatch: '#80b3d3' },
    { id: 'substation-fill', label: 'Substations', swatch: '#ff6633' },
    { id: 'repd-circle', label: 'REPD sites', swatch: '#2ca02c' }
  ];

  for (const item of items) {
    const cb = checkbox({
      label: item.label,
      checked: !!state.layerVis[item.id],
      swatch: item.swatch,
      onChange: (checked) => {
        state.layerVis[item.id] = checked;
        const vis = checked ? 'visible' : 'none';
        if (map.getLayer(item.id)) map.setLayoutProperty(item.id, 'visibility', vis);
        // Also toggle substation-line companion
        if (item.id === 'substation-fill' && map.getLayer('substation-line')) {
          map.setLayoutProperty('substation-line', 'visibility', vis);
        }
      }
    });
    sec.appendChild(cb);
  }

  return sec;
}

// ---------------------------------------------------------------------------
// UI helpers
// ---------------------------------------------------------------------------
function section(title, opts = {}) {
  const sec = document.createElement('div');
  sec.className = 'filter-section';
  if (opts.collapsible) {
    sec.classList.add('collapsible');
    if (opts.collapsed) sec.classList.add('collapsed');
  }
  const h = document.createElement('h4');
  h.textContent = title;
  sec.appendChild(h);

  const body = document.createElement('div');
  body.className = 'filter-section-body';
  sec.appendChild(body);

  if (opts.collapsible) {
    h.addEventListener('click', () => {
      sec.classList.toggle('collapsed');
    });
  }

  // Override appendChild to push into body instead
  const _append = sec.appendChild.bind(sec);
  sec.appendChild = (node) => {
    // First two children (h4 + body) are already attached; subsequent go into body
    if (node === h || node === body) return _append(node);
    return body.appendChild(node);
  };
  return sec;
}

function slider({ id, label, min, max, step, value, formatter, onInput }) {
  const wrapper = document.createElement('div');
  wrapper.className = 'slider-row';
  const fmt = formatter || ((v) => String(v));

  const labelEl = document.createElement('label');
  labelEl.innerHTML = `<span>${label}</span><span class="value">${fmt(value)}</span>`;
  wrapper.appendChild(labelEl);

  const input = document.createElement('input');
  input.type = 'range';
  input.id = id;
  input.min = String(min);
  input.max = String(max);
  input.step = String(step);
  input.value = String(value);
  input.addEventListener('input', () => {
    const v = Number(input.value);
    labelEl.querySelector('.value').textContent = fmt(v);
    onInput(v);
  });
  wrapper.appendChild(input);
  return wrapper;
}

function checkbox({ label, checked, swatch, onChange }) {
  const row = document.createElement('label');
  row.className = 'checkbox-row';
  const cb = document.createElement('input');
  cb.type = 'checkbox';
  cb.checked = !!checked;
  cb.addEventListener('change', () => onChange(cb.checked));
  row.appendChild(cb);
  if (swatch) {
    const sw = document.createElement('span');
    sw.className = 'swatch';
    sw.style.background = swatch;
    row.appendChild(sw);
  }
  const txt = document.createElement('span');
  txt.textContent = label;
  row.appendChild(txt);
  return row;
}

// ---------------------------------------------------------------------------
// Parcel filter expression + styling
// ---------------------------------------------------------------------------
function buildParcelFilterExpression({ tech, pvoutMin, windMin, distMaxKm, areaMin, excludes }) {
  const conditions = ['all'];

  if (tech === 'solar') {
    conditions.push(['>=', ['coalesce', ['to-number', ['get', 'mean_pvout_kwhkwp']], 0], pvoutMin]);
    conditions.push([
      '<=',
      ['coalesce', ['to-number', ['get', 'dist_substation_gen_headroom_m']], 1e9],
      distMaxKm * 1000
    ]);
  } else if (tech === 'wind') {
    conditions.push([
      '>=',
      ['coalesce', ['to-number', ['get', 'mean_wind_speed_100m_ms']], 0],
      windMin
    ]);
    conditions.push([
      '<=',
      ['coalesce', ['to-number', ['get', 'dist_substation_gen_headroom_m']], 1e9],
      distMaxKm * 1000
    ]);
  } else if (tech === 'battery') {
    conditions.push([
      '<=',
      ['coalesce', ['to-number', ['get', 'dist_substation_any_headroom_m']], 1e9],
      distMaxKm * 1000
    ]);
  }

  conditions.push(['>=', ['coalesce', ['to-number', ['get', 'area_ha']], 0], areaMin]);

  const flagFalse = (key) => [
    '!=',
    ['coalesce', ['to-string', ['get', key]], 'false'],
    'true'
  ];
  if (excludes.aonb) conditions.push(flagFalse('intersects_aonb'));
  if (excludes.national_park) conditions.push(flagFalse('intersects_national_park'));
  if (excludes.sssi) conditions.push(flagFalse('intersects_sssi'));
  if (excludes.green_belt) conditions.push(flagFalse('intersects_green_belt'));
  if (excludes.flood) conditions.push(flagFalse('intersects_flood'));

  return conditions;
}

let _parcelUpdateTimer = null;
function scheduleParcelUpdate(map) {
  if (_parcelUpdateTimer) clearTimeout(_parcelUpdateTimer);
  _parcelUpdateTimer = setTimeout(() => applyParcelStyling(map), 50);
}

function applyParcelStyling(map) {
  if (state.mode !== 'develop') return;
  if (!map.getLayer('parcels-fill')) return;
  const cfg = state.develop[state.tech];
  const expr = buildParcelFilterExpression({
    tech: state.tech,
    pvoutMin: cfg.pvoutMin,
    windMin: cfg.windMin,
    distMaxKm: cfg.distMaxKm,
    areaMin: cfg.areaMin,
    excludes: cfg.excludes
  });
  map.setPaintProperty('parcels-fill', 'fill-color', [
    'case',
    expr,
    '#2ca02c',
    '#cccccc'
  ]);
  map.setPaintProperty('parcels-fill', 'fill-opacity', ['case', expr, 0.5, 0.08]);
  map.setPaintProperty('parcels-line', 'line-color', ['case', expr, '#1f7a1f', '#999999']);
  map.setPaintProperty('parcels-line', 'line-opacity', ['case', expr, 0.7, 0.1]);
}

// ---------------------------------------------------------------------------
// Acquire (REPD) filter
// ---------------------------------------------------------------------------
let _acquireUpdateTimer = null;
function scheduleAcquireUpdate(map) {
  if (_acquireUpdateTimer) clearTimeout(_acquireUpdateTimer);
  _acquireUpdateTimer = setTimeout(() => applyAcquireFilters(map), 50);
}

function applyAcquireFilters(map) {
  if (!map.getLayer('repd-circle')) return;
  const cfg = state.acquire;

  // Tech: REPD "Technology Type" maps roughly to our chips
  const techCondition = buildTechMatch(cfg.techs);
  const statusCondition = buildStatusMatch(cfg.statuses);
  const capExpr = ['coalesce', ['to-number', ['get', 'Installed Capacity (MWelec)']], 0];

  const filter = [
    'all',
    techCondition,
    statusCondition,
    ['>=', capExpr, cfg.capacityMin],
    ['<=', capExpr, cfg.capacityMax]
  ];
  map.setFilter('repd-circle', filter);
}

function buildTechMatch(techsSet) {
  // Map our chip to substring match against Technology Type
  // Solar -> contains "Solar", Wind -> "Wind", Battery -> "Battery"/"Storage"
  // Hydro -> "Hydro", Other -> none of the above
  const knownTokens = {
    Solar: ['Solar Photovoltaics', 'Solar'],
    Wind: ['Wind Onshore', 'Wind Offshore', 'Wind'],
    Battery: ['Battery', 'Storage'],
    Hydro: ['Hydro', 'Pumped Storage Hydroelectricity']
  };
  const orParts = [];
  let otherSelected = techsSet.has('Other');
  for (const t of ['Solar', 'Wind', 'Battery', 'Hydro']) {
    if (!techsSet.has(t)) continue;
    for (const tok of knownTokens[t]) {
      orParts.push(['==', ['get', 'Technology Type'], tok]);
    }
  }

  if (otherSelected) {
    // "Other" = matches none of the known specific tokens
    const allKnown = [];
    for (const arr of Object.values(knownTokens)) for (const tok of arr) allKnown.push(tok);
    const notKnownExpr = ['all', ...allKnown.map((tok) => ['!=', ['get', 'Technology Type'], tok])];
    orParts.push(notKnownExpr);
  }

  if (orParts.length === 0) {
    // No tech selected => filter out everything
    return ['==', ['get', 'Technology Type'], '__never__'];
  }
  return ['any', ...orParts];
}

function buildStatusMatch(statusesSet) {
  const known = [
    'Operational',
    'Under Construction',
    'Planning Permission Granted',
    'Planning Application Submitted'
  ];
  const orParts = [];
  for (const s of known) {
    if (statusesSet.has(s)) orParts.push(['==', ['get', 'Development Status'], s]);
  }
  if (statusesSet.has('Other')) {
    orParts.push(['all', ...known.map((s) => ['!=', ['get', 'Development Status'], s])]);
  }
  if (orParts.length === 0) {
    return ['==', ['get', 'Development Status'], '__never__'];
  }
  return ['any', ...orParts];
}

// ---------------------------------------------------------------------------
// Interactions (click handlers + side panel)
// ---------------------------------------------------------------------------
function wireInteractions(map) {
  ['parcels-fill', 'substation-fill', 'repd-circle'].forEach((layer) => {
    map.on('mouseenter', layer, () => {
      map.getCanvas().style.cursor = 'pointer';
    });
    map.on('mouseleave', layer, () => {
      map.getCanvas().style.cursor = '';
    });
  });

  map.on('click', 'parcels-fill', (e) => {
    if (!e.features || !e.features.length) return;
    if (state.mode !== 'develop') return;
    const f = e.features[0];
    const p = f.properties || {};
    const sections = [
      {
        heading: 'Identity',
        rows: [
          ['Parcel ID', p.parcel_id ?? '-'],
          ['Local Authority', p.lad_name ?? '-'],
          ['Area (ha)', fmtNumber(p.area_ha, 2)]
        ]
      },
      {
        heading: 'Resource',
        rows: [
          ['Mean PVOUT (kWh/kWp/yr)', fmtNumber(p.mean_pvout_kwhkwp, 0)],
          ['Mean wind @100m (m/s)', fmtNumber(p.mean_wind_speed_100m_ms, 2)]
        ]
      },
      {
        heading: 'Grid',
        rows: [
          ['Nearest substation', p.nearest_substation_name ?? '-'],
          ['Dist to gen-headroom sub', fmtKm(p.dist_substation_gen_headroom_m)],
          ['Dist to any-headroom sub', fmtKm(p.dist_substation_any_headroom_m)]
        ]
      }
    ];
    showInfoPanel({
      title: `Parcel ${p.parcel_id ?? '(unknown)'}`,
      sections,
      constraintFlags: {
        AONB: truthy(p.intersects_aonb),
        'National Park': truthy(p.intersects_national_park),
        'Green Belt': truthy(p.intersects_green_belt),
        SSSI: truthy(p.intersects_sssi),
        'Flood Zone': truthy(p.intersects_flood)
      }
    });
  });

  map.on('click', 'substation-fill', (e) => {
    if (!e.features || !e.features.length) return;
    const f = e.features[0];
    const p = f.properties || {};
    const genhr = Number(p.genhr);
    const demhr = Number(p.demhr);
    const firmCap = Number(p.firm_cap);
    showInfoPanel({
      title: `Substation: ${p.name ?? '(unnamed)'}`,
      sections: [
        {
          heading: 'Identity',
          rows: [
            ['Name', p.name ?? '-'],
            ['Primary voltage', p.pvoltage ?? '-']
          ]
        },
        {
          heading: 'Capacity',
          rows: [
            ['Firm capacity (MVA)', fmtNumber(firmCap, 1)]
          ]
        }
      ],
      bars: [
        {
          label: 'Generation headroom (MVA)',
          value: genhr,
          max: 100,
          color: Number.isFinite(genhr) && genhr > 5 ? 'good' : Number.isFinite(genhr) && genhr > 0 ? 'warn' : 'zero'
        },
        {
          label: 'Demand headroom (MVA)',
          value: demhr,
          max: 100,
          color: Number.isFinite(demhr) && demhr > 5 ? 'good' : Number.isFinite(demhr) && demhr > 0 ? 'warn' : 'zero'
        }
      ]
    });
  });

  map.on('click', 'repd-circle', (e) => {
    if (!e.features || !e.features.length) return;
    const f = e.features[0];
    const p = f.properties || {};
    const ref = p['Ref ID'] ?? p['Ref Id'] ?? p['Reference'] ?? null;
    const planningRefRow = ref ? ['Reference', String(ref)] : null;

    showInfoPanel({
      title: p['Site Name'] ?? 'REPD Site',
      sections: [
        {
          heading: 'Project',
          rows: [
            ['Operator', p['Operator (or Applicant)'] ?? '-'],
            ['Technology', p['Technology Type'] ?? '-'],
            ['Status', p['Development Status'] ?? '-']
          ].filter(Boolean)
        },
        {
          heading: 'Capacity',
          rows: [['Installed (MWelec)', fmtNumber(p['Installed Capacity (MWelec)'], 2)]]
        },
        {
          heading: 'Planning',
          rows: [
            ['Local Authority', p['Planning Authority'] ?? '-'],
            planningRefRow
          ].filter(Boolean)
        }
      ]
    });
  });

  document.getElementById('info-panel-close').addEventListener('click', hideInfoPanel);
}

// ---------------------------------------------------------------------------
// Info panel rendering
// ---------------------------------------------------------------------------
function showInfoPanel({ title, sections = [], constraintFlags = null, bars = null }) {
  const panel = document.getElementById('info-panel');
  const body = document.getElementById('info-panel-body');

  let html = `<h3>${escapeHtml(title)}</h3>`;

  for (const sec of sections) {
    html += `<h4 class="section-heading">${escapeHtml(sec.heading)}</h4>`;
    html += '<table><tbody>';
    for (const [label, value] of sec.rows) {
      html += `<tr><td class="label">${escapeHtml(label)}</td><td class="value">${escapeHtml(
        value == null ? '-' : String(value)
      )}</td></tr>`;
    }
    html += '</tbody></table>';
  }

  if (bars && bars.length) {
    html += '<h4 class="section-heading">Headroom</h4>';
    for (const bar of bars) {
      const v = Number(bar.value);
      const pct = Number.isFinite(v) ? Math.max(0, Math.min(100, (v / bar.max) * 100)) : 0;
      const cls = bar.color === 'warn' ? 'warn' : bar.color === 'zero' ? 'zero' : '';
      html += `<div class="bar-label" style="display:flex;justify-content:space-between;font-size:12px;color:#555;">
        <span>${escapeHtml(bar.label)}</span>
        <span style="font-weight:600;color:#1a5fa7;">${Number.isFinite(v) ? v.toFixed(1) : '-'}</span>
      </div>`;
      html += `<div class="bar-wrap"><div class="bar-fill ${cls}" style="width:${pct.toFixed(1)}%;"></div></div>`;
    }
  }

  if (constraintFlags) {
    html += '<h4 class="section-heading">Constraints</h4><div class="chip-row">';
    for (const [name, on] of Object.entries(constraintFlags)) {
      html += `<span class="info-chip ${on ? 'flag-on' : 'flag-off'}">${
        on ? escapeHtml(name) : escapeHtml(name) + ' —'
      }</span>`;
    }
    html += '</div>';
  }

  body.innerHTML = html;
  panel.classList.add('visible');
  panel.setAttribute('aria-hidden', 'false');
}

function hideInfoPanel() {
  const panel = document.getElementById('info-panel');
  panel.classList.remove('visible');
  panel.setAttribute('aria-hidden', 'true');
}

// ---------------------------------------------------------------------------
// Methodology modal
// ---------------------------------------------------------------------------
function wireMethodologyModal() {
  const link = document.getElementById('methodology-link');
  const overlay = document.getElementById('modal-overlay');
  const closeBtn = document.getElementById('modal-close');
  const body = document.getElementById('modal-body');

  let loaded = false;
  link.addEventListener('click', async (e) => {
    e.preventDefault();
    if (!loaded) {
      try {
        const res = await fetch('/methodology.md');
        const text = await res.text();
        body.innerHTML = `<pre>${escapeHtml(text)}</pre>`;
        loaded = true;
      } catch (err) {
        body.innerHTML = '<p style="color:#c33">Failed to load methodology.</p>';
      }
    }
    overlay.classList.add('visible');
    overlay.setAttribute('aria-hidden', 'false');
  });
  closeBtn.addEventListener('click', closeModal);
  overlay.addEventListener('click', (e) => {
    if (e.target === overlay) closeModal();
  });
  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape' && overlay.classList.contains('visible')) closeModal();
  });

  function closeModal() {
    overlay.classList.remove('visible');
    overlay.setAttribute('aria-hidden', 'true');
  }
}

// ---------------------------------------------------------------------------
// Utils
// ---------------------------------------------------------------------------
function truthy(v) {
  if (v === true || v === 1) return true;
  if (typeof v === 'string') return v === 'true' || v === '1';
  return false;
}

function fmtNumber(v, decimals = 2) {
  const n = Number(v);
  if (!Number.isFinite(n)) return '-';
  return n.toFixed(decimals);
}

function fmtKm(v) {
  const n = Number(v);
  if (!Number.isFinite(n)) return '-';
  return `${(n / 1000).toFixed(1)} km`;
}

function escapeHtml(s) {
  return String(s)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');
}

// ---------------------------------------------------------------------------
boot().catch((err) => {
  console.error('Map boot failed:', err);
});
