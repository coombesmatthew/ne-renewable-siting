import maplibregl from 'maplibre-gl';
import 'maplibre-gl/dist/maplibre-gl.css';
import { Protocol } from 'pmtiles';

// ---------------------------------------------------------------------------
// PMTiles protocol registration
// ---------------------------------------------------------------------------
const protocol = new Protocol();
maplibregl.addProtocol('pmtiles', protocol.tile);

// ---------------------------------------------------------------------------
// App state — flexible parcel filter (replaces tech-preset) + Acquire REPD
// ---------------------------------------------------------------------------
// Total parcel count baked into manifest at ETL time. Used for the live
// "X of N match" badge. We don't recompute it from tiles because tiles only
// expose the visible viewport.
const TOTAL_PARCELS = 33363;

// Filter spec — each key maps to a parcel attribute / spatial test.
// The `enabled` flag is the master toggle for that filter; `value` is the
// threshold (where applicable). Distance-based constraint filters default to
// 0 km (= exclude only intersecting); user can dial up for buffer-style.
const FILTER_DEFAULTS = {
  // Resource thresholds
  minPvout:        { enabled: false, value: 920 },     // kWh/kWp/yr
  minWind:         { enabled: false, value: 7.0 },     // m/s @ 100 m
  // Grid: distance-to-gen-headroom-substation, with a minimum voltage tier.
  // Voltage tier picks which precomputed column to filter on (min_11/20/33/66/132 kV).
  // Default '11' = any voltage (smallest cumulative tier covers everything).
  maxDistGenHr:    { enabled: false, value: 10, voltage: '11' },  // km, voltage in kV
  maxDistAnyHr:    { enabled: false, value: 5 },                  // km (no voltage refinement)
  // Parcel size
  minArea:         { enabled: false, value: 5 },                  // ha
  // Constraint distance filters — slider 0..20 km. At 0 km, equivalent to
  // "exclude parcels that intersect". Above 0, excludes anything within that
  // many km of the constraint (buffer-style).
  minDistAonb:     { enabled: false, value: 0 },
  minDistNp:       { enabled: false, value: 0 },
  minDistGb:       { enabled: false, value: 0 },
  minDistSssi:     { enabled: false, value: 0 },
  minDistFlood:    { enabled: false, value: 0 },
  minDistListed:   { enabled: false, value: 0 },
  minDistMonument: { enabled: false, value: 0 }
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
  filters: structuredClone(FILTER_DEFAULTS),
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
    'built-up-areas': true,
    'constraint-listed-building': false,
    'constraint-scheduled-monument': false,
    'solar-raster': false,
    'wind-raster': false,
    'substation-tier-GSP': true,
    'substation-tier-BSP': true,
    'substation-tier-Primary': true,
    'parcels-fill': true,
    'repd-solar': true,
    'repd-wind': true,
    'repd-battery': true,
    'repd-hydro': true,
    'repd-other': true
  }
};

// REPD tech color palette + tech token mapping (which Technology Type values count as each)
const REPD_TECHS = [
  { id: 'repd-solar',   label: 'REPD — Solar',   color: '#f5a623', tokens: ['Solar Photovoltaics'] },
  { id: 'repd-wind',    label: 'REPD — Wind',    color: '#4a90e2', tokens: ['Wind Onshore', 'Wind Offshore'] },
  { id: 'repd-battery', label: 'REPD — Battery', color: '#9013fe', tokens: ['Battery'] },
  { id: 'repd-hydro',   label: 'REPD — Hydro',   color: '#50e3c2', tokens: ['Small Hydro', 'Large Hydro', 'Pumped Storage Hydroelectricity'] },
  { id: 'repd-other',   label: 'REPD — Other',   color: '#888888', tokens: null } // null = "everything not in the above lists"
];

// All known REPD layer ids
const REPD_LAYER_IDS = REPD_TECHS.map((t) => t.id);

// Substation tiers grouped by FUNCTIONAL TYPE (GSP / BSP / Primary). Each renders
// as a catchment-fill + point-circle pair, filtered by the `type` attribute
// baked into the PMTiles. Maps to the UK grid hierarchy in a renewable-siting
// context: GSP = transmission interface (>50 MW projects); BSP = utility-scale
// (5-50 MW); Primary = distribution-level (sub-MW to ~10 MW).
const SUBSTATION_TIERS = [
  { tier: 'GSP',     label: 'GSP — Grid Supply Point (transmission interface)',  color: '#cc1f1f' },
  { tier: 'BSP',     label: 'BSP — Bulk Supply Point (utility-scale)',           color: '#ff7f0e' },
  { tier: 'Primary', label: 'Primary substation (distribution)',                 color: '#6a7896' }
];
// Catchment fill + line + point IDs per tier — handy for legend toggle and click binding
const SUBSTATION_POINT_LAYER_IDS = SUBSTATION_TIERS.map((t) => `substation-point-${t.tier}`);

