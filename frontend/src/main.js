import maplibregl from 'maplibre-gl';
import 'maplibre-gl/dist/maplibre-gl.css';
import { Protocol } from 'pmtiles';

// ---------------------------------------------------------------------------
// PMTiles protocol registration
// ---------------------------------------------------------------------------
const protocol = new Protocol();
maplibregl.addProtocol('pmtiles', protocol.tile);

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
  });

  // Expose for debugging in browser console
  window._map = map;
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
      parcels: {
        type: 'vector',
        url: `pmtiles://${urls.tiles.parcels}`
      },
      substations: {
        type: 'vector',
        url: `pmtiles://${urls.tiles.substations}`
      },
      repd: {
        type: 'vector',
        url: `pmtiles://${urls.tiles.repd}`
      },
      constraints: {
        type: 'vector',
        url: `pmtiles://${urls.tiles.constraints}`
      },
      ne_polygon: {
        type: 'vector',
        url: `pmtiles://${urls.tiles.ne_polygon}`
      },
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
      {
        id: 'background',
        type: 'background',
        paint: { 'background-color': '#eef1f4' }
      },
      {
        id: 'basemap',
        type: 'raster',
        source: 'basemap',
        paint: { 'raster-opacity': 0.6 }
      },

      // ---- Raster layers (off by default) ----
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

      // ---- NE England outline (subtle context line) ----
      {
        id: 'ne-outline',
        type: 'line',
        source: 'ne_polygon',
        'source-layer': 'ne_polygon',
        paint: {
          'line-color': '#1a3552',
          'line-width': 1.2,
          'line-opacity': 0.5
        }
      },

      // ---- Parcels ----
      {
        id: 'parcels-fill',
        type: 'fill',
        source: 'parcels',
        'source-layer': 'parcels',
        paint: {
          'fill-color': '#4a90e2',
          'fill-opacity': 0.2
        }
      },
      {
        id: 'parcels-line',
        type: 'line',
        source: 'parcels',
        'source-layer': 'parcels',
        paint: {
          'line-color': '#4a90e2',
          'line-width': 0.5,
          'line-opacity': 0.4
        }
      },

      // ---- Constraint layers ----
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

      // ---- Substations ----
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
        paint: {
          'line-color': '#cc4422',
          'line-width': 1
        }
      },

      // ---- REPD points ----
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
// Interactions
// ---------------------------------------------------------------------------
function wireInteractions(map) {
  // Cursor change for clickable layers
  ['parcels-fill', 'substation-fill', 'repd-circle'].forEach((layer) => {
    map.on('mouseenter', layer, () => {
      map.getCanvas().style.cursor = 'pointer';
    });
    map.on('mouseleave', layer, () => {
      map.getCanvas().style.cursor = '';
    });
  });

  // Parcel click
  map.on('click', 'parcels-fill', (e) => {
    if (!e.features || !e.features.length) return;
    const f = e.features[0];
    const p = f.properties || {};
    showInfoPanel({
      title: `Parcel ${p.parcel_id ?? '(unknown)'}`,
      rows: [
        ['Local Authority', p.lad_name ?? '-'],
        ['Area (ha)', fmtNumber(p.area_ha, 2)],
        ['Mean PVOUT (kWh/kWp/yr)', fmtNumber(p.mean_pvout_kwhkwp, 0)],
        ['Mean wind @100m (m/s)', fmtNumber(p.mean_wind_speed_100m_ms, 2)],
        [
          'Distance to gen-headroom substation',
          fmtKm(p.dist_substation_gen_headroom_m)
        ],
        [
          'Distance to any-headroom substation',
          fmtKm(p.dist_substation_any_headroom_m)
        ],
        ['Nearest substation', p.nearest_substation_name ?? '-'],
        ['Constraints', constraintFlagsToString(p)]
      ]
    });
  });

  // Substation click
  map.on('click', 'substation-fill', (e) => {
    if (!e.features || !e.features.length) return;
    const f = e.features[0];
    const p = f.properties || {};
    showInfoPanel({
      title: `Substation: ${p.name ?? '(unnamed)'}`,
      rows: [
        ['Primary voltage', p.pvoltage ?? '-'],
        ['Firm capacity (MVA)', fmtNumber(p.firm_cap, 1)],
        ['Generation headroom (MVA)', fmtNumber(p.genhr, 1)],
        ['Demand headroom (MVA)', fmtNumber(p.demhr, 1)]
      ]
    });
  });

  // REPD click
  map.on('click', 'repd-circle', (e) => {
    if (!e.features || !e.features.length) return;
    const f = e.features[0];
    const p = f.properties || {};
    showInfoPanel({
      title: p['Site Name'] ?? 'REPD Site',
      rows: [
        ['Technology', p['Technology Type'] ?? '-'],
        ['Status', p['Development Status'] ?? '-'],
        ['Capacity (MWelec)', fmtNumber(p['Installed Capacity (MWelec)'], 2)]
      ]
    });
  });

  // Close button
  document.getElementById('info-panel-close').addEventListener('click', hideInfoPanel);
}

// ---------------------------------------------------------------------------
// Info panel helpers
// ---------------------------------------------------------------------------
function showInfoPanel({ title, rows }) {
  const panel = document.getElementById('info-panel');
  const body = document.getElementById('info-panel-body');
  const escapedRows = rows
    .map(
      ([label, value]) =>
        `<tr><td class="label">${escapeHtml(label)}</td><td class="value">${escapeHtml(
          value == null ? '-' : String(value)
        )}</td></tr>`
    )
    .join('');
  body.innerHTML = `
    <h3>${escapeHtml(title)}</h3>
    <table><tbody>${escapedRows}</tbody></table>
  `;
  panel.classList.add('visible');
  panel.setAttribute('aria-hidden', 'false');
}

function hideInfoPanel() {
  const panel = document.getElementById('info-panel');
  panel.classList.remove('visible');
  panel.setAttribute('aria-hidden', 'true');
}

function constraintFlagsToString(props) {
  const flags = [];
  if (truthy(props.intersects_aonb)) flags.push('AONB');
  if (truthy(props.intersects_national_park)) flags.push('National Park');
  if (truthy(props.intersects_green_belt)) flags.push('Green Belt');
  if (truthy(props.intersects_sssi)) flags.push('SSSI');
  if (truthy(props.intersects_flood)) flags.push('Flood Zone');
  return flags.length ? flags.join(', ') : 'none';
}

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