function repdTechFilter(tokens) {
  // tokens === null means "Other" — match anything NOT in the union of all known tokens
  if (tokens === null) {
    const allKnown = REPD_TECHS.flatMap((t) => t.tokens || []);
    return ['!', ['in', ['get', 'Technology Type'], ['literal', allKnown]]];
  }
  return ['in', ['get', 'Technology Type'], ['literal', tokens]];
}

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
    bindMapMoveLive(map);
    wireChat();
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
      built_up_areas: { type: 'vector', url: `pmtiles://${urls.tiles.built_up_areas}` },
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

      // Built-up areas: visual context layer (urban footprints from ONS BUA 2022).
      // Rendered as a hatched-grey fill with a darker outline so cities/towns
      // are recognisable without clobbering parcel suitability colours.
      {
        id: 'built-up-areas',
        type: 'fill',
        source: 'built_up_areas',
        'source-layer': 'built_up_areas',
        paint: { 'fill-color': '#666666', 'fill-opacity': 0.18 }
      },
      {
        id: 'built-up-areas-line',
        type: 'line',
        source: 'built_up_areas',
        'source-layer': 'built_up_areas',
        paint: { 'line-color': '#444444', 'line-width': 0.6, 'line-opacity': 0.5 }
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
      // Listed buildings are 99% Point features (only ~113 of 12,432 have a
      // polygon footprint) — render as small circles so they're actually
      // visible. The 113 polygons end up represented at their centroid; an
      // acceptable trade-off given how dominant points are.
      {
        id: 'constraint-listed-building',
        type: 'circle',
        source: 'constraints',
        'source-layer': 'listed_building',
        layout: { visibility: 'none' },
        paint: {
          'circle-color': '#bb6688',
          'circle-radius': [
            'interpolate', ['linear'], ['zoom'],
            8, 1.5,
            12, 3,
            14, 5
          ],
          'circle-stroke-color': '#ffffff',
          'circle-stroke-width': 0.5,
          'circle-opacity': 0.7
        }
      },

      // Substations — split into 5 voltage tiers (132/66/33/20/11 kV) so the user
      // can instantly see where each level of the grid sits. Each tier renders
      // BOTH the catchment polygon (translucent fill + line) AND a point marker
      // at the actual station location (sized by firm_cap). Both come from the
      // same PMTiles archive (two source-layers: substation_catchment + substation_point).
      ...SUBSTATION_TIERS.flatMap((t) => [
        {
          id: `substation-catchment-${t.tier}`,
          type: 'fill',
          source: 'substations',
          'source-layer': 'substation_catchment',
          filter: ['==', ['get', 'type'], t.tier],
          paint: { 'fill-color': t.color, 'fill-opacity': 0.12 }
        },
        {
          id: `substation-catchment-${t.tier}-line`,
          type: 'line',
          source: 'substations',
          'source-layer': 'substation_catchment',
          filter: ['==', ['get', 'type'], t.tier],
          paint: { 'line-color': t.color, 'line-width': 0.6, 'line-opacity': 0.5 }
        },
        {
          id: `substation-point-${t.tier}`,
          type: 'circle',
          source: 'substations',
          'source-layer': 'substation_point',
          filter: ['==', ['get', 'type'], t.tier],
          paint: {
            'circle-color': t.color,
            'circle-radius': [
              'interpolate', ['linear'],
              ['coalesce', ['to-number', ['get', 'firm_cap']], 1],
              0, 4, 50, 8, 200, 14, 500, 18
            ],
            'circle-stroke-color': '#ffffff',
            'circle-stroke-width': 1.5,
            'circle-opacity': 0.95
          }
        }
      ]),

      // REPD — split into 5 per-tech layers for clearer "what's where" reading.
      // Color BY tech, opacity BY status (Operational solid; in-planning fainter),
      // size BY capacity. All sourced from the same PMTiles archive (one fetch).
      ...REPD_TECHS.map((tech) => ({
        id: tech.id,
        type: 'circle',
        source: 'repd',
        'source-layer': 'repd',
        filter: repdTechFilter(tech.tokens),
        paint: {
          'circle-color': tech.color,
          'circle-radius': [
            'interpolate', ['linear'],
            ['coalesce', ['to-number', ['get', 'Installed Capacity (MWelec)']], 1],
            0, 3, 10, 6, 50, 10, 200, 14
          ],
          'circle-stroke-color': [
            'match', ['get', 'Development Status'],
            'Operational', '#1a1a1a',
            'Under Construction', '#ffffff',
            'Planning Permission Granted', '#fff066',
            '#bbbbbb'
          ],
          'circle-stroke-width': [
            'match', ['get', 'Development Status'],
            'Operational', 1.5,
            'Under Construction', 2,
            'Planning Permission Granted', 1.5,
            0.8
          ],
          'circle-opacity': [
            'match', ['get', 'Development Status'],
            'Operational', 0.95,
            'Under Construction', 0.85,
            'Planning Permission Granted', 0.7,
            0.4
          ]
        }
      }))
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
      // Toggle parcel visibility based on mode (develop = parcels visible)
      const vis = newMode === 'develop' ? 'visible' : 'none';
      ['parcels-fill', 'parcels-line'].forEach((id) => {
        if (map.getLayer(id)) map.setLayoutProperty(id, 'visibility', vis);
      });
      renderFilterPanel(map);
      if (newMode === 'develop') {
        applyParcelStyling(map);
        REPD_TECHS.forEach((tech) => {
          if (map.getLayer(tech.id)) map.setFilter(tech.id, repdTechFilter(tech.tokens));
          if (map.getLayer(tech.id) && state.layerVis[tech.id]) {
            map.setLayoutProperty(tech.id, 'visibility', 'visible');
          }
        });
      } else {
        applyAcquireFilters(map);
      }
      updatePanelTitle();
    });
  });

  // Filters button
  // Filter panel is now always-on (no Filters button, no close button).
  // Body class kept so legacy CSS selectors that nudge map controls past
  // the panel still work.
  document.body.classList.add('filter-open');
}

function updatePanelTitle() {
  const t = document.getElementById('filter-panel-title');
  if (state.mode === 'develop') {
    t.textContent = 'Develop · filter parcels';
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

// Filter spec used by both the panel renderer and the expression builder.
// Each entry: how to render, which parcel attribute it tests, how to translate
// (enabled, value) into a MapLibre filter clause.
//
// `kind: 'slider'` — single threshold slider
// `kind: 'slider+select'` — slider plus a dropdown (used for grid voltage tier)
// Distance constraints all use the same min..max range (0..20 km buffer).
const FILTER_SPECS = [
  { id: 'minPvout',     section: 'Resource',  label: 'Min Solar PVOUT',                  unit: 'kWh/kWp/yr', kind: 'slider', min: 800, max: 1000, step: 1,   fmt: (v) => v.toFixed(0) },
  { id: 'minWind',      section: 'Resource',  label: 'Min wind speed @ 100 m',           unit: 'm/s',        kind: 'slider', min: 5,   max: 13,   step: 0.1, fmt: (v) => v.toFixed(1) },
  // Grid — gen-headroom distance with required-voltage tier dropdown.
  { id: 'maxDistGenHr', section: 'Grid',      label: 'Max dist to gen-headroom sub at min voltage', unit: 'km', kind: 'slider+select', min: 1, max: 80, step: 1, fmt: (v) => v.toFixed(0),
    selectKey: 'voltage', selectLabel: 'Min voltage',
    selectOptions: [
      { value: '11',  label: '≥ 11 kV (any)' },
      { value: '20',  label: '≥ 20 kV' },
      { value: '33',  label: '≥ 33 kV (utility-scale)' },
      { value: '66',  label: '≥ 66 kV (large utility)' },
      { value: '132', label: '≥ 132 kV (GSP)' }
    ] },
  { id: 'maxDistAnyHr', section: 'Grid',      label: 'Max dist to any-headroom sub (battery-style)', unit: 'km', kind: 'slider', min: 1, max: 50, step: 1, fmt: (v) => v.toFixed(0) },
  { id: 'minArea',      section: 'Other',     label: 'Min parcel area',                  unit: 'ha',         kind: 'slider', min: 2,   max: 100,  step: 1,   fmt: (v) => v.toFixed(0) },
  // Constraint distance filters — sliders 0..20 km. 0 km = "exclude intersecting".
  { id: 'minDistAonb',     section: 'Constraints',  label: 'Min distance from AONB',                unit: 'km', kind: 'slider', min: 0, max: 20, step: 0.5, fmt: (v) => v.toFixed(1), swatch: '#88dd88' },
  { id: 'minDistNp',       section: 'Constraints',  label: 'Min distance from National Park',       unit: 'km', kind: 'slider', min: 0, max: 20, step: 0.5, fmt: (v) => v.toFixed(1), swatch: '#66cc66' },
  { id: 'minDistGb',       section: 'Constraints',  label: 'Min distance from Green Belt',          unit: 'km', kind: 'slider', min: 0, max: 20, step: 0.5, fmt: (v) => v.toFixed(1), swatch: '#aacc88' },
  { id: 'minDistSssi',     section: 'Constraints',  label: 'Min distance from SSSI',                unit: 'km', kind: 'slider', min: 0, max: 20, step: 0.5, fmt: (v) => v.toFixed(1), swatch: '#cc8866' },
  { id: 'minDistFlood',    section: 'Constraints',  label: 'Min distance from Flood Zone',          unit: 'km', kind: 'slider', min: 0, max: 20, step: 0.5, fmt: (v) => v.toFixed(1), swatch: '#5588cc' },
  { id: 'minDistListed',   section: 'Constraints',  label: 'Min distance from Listed Buildings',    unit: 'km', kind: 'slider', min: 0, max: 20, step: 0.5, fmt: (v) => v.toFixed(1), swatch: '#bb6688' },
  { id: 'minDistMonument', section: 'Constraints',  label: 'Min distance from Scheduled Monuments', unit: 'km', kind: 'slider', min: 0, max: 20, step: 0.5, fmt: (v) => v.toFixed(1), swatch: '#aa6688' }
];

function buildDevelopFilters(map) {
  const frag = document.createDocumentFragment();

  // Live "X of TOTAL match" badge — gets updated by applyParcelStyling.
  const countBadge = document.createElement('div');
  countBadge.className = 'count-badge';
  countBadge.id = 'parcel-count-badge';
  countBadge.textContent = `${TOTAL_PARCELS.toLocaleString()} of ${TOTAL_PARCELS.toLocaleString()} parcels match`;
  frag.appendChild(countBadge);

  // Group spec entries by section.
  const sections = {};
  for (const spec of FILTER_SPECS) {
    (sections[spec.section] ||= []).push(spec);
  }

  for (const sectionName of Object.keys(sections)) {
    const sec = section(sectionName);
    for (const spec of sections[sectionName]) {
      sec.appendChild(buildFilterRow(map, spec));
    }
    frag.appendChild(sec);
  }

  return frag;
}

// One row per filter: an "enable" checkbox + the value control (slider for
// thresholds, nothing for exclusion flags). The label doubles as the toggle.
function buildFilterRow(map, spec) {
  const f = state.filters[spec.id];
  const wrap = document.createElement('div');
  wrap.className = 'filter-row';
  wrap.dataset.filterId = spec.id;
  wrap.classList.toggle('enabled', f.enabled);

  // Enable checkbox — the master toggle for this filter.
  const enableLabel = document.createElement('label');
  enableLabel.className = 'filter-enable';
  const enableCb = document.createElement('input');
  enableCb.type = 'checkbox';
  enableCb.checked = f.enabled;
  enableLabel.appendChild(enableCb);
  if (spec.swatch) {
    const sw = document.createElement('span');
    sw.className = 'swatch';
    sw.style.backgroundColor = spec.swatch;
    enableLabel.appendChild(sw);
  }
  const labelText = document.createElement('span');
  labelText.className = 'filter-label';
  labelText.textContent = spec.label;
  enableLabel.appendChild(labelText);
  wrap.appendChild(enableLabel);

  let valueEl = null;
  if (spec.kind === 'slider' || spec.kind === 'slider+select') {
    const row = document.createElement('div');
    row.className = 'slider-row inline';
    const input = document.createElement('input');
    input.type = 'range';
    input.min = String(spec.min);
    input.max = String(spec.max);
    input.step = String(spec.step);
    input.value = String(f.value);
    input.disabled = !f.enabled;
    const valueSpan = document.createElement('span');
    valueSpan.className = 'value';
    valueSpan.textContent = `${spec.fmt(f.value)} ${spec.unit}`;
    input.addEventListener('input', () => {
      const v = Number(input.value);
      f.value = v;
      valueSpan.textContent = `${spec.fmt(v)} ${spec.unit}`;
      if (f.enabled) scheduleParcelUpdate(map);
    });
    row.appendChild(input);
    row.appendChild(valueSpan);
    wrap.appendChild(row);
    valueEl = input;
  }
  // Optional secondary dropdown (used by the gen-hr distance filter for voltage tier)
  let selectEl = null;
  if (spec.kind === 'slider+select') {
    const selRow = document.createElement('div');
    selRow.className = 'select-row inline';
    const selLabel = document.createElement('span');
    selLabel.className = 'sub-label';
    selLabel.textContent = spec.selectLabel + ': ';
    const sel = document.createElement('select');
    sel.disabled = !f.enabled;
    for (const opt of spec.selectOptions) {
      const o = document.createElement('option');
      o.value = opt.value;
      o.textContent = opt.label;
      if (String(f[spec.selectKey]) === opt.value) o.selected = true;
      sel.appendChild(o);
    }
    sel.addEventListener('change', () => {
      f[spec.selectKey] = sel.value;
      if (f.enabled) scheduleParcelUpdate(map);
    });
    selRow.appendChild(selLabel);
    selRow.appendChild(sel);
    wrap.appendChild(selRow);
    selectEl = sel;
  }

  enableCb.addEventListener('change', () => {
    f.enabled = enableCb.checked;
    wrap.classList.toggle('enabled', f.enabled);
    if (valueEl) valueEl.disabled = !f.enabled;
    if (selectEl) selectEl.disabled = !f.enabled;
    scheduleParcelUpdate(map);
  });

  return wrap;
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
  // Top-level container — multiple grouped sub-sections under it.
  const wrap = document.createElement('div');
  wrap.className = 'filter-section-wrap';

  // Toggle handler shared across all groups
  const onToggle = (item) => (checked) => {
    state.layerVis[item.id] = checked;
    const vis = checked ? 'visible' : 'none';
    if (map.getLayer(item.id)) map.setLayoutProperty(item.id, 'visibility', vis);
    // Substation tier meta-toggle controls 3 layers per tier
    if (item.id.startsWith('substation-tier-')) {
      const tier = item.id.slice('substation-tier-'.length);
      [`substation-catchment-${tier}`, `substation-catchment-${tier}-line`, `substation-point-${tier}`].forEach((id) => {
        if (map.getLayer(id)) map.setLayoutProperty(id, 'visibility', vis);
      });
    }
    // Parcels meta-toggle controls fill + line companion
    if (item.id === 'parcels-fill' && map.getLayer('parcels-line')) {
      map.setLayoutProperty('parcels-line', 'visibility', vis);
    }
    // BUA meta-toggle controls fill + line
    if (item.id === 'built-up-areas' && map.getLayer('built-up-areas-line')) {
      map.setLayoutProperty('built-up-areas-line', 'visibility', vis);
    }
  };

  const buildGroup = (title, items, opts = {}) => {
    // Layer groups are always-on (no collapse). Headers stay readable;
    // master toggle in the heading still flips all items in the group.
    const sec = section(title, { collapsible: false });

    // Master toggle in the section header — flips all items in the group
    // on/off at once. Reflects mixed/all-on/all-off via tri-state checkbox
    // (`indeterminate` when items disagree).
    const heading = sec.querySelector('h4');
    if (heading) {
      const masterWrap = document.createElement('span');
      masterWrap.className = 'group-master';
      const master = document.createElement('input');
      master.type = 'checkbox';
      master.title = `Toggle all ${title.toLowerCase()} layers`;
      // Stop the click from bubbling to the collapsible-section toggle.
      master.addEventListener('click', (e) => e.stopPropagation());
      masterWrap.appendChild(master);
      heading.appendChild(masterWrap);

      const itemCheckboxes = []; // we'll populate below

      const refreshMasterState = () => {
        const states = items.map((it) => !!state.layerVis[it.id]);
        const allOn = states.every(Boolean);
        const allOff = states.every((s) => !s);
        master.checked = allOn;
        master.indeterminate = !allOn && !allOff;
      };

      master.addEventListener('change', () => {
        const target = master.checked;
        for (const item of items) {
          // Run the same per-item onToggle so companion layers (substation
          // triple, parcels-line, BUA-line) get flipped too.
          onToggle(item)(target);
        }
        // Sync child checkbox UI states.
        for (const cb of itemCheckboxes) cb.checked = target;
        master.indeterminate = false;
      });

      // After items are appended, wire each child onChange so it bubbles
      // back into refreshMasterState.
      for (const item of items) {
        const row = checkbox({
          label: item.label,
          checked: !!state.layerVis[item.id],
          swatch: item.swatch,
          onChange: (checked) => {
            onToggle(item)(checked);
            refreshMasterState();
          }
        });
        const cb = row.querySelector('input[type="checkbox"]');
        if (cb) itemCheckboxes.push(cb);
        sec.appendChild(row);
      }
      refreshMasterState();
      return sec;
    }

    // Fallback (no heading found — shouldn't happen)
    for (const item of items) {
      sec.appendChild(checkbox({
        label: item.label,
        checked: !!state.layerVis[item.id],
        swatch: item.swatch,
        onChange: onToggle(item)
      }));
    }
    return sec;
  };

  // RISKS — what would block development. Includes hard exclusions (NP, SSSI),
  // soft constraints (AONB, Green Belt), built-up areas, flood zones, and
  // heritage layers (listed buildings, scheduled monuments).
  wrap.appendChild(buildGroup('Risks', [
    { id: 'constraint-aonb', label: 'AONB / National Landscape', swatch: '#88dd88' },
    { id: 'constraint-national-park', label: 'National Park', swatch: '#66cc66' },
    { id: 'constraint-green-belt', label: 'Green Belt', swatch: '#aacc88' },
    { id: 'constraint-sssi', label: 'SSSI', swatch: '#cc8866' },
    { id: 'constraint-flood', label: 'Flood Zone', swatch: '#5588cc' },
    { id: 'built-up-areas', label: 'Built-up areas', swatch: '#666666' },
    { id: 'constraint-listed-building', label: 'Listed Buildings', swatch: '#bb6688' },
    { id: 'constraint-scheduled-monument', label: 'Scheduled Monuments', swatch: '#aa6688' }
  ]));

  // SUBSTATIONS — by functional type (UK grid hierarchy)
  wrap.appendChild(buildGroup('Substations', SUBSTATION_TIERS.map((t) => ({
    id: `substation-tier-${t.tier}`, label: t.label, swatch: t.color
  }))));

  // RENEWABLE PROJECTS — REPD pipeline, split by tech
  wrap.appendChild(buildGroup('Renewable Projects', REPD_TECHS.map((t) => ({
    id: t.id, label: t.label, swatch: t.color
  }))));

  // OTHER — parcels (the developable-area layer) + raster resource overlays
  wrap.appendChild(buildGroup('Land & resource', [
    { id: 'parcels-fill', label: 'Developable parcels (≥2 ha)', swatch: '#4a90e2' },
    { id: 'solar-raster', label: 'Solar PVOUT raster', swatch: '#f4c542' },
    { id: 'wind-raster', label: 'Wind speed raster', swatch: '#80b3d3' }
  ]));

  return wrap;
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
// Build the MapLibre filter expression from the active filters in state.
// Disabled filters contribute no clauses; the result is `['all', ...]` of
// constraints all of which must hold for a parcel to render as "matching".
function buildParcelFilterExpression() {
  const f = state.filters;
  const conditions = ['all'];

  if (f.minPvout.enabled) {
    conditions.push(['>=', ['coalesce', ['to-number', ['get', 'mean_pvout_kwhkwp']], 0], f.minPvout.value]);
  }
  if (f.minWind.enabled) {
    conditions.push(['>=', ['coalesce', ['to-number', ['get', 'mean_wind_speed_100m_ms']], 0], f.minWind.value]);
  }
  // Grid: gen-hr distance filtered against the precomputed cumulative-tier
  // column for the user-selected min voltage. e.g. "≥33 kV" → uses
  // dist_genhr_min_33kv_m which is "distance to nearest gen-hr substation
  // whose pvoltage >= 33".
  if (f.maxDistGenHr.enabled) {
    const tier = f.maxDistGenHr.voltage || '11';
    const col = `dist_genhr_min_${tier}kv_m`;
    conditions.push(['<=',
      ['coalesce', ['to-number', ['get', col]], 1e9],
      f.maxDistGenHr.value * 1000]);
  }
  if (f.maxDistAnyHr.enabled) {
    conditions.push(['<=',
      ['coalesce', ['to-number', ['get', 'dist_substation_any_headroom_m']], 1e9],
      f.maxDistAnyHr.value * 1000]);
  }
  if (f.minArea.enabled) {
    conditions.push(['>=', ['coalesce', ['to-number', ['get', 'area_ha']], 0], f.minArea.value]);
  }

  // Constraint distance filters — slider value is km buffer; baked-in
  // attribute is metres-distance from parcel boundary to constraint boundary.
  // At slider=0 km this is "exclude parcels intersecting the constraint"
  // (since intersecting parcels have dist_X_m == 0, and we test >= which
  // includes 0 — so we use > to keep 0-distance parcels OUT when buffer > 0,
  // or use >= 0 = always-true at 0; really we want "dist > buffer_m" for any
  // non-zero buffer, and "dist > 0" at buffer=0 to exclude exact intersections).
  const distFilter = (key, km) => {
    if (km <= 0) {
      // 0 km buffer = exclude only parcels that strictly intersect (dist == 0)
      return ['>', ['coalesce', ['to-number', ['get', key]], 1e9], 0];
    }
    return ['>=', ['coalesce', ['to-number', ['get', key]], 1e9], km * 1000];
  };
  if (f.minDistAonb.enabled)     conditions.push(distFilter('dist_aonb_m', f.minDistAonb.value));
  if (f.minDistNp.enabled)       conditions.push(distFilter('dist_national_park_m', f.minDistNp.value));
  if (f.minDistGb.enabled)       conditions.push(distFilter('dist_green_belt_m', f.minDistGb.value));
  if (f.minDistSssi.enabled)     conditions.push(distFilter('dist_sssi_m', f.minDistSssi.value));
  if (f.minDistFlood.enabled)    conditions.push(distFilter('dist_flood_m', f.minDistFlood.value));
  if (f.minDistListed.enabled)   conditions.push(distFilter('dist_listed_building_m', f.minDistListed.value));
  if (f.minDistMonument.enabled) conditions.push(distFilter('dist_scheduled_monument_m', f.minDistMonument.value));

  return conditions;
}

function activeFilterCount() {
  return Object.values(state.filters).filter((f) => f.enabled).length;
}

function updateCountBadge(map) {
  const badge = document.getElementById('parcel-count-badge');
  if (!badge) return;
  const total = TOTAL_PARCELS;
  if (activeFilterCount() === 0) {
    badge.textContent = `${total.toLocaleString()} of ${total.toLocaleString()} parcels match`;
    badge.classList.remove('filtered');
    return;
  }
  // Match count is approximated from the currently-rendered viewport because
  // MapLibre filter expressions can't be evaluated client-side without
  // reimplementing the spec. queryRenderedFeatures with our filter does
  // exactly that — at the cost of being viewport-scoped.
  const expr = buildParcelFilterExpression();
  const matched = map.queryRenderedFeatures({ layers: ['parcels-fill'], filter: expr });
  const allRendered = map.queryRenderedFeatures({ layers: ['parcels-fill'] });
  const matchedIds = new Set(matched.map((f) => f.properties?.parcel_id).filter(Boolean));
  const totalIds = new Set(allRendered.map((f) => f.properties?.parcel_id).filter(Boolean));
  badge.textContent = `${matchedIds.size.toLocaleString()} match in view (${totalIds.size.toLocaleString()} loaded · ${total.toLocaleString()} total)`;
  badge.classList.add('filtered');
}

let _parcelUpdateTimer = null;
function scheduleParcelUpdate(map) {
  if (_parcelUpdateTimer) clearTimeout(_parcelUpdateTimer);
  _parcelUpdateTimer = setTimeout(() => applyParcelStyling(map), 80);
}

function applyParcelStyling(map) {
  if (state.mode !== 'develop') return;
  if (!map.getLayer('parcels-fill')) return;
  const expr = buildParcelFilterExpression();
  map.setPaintProperty('parcels-fill', 'fill-color', ['case', expr, '#2ca02c', '#cccccc']);
  map.setPaintProperty('parcels-fill', 'fill-opacity', ['case', expr, 0.5, 0.08]);
  map.setPaintProperty('parcels-line', 'line-color', ['case', expr, '#1f7a1f', '#999999']);
  map.setPaintProperty('parcels-line', 'line-opacity', ['case', expr, 0.7, 0.1]);
  updateCountBadge(map);
}

// Re-apply styling on pan/zoom (also refreshes the count badge which is
// viewport-scoped — `X match in view` updates as the user navigates).
function bindMapMoveLive(map) {
  let timer = null;
  map.on('moveend', () => {
    if (state.mode !== 'develop') return;
    if (timer) clearTimeout(timer);
    timer = setTimeout(() => applyParcelStyling(map), 200);
  });
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
  const cfg = state.acquire;
  const statusCondition = buildStatusMatch(cfg.statuses);
  const capExpr = ['coalesce', ['to-number', ['get', 'Installed Capacity (MWelec)']], 0];

  // Map chip name -> layer id
  const chipToLayer = {
    Solar: 'repd-solar',
    Wind: 'repd-wind',
    Battery: 'repd-battery',
    Hydro: 'repd-hydro',
    Other: 'repd-other'
  };

  for (const [chip, layerId] of Object.entries(chipToLayer)) {
    if (!map.getLayer(layerId)) continue;

    // Visibility = legend toggle AND chip selection
    const chipOn = cfg.techs.has(chip);
    const legendOn = state.layerVis[layerId] !== false;
    const visible = chipOn && legendOn;
    map.setLayoutProperty(layerId, 'visibility', visible ? 'visible' : 'none');

    // Filter (only meaningful when visible — but always set so the data is correct)
    const tech = REPD_TECHS.find((t) => t.id === layerId);
    map.setFilter(layerId, [
      'all',
      repdTechFilter(tech.tokens),
      statusCondition,
      ['>=', capExpr, cfg.capacityMin],
      ['<=', capExpr, cfg.capacityMax]
    ]);
  }
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
// All layer IDs that should produce a clickable info panel, ordered by
// priority (highest first). When a click hits multiple overlapping features,
// the first match in this list wins — so REPD/substation point markers
// (small, precise) take precedence over big polygon layers (parcels, BUAs).
const CLICKABLE_LAYERS = () => [
  // Renewable projects (point markers) — highest priority
  ...REPD_LAYER_IDS,
  // Substation point markers
  ...SUBSTATION_POINT_LAYER_IDS,
  // Constraint polygons — small/medium (heritage)
  'constraint-listed-building',
  'constraint-scheduled-monument',
  'constraint-sssi',
  'constraint-national-park',
  'constraint-aonb',
  'constraint-green-belt',
  'constraint-flood',
  // Built-up areas
  'built-up-areas',
  // Parcels — biggest overlap, lowest priority so they don't shadow specific features
  'parcels-fill'
];

function _renderRepd(p) {
  const ref = p['Ref ID'] ?? p['Ref Id'] ?? p['Reference'] ?? null;
  return {
    title: p['Site Name'] ?? 'REPD Site',
    sections: [
      {
        heading: 'Project',
        rows: [
          ['Operator', p['Operator (or Applicant)'] ?? '-'],
          ['Technology', p['Technology Type'] ?? '-'],
          ['Status', p['Development Status'] ?? '-']
        ]
      },
      {
        heading: 'Capacity',
        rows: [['Installed (MWelec)', fmtNumber(p['Installed Capacity (MWelec)'], 2)]]
      },
      {
        heading: 'Planning',
        rows: [
          ['Local Authority', p['Planning Authority'] ?? '-'],
          ref ? ['Reference', String(ref)] : null
        ].filter(Boolean)
      }
    ]
  };
}

function _renderSubstation(p) {
  const genhr = Number(p.genhr);
  const demhr = Number(p.demhr);
  const firmCap = Number(p.firm_cap);
  return {
    title: `Substation: ${p.name ?? '(unnamed)'}`,
    sections: [
      {
        heading: 'Identity',
        rows: [
          ['Name', p.name ?? '-'],
          ['Type', p.type ?? '-'],
          ['Primary voltage', p.pvoltage ? `${p.pvoltage} kV` : '-'],
          ['Local authority', p.local_authority ?? '-']
        ]
      },
      {
        heading: 'Capacity',
        rows: [['Firm capacity (MVA)', fmtNumber(firmCap, 1)]]
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
  };
}

function _renderParcel(p) {
  return {
    title: `Parcel ${p.parcel_id ?? '(unknown)'}`,
    sections: [
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
    ],
    constraintFlags: {
      AONB: truthy(p.intersects_aonb),
      'National Park': truthy(p.intersects_national_park),
      'Green Belt': truthy(p.intersects_green_belt),
      SSSI: truthy(p.intersects_sssi),
      'Flood Zone': truthy(p.intersects_flood)
    }
  };
}

function _renderBua(p) {
  return {
    title: p.BUA22NM ?? p.bua22nm ?? 'Built-up area',
    sections: [
      {
        heading: 'Identity',
        rows: [
          ['Name', p.BUA22NM ?? p.bua22nm ?? '-'],
          ['ONS code', p.BUA22CD ?? p.bua22cd ?? '-']
        ]
      }
    ]
  };
}

const _CONSTRAINT_LABELS = {
  'constraint-aonb': { title: 'AONB / National Landscape', dataset: 'national-landscape' },
  'constraint-national-park': { title: 'National Park', dataset: 'national-park' },
  'constraint-green-belt': { title: 'Green Belt', dataset: 'green-belt' },
  'constraint-sssi': { title: 'SSSI', dataset: 'site-of-special-scientific-interest' },
  'constraint-flood': { title: 'Flood Risk Zone', dataset: 'flood-risk-zone' },
  'constraint-listed-building': { title: 'Listed Building', dataset: 'listed-building' },
  'constraint-scheduled-monument': { title: 'Scheduled Monument', dataset: 'scheduled-monument' }
};

function _renderConstraint(layerId, p) {
  const meta = _CONSTRAINT_LABELS[layerId] ?? { title: 'Constraint', dataset: '?' };
  // planning.data.gov.uk schema commonly has: name, reference, entity, dataset, entry-date.
  // Flood-risk-zone has flood-risk-level, flood-risk-type instead of name.
  const isFlood = layerId === 'constraint-flood';
  const titleVal = isFlood
    ? `Flood Zone ${p['flood-risk-level'] ?? '?'} — ${p['flood-risk-type'] ?? 'planning'}`
    : (p.name || meta.title);
  const rows = [
    ['Type', meta.title],
    ['Name', p.name ?? '(no name)'],
    ['Reference', p.reference ?? '-'],
    ['Dataset', meta.dataset]
  ];
  if (isFlood) {
    rows.unshift(['Flood risk level', p['flood-risk-level'] ?? '-']);
    rows.unshift(['Flood risk type', p['flood-risk-type'] ?? '-']);
  }
  return {
    title: titleVal,
    sections: [{ heading: 'Identity', rows }]
  };
}

function wireInteractions(map) {
  // Build a small bbox around a click point so small markers (4-8 px circles)
  // don't require pixel-perfect clicks.
  const bboxAround = (point, pad = 5) => [
    [point.x - pad, point.y - pad],
    [point.x + pad, point.y + pad]
  ];

  // Pointer cursor on all clickable layers (no buffer — hover is precise)
  map.on('mousemove', (e) => {
    const layers = CLICKABLE_LAYERS().filter((id) => map.getLayer(id));
    const features = map.queryRenderedFeatures(e.point, { layers });
    map.getCanvas().style.cursor = features.length ? 'pointer' : '';
  });

  // Single coordinator click handler with priority-based dispatch.
  // Uses a 5px bbox around the click so substation/REPD point markers (small
  // circles) are forgiving to click. Per-layer handlers were dropped because
  // all overlapping features fire their handlers on the same click — the
  // last-registered handler "won" the panel, which made clicks on substation
  // points sitting on parcels show the parcel info. queryRenderedFeatures
  // lets us pick the most specific feature explicitly.
  map.on('click', (e) => {
    const layers = CLICKABLE_LAYERS().filter((id) => map.getLayer(id));
    const features = map.queryRenderedFeatures(bboxAround(e.point), { layers });
    if (!features.length) return;

    // Sort features by our priority order
    const prio = (id) => {
      const idx = layers.indexOf(id);
      return idx === -1 ? 999 : idx;
    };
    features.sort((a, b) => prio(a.layer.id) - prio(b.layer.id));
    const best = features[0];
    const p = best.properties || {};
    const layerId = best.layer.id;

    let panel;
    if (REPD_LAYER_IDS.includes(layerId)) panel = _renderRepd(p);
    else if (SUBSTATION_POINT_LAYER_IDS.includes(layerId)) panel = _renderSubstation(p);
    else if (layerId === 'parcels-fill') {
      if (state.mode !== 'develop') return; // suppress parcel clicks in Acquire
      panel = _renderParcel(p);
    } else if (layerId === 'built-up-areas') panel = _renderBua(p);
    else if (layerId in _CONSTRAINT_LABELS) panel = _renderConstraint(layerId, p);
    else return;

    showInfoPanel(panel);
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
// Chat widget (floating, SSE streaming)
// ---------------------------------------------------------------------------
const API_BASE =
  (typeof import.meta !== 'undefined' && import.meta.env && import.meta.env.VITE_API_BASE) ||
  (location.port === '5173' ? 'http://localhost:8000' : '');

async function sendChat(messages, onChunk, onDone) {
  let resp;
  try {
    resp = await fetch(`${API_BASE}/api/chat`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ messages }),
    });
  } catch (err) {
    onChunk(`Error: ${err.message || 'network failure'}`);
    onDone();
    return;
  }
  if (!resp.ok || !resp.body) {
    onChunk(`Error: ${resp.status}`);
    onDone();
    return;
  }
  const reader = resp.body.getReader();
  const decoder = new TextDecoder();
  let buffer = '';
  while (true) {
    const { value, done } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    const lines = buffer.split('\n');
    buffer = lines.pop() || '';
    for (const line of lines) {
      if (!line.startsWith('data: ')) continue;
      const payload = line.slice(6).trim();
      if (payload === '[DONE]') {
        onDone();
        return;
      }
      try {
        const parsed = JSON.parse(payload);
        if (parsed.text) onChunk(parsed.text);
        if (parsed.error) {
          onChunk(`Error: ${parsed.error}`);
          onDone();
          return;
        }
      } catch {
        // ignore malformed JSON chunk
      }
    }
  }
  onDone();
}

function wireChat() {
  const toggle = document.getElementById('chat-toggle');
  const panel = document.getElementById('chat-panel');
  const closeBtn = document.getElementById('chat-close');
  const form = document.getElementById('chat-form');
  const input = document.getElementById('chat-input');
  const messagesEl = document.getElementById('chat-messages');
  const sendBtn = document.getElementById('chat-send');
  if (!toggle || !panel || !form || !input || !messagesEl) return;

  const history = [];
  let firstOpen = true;

  const open = () => {
    panel.classList.add('visible');
    panel.setAttribute('aria-hidden', 'false');
    if (firstOpen) {
      addMessage(
        'assistant',
        'Ask me about parcels, substations, or REPD projects in NE England. Try: "Show me operational solar farms over 5 MW in Durham" or "What\'s the wind speed at -2.0, 55.3?".'
      );
      firstOpen = false;
    }
    setTimeout(() => input.focus(), 50);
  };
  const close = () => {
    panel.classList.remove('visible');
    panel.setAttribute('aria-hidden', 'true');
  };
  toggle.addEventListener('click', () =>
    panel.classList.contains('visible') ? close() : open()
  );
  closeBtn.addEventListener('click', close);

  function addMessage(role, text) {
    const el = document.createElement('div');
    el.className = `chat-message chat-message-${role}`;
    el.textContent = text;
    messagesEl.appendChild(el);
    messagesEl.scrollTop = messagesEl.scrollHeight;
    return el;
  }

  form.addEventListener('submit', async (e) => {
    e.preventDefault();
    const userText = input.value.trim();
    if (!userText) return;
    input.value = '';
    addMessage('user', userText);
    history.push({ role: 'user', content: userText });

    const assistantEl = addMessage('assistant', '');
    let assistantText = '';
    sendBtn.disabled = true;
    await sendChat(
      history,
      (chunk) => {
        assistantText += chunk;
        assistantEl.textContent = assistantText;
        messagesEl.scrollTop = messagesEl.scrollHeight;
      },
      () => {
        history.push({ role: 'assistant', content: assistantText });
        sendBtn.disabled = false;
        input.focus();
      }
    );
  });
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
