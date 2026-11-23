/* Geomon — first product web map. No build step: plain JavaScript + MapLibre GL JS. */
"use strict";

const api = (path) => fetch(path).then(async (r) => {
  if (!r.ok) throw new Error((await r.json().catch(() => ({}))).detail || `${path}: ${r.status}`);
  return r.json();
});
const fmt = (v, digits = 0) =>
  v === null || v === undefined || Number.isNaN(v) ? "–"
    : Number(v).toLocaleString(undefined, { maximumFractionDigits: digits, minimumFractionDigits: 0 });
const esc = (s) => String(s ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
const $ = (sel) => document.querySelector(sel);

// Units shown to users (pint names -> display)
const UNIT_LABEL = {
  percent: "%", "g/cm**3": "g/cm³", "m**3/m**3": "m³/m³", "cmol/kg": "cmol(c)/kg",
  dimensionless: "", degree: "°", person: "persons", dwelling: "dwellings",
  household: "households", count: "", "EUR/year": "€/year", "1": "",
  degC: "°C", "MJ/m**2": "MJ/m²", "W/m**2": "W/m²",
};
const unit = (u) => (u in UNIT_LABEL ? UNIT_LABEL[u] : u || "");

// ---------------------------------------------------------------------------
// Map and base layers
// ---------------------------------------------------------------------------
const IGN_WMTS = (layer, format) =>
  `https://data.geopf.fr/wmts?SERVICE=WMTS&REQUEST=GetTile&VERSION=1.0.0&LAYER=${layer}` +
  `&STYLE=normal&TILEMATRIXSET=PM&TILEMATRIX={z}&TILEROW={y}&TILECOL={x}&FORMAT=${format}`;

const BASEMAPS = [
  { id: "plan", label: "Plan IGN", tiles: IGN_WMTS("GEOGRAPHICALGRIDSYSTEMS.PLANIGNV2", "image/png"),
    attribution: "© IGN" },
  { id: "ortho", label: "Orthophotos IGN", tiles: IGN_WMTS("ORTHOIMAGERY.ORTHOPHOTOS", "image/jpeg"),
    attribution: "© IGN" },
  { id: "osm", label: "OpenStreetMap", tiles: "https://tile.openstreetmap.org/{z}/{x}/{y}.png",
    attribution: "© OpenStreetMap contributors" },
  { id: "none", label: "None" },
];

const style = { version: 8, sources: {}, layers: [
  { id: "background", type: "background", paint: { "background-color": "#eef0ec" } },
] };
for (const b of BASEMAPS.filter((b) => b.tiles)) {
  style.sources[`base-${b.id}`] = { type: "raster", tiles: [b.tiles], tileSize: 256, maxzoom: 19,
    attribution: b.attribution };
  style.layers.push({ id: `base-${b.id}`, type: "raster", source: `base-${b.id}`,
    layout: { visibility: b.id === "plan" ? "visible" : "none" } });
}

// A map position in the URL (a shared link, a reload) is kept; else the map fits the site. Read it
// before the map exists: MapLibre writes its starting position into the hash.
const startHash = location.hash;
const map = new maplibregl.Map({ container: "map", style, center: [4.97, 45.43], zoom: 12, hash: true });
map.addControl(new maplibregl.NavigationControl(), "top-right");
map.addControl(new maplibregl.ScaleControl({ unit: "metric" }), "bottom-right");

function setBasemap(id) {
  for (const b of BASEMAPS.filter((b) => b.tiles)) {
    map.setLayoutProperty(`base-${b.id}`, "visibility", b.id === id ? "visible" : "none");
  }
}
$("#basemaps").innerHTML = BASEMAPS.map((b, i) =>
  `<label><input type="radio" name="basemap" value="${b.id}" ${i === 0 ? "checked" : ""}> ${b.label}</label>`).join("");
$("#basemaps").addEventListener("change", (e) => setBasemap(e.target.value));

// ---------------------------------------------------------------------------
// Vector layers
// ---------------------------------------------------------------------------
const RPG_COLORS = {
  1: "#e6c229", 2: "#f4a259", 3: "#d9b26f", 4: "#c9a227", 5: "#f2e94e", 6: "#ffb000", 7: "#e9c46a",
  8: "#a3b18a", 9: "#bfa5d8", 11: "#cfcfcf", 14: "#7fb7be", 15: "#b5838d", 16: "#8cc084",
  17: "#c2b280", 18: "#57a773", 19: "#9bd770", 20: "#e07a5f", 21: "#8e3b46", 22: "#a0522d",
  23: "#6b8f71", 24: "#9d8189", 25: "#ef476f", 26: "#90be6d", 28: "#adb5bd",
};
const rpgColorExpr = ["match", ["to-number", ["get", "code_group"]],
  ...Object.entries(RPG_COLORS).flatMap(([k, c]) => [Number(k), c]), "#adb5bd"];

// tiles: vector tiles of that layer (only the tiles in view are loaded, from zoom minzoom);
// source: a GeoJSON loaded whole (small layers)
const VECTOR_LAYERS = [
  { id: "osm-landuse", label: "OSM land use", tiles: "landuse", minzoom: 10, on: false,
    layers: [{ type: "fill", paint: { "fill-color": ["match", ["get", "kind"],
      ["forest", "wood"], "#7fb77e", ["meadow", "grassland"], "#c5e1a5", ["farmland"], "#f3e9c6",
      ["orchard", "vineyard"], "#d9a9a0", ["residential"], "#e0d6d6", "#d6dcc7"], "fill-opacity": 0.45 } }],
    swatch: "#7fb77e" },
  { id: "rpg", label: "RPG parcels (year below)", tiles: "rpg", minzoom: 9, on: true,
    layers: [
      { type: "fill", paint: { "fill-color": rpgColorExpr,
        "fill-opacity": ["case", ["get", "in_commune"], 0.6, 0.3] } },
      { type: "line", paint: { "line-color": "#3d3d3d", "line-width": 0.4, "line-opacity": 0.6 } },
    ], swatch: "#57a773" },
  { id: "osm-water", label: "OSM water", tiles: "water", minzoom: 9, on: true,
    layers: [{ type: "fill", paint: { "fill-color": "#6aa9d8", "fill-opacity": 0.7 } }], swatch: "#6aa9d8" },
  { id: "osm-waterways", label: "OSM waterways", tiles: "waterways", minzoom: 9, on: true,
    layers: [{ type: "line", paint: { "line-color": "#3a86c8",
      "line-width": ["match", ["get", "kind"], ["river"], 2.5, ["stream"], 1.2, 0.8] } }], swatch: "#3a86c8" },
  { id: "osm-buildings", label: "OSM buildings", tiles: "buildings", minzoom: 13, on: false,
    layers: [{ type: "fill", paint: { "fill-color": "#8d7b6a", "fill-opacity": 0.8 } }], swatch: "#8d7b6a" },
  { id: "osm-roads", label: "OSM roads", tiles: "roads", minzoom: 10, on: false,
    layers: [{ type: "line", paint: { "line-color": ["match", ["get", "kind"],
      ["motorway", "trunk", "primary"], "#d1495b", ["secondary", "tertiary"], "#edae49", "#6c757d"],
      "line-width": ["match", ["get", "kind"], ["motorway", "trunk", "primary"], 3,
        ["secondary", "tertiary"], 2, ["track", "path", "footway"], 0.6, 1.2] } }], swatch: "#edae49" },
  { id: "osm-railways", label: "OSM railways", tiles: "railways", minzoom: 9, on: false,
    layers: [{ type: "line", paint: { "line-color": "#333", "line-width": 1.5, "line-dasharray": [3, 2] } }],
    swatch: "#333" },
  { id: "osm-pois", label: "OSM places & POIs", tiles: "pois", minzoom: 12, on: false,
    layers: [{ type: "circle", paint: { "circle-radius": 4, "circle-color": "#6f42c1",
      "circle-stroke-color": "#fff", "circle-stroke-width": 1 } }], swatch: "#6f42c1" },
  { id: "admin", label: "Communes & EPCI (AOI communes in red)", source: "/api/admin.geojson?levels=COM,EPCI", on: true,
    layers: [
      { type: "fill", filter: ["get", "is_aoi"], paint: { "fill-color": "#2f6f4f", "fill-opacity": 0.05 } },
      { type: "line", filter: ["==", ["get", "level"], "COM"], paint: {
        "line-color": ["case", ["get", "is_aoi"], "#c0392b", "#555"],
        "line-width": ["case", ["get", "is_aoi"], 1.5, 1], "line-dasharray": [4, 2] } },
      { type: "line", filter: ["==", ["get", "level"], "EPCI"], paint: { "line-color": "#6f42c1", "line-width": 2 } },
      { type: "symbol", filter: ["==", ["get", "level"], "COM"], layout: { "text-field": ["get", "name"],
        "text-size": 12, "symbol-placement": "point" },
        paint: { "text-color": "#333", "text-halo-color": "#fff", "text-halo-width": 1.5 } },
    ], swatch: "#c0392b" },
];

let firstVectorLayer = null;

function addAreaLayer() {
  map.addSource("area-sel", { type: "geojson", data: { type: "FeatureCollection", features: [] } });
  map.addLayer({ id: "area-sel-line", type: "line", source: "area-sel",
    paint: { "line-color": "#c0392b", "line-width": 3.5, "line-opacity": 0.9 } });
}

const EMPTY_FC = { type: "FeatureCollection", features: [] };

function addVectorLayers() {
  const site = encodeURIComponent(aoiInfo.code);
  for (const v of VECTOR_LAYERS) {
    if (v.tiles) {
      // Tiles are only requested while one of the source's layers is visible. Tile URLs must be
      // absolute (they are fetched by the map's web workers).
      map.addSource(v.id, { type: "vector", minzoom: v.minzoom ?? 0, maxzoom: 15,
        tiles: [`${location.origin}/api/tiles/${v.tiles}/{z}/{x}/{y}.pbf?site=${site}`] });
      v.loaded = true;
    } else {
      // A GeoJSON source is downloaded as soon as it is added, even when its layers are hidden:
      // layers that start hidden get their data the first time they are shown
      map.addSource(v.id, { type: "geojson", data: v.on ? v.source : EMPTY_FC });
      v.loaded = v.on;
    }
    v.layers.forEach((l, i) => {
      const id = `${v.id}-${i}`;
      if (!firstVectorLayer) firstVectorLayer = id;
      map.addLayer({ id, source: v.id, ...(v.tiles ? { "source-layer": v.tiles } : {}), ...l,
        layout: { ...(l.layout || {}), visibility: v.on ? "visible" : "none" } });
    });
  }
  $("#vector-layers").innerHTML = VECTOR_LAYERS.map((v) =>
    `<label><input type="checkbox" value="${v.id}" ${v.on ? "checked" : ""}>
     <span class="swatch" style="background:${v.swatch}"></span> ${v.label}</label>`).join("");
  $("#vector-layers").addEventListener("change", (e) => {
    const v = VECTOR_LAYERS.find((x) => x.id === e.target.value);
    if (e.target.checked && !v.loaded) { map.getSource(v.id).setData(v.source); v.loaded = true; }
    v.layers.forEach((_, i) => map.setLayoutProperty(`${v.id}-${i}`, "visibility", e.target.checked ? "visible" : "none"));
  });
}

// ---------------------------------------------------------------------------
// Raster overlays (DEM, soil) rendered by the API
// ---------------------------------------------------------------------------
let rasterCatalogue = [];

// Two overlays: terrain (Layers tab, stays on the map) and soil (Soils tab, shown on that tab only)
const OVERLAYS = {
  raster: { sel: "#raster-var", depth: "#raster-depth", depthRow: "#depth-row", opacity: "#raster-opacity", legend: "#raster-legend" },
  soil: { sel: "#soil-var", depth: "#soil-depth", depthRow: "#soil-depth-row", opacity: "#soil-opacity", legend: "#soil-legend" },
};

const soilSource = () => $("#soil-source").value;

function fillSoilVars() {
  const ds = rasterCatalogue.find((d) => d.name === soilSource());
  const keep = $("#soil-var").value.split(":")[1];
  $("#soil-var").innerHTML = '<option value="">— none —</option>' + (ds ? ds.variables.map((v) =>
    `<option value="${ds.name}:${v.name}">${esc(v.long_name)}${v.units && unit(v.units) ? ` (${esc(unit(v.units))})` : ""}</option>`).join("") : "");
  if (ds && keep && ds.variables.some((v) => v.name === keep)) $("#soil-var").value = `${ds.name}:${keep}`;
}

async function initRasters() {
  rasterCatalogue = await api("/api/rasters");
  if (!rasterCatalogue.some((d) => d.name === "soil_inrae")) $("#soil-source").value = "soil";
  fillSoilVars();
  $("#soil-source").addEventListener("change", () => {
    fillSoilVars(); updateOverlay("soil"); updateSoilArea().catch(() => {});
    if (soilPointAt) soilAtPoint(soilPointAt).catch(() => {});
  });
  for (const ds of rasterCatalogue) {
    if (ds.name.startsWith("soil")) continue;
    const sel = $(OVERLAYS.raster.sel);
    const g = document.createElement("optgroup");
    g.label = ds.name === "dem" ? "Elevation (IGN RGE ALTI)" : "Soil (SoilGrids 2.0)";
    for (const v of ds.variables) {
      const o = document.createElement("option");
      o.value = `${ds.name}:${v.name}`;
      o.textContent = `${v.long_name}${v.units && unit(v.units) ? ` (${unit(v.units)})` : ""}`;
      g.appendChild(o);
    }
    sel.appendChild(g);
  }
  for (const [id, c] of Object.entries(OVERLAYS)) {
    $(c.sel).addEventListener("change", () => updateOverlay(id));
    $(c.depth).addEventListener("change", () => updateOverlay(id));
    $(c.opacity).addEventListener("input", (e) => {
      if (map.getLayer(id)) map.setPaintProperty(id, "raster-opacity", Number(e.target.value));
    });
  }
}

const updateRaster = () => updateOverlay("raster");

async function updateOverlay(id) {
  const c = OVERLAYS[id];
  const value = $(c.sel).value;
  if (map.getLayer(id)) map.removeLayer(id);
  if (map.getSource(id)) map.removeSource(id);
  $(c.legend).innerHTML = "";
  if (!value) { $(c.depthRow).hidden = true; return; }

  const [name, variable] = value.split(":");
  const ds = rasterCatalogue.find((d) => d.name === name);
  const meta = ds.variables.find((v) => v.name === variable);
  const depthSel = $(c.depth);
  if (meta.has_depth) {
    if (depthSel.dataset.for !== name) {
      depthSel.innerHTML = ds.depths.map((d, i) => `<option value="${i}">${d}</option>`).join("");
      depthSel.dataset.for = name;
    }
    $(c.depthRow).hidden = false;
  } else {
    $(c.depthRow).hidden = true;
  }
  const depth = meta.has_depth ? Number(depthSel.value || 0) : 0;
  const info = await api(`/api/raster/${name}/${variable}/info?depth=${depth}`);
  if ($(c.sel).value !== value) return;   // another variable chosen meanwhile
  if (map.getLayer(id)) map.removeLayer(id);
  if (map.getSource(id)) map.removeSource(id);
  // XYZ tiles at the resolution of the zoom (beyond maxzoom, the deepest tiles are enlarged)
  map.addSource(id, { type: "raster", tiles: [`${location.origin}${info.tiles}`], tileSize: 256,
    bounds: info.bounds, maxzoom: info.maxzoom });
  const visible = id !== "soil" || $("#tab-soil").classList.contains("active");
  map.addLayer({ id, type: "raster", source: id, layout: { visibility: visible ? "visible" : "none" },
    paint: { "raster-opacity": Number($(c.opacity).value), "raster-resampling": "nearest" } },
    firstVectorLayer);
  renderRasterLegend(info, meta, c.legend);
}

// A colour ramp (legend: [{color}]) and three labels under it
function rampLegend(legend, labels) {
  const stops = legend.map((l, i) => `${l.color} ${(100 * i) / (legend.length - 1)}%`).join(",");
  return `<div class="ramp" style="background:linear-gradient(to right,${stops})"></div>
    <div class="ramp-labels">${labels.map((l) => `<span>${l}</span>`).join("")}</div>`;
}

// An animation of map images (radar, nowcast, forecast maps): a slider, play / pause, and
// show(frame, i) to display a frame. holdAt(i): pause there a moment while playing.
function imagePlayer({ slider, button, stepMs, show, holdAt = () => false }) {
  const p = { frames: [], i: 0, timer: null };
  p.show = (i) => {
    if (!p.frames.length) return;
    p.i = i;
    $(slider).value = i;
    show(p.frames[i], i);
  };
  p.stop = () => { clearInterval(p.timer); p.timer = null; $(button).textContent = "▶"; };
  p.play = () => {
    if (!p.frames.length) return;
    $(button).textContent = "❚❚";
    let hold = 0;
    if (p.i >= p.frames.length - 1) p.i = -1;
    p.timer = setInterval(() => {
      if (holdAt(p.i) && hold++ < 4) return;
      hold = 0;
      p.show((p.i + 1) % p.frames.length);
    }, stepMs);
  };
  p.load = (frames, start) => {
    p.stop();
    p.frames = frames;
    $(slider).max = Math.max(0, frames.length - 1);
    p.show(start);
  };
  p.clear = () => { p.stop(); p.frames = []; };
  $(button).addEventListener("click", () => (p.timer ? p.stop() : p.play()));
  $(slider).addEventListener("input", (e) => { p.stop(); p.show(Number(e.target.value)); });
  return p;
}

// A map image overlay (one source and layer of the same id) below the vector layers
function setImageLayer(id, url, coordinates, opacity) {
  if (map.getSource(id)) { map.getSource(id).updateImage({ url, coordinates }); return; }
  map.addSource(id, { type: "image", url, coordinates });
  map.addLayer({ id, type: "raster", source: id,
    paint: { "raster-opacity": opacity, "raster-fade-duration": 0, "raster-resampling": "nearest" } }, firstVectorLayer);
}
function removeLayerAndSource(id) {
  if (map.getLayer(id)) map.removeLayer(id);
  if (map.getSource(id)) map.removeSource(id);
}
const preload = (frames) => frames.map((f) => { const im = new Image(); im.src = f.url; return im; });   // the browser caches them

function renderRasterLegend(info, meta, target = "#raster-legend") {
  const u = unit(info.units);
  let html = `<div class="title">${esc(info.long_name)}${u ? ` (${esc(u)})` : ""}</div>`;
  if (meta.categorical) {
    html += info.legend.map((l) => `<div class="item"><span class="swatch" style="background:${l.color}"></span>${esc(l.label)}</div>`).join("");
  } else if (meta.name !== "hillshade") {
    const digits = info.vmax - info.vmin < 1 ? 3 : info.vmax - info.vmin < 20 ? 1 : 0;
    html += rampLegend(info.legend, [info.vmin, (info.vmin + info.vmax) / 2, info.vmax].map((v) => fmt(v, digits)))
      + '<div class="muted">Colour scale: 2nd–98th percentile over the area.</div>';
  }
  $(target).innerHTML = html;
}

// ---------------------------------------------------------------------------
// RPG summary
// ---------------------------------------------------------------------------
let rpgYear = null;      // null: the latest RPG

// RPG years: parcels since 2015, before that the farms' îlots with their majority crop group
async function initRpgYears() {
  const y = await api("/api/rpg/years");
  const opts = [...y.parcels.map((v) => [v, `${v} · parcels and crops`]), ...y.ilots.map((v) => [v, `${v} · îlots, majority crop group`])]
    .sort((a, b) => b[0] - a[0]);
  $("#rpg-year").innerHTML = opts.map(([v, l]) => `<option value="${v}" ${v === y.latest ? "selected" : ""}>${l}</option>`).join("");
  $("#rpg-year").addEventListener("change", (e) => {
    rpgYear = Number(e.target.value);
    // The summary first: the new tiles take the browser's connections to the server for a while
    updateRpgSummary().catch(() => {});
    const site = encodeURIComponent(aoiInfo.code);
    map.getSource("rpg")?.setTiles([`${location.origin}/api/tiles/rpg/{z}/{x}/{y}.pbf?site=${site}&year=${rpgYear}`]);
  });
}

let rpgSeq = 0;
async function updateRpgSummary() {
  const seq = ++rpgSeq;      // a slower earlier answer (another area or year) must not overwrite this one
  const s = await api(`/api/rpg/summary?area=${encodeURIComponent(currentArea)}${rpgYear ? `&year=${rpgYear}` : ""}`);
  if (seq !== rpgSeq) return;
  $("#rpg-summary").innerHTML =
    (s.ilots
      ? `<div class="muted">Îlots by majority crop group in ${esc(s.area.name)}, ${s.year} — total ${fmt(s.total_ha, 1)} ha. Before 2015 the RPG gives each farm îlot (a block of parcels) and its main crop only.</div>`
      : `<div class="muted">Declared crops in ${esc(s.area.name)}, ${s.year} — total ${fmt(s.total_ha, 1)} ha</div>`) +
    s.groups.map((g) => `<div class="item"><span class="swatch" style="background:${RPG_COLORS[Number(g.code_group)] || "#adb5bd"}"></span>
      ${esc(g.crop_group)}<span class="val">${fmt(g.area_ha, 1)} ha · ${g.parcels}</span></div>`).join("");
}

// ---------------------------------------------------------------------------
// Statistics tab
// ---------------------------------------------------------------------------
let aoiInfo = null;
let currentArea = null;   // code of the selected area: the group (site) or one of its communes

const areaOf = (code) => aoiInfo.areas.find((a) => a.code === code);
const areaLabel = (a) => a.kind === "site" ? `Group — ${a.name} (${a.members.length} communes)`
  : a.kind === "farm" ? `${a.name} — ${a.plots} plots, ${fmt(a.declared_ha ?? a.area_ha, 1)} ha` : `${a.name} (${a.code})`;

// Beyond this many communes (a département site), lists group them instead of showing each one
const MANY_COMMUNES = 12;

function renderHierarchy() {
  const many = aoiInfo.communes.length > MANY_COMMUNES;
  const shown = many ? aoiInfo.hierarchy.filter((h) => h.level !== "COM") : aoiInfo.hierarchy;
  $("#hierarchy").innerHTML = (many ? `<div class="lvl"><span class="k">Communes</span><span>${aoiInfo.communes.length} (choose one in the Area menu)</span></div>` : "") +
    shown.map((h) =>
    `<div class="lvl"><span class="k">${esc(h.level_name)}</span><span>${esc(h.name)} <span class="muted">(${esc(h.code)})</span></span></div>`
  ).join("") + `<div class="muted" style="font-size:11px;margin-top:4px">Geography: COG ${aoiInfo.hierarchy[0]?.vintage ?? ""}, valid from ${aoiInfo.hierarchy[0]?.valid_from ?? ""}</div>`;
  // Statistics areas: the group (sums of the communes), each commune, then the parent units
  const group = aoiInfo.is_group
    ? [`<option value="GRP:${esc(aoiInfo.code)}">Group — ${esc(aoiInfo.name)} (sum of ${aoiInfo.members.length} communes)</option>`] : [];
  const units = aoiInfo.hierarchy.filter((h) => h.level !== "ARR").map((h) =>
    `<option value="${h.level}:${h.code}">${esc(h.level_name)} — ${esc(h.name)}</option>`);
  $("#stats-geo").innerHTML = [...group, ...units].join("");
}

function renderAreaSelector() {
  const option = (a) => `<option value="${esc(a.code)}">${esc(areaLabel(a))}</option>`;
  const communes = aoiInfo.areas.filter((a) => a.kind === "commune");
  if (communes.length > MANY_COMMUNES) {
    // Many communes: grouped by intercommunality (EPCI), alphabetically within each
    const epciOf = Object.fromEntries(aoiInfo.communes.map((c) => [c.code, c.epci]));
    const epciName = Object.fromEntries(aoiInfo.hierarchy.filter((h) => h.level === "EPCI").map((h) => [h.code, h.name]));
    const groups = new Map();
    for (const a of [...communes].sort((x, y) => x.name.localeCompare(y.name))) {
      const k = epciOf[a.code] || "";
      if (!groups.has(k)) groups.set(k, []);
      groups.get(k).push(a);
    }
    const sorted = [...groups.entries()].sort(([a], [b]) => (epciName[a] || "~").localeCompare(epciName[b] || "~"));
    $("#area").innerHTML = aoiInfo.areas.filter((a) => a.kind !== "commune").map(option).join("") +
      sorted.map(([k, list]) => `<optgroup label="${esc(epciName[k] || "Other communes")} (${list.length})">${list.map(option).join("")}</optgroup>`).join("");
  } else {
    $("#area").innerHTML = aoiInfo.areas.map(option).join("");
  }
  $("#area-row").hidden = aoiInfo.areas.length < 2;
  $("#area").addEventListener("change", (e) => setArea(e.target.value, true));
}

function setArea(code, fit = false) {
  currentArea = code;
  $("#area").value = code;
  const a = areaOf(code);
  const c = aoiInfo.communes.find((x) => x.code === code);
  $("#aoi-title").textContent = a.kind === "site"
    ? `${a.name} · ${a.members.length} communes · ${fmt(a.area_ha)} ha`
    : a.kind === "farm" ? `${aoiInfo.name} · ${a.name} · ${a.plots} plots · ${fmt(a.area_ha, 1)} ha`
    : `${a.name} (INSEE ${a.code}${c?.postal_code ? `, ${c.postal_code}` : ""}) · ${fmt(a.area_ha)} ha`;
  if (map.getSource("area-sel")) map.getSource("area-sel").setData(`/api/area.geojson?area=${encodeURIComponent(code)}`);
  if (fit) map.fitBounds(c ? c.bbox : a.kind === "farm" ? aoiInfo.farm.bbox : aoiInfo.bbox, { padding: 30, duration: 500 });
  // The statistics follow the selected area
  const geo = a.kind === "site" ? `GRP:${a.code}` : `COM:${a.code}`;
  if ([...$("#stats-geo").options].some((o) => o.value === geo)) {
    $("#stats-geo").value = geo;
    $("#stats-period").innerHTML = "";
    updateStats();
  }
  updateRpgSummary().catch(() => {});
  updateSoilArea().catch(() => {});
  updateAreaSummary().catch((err) => { $("#area-summary").innerHTML = `<p class="muted">${esc(err.message)}</p>`; });
  if (wx.catalogue) setWeatherLocation(wx.lngLat, !wx.lngLat);  // the area average follows the area
  updateS2().catch((err) => { $("#s2-status").innerHTML = esc(err.message); });
  updateDMP();
  updateS1().catch((err) => { $("#s1-status").innerHTML = esc(err.message); });
}

async function updateAreaSummary() {
  $("#area-summary").innerHTML = '<p class="muted">Loading…</p>';
  const s = await api(`/api/area/summary?area=${encodeURIComponent(currentArea)}`);
  const dem = s.rasters.dem?.variables, soil = s.rasters.soil;
  const sv = soil?.variables;
  const row = (k, v) => `<tr><td>${k}</td><td class="num">${v}</td></tr>`;
  const top = (name, digits = 1) => sv?.[name]?.mean ? `${fmt(sv[name].mean[0], digits)} ${esc(unit(sv[name].units))}` : "–";
  const crops = s.rpg.groups.slice(0, 5).map((g) => `${esc(g.crop_group)} ${fmt(g.area_ha)} ha`).join(" · ");
  $("#area-summary").innerHTML = `<div class="card"><h3>${esc(s.area.name)}</h3>
    <div class="meta">${s.area.kind === "site" ? `Group of ${s.area.members.length} communes` : s.area.kind === "farm" ? "The farm's plots" : `Commune ${esc(s.area.code)}`}
      · terrain and soil averaged over the area</div>
    <table class="kv">
      ${row("Area", `${fmt(s.area.area_ha)} ha`)}
      ${row("Declared crops (RPG)", `${fmt(s.rpg.total_ha)} ha (${fmt(100 * s.rpg.total_ha / s.area.area_ha)} %)`)}
      ${dem ? row("Elevation (min–max)", `${fmt(dem.elevation.mean)} m (${fmt(dem.elevation.min)}–${fmt(dem.elevation.max)})`) : ""}
      ${dem ? row("Slope (10–90 %)", `${fmt(dem.slope.mean, 1)}° (${fmt(dem.slope.p10, 1)}–${fmt(dem.slope.p90, 1)})`) : ""}
      ${sv ? row("Water capacity 0–200 cm", `${fmt(sv.awc_total.mean)} mm`) : ""}
      ${sv ? row(`Topsoil texture (${esc(soil.depths[0])})`, esc(sv.texture_usda?.dominant?.[0] ?? "–")) : ""}
      ${sv ? row("Topsoil clay · sand", `${top("clay")} · ${top("sand")}`) : ""}
      ${sv ? row("Topsoil organic carbon · pH", `${top("soc")} · ${top("phh2o")}`) : ""}
    </table>
    ${crops ? `<div class="meta" style="margin-top:4px">Main crop groups: ${crops}</div>` : ""}
    <div class="muted" style="font-size:11px">Soil: SoilGrids ${fmt(soil?.resolution_m)} m, ${soil?.cells ?? 0} cells; elevation: RGE ALTI ${fmt(s.rasters.dem?.resolution_m)} m.</div></div>`;
}

function lineChart(points, { width = 360, height = 160, unitLabel = "" } = {}) {
  const pts = points.filter((p) => p.value !== null).map((p) => ({ x: Number(p.time), y: p.value }));
  if (pts.length < 2) return '<p class="muted">Not enough data.</p>';
  const m = { l: 44, r: 8, t: 8, b: 20 };
  const xs = pts.map((p) => p.x), ys = pts.map((p) => p.y);
  const x0 = Math.min(...xs), x1 = Math.max(...xs);
  const y0 = 0, y1 = Math.max(...ys) * 1.1;
  const sx = (x) => m.l + ((x - x0) / (x1 - x0)) * (width - m.l - m.r);
  const sy = (y) => height - m.b - ((y - y0) / (y1 - y0)) * (height - m.t - m.b);
  const path = pts.map((p, i) => `${i ? "L" : "M"}${sx(p.x).toFixed(1)},${sy(p.y).toFixed(1)}`).join("");
  const yticks = [0, 0.25, 0.5, 0.75, 1].map((f) => y0 + f * (y1 - y0));
  return `<svg viewBox="0 0 ${width} ${height}" role="img">
    ${yticks.map((t) => `<line x1="${m.l}" x2="${width - m.r}" y1="${sy(t)}" y2="${sy(t)}" stroke="#e3e3e3"/>
      <text x="${m.l - 4}" y="${sy(t) + 3}" text-anchor="end">${fmt(t)}</text>`).join("")}
    <path d="${path}" fill="none" stroke="#2f6f4f" stroke-width="2"/>
    ${pts.map((p) => `<circle cx="${sx(p.x)}" cy="${sy(p.y)}" r="2.5" fill="#2f6f4f"><title>${p.x}: ${fmt(p.y)} ${esc(unitLabel)}</title></circle>`).join("")}
    <text x="${m.l}" y="${height - 4}">${x0}</text>
    <text x="${width - m.r}" y="${height - 4}" text-anchor="end">${x1}</text>
  </svg>`;
}

async function updateStats() {
  const [level, code] = $("#stats-geo").value.split(":");
  const q = `level=${level}&code=${encodeURIComponent(code)}`;
  const [series, summary] = await Promise.all([
    api(`/api/stats/series?dataset=DS_POPULATIONS_HISTORIQUES&${q}`),
    api(`/api/stats/summary?${q}`),
  ]);
  const pop = series.series.find((s) => s.measure === "PMUN") || series.series[0];
  $("#pop-chart").innerHTML = pop
    ? `<div class="muted">${esc(pop.label)} (${esc(unit(pop.unit))})</div>${lineChart(pop.points, { unitLabel: unit(pop.unit) })}`
    : '<p class="muted">No data.</p>';

  const valueCell = (i) => i.not_additive ? '<span class="muted" title="Rates, means and medians cannot be summed over communes">not summable</span>'
    : i.incomplete ? '<span class="muted" title="Not available for every commune">incomplete</span>'
    : i.confidential ? '<span class="muted">confidential</span>' : `${fmt(i.value, 1)} ${esc(unit(i.unit))}`;
  const breakdownTable = (b) => {
    const max = Math.max(...b.rows.map((r) => r.value || 0), 1);
    return `<details><summary>${esc(b.measure_label)} by ${esc(b.concept_label.toLowerCase())}</summary>
      <table class="kv">${b.rows.map((r) => `<tr><td>${esc(r.label)}</td>
        <td style="width:35%"><div class="bar" style="width:${(100 * (r.value || 0)) / max}%"></div></td>
        <td class="num">${valueCell(r)}</td></tr>`).join("")}</table></details>`;
  };
  $("#stats-summary").innerHTML = summary.map((d) => `
    <div class="card"><h3>${esc(d.title)}</h3>
      <div class="meta">${esc(d.dataset)} · ${esc(d.time_period)} · geography COG ${esc(d.geo_vintage ?? "")}${d.level === "GRP" ? " · sum of the communes" : ""}</div>
      <table class="kv">${d.indicators.map((i) => `<tr><td>${esc(i.label)}</td><td class="num">${valueCell(i)}</td></tr>`).join("")}</table>
      ${d.breakdowns.map(breakdownTable).join("")}
    </div>`).join("") || '<p class="muted">No statistics for this area.</p>';
  updateTable();
}

async function initStatsExplorer() {
  const datasets = await api("/api/stats/datasets");
  $("#stats-dataset").innerHTML = datasets.map((d) =>
    `<option value="${d.id}">${esc(d.title_fr || d.id)}</option>`).join("");
  $("#stats-dataset").addEventListener("change", () => { $("#stats-period").innerHTML = ""; updateTable(); });
  $("#stats-period").addEventListener("change", updateTable);
  $("#stats-geo").addEventListener("change", () => { $("#stats-period").innerHTML = ""; updateStats(); });
}

async function updateTable() {
  const [level, code] = $("#stats-geo").value.split(":");
  const ds = $("#stats-dataset").value;
  if (!ds) return;
  const period = $("#stats-period").value;
  const t = await api(`/api/stats/table?dataset=${ds}&level=${level}&code=${encodeURIComponent(code)}${period ? `&time=${period}` : ""}`);
  if (!period) {
    $("#stats-period").innerHTML = t.periods.map((p) => `<option ${p === t.time_period ? "selected" : ""}>${p}</option>`).join("");
  }
  $("#stats-table").innerHTML = t.rows.length ? `<table class="kv"><tr>${t.columns.map((c) => `<th>${esc(c.label)}</th>`).join("")}<th class="num">Value</th></tr>
    ${t.rows.map((r) => `<tr>${t.columns.map((c) => `<td>${esc(r.dims[c.key])}</td>`).join("")}
      <td class="num">${r.value === null ? `<span class="muted">${r.not_additive ? "not summable" : r.incomplete ? "incomplete" : "n/a"}</span>` : `${fmt(r.value, 1)} ${esc(unit(r.unit))}`}</td></tr>`).join("")}
  </table>` : '<p class="muted">No observations.</p>';
}

// ---------------------------------------------------------------------------
// Weather tab
// ---------------------------------------------------------------------------
// Categorical slots 1-3 of the reference palette plus a deeper magenta for AgERA5
// (slot 4 yellow and slot 5 magenta are too close to orange: validated all-pairs;
// aqua is below 3:1 on white, so the legend and the comparison table carry the labels).
// ERA5 shares ERA5-Land's orange, dashed: the same family (ERA5-Land re-runs ERA5's land model),
// and no fifth hue passes against these four (violet ~ blue, yellow and red ~ orange, green ~
// orange for protans: validated all-pairs), so the dash is the second encoding.
const WX_SERIES = [
  { key: "mf", label: "Météo-France AROME/ARPEGE", color: "#2a78d6", product: "om_mf_hist" },
  { key: "era5land", label: "ERA5-Land", color: "#eb6834", product: "era5land" },
  { key: "era5", label: "ERA5", color: "#eb6834", dash: "5 3", product: "era5" },
  { key: "agera5", label: "AgERA5", color: "#c2407a", product: "agera5" },
  { key: "sim2", label: "SIM2 / SAFRAN", color: "#1baf7a", product: "sim2" },
  { key: "stations", label: "Météo-France stations (measured)", color: "#3d3d3a", product: "mf_stations" },
  { key: "comephore", label: "COMEPHORE", color: "#7b4fd6", product: "comephore" },
  { key: "mf_etp", label: "Météo-France ETP grid", color: "#1baf7a", dash: "2 3", product: "mf_etp" },
  { key: "clms_ssm", label: "CLMS surface soil moisture", color: "#a0522d", product: "clms_ssm" },
  { key: "clms_swi", label: "CLMS Soil Water Index", color: "#a0522d", dash: "4 2", product: "clms_swi" },
];
// The stations a measured series was taken from: "ST OVIN 5.7 km (+2)", the details as a tooltip
function stationsText(s) {
  const st = Object.values(s.stations || {});
  if (!st.length) return null;
  const tip = st.map((x) => `${x.name} (${fmt(x.distance_km, 1)} km): ${x.variables.join(", ")}`).join("\n");
  return `<span title="${esc(tip)}">${esc(st[0].name)} ${fmt(st[0].distance_km, 1)} km${st.length > 1 ? ` (+${st.length - 1})` : ""}</span>`;
}
const WX_GROUPS = [
  ["Temperature", /^t_/], ["Precipitation and water", /^(precip|rain|snowfall|et0|evap|effective_rain|drainage|runoff)/],
  ["Radiation and energy", /^(radiation|longwave|sensible)/], ["Humidity", /^(rh_|vp|q_)/],
  ["Wind, clouds and boundary layer", /^(wind|gust|cloud|blh)/],
  ["Snow", /^(snow_depth|swe)/], ["Soil", /^(soil_|swi|wg_|ssm)/],
];
const wx = { catalogue: null, lngLat: null, seq: 0 };
// Swatch of a series: its colour, dashed like its line when it has a dash
const swatchBg = (s) => (s.dash ? `repeating-linear-gradient(90deg,${s.color} 0 4px,transparent 4px 6px)` : s.color);

async function initWeather() {
  wx.catalogue = await api("/api/weather/products");
  const avail = new Set(wx.catalogue.series.filter((s) => s.available).flatMap((s) => s.variables));
  const vars = wx.catalogue.variables.filter((v) => avail.has(v.name));
  $("#wx-var").innerHTML = WX_GROUPS.map(([g, re]) => {
    const opts = vars.filter((v) => re.test(v.name));
    return opts.length ? `<optgroup label="${g}">${opts.map((v) =>
      `<option value="${v.name}">${esc(v.long_name)}${unit(v.units) ? ` (${esc(unit(v.units))})` : ""}</option>`).join("")}</optgroup>` : "";
  }).join("");
  // The product behind each series (the forecast alone while an archive is not downloaded yet)
  for (const s of WX_SERIES) s.product = wx.catalogue.series.find((c) => c.key === s.key)?.product || s.product;
  // Normals: the series whose record covers the reference period
  $("#wx-nsource").innerHTML = wx.catalogue.series.filter((s) => s.available && s.start && s.start <= "1991-01-01")
    .map((s) => `<option value="${s.key}" ${s.key === "sim2" ? "selected" : ""}>${esc(s.label)}</option>`).join("");  // SIM2: updated daily
  $("#wx-series").innerHTML = WX_SERIES.map((s) => {
    const meta = wx.catalogue.series.find((c) => c.key === s.key);
    return `<label><input type="checkbox" value="${s.key}" ${meta?.available ? "checked" : "disabled"}>
      <span class="swatch" style="background:${swatchBg(s)}"></span> ${esc(meta?.label || s.label)}</label>`;
  }).join("");
  for (const id of ["#wx-var", "#wx-period", "#wx-series"]) $(id).addEventListener("change", updateWeather);
  fillNormalYears();
  $("#wx-view").addEventListener("change", setWeatherView);
  $("#wx-nstart").addEventListener("change", () => { fillNormalYears(); updateWeather(); });
  for (const id of ["#wx-nsource", "#wx-nyear", "#wx-nforecast"]) $(id).addEventListener("change", updateWeather);
  $("#wx-nzoom").addEventListener("change", () => { if (wx.normal) renderNormal(wx.normal); });
  $("#wx-cells").addEventListener("change", updateWeatherCells);
  $("#wx-series").addEventListener("change", updateWeatherCells);
  const p = Object.fromEntries(wx.catalogue.products.map((x) => [x.name, x]));
  $("#wx-sources").innerHTML = `Météo-France archived forecasts at Open-Meteo: AROME HD 1.5 km (temperature, humidity, wind;
    precipitation from 2024), AROME 2.5 km (all variables from 2024-01-02), ARPEGE 11 km before.
    ERA5-Land 0.1° (soil and snow) and ERA5 0.25° (cloud cover, boundary layer, gusts, heat fluxes), hourly, from the Copernicus CDS, about 10 days behind;
    their ET0 is computed (FAO-56 hourly), ERA5's actual evapotranspiration comes from its latent heat flux. Forecast: AROME/ARPEGE
    up to ~4 days, then ECMWF IFS 9 km to 15 days. AgERA5 (Copernicus CDS): ERA5 corrected to 0.1°, about 10 days behind. Daily values over the local day (${esc(wx.catalogue.local_time_zone)});
    SIM2 keeps Météo-France's day definitions (rain and ET over 06–06 UTC).
    Measured: daily values of the Météo-France stations within 40 km, each variable from the nearest station measuring it
    (rain over 06–06 UTC). COMEPHORE: Météo-France's hourly rain reanalysis at 1 km (radar merged with rain gauges, since 1997,
    about two months behind), summed over UTC days. Météo-France ETP grid: daily Penman-Monteith ETP at 0.125°, since 1970.
    Updated: ${["om_mf_hist", "om_forecast", "era5land", "era5", "om_era5land", "agera5", "sim2", "mf_stations", "comephore", "mf_etp"].map((n) => p[n]?.updated ? `${n} ${p[n].updated.slice(0, 16).replace("T", " ")} UTC` : "").filter(Boolean).join(" · ")}.`;
}

function weatherPeriod() {
  const today = new Date();
  const iso = (d) => d.toISOString().slice(0, 10);
  switch ($("#wx-period").value) {
    case "1y": { const d = new Date(today); d.setFullYear(d.getFullYear() - 1); return { start: iso(d), step: "week" }; }
    case "all": return { start: null, step: "month" };
    default: { const d = new Date(today); d.setDate(d.getDate() - 60); return { start: iso(d), step: "day" }; }
  }
}

// lngLat null: average over the selected area (each grid cell weighted by its overlap)
function setWeatherLocation(lngLat, refresh = true) {
  wx.lngLat = lngLat;
  const a = areaOf(currentArea);
  $("#wx-location").innerHTML = lngLat
    ? `Location: point ${lngLat.lat.toFixed(4)}, ${lngLat.lng.toFixed(4)} · <a href="#" id="wx-reset">back to the average over ${esc(a.name)}</a>`
    : `Location: average over <b>${esc(a.name)}</b> (grid cells weighted by their overlap). Click the map for a point.`;
  $("#wx-reset")?.addEventListener("click", (e) => {
    e.preventDefault();
    marker.remove();
    setWeatherLocation(null);
  });
  if (refresh) { updateWeather(); updateForecast(); }
}
const wxWhere = () => wx.lngLat ? { lon: wx.lngLat.lng, lat: wx.lngLat.lat } : { area: currentArea };

// The views of the Weather tab: their controls, whether they use the variable, how they update
// and what they leave on the map
const WX_VIEWS = {
  sources: { controls: "#wx-sources-controls", usesVar: true },
  normal: { controls: "#wx-normal-controls", usesVar: true, update: () => updateNormals() },
  radar: { controls: "#wx-radar-controls", update: () => updateRadar(), clear: () => removeRadarImage() },
  obs: { controls: "#wx-obs-controls", update: () => updateObs() },
  maps: { controls: "#wx-maps-controls", update: () => updateMaps(), clear: () => removeAromeImage() },
  ensemble: { update: () => updateEnsemble() },
  sea: { update: () => updateSea() },
};
const wxViewActive = (name) => $("#tab-weather").classList.contains("active") && $("#wx-view").value === name;

async function updateWeather() {
  const view = WX_VIEWS[$("#wx-view").value];
  if (!currentArea || (view.usesVar && !$("#wx-var").value)) return;
  if (view.update) return view.update();
  const seq = ++wx.seq;
  const v = $("#wx-var").value;
  const keys = [...document.querySelectorAll("#wx-series input:checked")].map((i) => i.value);
  const { start, step } = weatherPeriod();
  if (!keys.length) { $("#wx-chart").innerHTML = '<p class="muted">Select at least one source.</p>'; $("#wx-compare").innerHTML = ""; return; }
  $("#wx-chart").innerHTML = '<p class="muted">Loading…</p>';
  const q = new URLSearchParams({ ...wxWhere(), var: v, series: keys.join(","), step });
  if (start) q.set("start", start);
  const d = await api(`/api/weather/daily?${q}`);
  if (seq !== wx.seq) return;  // a newer request superseded this one
  const series = d.series.filter((s) => s.available && s.values?.some((x) => x !== null))
    .map((s) => ({ ...s, ...WX_SERIES.find((w) => w.key === s.key) }));
  const missing = d.series.filter((s) => !series.find((x) => x.key === s.key)).map((s) => WX_SERIES.find((w) => w.key === s.key).label);
  const u = unit(d.variable.units);
  const stepLabel = { day: "daily", week: "weekly", month: "monthly" }[step];
  const howLabel = d.variable.how === "sum" ? `${stepLabel} total` : `${stepLabel} mean`;
  $("#wx-chart").innerHTML = `<div class="muted">${esc(d.variable.long_name)}${u ? ` (${esc(u)})` : ""}, ${howLabel}
      ${d.variable.agera5 ? ` · AgERA5: ${esc(d.variable.agera5)}` : ""}</div>` +
    (series.length ? weatherChart(series, step, u) : '<p class="muted">No data for this variable.</p>') +
    (missing.length ? `<div class="muted" style="font-size:11px">Not provided by: ${missing.map(esc).join(", ")}.</div>` : "");
  bindWeatherHover(series, u);
  $("#wx-compare").innerHTML = weatherCompare(series, d.variable, u);
}

// Round tick values from lo to at least hi
function niceTicks(lo, hi, n = 5) {
  if (lo === hi) { lo -= 1; hi += 1; }
  const raw = (hi - lo) / n, mag = 10 ** Math.floor(Math.log10(raw));
  const stepv = [1, 2, 2.5, 5, 10].map((f) => f * mag).find((s) => s >= raw);
  const t0 = Math.floor(lo / stepv) * stepv, ticks = [];
  for (let t = t0; t <= hi + stepv * 0.5 || ticks[ticks.length - 1] < hi; t += stepv) ticks.push(Number(t.toFixed(10)));
  return ticks;
}

// SVG pieces shared by the charts. keep(i): whether index i is drawn (all by default).
const svgPath = (vals, sx, sy, keep = () => true) => {
  let s = "", pen = false;
  vals.forEach((v, i) => {
    if (v === null || !keep(i)) { pen = false; return; }
    s += `${pen ? "L" : "M"}${sx(i).toFixed(1)},${sy(v).toFixed(1)}`; pen = true;
  });
  return s;
};
// One polygon per run of indices with both bounds
const svgBand = (lo, hi, sx, sy, color, op, keep = () => true) => {
  let out = "", run = [];
  const flush = () => {
    if (run.length > 1) out += `<polygon points="${run.map((i) => `${sx(i).toFixed(1)},${sy(hi[i]).toFixed(1)}`).join(" ")} ${run.slice().reverse().map((i) => `${sx(i).toFixed(1)},${sy(lo[i]).toFixed(1)}`).join(" ")}" fill="${color}" fill-opacity="${op}"/>`;
    run = [];
  };
  lo.forEach((v, i) => { if (v !== null && hi[i] !== null && keep(i)) run.push(i); else flush(); });
  flush();
  return out;
};
// Horizontal grid lines with their labels (decimals from the range unless given)
const yGrid = (ticks, sy, M, W, digits) => {
  const span = Math.abs(ticks[ticks.length - 1] - ticks[0]);
  const dg = digits ?? (span < 2 ? 2 : span < 20 ? 1 : 0);
  return ticks.map((v) => `<line x1="${M.l}" x2="${W - M.r}" y1="${sy(v)}" y2="${sy(v)}" stroke="#e6e6e3"/>
    <text x="${M.l - 4}" y="${sy(v) + 3}" text-anchor="end">${fmt(v, dg)}</text>`).join("");
};
// Tooltip beside the crosshair at x (viewBox units), kept inside the chart
function placeTip(tip, svg, x, W) {
  tip.hidden = false;
  const box = svg.getBoundingClientRect(), left = x * (box.width / W);
  const want = left + (left > box.width / 2 ? -tip.offsetWidth - 10 : 10);
  tip.style.left = `${Math.max(0, Math.min(want, box.width - tip.offsetWidth))}px`;
  tip.style.top = "18px";
}
// x of a pointer event in viewBox units
const svgX = (svg, e) => { const pt = svg.createSVGPoint(); pt.x = e.clientX; pt.y = e.clientY; return pt.matrixTransform(svg.getScreenCTM().inverse()).x; };
// Hides the crosshair, dots and tooltip when the pointer leaves the chart
const hideCrosshair = (cross, dots, tip) => { cross.setAttribute("visibility", "hidden"); dots.innerHTML = ""; tip.hidden = true; };
const monthName = (iso) => MONTHS[Number(iso.slice(5, 7)) - 1];

const WX_W = 400, WX_H = 210, WX_M = { l: 40, r: 10, t: 14, b: 22 };

function weatherChart(series, step) {
  const dates = [...new Set(series.flatMap((s) => s.dates))].sort();
  const t = (s) => Date.parse(`${s}T00:00:00Z`);
  const x0 = t(dates[0]), x1 = t(dates[dates.length - 1]) || x0 + 1;
  const vals = series.flatMap((s) => s.values.filter((v) => v !== null));
  let lo = Math.min(...vals), hi = Math.max(...vals);
  if (lo > 0 && lo / (hi || 1) < 0.5) lo = 0;  // anchor positive quantities on zero
  const ticks = niceTicks(lo, hi);
  const y0 = ticks[0], y1 = ticks[ticks.length - 1];
  const sx = (d) => WX_M.l + ((t(d) - x0) / (x1 - x0 || 1)) * (WX_W - WX_M.l - WX_M.r);
  const sy = (v) => WX_H - WX_M.b - ((v - y0) / (y1 - y0 || 1)) * (WX_H - WX_M.t - WX_M.b);
  wx.scale = { sx, sy, dates, series };

  // Forecast band (from the first forecast day to the end)
  const ff = series.find((s) => s.forecast_from)?.forecast_from;
  let band = "";
  if (ff && step === "day" && ff <= dates[dates.length - 1]) {
    const xa = sx(ff < dates[0] ? dates[0] : ff);
    band = `<rect x="${xa}" y="${WX_M.t}" width="${WX_W - WX_M.r - xa}" height="${WX_H - WX_M.t - WX_M.b}" fill="#f1f1ee"/>
      <text x="${xa + 4}" y="${WX_M.t + 10}">Forecast</text>`;
  }
  // x labels: about five, on month or day boundaries
  const nLab = Math.min(5, dates.length);
  const xl = Array.from({ length: nLab }, (_, i) => dates[Math.round((i * (dates.length - 1)) / Math.max(1, nLab - 1))]);
  const fmtD = (d) => step === "month" ? d.slice(0, 7) : d.slice(5);
  const paths = series.map((s) => {
    let p = "", pen = false;
    s.dates.forEach((d, i) => {
      const v = s.values[i];
      if (v === null) { pen = false; return; }
      p += `${pen ? "L" : "M"}${sx(d).toFixed(1)},${sy(v).toFixed(1)}`;
      pen = true;
    });
    return `<path d="${p}" fill="none" stroke="${s.color}" stroke-width="2" ${s.dash ? `stroke-dasharray="${s.dash}"` : ""} stroke-linejoin="round" stroke-linecap="round"/>`;
  }).join("");
  const legend = series.length > 1 ? `<div class="wx-legend">${series.map((s) =>
    `<span class="k"><span class="line" style="background:${swatchBg(s)}"></span>${esc(s.label)}
     <span class="muted">${stationsText(s) ? `· ${stationsText(s)}` : s.point ? `· ${fmt(s.point.distance_km, 1)} km` : ""}</span></span>`).join("")}</div>` : "";
  return `${legend}<div class="wx-plot"><svg viewBox="0 0 ${WX_W} ${WX_H}" role="img" aria-label="Weather chart">
    ${band}
    ${yGrid(ticks, sy, WX_M, WX_W)}
    ${xl.map((d, i) => `<text x="${sx(d)}" y="${WX_H - 6}" text-anchor="${i === 0 ? "start" : i === xl.length - 1 ? "end" : "middle"}">${fmtD(d)}</text>`).join("")}
    ${paths}
    <line class="wx-cross" y1="${WX_M.t}" y2="${WX_H - WX_M.b}" stroke="#8a8a86" stroke-width="1" visibility="hidden"/>
    <g class="wx-dots"></g>
    <rect class="wx-hit" x="${WX_M.l}" y="0" width="${WX_W - WX_M.l - WX_M.r}" height="${WX_H}" fill="transparent"/>
  </svg><div class="wx-tip" hidden></div></div>`;
}

function bindWeatherHover(series, u) {
  const svg = $("#wx-chart svg");
  if (!svg) return;
  const { sx, sy, dates } = wx.scale;
  const cross = svg.querySelector(".wx-cross"), dots = svg.querySelector(".wx-dots"), tip = $("#wx-chart .wx-tip");
  const ff = series.find((s) => s.forecast_from)?.forecast_from;
  const hit = svg.querySelector(".wx-hit");
  hit.addEventListener("pointermove", (e) => {
    const px = svgX(svg, e);
    let best = dates[0], bd = Infinity;
    for (const d of dates) { const dd = Math.abs(sx(d) - px); if (dd < bd) { bd = dd; best = d; } }
    const x = sx(best);
    cross.setAttribute("x1", x); cross.setAttribute("x2", x); cross.setAttribute("visibility", "visible");
    const rows = series.map((s) => ({ s, v: s.values[s.dates.indexOf(best)] ?? null }));
    dots.innerHTML = rows.filter((r) => r.v !== null).map((r) =>
      `<circle cx="${x}" cy="${sy(r.v)}" r="4" fill="${r.s.color}" stroke="#fff" stroke-width="2"/>`).join("");
    tip.innerHTML = `<div class="d">${best}${ff && best >= ff && $("#wx-period").value === "60d" ? ' <span class="fc">forecast</span>' : ""}</div>` +
      rows.map((r) => `<div class="r"><span class="swatch" style="background:${swatchBg(r.s)}"></span>${esc(r.s.label)}<b>${r.v === null ? "–" : `${fmt(r.v, 1)} ${esc(u)}`}</b></div>`).join("");
    placeTip(tip, svg, x, WX_W);
  });
  hit.addEventListener("pointerleave", () => hideCrosshair(cross, dots, tip));
}

function weatherCompare(series, variable, u) {
  if (!series.length) return "";
  const ff = series.find((s) => s.forecast_from)?.forecast_from;
  const past = (s) => s.dates.map((d, i) => [d, s.values[i]]).filter(([d, v]) => v !== null && !(ff && d >= ff));
  const ref = series.find((s) => s.key === "sim2");
  const refMap = ref ? new Map(past(ref)) : null;
  const rows = series.map((s) => {
    const pts = past(s);
    const mean = pts.reduce((a, [, v]) => a + v, 0) / (pts.length || 1);
    let diff = "", corr = "";
    if (refMap && s !== ref) {
      const pairs = pts.filter(([d]) => refMap.has(d)).map(([d, v]) => [v, refMap.get(d)]);
      if (pairs.length > 2) {
        const ma = pairs.reduce((a, p) => a + p[0], 0) / pairs.length, mb = pairs.reduce((a, p) => a + p[1], 0) / pairs.length;
        const cov = pairs.reduce((a, p) => a + (p[0] - ma) * (p[1] - mb), 0);
        const va = pairs.reduce((a, p) => a + (p[0] - ma) ** 2, 0), vb = pairs.reduce((a, p) => a + (p[1] - mb) ** 2, 0);
        diff = `${ma - mb >= 0 ? "+" : ""}${fmt(ma - mb, 2)}`;
        corr = va && vb ? fmt(cov / Math.sqrt(va * vb), 2) : "–";
      }
    }
    return `<tr><td><span class="swatch" style="background:${swatchBg(s)}"></span> ${esc(s.label)}</td>
      <td class="num">${pts.length}</td><td class="num">${fmt(mean, 2)}</td><td class="num">${diff || "–"}</td><td class="num">${corr || "–"}</td>
      <td class="num">${stationsText(s) || (s.point ? `${fmt(s.point.distance_km, 1)} km` : s.cells ? `${s.cells.length} cell${s.cells.length > 1 ? "s" : ""}` : "–")}</td></tr>`;
  }).join("");
  return `<table class="kv" style="margin-top:6px"><tr><th>Source (observed period)</th><th class="num">n</th>
    <th class="num">Mean${u ? ` (${esc(u)})` : ""}</th><th class="num" title="Mean difference with SIM2 over common dates">Δ SIM2</th>
    <th class="num" title="Correlation with SIM2 over common dates">r SIM2</th><th class="num" title="Distance to the grid point used, or number of grid cells averaged">Point</th></tr>${rows}</table>
    <div class="muted" style="font-size:11px">Forecast days excluded. Δ and r: against SIM2 on common ${esc(variable.how === "sum" ? "periods" : "dates")}.</div>`;
}

// ---------------------------------------------------------------------------
// Weather against the 1991-2020 normal (WMO reference period), from the same source
// ---------------------------------------------------------------------------
const NORMAL_COLORS = { current: "#2a78d6", normal: "#5f5f5a" };

// "2026", or "2025–26" for a year starting on another day than 1 January
const periodLabel = (year, mmdd) => (mmdd === "01-01" ? String(year) : `${year}–${String(year + 1).slice(2)}`);

function fillNormalYears() {
  const mmdd = $("#wx-nstart").value;
  const now = new Date();
  const today = `${String(now.getMonth() + 1).padStart(2, "0")}-${String(now.getDate()).padStart(2, "0")}`;
  const last = today >= mmdd ? now.getFullYear() : now.getFullYear() - 1;
  const keep = Number($("#wx-nyear").value);
  const opts = [];
  for (let k = last; k >= 1991; k--) opts.push(`<option value="${k}">${periodLabel(k, mmdd)}${k === last ? " (current)" : ""}</option>`);
  $("#wx-nyear").innerHTML = opts.join("");
  if (keep >= 1991 && keep <= last) $("#wx-nyear").value = keep;
}

function setWeatherView() {
  const view = $("#wx-view").value;
  for (const [name, v] of Object.entries(WX_VIEWS)) {
    if (v.controls) $(v.controls).hidden = name !== view;
    if (name !== view) v.clear?.();
  }
  $("#wx-var-row").hidden = !WX_VIEWS[view].usesVar;
  updateWeather();
}

async function updateNormals() {
  const seq = ++wx.seq;
  const start = `${$("#wx-nyear").value}-${$("#wx-nstart").value}`;
  $("#wx-chart").innerHTML = '<p class="muted">Loading…</p>';
  $("#wx-compare").innerHTML = "";
  const q = new URLSearchParams({ ...wxWhere(), var: $("#wx-var").value, series: $("#wx-nsource").value, start,
    forecast: $("#wx-nforecast").checked });
  let d;
  try { d = await api(`/api/weather/normals?${q}`); }
  catch (e) { if (seq === wx.seq) $("#wx-chart").innerHTML = `<p class="muted">${esc(String(e.message || e))}</p>`; return; }
  if (seq !== wx.seq) return;
  if (!d.available) {
    $("#wx-chart").innerHTML = `<p class="muted">No normal for this variable and source: ${esc(d.reason || "")}.</p>`;
    return;
  }
  wx.normal = d;
  renderNormal(d);
  $("#wx-compare").innerHTML = normalSummary(d);
}

const isSum = (d) => d.how === "sum";
const fmtDay = (iso) => `${Number(iso.slice(8, 10))} ${monthName(iso)}`;
const signed = (v, digits = 1) => { const r = Number(v.toFixed(digits)); return `${r > 0 ? "+" : r < 0 ? "−" : "±"}${Math.abs(r).toFixed(digits)}`; };
// The continuation starts with the Météo-France archive (a source behind the forecast start)
const bridged = (fc) => fc?.forecast_day != null && fc.forecast_day > fc.first_day;

// Nested bands of the reference years, outer to inner; the same grey, stacked opacities
const NORMAL_BANDS = [
  { lo: "min", hi: "max", op: 0.08, label: "Min–max" },
  { lo: "p10", hi: "p90", op: 0.12, label: "10th–90th percentile" },
  { lo: "p25", hi: "p75", op: 0.18, label: "Interquartile range (25th–75th)" },
];

// Days shown: the whole year, or the last 90 days of data and what follows them
function normalWindow(d) {
  if ($("#wx-nzoom").value !== "recent" || d.last_day < 0) return [0, d.days - 1];
  const end = d.ahead ? d.ahead.last_day : d.last_day;
  return [Math.max(0, d.last_day - 89), Math.min(d.days - 1, end + 3)];
}

// The chart and its hover, from one set of series and scales
function renderNormal(d) {
  const u = unit(d.variable.units);
  const src = isSum(d) ? d.cumulative : d.daily;
  const fc = d.ahead, ah = fc ? (isSum(d) ? fc.cumulative : fc.daily) : null;
  const [k0, k1] = normalWindow(d);
  const keep = (i) => i >= k0 && i <= k1;
  const all = [src.current, src.min, src.max, src.normal, ah || []].flatMap((a) => a.slice(k0, k1 + 1)).filter((x) => x !== null);
  let lo = Math.min(...all);
  const hi = Math.max(...all);
  if (isSum(d) || (lo > 0 && lo / (hi || 1) < 0.5)) lo = Math.min(0, lo);
  const ticks = niceTicks(lo, hi);
  const y0 = ticks[0], y1 = ticks[ticks.length - 1];
  const sx = (k) => WX_M.l + ((k - k0) / (k1 - k0 || 1)) * (WX_W - WX_M.l - WX_M.r);
  const sy = (val) => WX_H - WX_M.b - ((val - y0) / (y1 - y0 || 1)) * (WX_H - WX_M.t - WX_M.b);
  const path = (vals) => svgPath(vals, sx, sy, keep);
  const months = d.dates.map((ds, k) => [k, ds]).filter(([k, ds]) => ds.endsWith("-01") && keep(k));
  const zoomed = k1 - k0 < 200;
  const ref = d.reference, last = d.last_day;
  const shade = fc?.forecast_day != null
    ? `<rect x="${sx(fc.forecast_day)}" y="${WX_M.t}" width="${Math.max(1, sx(fc.last_day) - sx(fc.forecast_day))}" height="${WX_H - WX_M.t - WX_M.b}" fill="#f1f1ee"/>` : "";
  $("#wx-chart").innerHTML = `<div class="muted">${esc(d.variable.long_name)}${u ? ` (${esc(u)})` : ""}, ${isSum(d) ? `total since ${fmtDay(d.start)}` : "daily value"} · ${esc(d.series.label)}
      ${d.area ? `over ${esc(d.area.name)}` : d.point ? `at ${fmt(d.point.distance_km, 1)} km` : ""}</div>
    <div class="wx-legend">
      <span class="k"><span class="line" style="background:${NORMAL_COLORS.current};height:3px"></span>${periodLabel(Number(d.start.slice(0, 4)), d.start.slice(5))}</span>
      ${fc ? `<span class="k"><span class="line" style="background:repeating-linear-gradient(90deg,${NORMAL_COLORS.current} 0 3px,transparent 3px 5px)"></span>${bridged(fc) ? "Météo-France archive, then forecast" : "Forecast"} (shaded)</span>` : ""}
      <span class="k"><span class="line" style="background:${NORMAL_COLORS.normal}"></span>Normal ${ref.first}–${ref.last} (mean)</span>
      ${NORMAL_BANDS.map((b, i) => `<span class="k"><span class="line" style="background:${NORMAL_COLORS.normal};opacity:${NORMAL_BANDS.slice(0, i + 1).reduce((a, x) => a + x.op, 0).toFixed(2)};height:8px"></span>${b.label}</span>`).join("")}
    </div>
    <div class="wx-plot"><svg viewBox="0 0 ${WX_W} ${WX_H}" role="img" aria-label="${esc(d.variable.long_name)} against the ${ref.first}–${ref.last} normal">
    ${shade}
    ${yGrid(ticks, sy, WX_M, WX_W)}
    ${months.filter((_, i) => zoomed || i % 2 === 0).map(([k, ds]) => `<line x1="${sx(k)}" x2="${sx(k)}" y1="${WX_H - WX_M.b}" y2="${WX_H - WX_M.b + 3}" stroke="#b8b8b4"/><text x="${sx(k)}" y="${WX_H - 6}" text-anchor="middle">${zoomed ? "1 " : ""}${monthName(ds)}</text>`).join("")}
    ${NORMAL_BANDS.map((b) => svgBand(src[b.lo], src[b.hi], sx, sy, NORMAL_COLORS.normal, b.op, keep)).join("")}
    <path d="${path(src.normal)}" fill="none" stroke="${NORMAL_COLORS.normal}" stroke-width="2" stroke-dasharray="5 3" stroke-linejoin="round"/>
    <path d="${path(src.current)}" fill="none" stroke="${NORMAL_COLORS.current}" stroke-width="2" stroke-linejoin="round" stroke-linecap="round"/>
    ${ah ? `<path d="${path(ah)}" fill="none" stroke="${NORMAL_COLORS.current}" stroke-width="2" stroke-dasharray="3 2" stroke-linejoin="round"/>` : ""}
    ${last >= 0 && keep(last) ? `<circle cx="${sx(last)}" cy="${sy(src.current[last])}" r="4" fill="${NORMAL_COLORS.current}" stroke="#fff" stroke-width="2"/>` : ""}
    <line class="wx-cross" y1="${WX_M.t}" y2="${WX_H - WX_M.b}" stroke="#8a8a86" visibility="hidden"/>
    <g class="wx-dots"></g>
    <rect class="wx-hit" x="${WX_M.l}" y="0" width="${WX_W - WX_M.l - WX_M.r}" height="${WX_H}" fill="transparent"/>
  </svg><div class="wx-tip" hidden></div></div>`;

  // Hover: values of the day under the pointer
  const svg = $("#wx-chart svg");
  const cross = svg.querySelector(".wx-cross"), dots = svg.querySelector(".wx-dots"), tip = $("#wx-chart .wx-tip");
  const hit = svg.querySelector(".wx-hit");
  const row = (label, color, val, extra = "") => `<div class="r">${color ? `<span class="swatch" style="background:${color}"></span>` : ""}${label}<b>${val == null ? "–" : `${fmt(val, 1)} ${esc(u)}`}${extra}</b></div>`;
  const range = (label, a, b) => `<div class="r">${label}<b>${a == null ? "–" : `${fmt(a, 1)} – ${fmt(b, 1)}`}</b></div>`;
  hit.addEventListener("pointermove", (e) => {
    const k = Math.max(k0, Math.min(k1, k0 + Math.round(((svgX(svg, e) - WX_M.l) / (WX_W - WX_M.l - WX_M.r)) * (k1 - k0))));
    const x = sx(k), nm = src.normal[k];
    const inAhead = ah && k > last && ah[k] !== null;
    const isFc = inAhead && fc.forecast_day != null && k >= fc.forecast_day;
    const c = inAhead ? ah[k] : src.current[k];
    const kind = !inAhead ? (isSum(d) ? "Total" : "Observed")
      : isFc ? (isSum(d) ? "Total, forecast" : "Forecast") : (isSum(d) ? "Total, MF archive" : "Météo-France archive");
    cross.setAttribute("x1", x); cross.setAttribute("x2", x); cross.setAttribute("visibility", "visible");
    dots.innerHTML = [[c, NORMAL_COLORS.current], [nm, NORMAL_COLORS.normal]].filter(([val]) => val !== null)
      .map(([val, col]) => `<circle cx="${x}" cy="${sy(val)}" r="4" fill="${col}" stroke="#fff" stroke-width="2"/>`).join("");
    tip.innerHTML = `<div class="d">${d.dates[k]}${isFc ? ' <span class="fc">forecast</span>' : ""}</div>` +
      row(kind, NORMAL_COLORS.current, c, c !== null && nm !== null ? ` (${signed(c - nm)})` : "") +
      row("Normal", NORMAL_COLORS.normal, nm) +
      range("25th–75th", src.p25[k], src.p75[k]) + range("10th–90th", src.p10[k], src.p90[k]) + range("Min–max", src.min[k], src.max[k]) +
      (isSum(d) ? row("This day", null, inAhead ? fc.daily[k] : d.daily.current[k], d.daily.normal[k] !== null ? ` · normal ${fmt(d.daily.normal[k], 1)}` : "") : "");
    placeTip(tip, svg, x, WX_W);
  });
  hit.addEventListener("pointerleave", () => hideCrosshair(cross, dots, tip));
}

function normalSummary(d) {
  if (!d.summary.length) return '<p class="muted">No observation in this period yet.</p>';
  const u = unit(d.variable.units), sum = isSum(d), fc = d.ahead;
  const name = { "7d": "Last 7 days", "30d": "Last 30 days", start: `Since ${fmtDay(d.start)}`,
    ahead: bridged(fc) ? "After the last data" : "Next days (forecast)", start_ahead: `Since ${fmtDay(d.start)} + forecast` };
  const small = (html) => `<div class="muted" style="font-size:11px">${html}</div>`;
  const rows = d.summary.map((r) => {
    const pos = r.current > r.p90 ? " ▲" : r.current < r.p10 ? " ▼" : "";
    const rec = r.current > r.max ? "highest" : r.current < r.min ? "lowest" : "";
    return `<tr${r.period.endsWith("ahead") ? ' style="font-style:italic"' : ""}><td>${name[r.period]}<div class="muted" style="font-size:11px;white-space:nowrap">${fmtDay(d.dates[r.from_day])}–${fmtDay(d.dates[r.to_day])}</div></td>
      <td class="num"><b>${fmt(r.current, 1)}</b></td><td class="num">${fmt(r.normal, 1)}</td>
      <td class="num">${signed(r.diff)}${sum && r.ratio !== undefined ? small(`${fmt(100 * r.ratio)} %`) : ""}</td>
      <td class="num">${fmt(r.p25, 1)} – ${fmt(r.p75, 1)}${small(`${fmt(r.p10, 1)} – ${fmt(r.p90, 1)}${pos}`)}
        ${small(rec ? `<b>${rec}</b> of ${r.years + 1}` : `rank ${r.below}/${r.years}`)}</td></tr>`;
  }).join("");
  const days = !sum && d.days_compared ? `<br>Since ${fmtDay(d.start)}: ${d.days_above_p90} days above the 90th percentile of their date,
    ${d.days_below_p10} below the 10th (about ${fmt(d.days_compared / 10)} each in a normal year).` : "";
  const fcNote = fc ? ` Italic rows include ${bridged(fc) ? `the Météo-France archive from ${fmtDay(d.dates[fc.first_day])}, then ` : ""}the forecast (run ${esc((fc.run || "").replace("T", " ").slice(0, 16))} UTC, AROME/ARPEGE then ECMWF IFS) to ${fmtDay(d.dates[fc.last_day])}:
    another model than ${esc(d.series.label)}, with its own bias.` : "";
  return `<div class="table-wrap"><table class="kv" style="margin-top:6px"><tr><th></th><th class="num">${sum ? "Total" : "Mean"}${u ? ` (${esc(u)})` : ""}</th>
    <th class="num">Normal</th><th class="num">Diff.</th>
    <th class="num" title="Interquartile range (25th–75th percentile) of the reference years over the same days; below it, the 10th–90th percentile and the rank">IQR<div style="font-weight:400">10–90 %</div></th></tr>${rows}</table></div>
    <div class="muted" style="font-size:11px">Normal: ${esc(d.series.label)} over the same days of ${d.reference.years.length} reference years
    (${d.reference.first}–${d.reference.last}, WMO reference period), from the same source as the observations so that differences between products cancel out.
    Diff.: difference with the normal${sum ? " (and share of it)" : ""}. IQR: 25th–75th percentile of those years; below it the 10th–90th (▲ above, ▼ below) and the rank: reference years with a lower value, or a record. Latest data: ${d.last}.${sum && d.missing_days ? ` ${d.missing_days} missing days counted as zero.` : ""}${fcNote}${days}</div>`;
}

async function updateForecast() {
  if (!currentArea) return;
  let f;
  try { f = await api(`/api/weather/forecast?${new URLSearchParams(wxWhere())}`); }
  catch { $("#wx-forecast").innerHTML = '<p class="muted">No forecast loaded yet (ingest step "forecast").</p>'; return; }
  const mfUntil = (f.models.meteofrance_seamless || "").replace("until ", "").slice(0, 10);
  $("#wx-forecast").innerHTML = `<div class="meta muted" style="font-size:11px">Run ${esc(f.run.replace("T", " "))} UTC ·
      Météo-France until ${esc(mfUntil)}, ECMWF IFS after · ${f.area ? `average of ${f.point.cells} cells over ${esc(f.area.name)}` : `point at ${fmt(f.point.distance_km, 1)} km`}, ${fmt(f.point.elevation)} m</div>
    <div class="table-wrap"><table class="kv"><tr><th>Day</th><th class="num">Tmin</th><th class="num">Tmax</th><th class="num">Rain</th>
      <th class="num">ET0</th><th class="num">Radiation</th><th class="num">Wind</th></tr>
      <tr class="muted"><th></th><th class="num">°C</th><th class="num">°C</th><th class="num">mm</th><th class="num">mm</th><th class="num">MJ/m²</th><th class="num">m/s</th></tr>
      ${f.days.map((d) => `<tr${d.date > mfUntil ? ' class="muted"' : ""}><td>${new Date(`${d.date}T12:00:00`).toLocaleDateString(undefined, { weekday: "short", day: "numeric", month: "short" })}</td>
        <td class="num">${fmt(d.t_min, 1)}</td><td class="num">${fmt(d.t_max, 1)}</td><td class="num">${fmt(d.precip, 1)}</td>
        <td class="num">${fmt(d.et0, 1)}</td><td class="num">${fmt(d.radiation, 1)}</td><td class="num">${fmt(d.wind_mean, 1)}</td></tr>`).join("")}
    </table></div><div class="muted" style="font-size:11px">Greyed days: ECMWF IFS (9 km).</div>`;
}

async function updateWeatherCells() {
  for (const s of WX_SERIES) {
    for (const suffix of ["fill", "line"]) if (map.getLayer(`wx-${s.key}-${suffix}`)) map.removeLayer(`wx-${s.key}-${suffix}`);
    if (map.getSource(`wx-${s.key}`)) map.removeSource(`wx-${s.key}`);
  }
  if (!$("#wx-cells").checked) return;
  const keys = [...document.querySelectorAll("#wx-series input:checked")].map((i) => i.value);
  for (const s of WX_SERIES.filter((x) => keys.includes(x.key))) {
    map.addSource(`wx-${s.key}`, { type: "geojson", data: `/api/weather/cells.geojson?product=${s.product}` });
    map.addLayer({ id: `wx-${s.key}-fill`, type: "fill", source: `wx-${s.key}`, paint: { "fill-color": s.color, "fill-opacity": 0.06 } });
    map.addLayer({ id: `wx-${s.key}-line`, type: "line", source: `wx-${s.key}`,
      paint: { "line-color": s.color, "line-width": 1.5, ...(s.dash ? { "line-dasharray": s.dash.split(" ").map(Number) } : {}) } });
  }
}

// ---------------------------------------------------------------------------
// Sentinel-2 tab: index time series over the selected area, index image per date
// ---------------------------------------------------------------------------
const S2_COLOR = "#2a78d6";      // single series: categorical slot 1
const s2 = { info: null, points: [], date: null, seq: 0 };
const S2_W = 380, S2_H = 190, S2_M = { l: 40, r: 10, t: 10, b: 22 };

async function initS2() {
  [s2.info, s2.modis] = await Promise.all([api("/api/s2/info"), api("/api/modis/info").catch(() => null)]);
  if (!s2.info.available) {
    $("#s2-status").innerHTML = `No Sentinel-2 data yet. ${esc(s2.info.hint || "")}`;
    return;
  }
  const i = s2.info;
  $("#s2-index").innerHTML = i.indices.map((x) => `<option value="${x.name}">${esc(x.long_name)}</option>`).join("");
  setS2Years(i.years);
  const missing = i.missing_indices.map((x) => `${x.name.toUpperCase()} (add ${x.bands.filter((b) => !i.bands.some((h) => h.name === b)).join(", ")})`);
  s2.sources = `Sentinel-2 L2A (Sen2Cor), ${esc(i.collection)}, openEO Copernicus Data Space · ${fmt(i.resolution_m)} m, ${esc(i.crs)} ·
    ${i.dates} dates ${esc(i.first)} → ${esc(i.last)} · bands ${i.bands.map((b) => b.name).join(", ")} + SCL ·
    scenes with tile cloud cover ≤ ${fmt(i.max_cloud_cover)} %. Clear pixels: SCL ${Object.values(i.clear_classes).map(esc).join(", ").toLowerCase()}, with red reflectance below ${fmt(i.max_clear_red, 2)} (removes fog and low cloud classified as bare soil).
    ${missing.length ? `Other indices: ${missing.map(esc).join("; ")} to GEOMON_S2_BANDS.` : ""}`;
  $("#s2-sources").innerHTML = s2.sources;
  const m = s2.modis;
  if (m?.available) {
    s2.modisSources = `MODIS 13Q1 collection 6.1, 16-day NDVI composites of Terra (MOD13Q1, ${fmt(m.platforms.terra || 0)}) and Aqua
      (MYD13Q1, ${fmt(m.platforms.aqua || 0)}), 8 days apart, Microsoft Planetary Computer (${esc(m.collection)}) · ${fmt(m.resolution_m)} m,
      sinusoidal grid · ${m.dates} composites ${esc(m.first)} → ${esc(m.last)}, each dated by its first day.
      Reliable cells: pixel reliability good or marginal (not snow, ice or cloud). Areas smaller than a cell take the cell under them.`;
  } else {
    $("#s2-source").querySelector('option[value="modis"]').disabled = true;
  }
  for (const id of ["#s2-index", "#s2-year", "#s2-clear", "#s2-source"]) $(id).addEventListener("change", updateS2);
  $("#s2-view").addEventListener("change", updateS2);
  $("#s2-season").addEventListener("change", () => { s2.season = Number($("#s2-season").value); updateS2(); });
  $("#s2-group").addEventListener("change", () => { s2.group = $("#s2-group").value; updateS2(); });
  $("#s2-refs").addEventListener("change", () => s2.seasonData && renderSeason());
  $("#s2-weight").addEventListener("change", updateS2);
  $("#s2-refarea").addEventListener("change", updateS2);
  $("#s2-date").addEventListener("change", (e) => showS2Image(e.target.value));
  $("#s2-mask").addEventListener("change", () => showS2Image(s2.date));
  $("#s2-opacity").addEventListener("input", (e) => {
    for (const id of s2.imageIds || []) if (map.getLayer(id)) map.setPaintProperty(id, "raster-opacity", Number(e.target.value));
  });
}

function setS2Years(years) {
  const keep = $("#s2-year").value;
  $("#s2-year").innerHTML = `<option value="">All years (${years[0]}–${years[years.length - 1]})</option>` +
    [...years].reverse().map((y) => `<option value="${y}">${y}</option>`).join("");
  if (years.includes(Number(keep))) $("#s2-year").value = keep;
}

async function updateS2() {
  if (!s2.info?.available || !currentArea) return;
  const modis = $("#s2-source").value === "modis";
  // MODIS cells (5.4 ha) are larger than most parcels: no parcel view
  $("#s2-view").querySelector('option[value="parcel"]').disabled = modis;
  if (modis && $("#s2-view").value === "parcel") $("#s2-view").value = "area";
  const view = $("#s2-view").value;
  if (modis !== (s2.source === "modis")) {          // switching source: its years, mask, weights and sources
    s2.source = modis ? "modis" : "s2";
    setS2Years(modis ? s2.modis.years : s2.info.years);
    $("#s2-mask-label").textContent = modis ? "Hide unreliable cells (snow, ice, cloud)" : "Hide clouds, shadows and snow (SCL)";
    $("#s2-weight-area").textContent = modis ? "By the group's share of each cell" : "By area";
    $("#s2-weight-unit").textContent = modis ? "Each cell equally" : "One parcel, one vote";
    $("#s2-sources").innerHTML = modis ? s2.modisSources : s2.sources;
    s2.date = null;
    s2.seasonData = null;
  }
  $("#s2-index-row").hidden = modis;
  $("#s2-area-controls").hidden = view !== "area";
  $("#s2-season-controls").hidden = view === "area";
  $("#s2-group-row").hidden = view !== "crop";
  $("#s2-own-row").hidden = view !== "parcel";
  $("#s2-compare").innerHTML = "";
  $("#s2-clms").innerHTML = "";
  $("#s2-history").innerHTML = "";
  if (view !== "parcel") removeS2Parcel();
  if (view === "crop") return updateS2Crop();
  if (view === "parcel") return updateS2Parcel();
  const seq = ++s2.seq;
  const index = modis ? "ndvi" : $("#s2-index").value, year = $("#s2-year").value, minClear = $("#s2-clear").value;
  const a = areaOf(currentArea), area = encodeURIComponent(currentArea);
  $("#s2-status").innerHTML = modis
    ? `MODIS NDVI over <b>${esc(a.name)}</b>, reliable 250 m cells only. Click a point to show its composite.`
    : `Mean over <b>${esc(a.name)}</b>, clear pixels only. Click a point to show its image.`;
  const d = await api(modis ? `/api/modis/series?area=${area}&min_clear=${minClear}`
    : `/api/s2/series?index=${index}&area=${area}&min_clear=${minClear}`);
  if (seq !== s2.seq) return;
  s2.points = d.points.filter((p) => p.mean !== null && (!year || p.date.startsWith(year)));
  const ix = modis ? { long_name: "MODIS NDVI (Terra and Aqua 16-day composites)", vmin: s2.modis.vmin } : null;
  if (modis && d.n_area !== null) $("#s2-status").innerHTML += ` ${fmt(d.n_area)} cells.`;
  $("#s2-chart").innerHTML = s2.points.length ? s2Chart(index, ix) : '<p class="muted">No date with enough clear sky.</p>';
  bindS2Hover();
  $("#s2-date").innerHTML = [...s2.points].reverse().map((p) =>
    `<option value="${p.date}">${p.date}${p.platform ? ` · ${p.platform === "aqua" ? "Aqua" : "Terra"}` : ""} · ${fmt(100 * p.clear_fraction)} % clear · ${fmt(p.mean, 2)}</option>`).join("");
  const keep = s2.points.find((p) => p.date === s2.date);
  showS2Image(keep ? s2.date : s2.points[s2.points.length - 1]?.date);
}

// Running median of a field over +/- half a window (days), at each date
function runningMedian(pts, key, days) {
  const t = pts.map((p) => Date.parse(`${p.date}T00:00:00Z`));
  const h = (days / 2) * 86400000;
  let lo = 0, hi = 0;
  return pts.map((p, i) => {
    while (t[lo] < t[i] - h) lo++;
    while (hi < pts.length && t[hi] <= t[i] + h) hi++;
    const v = pts.slice(lo, hi).map((q) => q[key]).filter((x) => x !== null).sort((x, y) => x - y);
    return v.length ? (v.length % 2 ? v[(v.length - 1) / 2] : (v[v.length / 2 - 1] + v[v.length / 2]) / 2) : null;
  });
}

function s2Chart(index, ixOverride = null) {
  const ix = ixOverride || s2.info.indices.find((x) => x.name === index);
  const pts = s2.points;
  const t = (d) => Date.parse(`${d}T00:00:00Z`);
  const x0 = t(pts[0].date), x1 = Math.max(t(pts[pts.length - 1].date), x0 + 86400000);
  const years = (x1 - x0) / (365 * 86400000);
  // Over several years: a 30-day running median (line and band); one year: every date
  const smooth = years > 1.5;
  const mid = smooth ? runningMedian(pts, "mean", 30) : pts.map((p) => p.mean);
  const p10 = smooth ? runningMedian(pts, "p10", 30) : pts.map((p) => p.p10);
  const p90 = smooth ? runningMedian(pts, "p90", 30) : pts.map((p) => p.p90);
  const lo = Math.min(...p10.filter((v) => v !== null), ...mid), hi = Math.max(...p90.filter((v) => v !== null), ...mid);
  const ticks = niceTicks(Math.min(lo, ix.vmin < 0 ? lo : 0), hi);
  const y0 = ticks[0], y1 = ticks[ticks.length - 1];
  const sx = (d) => S2_M.l + ((t(d) - x0) / (x1 - x0)) * (S2_W - S2_M.l - S2_M.r);
  const sy = (v) => S2_H - S2_M.b - ((v - y0) / (y1 - y0 || 1)) * (S2_H - S2_M.t - S2_M.b);
  s2.scale = { sx, sy };
  // Segments: broken where two dates are more than 45 days apart
  const segs = [];
  pts.forEach((p, i) => {
    if (!i || t(p.date) - t(pts[i - 1].date) > 45 * 86400000) segs.push([]);
    segs[segs.length - 1].push(i);
  });
  const line = segs.map((sg) => sg.map((i, k) => `${k ? "L" : "M"}${sx(pts[i].date).toFixed(1)},${sy(mid[i]).toFixed(1)}`).join("")).join("");
  const band = segs.map((sg) => {
    const ok = sg.filter((i) => p10[i] !== null && p90[i] !== null);
    if (ok.length < 2) return "";
    const up = ok.map((i) => `${sx(pts[i].date).toFixed(1)},${sy(p90[i]).toFixed(1)}`);
    const dn = ok.slice().reverse().map((i) => `${sx(pts[i].date).toFixed(1)},${sy(p10[i]).toFixed(1)}`);
    return `<polygon points="${up.join(" ")} ${dn.join(" ")}" fill="${S2_COLOR}" fill-opacity="0.12"/>`;
  }).join("");
  const nLab = 5;
  const xl = Array.from({ length: nLab }, (_, i) => x0 + (i * (x1 - x0)) / (nLab - 1));
  const fmtX = (ms) => { const d = new Date(ms).toISOString(); return years > 2 ? d.slice(0, 4) : d.slice(0, 7); };
  const dots = smooth ? "" : pts.map((p) => `<circle cx="${sx(p.date).toFixed(1)}" cy="${sy(p.mean).toFixed(1)}" r="4" fill="${S2_COLOR}" stroke="#fff" stroke-width="2"/>`).join("");
  const caption = smooth
    ? "30-day running median of the area mean (line) and of its pixels' 10th–90th percentiles (band)"
    : "mean over the area at each date (dots), 10th–90th percentile of its pixels (band)";
  return `<div class="muted">${esc(ix.long_name)}: ${caption} · ${pts.length} dates</div>
    <div class="wx-plot"><svg viewBox="0 0 ${S2_W} ${S2_H}" role="img" aria-label="Sentinel-2 index time series">
    ${ticks.map((v) => `<line x1="${S2_M.l}" x2="${S2_W - S2_M.r}" y1="${sy(v)}" y2="${sy(v)}" stroke="#e6e6e3"/>
      <text x="${S2_M.l - 4}" y="${sy(v) + 3}" text-anchor="end">${fmt(v, 2)}</text>`).join("")}
    ${xl.map((ms, i) => `<text x="${S2_M.l + (i * (S2_W - S2_M.l - S2_M.r)) / (nLab - 1)}" y="${S2_H - 6}" text-anchor="${i === 0 ? "start" : i === nLab - 1 ? "end" : "middle"}">${fmtX(ms)}</text>`).join("")}
    ${band}
    <path d="${line}" fill="none" stroke="${S2_COLOR}" stroke-width="2" stroke-linejoin="round" stroke-linecap="round"/>
    ${dots}
    <line class="wx-cross" y1="${S2_M.t}" y2="${S2_H - S2_M.b}" stroke="#8a8a86" visibility="hidden"/>
    <circle class="s2-sel" r="5" fill="none" stroke="#1a1a19" stroke-width="2" visibility="hidden"/>
    <g class="wx-dots"></g>
    <rect class="wx-hit" x="${S2_M.l}" y="0" width="${S2_W - S2_M.l - S2_M.r}" height="${S2_H}" fill="transparent" style="cursor:pointer"/>
  </svg><div class="wx-tip" hidden></div></div>`;
}

function nearestS2(e, svg) {
  const pt = svg.createSVGPoint(); pt.x = e.clientX; pt.y = e.clientY;
  const x = pt.matrixTransform(svg.getScreenCTM().inverse()).x;
  let best = null, bd = Infinity;
  for (const p of s2.points) { const d = Math.abs(s2.scale.sx(p.date) - x); if (d < bd) { bd = d; best = p; } }
  return best;
}

function bindS2Hover() {
  const svg = $("#s2-chart .wx-plot svg");
  if (!svg) return;
  const cross = svg.querySelector(".wx-cross"), dots = svg.querySelector(".wx-dots"), tip = $("#s2-chart .wx-tip");
  const hit = svg.querySelector(".wx-hit");
  hit.addEventListener("pointermove", (e) => {
    const p = nearestS2(e, svg);
    if (!p) return;
    const x = s2.scale.sx(p.date);
    cross.setAttribute("x1", x); cross.setAttribute("x2", x); cross.setAttribute("visibility", "visible");
    dots.innerHTML = `<circle cx="${x}" cy="${s2.scale.sy(p.mean)}" r="5" fill="${S2_COLOR}" stroke="#fff" stroke-width="2"/>`;
    tip.hidden = false;
    tip.innerHTML = `<b>${p.date}</b>${p.platform ? ` · ${p.platform === "aqua" ? "Aqua" : "Terra"}, 16 days` : ""}<br>mean ${fmt(p.mean, 3)} · median ${fmt(p.median, 3)}<br>p10–p90 ${fmt(p.p10, 2)}–${fmt(p.p90, 2)}<br>${fmt(100 * p.clear_fraction)} % of the area clear`;
    const left = (x / S2_W) * svg.clientWidth;
    tip.style.left = `${Math.min(left + 10, svg.clientWidth - 170)}px`;
    tip.style.top = "8px";
  });
  hit.addEventListener("pointerleave", () => { cross.setAttribute("visibility", "hidden"); dots.innerHTML = ""; tip.hidden = true; });
  hit.addEventListener("click", (e) => { const p = nearestS2(e, svg); if (p) showS2Image(p.date); });
  markS2Selection();
}

function markS2Selection() {
  const sel = $("#s2-chart .s2-sel");
  const p = s2.points.find((x) => x.date === s2.date);
  if (!sel) return;
  if (!p) { sel.setAttribute("visibility", "hidden"); return; }
  sel.setAttribute("cx", s2.scale.sx(p.date)); sel.setAttribute("cy", s2.scale.sy(p.mean));
  sel.setAttribute("visibility", "visible");
}

function removeS2Image() {
  for (const id of s2.imageIds || []) {
    if (map.getLayer(id)) map.removeLayer(id);
    if (map.getSource(id)) map.removeSource(id);
  }
  s2.imageIds = [];
}

async function showS2Image(date) {
  removeS2Image();
  s2.date = date;
  $("#s2-legend").innerHTML = "";
  if (!date || !$("#tab-s2").classList.contains("active")) return;
  $("#s2-date").value = date;
  markS2Selection();
  const modis = s2.source === "modis";
  const index = modis ? "ndvi" : $("#s2-index").value, masked = $("#s2-mask").checked;
  const q = `date=${date}&masked=${masked}`;
  const info = await api(modis ? `/api/modis/ndvi/info?${q}` : `/api/s2/${index}/info?${q}`);
  if (s2.date !== date) return;
  removeS2Image();
  // One image per part of the site (clusters of communes far apart have their own grid)
  (info.images || [{ url: `/api/s2/${index}.png?${q}`, coordinates: info.coordinates }]).forEach((im, k) => {
    const id = `s2-image-${k}`;
    map.addSource(id, { type: "image", url: im.url, coordinates: im.coordinates });
    map.addLayer({ id, type: "raster", source: id,
      paint: { "raster-opacity": Number($("#s2-opacity").value), "raster-resampling": "nearest" } }, firstVectorLayer);
    s2.imageIds.push(id);
  });
  const stops = info.legend.map((l, i) => `${l.color} ${(100 * i) / (info.legend.length - 1)}%`).join(",");
  const title = modis ? `MODIS NDVI, ${info.platform === "aqua" ? "Aqua" : "Terra"} composite from ${date}${masked ? " (unreliable cells hidden)" : ""}`
    : `${index.toUpperCase()}, ${date}${masked ? " (clouds hidden)" : ""}`;
  $("#s2-legend").innerHTML = `<div class="title">${esc(title)}</div>
    <div class="ramp" style="background:linear-gradient(to right,${stops})"></div>
    <div class="ramp-labels"><span>${fmt(info.vmin, 1)}</span><span>${fmt((info.vmin + info.vmax) / 2, 2)}</span><span>${fmt(info.vmax, 1)}</span></div>`;
}

// ---------------------------------------------------------------------------
// Sentinel-2 seasons: crop group or parcel curve against 3-year, 5-year and long-term references
// ---------------------------------------------------------------------------
// Categorical slots 1-4 (validated all-pairs, as for the weather sources); long-term in neutral grey
const SEASON_SERIES = {
  current: { color: "#2a78d6", width: 2.5 },
  "3y": { color: "#eb6834", width: 1.75, label: "3-year mean", short: "3-year" },
  "5y": { color: "#1baf7a", width: 1.75, label: "5-year mean", short: "5-year" },
  lt: { color: "#5f5f5a", width: 1.75, dash: "5 3", label: "Long-term mean", short: "Long-term" },
  own: { color: "#c2407a", width: 1.75, label: "This parcel, earlier seasons", short: "Own past" },
};
const SN_W = 380, SN_H = 200, SN_M = { l: 34, r: 8, t: 8, b: 22 };
const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
const dayDate = (start, day) => new Date(Date.parse(`${start}T00:00:00Z`) + day * 86400000).toISOString().slice(0, 10);

function fillSeasons(seasons, latestRpg) {
  const sel = $("#s2-season");
  if (!s2.season || !seasons.includes(s2.season)) s2.season = Math.max(...seasons.filter((s) => s <= latestRpg));
  sel.innerHTML = [...seasons].reverse().map((s) =>
    `<option value="${s}" ${s === s2.season ? "selected" : ""}>${s} (Sep ${s - 1} – Dec ${s})${s > latestRpg ? " · RPG not yet published" : ""}</option>`).join("");
}

async function updateS2Crop() {
  const seq = ++s2.seq;
  const modis = s2.source === "modis";
  const base = modis ? "/api/modis" : "/api/s2", ixq = modis ? "" : `index=${$("#s2-index").value}&`;
  const unit = modis ? "cells" : "parcels", area = encodeURIComponent(currentArea);
  let c;
  try { c = await api(`${base}/crops?${ixq}area=${area}${s2.season ? `&season=${s2.season}` : ""}`); }
  catch (e) {
    if (seq !== s2.seq) return;
    $("#s2-chart").innerHTML = `<p class="muted">No crop-group curves yet: run <code>docker compose run --rm ingest ${modis ? "modis-crops" : "sentinel2-parcels"}</code>.</p>`;
    return;
  }
  if (seq !== s2.seq) return;
  fillSeasons(c.seasons, c.latest_rpg);
  if (!c.seasons.includes(c.season)) s2.season = c.season;
  if (!s2.group || !c.groups.some((g) => g.code === s2.group)) s2.group = (c.groups.find((g) => g.parcels) || c.groups[0])?.code;
  $("#s2-group").innerHTML = c.groups.map((g) => `<option value="${g.code}" ${g.code === s2.group ? "selected" : ""}>${esc(g.label)} · ${g.parcels ? `${g.parcels} ${unit}, ${fmt(g.ha)} ha${g.status === "assumed" ? " (assumed)" : g.status === "ilots" ? " (RPG îlots)" : g.status === "farm" ? " (your land cover)" : ""}` : `no ${modis ? "pure cell" : "parcel"} this season`}</option>`).join("");
  if (!s2.group) { $("#s2-chart").innerHTML = `<p class="muted">No ${modis ? "pure cell" : "parcel"} in this area.</p>`; return; }
  // The farm's plots: references from the whole group (default) or from the plots' own earlier seasons
  const isFarm = areaOf(currentArea).kind === "farm";
  $("#s2-refarea-row").hidden = !isFarm;
  if (isFarm && !$("#s2-refarea").options.length) {
    $("#s2-refarea").innerHTML = `<option value="${esc(aoiInfo.code)}">The whole group (${esc(aoiInfo.name)})</option>
      <option value="farm">My plots, earlier seasons</option>`;
  }
  const refArea = isFarm ? $("#s2-refarea").value : currentArea;
  const d = await api(`${base}/crop?${ixq}weight=${$("#s2-weight").value}&area=${area}&ref_area=${encodeURIComponent(refArea)}&group=${encodeURIComponent(s2.group)}&season=${s2.season}`);
  if (seq !== s2.seq) return;
  const a = areaOf(currentArea);
  const how = d.smoothing === "whittaker" ? "Whittaker-smoothed" : "interpolated between clear dates";
  const refsFrom = `References: the seasons before ${d.season}${d.ref_area !== d.area ? `, in ${esc(areaOf(d.ref_area)?.name || d.ref_area)}` : ""}`;
  $("#s2-status").innerHTML = modis
    ? `<b>${esc(d.label)}</b> in <b>${esc(a.name)}</b>, MODIS NDVI: mean of the daily curves of the 250 m cells (${fmt(d.cell_ha, 1)} ha) covered at least ${fmt(100 * d.purity)} % by the group,
      ${d.weight === "pixels" ? "weighted by the group's share of each" : "each cell equally"} (${how}; reliable composites, placed at the middle of their 16 days).
      ${refsFrom}${d.refs.lt ? ` (long term since ${d.refs.lt.seasons[0]})` : ""}.
      Crops: RPG parcels since ${Math.min(...c.rpg_years)}${c.ilot_years?.length ? `, RPG îlots ${c.ilot_years[0]}–${c.ilot_years[c.ilot_years.length - 1]} (an îlot counts for its majority group by the share it covers)` : ""}; before, permanent crops assumed.
      ${d.note ? `<br>${esc(d.note)}` : ""}`
    : `<b>${esc(d.label)}</b> in <b>${esc(a.name)}</b>: ${d.weight === "pixels" ? "mean of the parcels' daily curves, weighted by their area" : "mean of the parcels' daily curves, one parcel one vote"}
    (${how}) (each parcel without its 10 m border, clear on at least ${fmt(100 * d.min_clear)} % of it). ${refsFrom}.
    ${d.note ? `<br>${esc(d.note)}` : ""}`;
  s2.seasonData = { kind: "crop", unit, d, current: d.current?.mean, spread: d.current && [d.current.q25, d.current.q75],
    obs: d.observations.map((o) => ({ day: o.day, date: o.date, v: o.mean, used: true, n: o.parcels })),
    refs: d.refs, own: null, title: `${d.season}${d.current ? ` · ${d.current.parcels} ${unit}, ${fmt(d.current.ha)} ha` : ""}` };
  renderSeason();
}

async function updateS2Parcel(lngLat) {
  if (lngLat) s2.parcelAt = lngLat;
  if (!s2.parcelAt) {
    $("#s2-status").innerHTML = "Click a parcel on the map to see its history: its crop and curve for every season since 2015, against its crop group and its own earlier seasons.";
    $("#s2-chart").innerHTML = "";
    return;
  }
  const seq = ++s2.seq;
  const index = $("#s2-index").value, { lng, lat } = s2.parcelAt;
  let d;
  const season = s2.season || new Date().getFullYear();
  try { d = await api(`/api/s2/parcel?index=${index}&lon=${lng}&lat=${lat}&season=${season}`); }
  catch (e) {
    if (seq !== s2.seq) return;
    $("#s2-status").innerHTML = "No RPG parcel with clear observations at this point. Click another parcel.";
    $("#s2-chart").innerHTML = ""; $("#s2-clms").innerHTML = ""; removeS2Parcel(); return;
  }
  if (seq !== s2.seq) return;
  s2.season = d.season;
  const latest = Math.max(...d.history.map((h) => h.rpg_year).filter(Boolean));
  fillSeasons(d.history.filter((h) => h.n_obs).map((h) => h.season), latest);
  const p = d.parcel;
  // Group references: the selected area if the parcel is in it, otherwise the whole site
  const area = (p.areas || []).includes(currentArea) ? currentArea : aoiInfo.code;
  const g = await api(`/api/s2/crop?index=${index}&weight=${$("#s2-weight").value}&area=${encodeURIComponent(area)}&group=${encodeURIComponent(d.reference_group)}&season=${d.season}`);
  if (seq !== s2.seq) return;
  const crop = p.status === "unknown"
    ? `crop not known yet (RPG ${d.season} not published; ${esc(d.history.find((h) => h.season === p.rpg_year)?.crop || "?")} in ${p.rpg_year})`
    : `${esc(p.crop)}${p.status === "assumed" ? ` (assumed from RPG ${p.rpg_year})` : p.status === "farm" ? " (from your land-cover file)" : ""}`;
  const unitLabel = p.unit === "field" ? `Your field ${esc(p.field ?? "")} (land cover ${d.season}, its own outline)`
    : `Parcel ${esc(p.parcel_id)} (RPG ${p.rpg_year})`;
  $("#s2-status").innerHTML = `${unitLabel}, ${fmt(p.surf_ha, 2)} ha, season ${d.season}: <b>${crop}</b>.
    Compared with <b>${esc(d.reference_group_label)}</b> in ${esc(areaOf(area)?.name || area)} (mean of its parcels).`;
  s2.seasonData = { kind: "parcel", d, current: d.curve, spread: null,
    obs: d.observations.map((o) => ({ day: o.day, date: o.date, v: o.mean, used: o.used, clear: o.clear_fraction })),
    refs: g.refs, own: d.own, groupLabel: d.reference_group_label, title: `${d.season} · parcel ${p.parcel_id}` };
  showS2Parcel(d.geometry);
  renderSeason();
  renderHistory(d);
  updateParcelCLMS(d);
}

// ---- Parcel view: soil moisture and dry matter productivity of the parcel (Copernicus, the cells
// under the parcel weighted by its area in each)
const SOIL_VARS = [["ssm", "Surface (Sentinel-1)", "#2a78d6"], ["swi002", "SWI T=2", "#e3b98c"], ["swi010", "SWI T=10", "#c98b55"],
  ["swi020", "SWI T=20", "#a0522d"], ["swi040", "SWI T=40", "#76391b"], ["swi100", "SWI T=100", "#3f1d0c"]];
const SOIL_NAMES = { ssm: "Surface soil moisture (Sentinel-1, top few cm)", swi002: "Soil Water Index T=2 (near the surface)",
  swi010: "Soil Water Index T=10", swi020: "Soil Water Index T=20", swi040: "Soil Water Index T=40", swi100: "Soil Water Index T=100 (root zone)" };
const yearSpan = (ys) => (ys.length ? `${Math.min(...ys)}–${Math.max(...ys)}` : "other years");
const dayMonth = (iso) => (iso ? new Date(`${iso}T00:00:00`).toLocaleDateString(undefined, { day: "numeric", month: "short", year: "2-digit" }) : "–");

async function updateParcelCLMS(d) {
  const seq = s2.seq, p = d.parcel;
  s2.clms = null;
  $("#s2-clms").innerHTML = "";
  let c;
  try { c = await api(`/api/clms/parcel?season=${d.season}&rpg_year=${p.rpg_year}&parcel_id=${encodeURIComponent(p.parcel_id)}`); }
  catch (e) {
    if (seq === s2.seq) $("#s2-clms").innerHTML = `<h2>Soil moisture and productivity</h2><p class="muted">${esc(e.message)}</p>`;
    return;
  }
  if (seq !== s2.seq) return;
  s2.clms = c;
  renderParcelCLMS();
}

function renderParcelCLMS() {
  const c = s2.clms;
  if (!c) return;
  const ref = $("#clms-ref")?.value || "swi040";
  const cellText = (n, size) => `${n} cell${n > 1 ? "s" : ""} of ${size}`;
  let html = "<h2>Soil moisture and productivity</h2>";
  if (c.soil) {
    const s = c.soil, times = s.times.map((t) => `${t}T00:00`);
    const lines = SOIL_VARS.filter(([k]) => s.series[k] && k !== "ssm")
      .map(([k, label, color]) => ({ label, color, values: s.series[k].values }));
    if (s.series.ssm) lines.push({ label: "Surface (Sentinel-1)", color: "#2a78d6", values: s.series.ssm.values, dots: true });
    if (s.series[ref]) lines.push({ label: `${SOIL_VARS.find(([k]) => k === ref)[1]}, mean ${yearSpan(s.stats[ref].reference_years)}`,
      color: "#8a8a86", dash: "4 3", values: s.series[ref].reference });
    html += `<label class="row">Compare with the other years <select id="clms-ref">${SOIL_VARS.filter(([k]) => s.series[k])
        .map(([k, label]) => `<option value="${k}"${k === ref ? " selected" : ""}>${label}</option>`).join("")}</select></label>`
      + timeChart({ times, leftUnit: "%", lines,
        label: `Soil moisture of the parcel, % of saturation (Copernicus, ${cellText(s.cells, "1 km")}), to ${dayMonth(s.last)}` });
  }
  if (c.dmp) {
    const m = c.dmp, times = m.times.map((t) => `${t}T00:00`), ys = yearSpan(m.reference_years);
    html += timeChart({ times, leftUnit: "kg/ha/d", label: `Dry matter productivity of the parcel, kg per hectare and day (Copernicus, ${cellText(m.cells, "300 m")}), to ${dayMonth(m.last)}`,
        lines: [{ label: `${c.season}`, color: "#1baf7a", values: m.values },
                { label: `Mean ${ys}`, color: "#8a8a86", values: m.reference.mean },
                { label: "Lowest / highest", color: "#c9c9c4", dash: "2 3", values: m.reference.min },
                { label: "", color: "#c9c9c4", dash: "2 3", values: m.reference.max }] })
      + timeChart({ times, leftUnit: "t/ha", label: `Dry matter produced since ${dayMonth(c.start)} (t/ha)`,
        lines: [{ label: `${c.season}`, color: "#1baf7a", values: m.cumulative },
                { label: `Mean ${ys}`, color: "#8a8a86", values: m.cumulative_reference }] });
  }
  const row = (name, title, st, unit, digits = 1) => `<tr><td title="${esc(title)}" style="white-space:nowrap">${esc(name)}</td><td class="num"><b>${fmt(st.mean, digits)}</b></td>
    <td class="num">${fmt(st.reference, digits)}</td><td class="num">${st.difference === null ? "–" : `${st.difference > 0 ? "+" : ""}${fmt(st.difference, digits)}`}</td>
    <td class="num">${fmt(st.min, digits)}<br><span class="muted" style="font-size:10px">${dayMonth(st.min_date)}</span></td>
    <td class="num">${fmt(st.max, digits)}<br><span class="muted" style="font-size:10px">${dayMonth(st.max_date)}</span></td><td class="muted">${unit}</td></tr>`;
  const rows = [];
  if (c.soil) for (const [k, label] of SOIL_VARS) if (c.soil.stats[k]) rows.push(row(label, SOIL_NAMES[k], c.soil.stats[k], "%"));
  if (c.dmp) {
    const st = c.dmp.stats;
    rows.push(row("DMP", "Dry matter productivity, mean", st, "kg/ha/d"));
    rows.push(`<tr><td title="Dry matter produced over the period" style="white-space:nowrap">Dry matter</td><td class="num"><b>${fmt(st.total, 1)}</b></td><td class="num">${fmt(st.reference_total, 1)}</td>
      <td class="num">${st.difference_pct === null ? "–" : `${st.difference_pct > 0 ? "+" : ""}${fmt(st.difference_pct)} %`}</td><td></td><td></td><td class="muted">t/ha</td></tr>`);
  }
  const csv = `/api/clms/parcels.csv?season=${c.season}&site=${encodeURIComponent(aoiInfo.code)}`;
  html += `<div class="table-wrap"><table class="kv"><tr><th></th><th class="num">${c.season}</th><th class="num">Other years</th>
      <th class="num">Diff.</th><th class="num">Lowest</th><th class="num">Highest</th><th></th></tr>${rows.join("")}</table></div>
    <div class="muted" style="font-size:11px">Means over the season from ${dayMonth(c.start)} to the last published date; other years: the
      same period of each year of the record, to the same date. The parcel's value is the mean of the cells it overlaps, weighted by
      its area in each: the cells (1 km, 300 m) are larger than most parcels, so they tell the soil water and growth of its surroundings
      (compare seasons, and distant parcels, rather than neighbours). Soil moisture in % of saturation: the surface from the Sentinel-1
      radar on the days of a pass, the Soil Water Index from the surface (T=2) to the root zone (T=100).
      <a href="${csv}" download>Statistics of every parcel of the season ${c.season} (CSV)</a>.</div>`;
  $("#s2-clms").innerHTML = html;
  $("#clms-ref")?.addEventListener("change", renderParcelCLMS);
}

// The Copernicus HR-VPP season of a parcel (start, peak, end from the Plant Phenology Index), in words
function vppText(sd) {
  const v = sd.d.vpp;
  if (sd.kind !== "parcel" || !v) return "";
  const day = (iso) => iso ? new Date(iso).toLocaleDateString(undefined, { day: "numeric", month: "short", year: "numeric" }) : "–";
  return `<div class="muted" style="font-size:11px;margin-top:4px"><span style="color:#7b4fd6">▮</span>
    Copernicus HR-VPP season (Plant Phenology Index, 10 m): start ${day(v.sos)}, peak ${day(v.peak)}, end ${day(v.eos)}
    (${fmt(v.length_days)} days); PPI peak ${fmt(v.ppi_max, 2)}, amplitude ${fmt(v.ppi_amplitude, 2)}, season productivity ${fmt(v.season_productivity, 0)}.
    Median of ${v.n_pixels} pixels (${v.n_dated} with dates).</div>`;
}

function seasonChart(sd) {
  const { d } = sd;
  const on = new Set([...document.querySelectorAll("#s2-refs input:checked")].map((i) => i.value));
  const series = [];
  for (const k of ["lt", "5y", "3y"]) {
    const r = sd.refs[k];
    if (r && on.has(k)) series.push({ key: k, ...SEASON_SERIES[k], values: r.mean,
      label: `${SEASON_SERIES[k].label} (${r.seasons[0]}–${r.seasons[r.seasons.length - 1]}${r.seasons.length < (k === "3y" ? 3 : k === "5y" ? 5 : 0) ? `, ${r.seasons.length} seasons` : ""})` });
  }
  if (sd.own && on.has("own")) series.push({ key: "own", ...SEASON_SERIES.own, values: sd.own.mean,
    label: `This parcel, earlier ${sd.groupLabel} seasons (${sd.own.seasons.join(", ")})` });
  if (sd.current) series.push({ key: "current", ...SEASON_SERIES.current, values: sd.current,
    label: sd.kind === "crop" ? `${d.season}, mean of the ${sd.unit}` : `${d.season}, this parcel` });
  const lt = sd.refs.lt && on.has("lt") && sd.refs.lt.p10 ? sd.refs.lt : null;
  const all = [...series.flatMap((s) => s.values), ...sd.obs.map((o) => o.v),
    ...(lt ? [...lt.p10, ...lt.p90] : []), ...(sd.spread ? sd.spread.flat() : [])].filter((v) => v !== null);
  if (!all.length) return '<p class="muted">No curve for this season.</p>';
  const ticks = niceTicks(Math.min(0, ...all), Math.max(...all));
  const y0 = ticks[0], y1 = ticks[ticks.length - 1];
  const sx = (day) => SN_M.l + (day / (d.days - 1)) * (SN_W - SN_M.l - SN_M.r);
  const sy = (v) => SN_H - SN_M.b - ((v - y0) / (y1 - y0 || 1)) * (SN_H - SN_M.t - SN_M.b);
  s2.seasonScale = { sx, sy, series };
  const path = (vals) => svgPath(vals, sx, sy);
  const band = (lo, hi, color, op) => svgBand(lo, hi, sx, sy, color, op);
  // Month ticks: the 1st of each month from September to December of the next year
  const months = [];
  for (let day = 0; day < d.days; day++) { const ds = dayDate(d.start, day); if (ds > d.end) break; if (ds.endsWith("-01")) months.push([day, ds]); }
  const xt = months.filter((_, i) => i % 2 === 0);
  const jan = d.jan1_day;
  return `<div class="wx-legend">${series.slice().reverse().map((s) => `<span class="k"><span class="line" style="background:${s.color};height:${s.key === "current" ? 3 : 2}px"></span>${esc(s.label)}</span>`).join("")}
    ${lt ? `<span class="k"><span class="line" style="background:#5f5f5a;opacity:.25;height:8px"></span>Long-term 10th–90th percentile of seasons</span>` : ""}
    ${sd.spread ? `<span class="k"><span class="line" style="background:#2a78d6;opacity:.2;height:8px"></span>${d.season}, middle half of the ${sd.unit}</span>` : ""}
    <span class="k"><span class="dot" style="background:#2a78d6"></span>${sd.kind === "crop" ? (sd.unit === "cells" ? "Composite (mean of the reliable cells)" : "Clear date (mean of the parcels)") : "Clear date"}</span>
    ${sd.kind === "parcel" ? `<span class="k"><span class="dot" style="background:#fff;border:1.2px solid #8a8a86"></span>Partly cloudy, not used</span>` : ""}</div>
    <div class="wx-plot"><svg viewBox="0 0 ${SN_W} ${SN_H}" role="img" aria-label="Seasonal index curve and references">
    ${yGrid(ticks, sy, SN_M, SN_W, 1)}
    <line x1="${sx(jan)}" x2="${sx(jan)}" y1="${SN_M.t}" y2="${SN_H - SN_M.b}" stroke="#d0d0cc" stroke-dasharray="2 2"/>
    ${xt.map(([day, ds]) => `<text x="${sx(day)}" y="${SN_H - 6}" text-anchor="middle">${monthName(ds)}${ds.slice(5, 7) === "01" ? ` ${ds.slice(0, 4)}` : ""}</text>`).join("")}
    ${lt ? band(lt.p10, lt.p90, "#5f5f5a", 0.13) : ""}
    ${sd.spread ? band(sd.spread[0], sd.spread[1], "#2a78d6", 0.14) : ""}
    ${series.map((s) => `<path d="${path(s.values)}" fill="none" stroke="${s.color}" stroke-width="${s.width}" ${s.dash ? `stroke-dasharray="${s.dash}"` : ""} stroke-linejoin="round" stroke-linecap="round"/>`).join("")}
    ${sd.obs.map((o) => o.used
      ? `<circle cx="${sx(o.day).toFixed(1)}" cy="${sy(o.v).toFixed(1)}" r="3.5" fill="#2a78d6" stroke="#fff" stroke-width="1.5"/>`
      : `<circle cx="${sx(o.day).toFixed(1)}" cy="${sy(o.v).toFixed(1)}" r="3" fill="#fff" stroke="#8a8a86" stroke-width="1.2"/>`).join("")}
    ${sd.kind === "parcel" && d.vpp ? [["sos_day", "start"], ["peak_day", "peak"], ["eos_day", "end"]]
      .filter(([k]) => d.vpp[k] !== null && d.vpp[k] >= 0 && d.vpp[k] < d.days).map(([k, label]) => `<line x1="${sx(d.vpp[k])}" x2="${sx(d.vpp[k])}"
        y1="${SN_M.t}" y2="${SN_H - SN_M.b}" stroke="#7b4fd6" stroke-dasharray="4 3"/><text x="${sx(d.vpp[k]) + 2}" y="${SN_M.t + 8}" fill="#7b4fd6" font-size="9">${label}</text>`).join("") : ""}
    <line class="wx-cross" y1="${SN_M.t}" y2="${SN_H - SN_M.b}" stroke="#8a8a86" visibility="hidden"/>
    <g class="wx-dots"></g>
    <rect class="wx-hit" x="${SN_M.l}" y="0" width="${SN_W - SN_M.l - SN_M.r}" height="${SN_H}" fill="transparent" style="cursor:pointer"/>
  </svg><div class="wx-tip" hidden></div></div>
  <div class="muted" style="font-size:11px">Season ${d.season}: 1 September ${d.season - 1} to 31 December ${d.season} (autumn sowing to the last harvests). Click a clear date to show its image.</div>`;
}

function bindSeasonHover() {
  const svg = $("#s2-chart .wx-plot svg");
  if (!svg || !s2.seasonScale) return;
  const { sx, sy, series } = s2.seasonScale, sd = s2.seasonData, d = sd.d;
  const cross = svg.querySelector(".wx-cross"), dots = svg.querySelector(".wx-dots"), tip = $("#s2-chart .wx-tip");
  const hit = svg.querySelector(".wx-hit");
  const dayAt = (e) => Math.max(0, Math.min(d.days - 1, Math.round(((svgX(svg, e) - SN_M.l) / (SN_W - SN_M.l - SN_M.r)) * (d.days - 1))));
  const nearObs = (day) => { let b = null, bd = 4; for (const o of sd.obs) { const dd = Math.abs(o.day - day); if (dd <= bd) { bd = dd; b = o; } } return b; };
  hit.addEventListener("pointermove", (e) => {
    const day = dayAt(e), x = sx(day), o = nearObs(day);
    cross.setAttribute("x1", x); cross.setAttribute("x2", x); cross.setAttribute("visibility", "visible");
    const rows = series.slice().reverse().map((s) => ({ s, v: s.values[day] }));
    dots.innerHTML = rows.filter((r) => r.v !== null).map((r) => `<circle cx="${x}" cy="${sy(r.v)}" r="3.5" fill="${r.s.color}" stroke="#fff" stroke-width="1.5"/>`).join("");
    tip.innerHTML = `<div class="d">${dayDate(d.start, day)}</div>` +
      rows.map((r) => `<div class="r"><span class="swatch" style="background:${r.s.color}"></span>${esc(r.s.key === "current" ? String(d.season) : r.s.label.split(" (")[0])}<b>${fmt(r.v, 2)}</b></div>`).join("") +
      (o ? `<div class="d" style="margin-top:3px">${sd.unit === "cells" ? `Composite from ${o.date}` : `Observed ${o.date}`}: <b>${fmt(o.v, 2)}</b>${o.n ? ` · ${o.n} ${sd.unit || "parcels"}` : ""}${o.clear !== undefined ? ` · ${fmt(100 * o.clear)} % clear${o.used ? "" : ", not used"}` : ""}</div>` : "");
    placeTip(tip, svg, x, SN_W);
  });
  hit.addEventListener("pointerleave", () => hideCrosshair(cross, dots, tip));
  hit.addEventListener("click", (e) => { const o = nearObs(dayAt(e)); if (o) showS2Image(o.date); });
}

function renderSeason() {
  const sd = s2.seasonData;
  $("#s2-chart").innerHTML = seasonChart(sd) + vppText(sd);
  bindSeasonHover();
  // Comparison: on the latest observation (season in progress), at the peak (1 Jan - 31 Aug) and
  // averaged from 1 January (to the latest observation, or over the year when the season is over)
  const cur = sd.current;
  const days = cur ? cur.map((v, i) => (v === null ? -1 : i)).filter((i) => i >= 0) : [];
  if (!days.length) { $("#s2-compare").innerHTML = ""; return; }
  const d = sd.d, jan = d.jan1_day, last = days[days.length - 1];
  const lastDate = s2.source === "modis" ? s2.modis?.last : s2.info?.last;
  const lastObs = lastDate ? Math.round((Date.parse(`${lastDate}T00:00:00Z`) - Date.parse(`${d.start}T00:00:00Z`)) / 86400000) : -1;
  const ongoing = lastObs <= d.days && last >= lastObs - 30;
  const endDay = ongoing ? last : Math.min(d.days - 1, jan + 364);
  const peakRange = [jan, Math.min(jan + 243, endDay + 1)];
  const peak = (vals) => { let k = -1; for (let i = peakRange[0]; i < peakRange[1]; i++) if (vals[i] !== null && (k < 0 || vals[i] > vals[k])) k = i; return k; };
  const mean = (vals, ref) => { const ok = []; for (let i = jan; i <= endDay; i++) if (vals[i] !== null && (!ref || ref[i] !== null)) ok.push(i);
    return ok.length >= 15 ? ok : null; };
  const avg = (vals, ok) => ok.reduce((a, i) => a + vals[i], 0) / ok.length;
  const f2 = (v) => (v === null || v === undefined ? "–" : v.toFixed(2));
  const sign = (v) => { if (v === null || v === undefined) return "–"; const r = Math.round(v * 100) / 100;
    return `${r > 0 ? "+" : r < 0 ? "−" : "±"}${Math.abs(r).toFixed(2)}`; };
  const md = (day) => (day < 0 ? "–" : dayDate(d.start, day).slice(5));
  const refs = [["3y", sd.refs["3y"]], ["5y", sd.refs["5y"]], ["lt", sd.refs.lt], ["own", sd.own]].filter(([, r]) => r);
  const pc = peak(cur), okc = mean(cur);
  const period = `1 Jan–${ongoing ? md(endDay) : "31 Dec"}`;
  const head = `<tr><th></th>${ongoing ? `<th class="num">${md(last)}</th><th class="num">Diff.</th>` : ""}
    <th class="num">Peak</th><th class="num">Date</th><th class="num">Mean ${period}</th><th class="num">Diff.</th></tr>`;
  const row = (label, color, vals, isCur) => {
    const k = peak(vals), ok = isCur ? okc : mean(vals, cur);
    const m = ok ? avg(vals, ok) : null, mc = ok ? avg(cur, ok) : null;
    return `<tr><td style="white-space:nowrap"><span class="swatch" style="background:${color}"></span> ${esc(label)}</td>
      ${ongoing ? `<td class="num">${isCur ? `<b>${f2(cur[last])}</b>` : f2(vals[last])}</td><td class="num">${isCur || vals[last] === null ? "" : sign(cur[last] - vals[last])}</td>` : ""}
      <td class="num">${k < 0 ? "–" : isCur ? `<b>${f2(vals[k])}</b>` : f2(vals[k])}</td><td class="num">${md(k)}</td>
      <td class="num">${m === null ? "–" : isCur ? `<b>${f2(m)}</b>` : f2(m)}</td><td class="num">${isCur || m === null ? "" : sign(mc - m)}</td></tr>`;
  };
  const rank = sd.kind === "crop" && ongoing && d.comparison?.rank;
  $("#s2-compare").innerHTML = `<table class="kv">${head}
    ${row(String(d.season), SEASON_SERIES.current.color, cur, true)}
    ${refs.map(([k, r]) => row(SEASON_SERIES[k].short, SEASON_SERIES[k].color, r.mean, false)).join("")}
    </table><div class="muted" style="font-size:11px">Differences: ${d.season} minus the reference${refs.length ? ", on the same days" : ""}.
    Peak between 1 January and 31 August.${rank ? ` On ${md(last)}, ${d.season} is higher than ${rank.below} of the ${rank.seasons} earlier seasons.` : ""}</div>`;
}

function renderHistory(d) {
  const rows = d.history.filter((h) => h.status !== "none");
  $("#s2-history").innerHTML = `<h2>Parcel history</h2><div class="table-wrap"><table class="kv s2-hist">
    <tr><th>Season</th><th>Crop</th><th class="num">ha</th><th class="num">Peak</th><th class="num">Peak date</th><th class="num">Clear dates</th></tr>
    ${[...rows].reverse().map((h) => `<tr data-season="${h.season}" class="${h.season === d.season ? "sel" : ""}" style="cursor:${h.n_obs ? "pointer" : "default"}">
      <td>${h.season}</td><td>${h.status === "unknown" ? '<span class="muted">not published yet</span>'
        : h.status === "ilots" ? `${esc(h.crop)} <span class="muted">(majority of the farm's îlot${h.share ? `, ${fmt(100 * h.share)} %` : ""})</span>`
        : h.status === "farm" ? `${esc(h.crop)} <span class="muted">(your land cover${h.rpg_crop ? `; RPG: ${esc(h.rpg_crop)}` : ""})</span>`
        : `${esc(h.crop)}${h.status === "assumed" ? ' <span class="muted">(assumed)</span>' : ""}`}</td>
      <td class="num">${fmt(h.surf_ha, 2)}</td><td class="num">${fmt(h.peak, 2)}</td><td class="num">${h.peak_date ? h.peak_date.slice(5) : "–"}</td><td class="num">${h.n_obs ?? "–"}</td></tr>`).join("")}
  </table></div><div class="muted" style="font-size:11px">Each season uses that year's RPG parcel at the clicked point (outlines and identifiers change every year), or your field in the seasons of your land-cover files. Before 2015 the RPG only gives the farm's îlot (a block of parcels) and its majority crop group. Click a row to show its season.</div>`;
  $("#s2-history").querySelectorAll("tr[data-season]").forEach((tr) => tr.addEventListener("click", () => {
    const h = d.history.find((x) => x.season === Number(tr.dataset.season));
    if (h?.n_obs) { s2.season = h.season; updateS2Parcel(); }
  }));
}

function showS2Parcel(geom) {
  const data = { type: "Feature", geometry: geom, properties: {} };
  if (map.getSource("s2-parcel")) map.getSource("s2-parcel").setData(data);
  else {
    map.addSource("s2-parcel", { type: "geojson", data });
    map.addLayer({ id: "s2-parcel", type: "line", source: "s2-parcel", paint: { "line-color": "#1a1a19", "line-width": 2.5 } });
  }
}

function removeS2Parcel() {
  if (map.getLayer("s2-parcel")) map.removeLayer("s2-parcel");
  if (map.getSource("s2-parcel")) map.removeSource("s2-parcel");
}

// ---------------------------------------------------------------------------
// Sentinel-1 tab: backscatter series over the selected area (one line per relative orbit), image per date
// ---------------------------------------------------------------------------
// Categorical slots 1-3 of the reference palette (validated all-pairs): one per relative orbit of a
// direction (at most three over these sites); the legend and the tooltip name each orbit.
const S1_ORBIT_COLORS = ["#2a78d6", "#eb6834", "#1baf7a"];
const s1 = { info: null, points: [], date: null, seq: 0, imageIds: [] };
const S1_W = 380, S1_H = 190, S1_M = { l: 40, r: 10, t: 10, b: 22 };

const s1Orbits = () => (s1.info?.directions[$("#s1-dir").value]?.orbits || []).map((o) => o.orbit);
const s1Color = (orbit) => S1_ORBIT_COLORS[s1Orbits().indexOf(orbit)] || "#5f5f5a";

async function initS1() {
  s1.info = await api("/api/s1/info");
  if (!s1.info.available) {
    $("#s1-status").innerHTML = `No Sentinel-1 data yet. ${esc(s1.info.hint || "")}`;
    return;
  }
  const i = s1.info;
  $("#s1-measure").innerHTML = i.measures.map((m) => `<option value="${m.name}">${esc(m.long_name)}${m.units ? ` (${m.units})` : ""}</option>`).join("");
  const years = [...new Set(Object.values(i.directions).flatMap((d) => d.years))].sort();
  $("#s1-year").innerHTML = `<option value="">All (${years[0]}–${years[years.length - 1]})</option>` +
    [...years].reverse().map((y) => `<option value="${y}">${y}</option>`).join("");
  for (const id of ["#s1-dir", "#s1-measure", "#s1-year"]) $(id).addEventListener("change", updateS1);
  $("#s1-date").addEventListener("change", (e) => showS1Image(e.target.value));
  $("#s1-opacity").addEventListener("input", (e) => {
    for (const id of s1.imageIds) if (map.getLayer(id)) map.setPaintProperty(id, "raster-opacity", Number(e.target.value));
  });
  const dirs = Object.entries(i.directions).map(([, d]) => `${d.label}: ${d.dates} dates, orbits ${d.orbits.map((o) => o.orbit).join(", ")}`).join("; ");
  $("#s1-sources").innerHTML = `Sentinel-1 GRD (IW, VV + VH), ${esc(i.collection)}, openEO Copernicus Data Space, ${i.resolution_m} m, from ${esc(i.start)} ·
    ${esc(dirs)} · backscatter ${esc(i.coefficient)}: ${esc(i.note)}. Area means are taken in linear power.`;
}

async function updateS1() {
  if (!s1.info?.available || !currentArea) return;
  const seq = ++s1.seq;
  const dir = $("#s1-dir").value, measure = $("#s1-measure").value, year = $("#s1-year").value;
  const a = areaOf(currentArea);
  const d = await api(`/api/s1/series?measure=${measure}&direction=${dir}&area=${encodeURIComponent(currentArea)}`);
  if (seq !== s1.seq) return;
  s1.points = d.points.filter((p) => !year || p.date.startsWith(year));
  s1.series = d;
  $("#s1-status").innerHTML = `Mean over <b>${esc(a.name)}</b> of each ${esc(s1.info.directions[dir].label)} acquisition. Values depend on the viewing geometry: compare an orbit with itself. Click a date to show its image.`;
  $("#s1-chart").innerHTML = s1.points.length ? s1Chart(d) : '<p class="muted">No acquisition.</p>';
  bindS1Hover();
  $("#s1-date").innerHTML = [...s1.points].reverse().map((p) =>
    `<option value="${p.date}">${p.date} · orbit ${p.orbit ?? "?"} · ${fmt(p.mean, 2)}${d.units ? ` ${d.units}` : ""}</option>`).join("");
  const keep = s1.points.find((p) => p.date === s1.date);
  showS1Image(keep ? s1.date : s1.points[s1.points.length - 1]?.date);
}

function s1Chart(d) {
  const pts = s1.points;
  const t = (iso) => Date.parse(`${iso}T00:00:00Z`);
  const x0 = t(pts[0].date), x1 = Math.max(t(pts[pts.length - 1].date), x0 + 86400000);
  const vals = pts.map((p) => p.mean);
  const ticks = niceTicks(Math.min(...vals), Math.max(...vals));
  const y0 = ticks[0], y1 = ticks[ticks.length - 1];
  const sx = (iso) => S1_M.l + ((t(iso) - x0) / (x1 - x0)) * (S1_W - S1_M.l - S1_M.r);
  const sy = (v) => S1_H - S1_M.b - ((v - y0) / (y1 - y0 || 1)) * (S1_H - S1_M.t - S1_M.b);
  s1.scale = { sx, sy };
  // One line per relative orbit (the same viewing geometry), broken across gaps of more than 30 days
  const orbits = [...new Set(pts.map((p) => p.orbit))];
  const lines = orbits.map((o) => {
    const op = pts.filter((p) => p.orbit === o);
    let path = "";
    op.forEach((p, k) => { path += `${k && t(p.date) - t(op[k - 1].date) <= 30 * 86400000 ? "L" : "M"}${sx(p.date).toFixed(1)},${sy(p.mean).toFixed(1)}`; });
    return `<path d="${path}" fill="none" stroke="${s1Color(o)}" stroke-width="1.5" stroke-opacity="0.8"/>`;
  }).join("");
  const dots = pts.map((p) => `<circle cx="${sx(p.date).toFixed(1)}" cy="${sy(p.mean).toFixed(1)}" r="2.5" fill="${s1Color(p.orbit)}"/>`).join("");
  const years = (x1 - x0) / (365 * 86400000);
  const xl = Array.from({ length: 5 }, (_, i) => x0 + (i * (x1 - x0)) / 4);
  const fmtX = (ms) => { const iso = new Date(ms).toISOString(); return years > 2 ? iso.slice(0, 4) : iso.slice(0, 7); };
  const legend = `<div class="wx-legend">${orbits.map((o) => `<span class="k"><span class="line" style="background:${s1Color(o)}"></span>Orbit ${o ?? "?"} (${pts.filter((p) => p.orbit === o).length} dates)</span>`).join("")}</div>`;
  return `<div class="muted">${esc(d.long_name)}${d.units ? ` (${d.units})` : ""}, mean over the area · ${pts.length} acquisitions</div>${legend}
    <div class="wx-plot"><svg viewBox="0 0 ${S1_W} ${S1_H}" role="img" aria-label="Sentinel-1 backscatter time series">
    ${yGrid(ticks, sy, S1_M, S1_W)}
    ${xl.map((ms, i) => `<text x="${S1_M.l + (i * (S1_W - S1_M.l - S1_M.r)) / 4}" y="${S1_H - 6}" text-anchor="${i === 0 ? "start" : i === 4 ? "end" : "middle"}">${fmtX(ms)}</text>`).join("")}
    ${lines}${dots}
    <line class="wx-cross" y1="${S1_M.t}" y2="${S1_H - S1_M.b}" stroke="#8a8a86" visibility="hidden"/>
    <circle class="s1-sel" r="5" fill="none" stroke="#1a1a19" stroke-width="2" visibility="hidden"/>
    <g class="wx-dots"></g>
    <rect class="wx-hit" x="${S1_M.l}" y="0" width="${S1_W - S1_M.l - S1_M.r}" height="${S1_H}" fill="transparent" style="cursor:pointer"/>
  </svg><div class="wx-tip" hidden></div></div>`;
}

function bindS1Hover() {
  const svg = $("#s1-chart .wx-plot svg");
  if (!svg) return;
  const cross = svg.querySelector(".wx-cross"), dots = svg.querySelector(".wx-dots"), tip = $("#s1-chart .wx-tip");
  const hit = svg.querySelector(".wx-hit"), units = s1.series.units;
  const nearest = (e) => {
    const x = svgX(svg, e);
    let best = null, bd = Infinity;
    for (const p of s1.points) { const dd = Math.abs(s1.scale.sx(p.date) - x); if (dd < bd) { bd = dd; best = p; } }
    return best;
  };
  hit.addEventListener("pointermove", (e) => {
    const p = nearest(e);
    if (!p) return;
    const x = s1.scale.sx(p.date);
    cross.setAttribute("x1", x); cross.setAttribute("x2", x); cross.setAttribute("visibility", "visible");
    dots.innerHTML = `<circle cx="${x}" cy="${s1.scale.sy(p.mean)}" r="4.5" fill="${s1Color(p.orbit)}" stroke="#fff" stroke-width="2"/>`;
    tip.innerHTML = `<div class="d">${p.date} · orbit ${p.orbit ?? "?"}</div>` +
      `<div class="r"><span class="swatch" style="background:${s1Color(p.orbit)}"></span>Mean<b>${fmt(p.mean, 2)} ${esc(units)}</b></div>` +
      `<div class="r">Median<b>${fmt(p.median, 2)} ${esc(units)}</b></div>` +
      `<div class="r">10th–90th<b>${fmt(p.p10, 1)} – ${fmt(p.p90, 1)}</b></div>` +
      `<div class="r">Area covered<b>${fmt(100 * p.clear_fraction)} %</b></div>`;
    placeTip(tip, svg, x, S1_W);
  });
  hit.addEventListener("pointerleave", () => hideCrosshair(cross, dots, tip));
  hit.addEventListener("click", (e) => { const p = nearest(e); if (p) showS1Image(p.date); });
  markS1Selection();
}

function markS1Selection() {
  const sel = $("#s1-chart .s1-sel");
  const p = s1.points.find((x) => x.date === s1.date);
  if (!sel) return;
  if (!p) { sel.setAttribute("visibility", "hidden"); return; }
  sel.setAttribute("cx", s1.scale.sx(p.date)); sel.setAttribute("cy", s1.scale.sy(p.mean));
  sel.setAttribute("visibility", "visible");
}

function removeS1Image() {
  for (const id of s1.imageIds) {
    if (map.getLayer(id)) map.removeLayer(id);
    if (map.getSource(id)) map.removeSource(id);
  }
  s1.imageIds = [];
}

async function showS1Image(date) {
  removeS1Image();
  s1.date = date;
  $("#s1-legend").innerHTML = "";
  if (!date || !$("#tab-s1").classList.contains("active")) return;
  $("#s1-date").value = date;
  markS1Selection();
  const measure = $("#s1-measure").value, dir = $("#s1-dir").value;
  const info = await api(`/api/s1/${measure}/info?date=${date}&direction=${dir}`);
  if (s1.date !== date) return;
  removeS1Image();
  info.images.forEach((im, k) => {
    const id = `s1-image-${k}`;
    map.addSource(id, { type: "image", url: im.url, coordinates: im.coordinates });
    map.addLayer({ id, type: "raster", source: id,
      paint: { "raster-opacity": Number($("#s1-opacity").value), "raster-resampling": "nearest" } }, firstVectorLayer);
    s1.imageIds.push(id);
  });
  const stops = info.legend.map((l, i) => `${l.color} ${(100 * i) / (info.legend.length - 1)}%`).join(",");
  $("#s1-legend").innerHTML = `<div class="title">${esc(info.long_name)}, ${esc(date)} (orbit ${info.orbit ?? "?"})</div>
    <div class="ramp" style="background:linear-gradient(to right,${stops})"></div>
    <div class="ramp-labels"><span>${fmt(info.vmin, 1)}</span><span>${fmt((info.vmin + info.vmax) / 2, 1)}</span><span>${fmt(info.vmax, 1)}${info.units ? ` ${esc(info.units)}` : ""}</span></div>`;
}

// ---------------------------------------------------------------------------
// Point query
// ---------------------------------------------------------------------------
const marker = new maplibregl.Marker({ color: "#c0392b" });
const SOIL_COLUMNS = [
  ["texture_usda", "Texture"], ["clay", "Clay"], ["sand", "Sand"], ["silt", "Silt"], ["soc", "SOC"],
  ["bdod", "BD"], ["cfvo", "Coarse"], ["phh2o", "pH"], ["theta_fc", "θFC"], ["theta_wp", "θWP"],
  ["ksat", "Ksat"], ["awc", "AWC"],
];

async function queryPoint(lngLat) {
  marker.setLngLat(lngLat).addTo(map);
  showTab("point");
  $("#point-info").innerHTML = '<p class="muted">Loading…</p>';
  const p = await api(`/api/point?lon=${lngLat.lng}&lat=${lngLat.lat}`);
  let html = `<div class="card"><h3>Location</h3><table class="kv">
    <tr><td>Coordinates</td><td class="num">${lngLat.lat.toFixed(5)}, ${lngLat.lng.toFixed(5)}</td></tr>
    <tr><td>Commune</td><td class="num">${p.commune ? `${esc(p.commune.name)} (${p.commune.code})` : "–"}</td></tr>`;
  const dem = p.rasters.dem?.variables;
  if (dem) {
    html += `<tr><td>Elevation</td><td class="num">${fmt(dem.elevation.value, 1)} m</td></tr>
      <tr><td>Slope</td><td class="num">${fmt(dem.slope.value, 1)}°</td></tr>
      <tr><td>Aspect</td><td class="num">${fmt(dem.aspect.value)}° from N</td></tr>`;
  }
  html += "</table></div>";

  if (p.parcel) {
    html += `<div class="card"><h3>RPG ${p.parcel.year} parcel</h3><table class="kv">
      <tr><td>Crop</td><td class="num">${esc(p.parcel.crop || p.parcel.code_cultu)} (${esc(p.parcel.code_cultu)})</td></tr>
      <tr><td>Crop group</td><td class="num">${esc(p.parcel.crop_group || "")}</td></tr>
      <tr><td>Declared area</td><td class="num">${fmt(p.parcel.surf_ha, 2)} ha</td></tr>
      <tr><td>Organic</td><td class="num">${p.parcel.bio === null ? "–" : p.parcel.bio ? "yes" : "no"}</td></tr>
    </table></div>`;
  } else {
    html += '<div class="card"><h3>RPG parcel</h3><span class="muted">No declared parcel here.</span></div>';
  }

  const soil = p.rasters.soil;
  if (soil) html += soilProfileHtml(soil, `Soil profile (SoilGrids, ${fmt(soil.resolution_m)} m)`, false);
  $("#point-info").innerHTML = html;
}

// A soil profile table: the values at a point (``values``, ``labels``) or the means over an area
// (``mean``, ``dominant`` texture)
function soilProfileHtml(soil, title, area) {
  const v = soil.variables;
  const cols = SOIL_COLUMNS.filter(([k]) => v[k]);
  const awc = area ? v.awc_total?.mean : v.awc_total?.value;
  const sd = area ? v.awc_total_sd?.mean : v.awc_total_sd?.value;
  return `<div class="card"><h3>${esc(title)}</h3>
    ${awc !== undefined && awc !== null ? `<div class="meta">Available water capacity (réserve utile) 0–200 cm: <b>${fmt(awc)} mm</b>${sd ? ` ± ${fmt(sd)}` : ""} (${soil.inrae ? "INRAE" : "estimate"}${area ? ", mean over the area" : ""})</div>` : ""}
    <div class="table-wrap"><table class="kv soil"><tr><th>Depth</th>${cols.map(([k, l]) =>
      `<th class="num" title="${esc(v[k]?.long_name)}">${l}${v[k] && unit(v[k].units) ? `<br><span class="muted">${esc(unit(v[k].units))}</span>` : ""}</th>`).join("")}</tr>
    ${soil.depths.map((d, i) => `<tr><td>${d}</td>${cols.map(([k]) => {
      if (k === "texture_usda") return `<td class="num">${esc((area ? v[k].dominant : v[k].labels)?.[i] ?? "–")}</td>`;
      const digits = ["theta_fc", "theta_wp", "bdod"].includes(k) ? 2 : 1;
      return `<td class="num">${fmt((area ? v[k].mean : v[k].values)?.[i], digits)}</td>`;
    }).join("")}</tr>`).join("")}
    </table></div>
    <div class="muted" style="font-size:11px;margin-top:4px">${area ? "Means over the area (texture: the dominant class). " : ""}${soil.inrae
      ? "INRAE / Gis Sol digital soil maps (90 m): θFC, θWP and AWC from French pedotransfer functions (SOLHYDRO measurements)."
      : "SoilGrids 2.0: θFC, θWP, Ksat and AWC are Saxton &amp; Rawls (2006) pedotransfer estimates."}</div>
  </div>`;
}

// ---------------------------------------------------------------------------
// Soils tab
// ---------------------------------------------------------------------------
let soilSeq = 0;
async function updateSoilArea() {
  if (!currentArea || !$("#tab-soil").classList.contains("active")) return;
  const seq = ++soilSeq;
  $("#soil-area").innerHTML = '<p class="muted">Loading…</p>';
  const s = await api(`/api/area/summary?area=${encodeURIComponent(currentArea)}`);
  if (seq !== soilSeq) return;
  const soil = s.rasters[soilSource()];
  if (soil) soil.inrae = soilSource() === "soil_inrae";
  $("#soil-area").innerHTML = soil
    ? soilProfileHtml(soil, `${areaOf(currentArea).name} · ${fmt(soil.cells)} cells of ${fmt(soil.resolution_m)} m`, true)
    : `<p class="muted">No soil data: run <code>docker compose run --rm ingest ${soilSource() === "soil_inrae" ? "soil-inrae" : "soil"}</code>.</p>`;
}

let soilPointAt = null;
async function soilAtPoint(lngLat) {
  soilPointAt = lngLat;
  marker.setLngLat(lngLat).addTo(map);
  $("#soil-point").innerHTML = '<p class="muted">Loading…</p>';
  const [p, at] = await Promise.all([api(`/api/point?lon=${lngLat.lng}&lat=${lngLat.lat}`),
    api(`/api/soil/at?lon=${lngLat.lng}&lat=${lngLat.lat}`).catch(() => ({}))]);
  const soil = p.rasters[soilSource()];
  if (soil) soil.inrae = soilSource() === "soil_inrae";
  const any = soil && Object.values(soil.variables).some((v) => (v.values || []).some((x) => x !== null && x !== undefined));
  const sw = (c) => (c ? `<span class="swatch" style="background:${c}"></span> ` : "");
  const maps = [
    at.soils ? `<tr><td>Dominant soil</td><td>${sw(at.soils.color)}<b>${esc(at.soils.label)}</b><br><span class="muted">${esc(at.soils.detail || "")}</span></td></tr>` : "",
    at.geology ? `<tr><td>Geology</td><td>${sw(at.geology.color)}${esc(at.geology.label || "")}</td></tr>` : "",
    at.clay ? `<tr><td>Clay shrink-swell</td><td>${sw(at.clay.color)}${esc(at.clay.label)}</td></tr>` : "",
  ].join("");
  const where = `${lngLat.lat.toFixed(5)}, ${lngLat.lng.toFixed(5)}${p.commune ? ` · ${p.commune.name}` : ""}`;
  $("#soil-point").innerHTML = (maps ? `<div class="card"><h3>${esc(where)}</h3><table class="kv">${maps}</table></div>` : "") +
    (any ? soilProfileHtml(soil, `Soil profile (${soilSource() === "soil_inrae" ? "INRAE, 90 m" : "SoilGrids, 250 m"})`, false)
      : '<p class="muted">No soil properties at this point (sea, town, or outside the site).</p>');
}

// ---------------------------------------------------------------------------
// Soils tab: soil and geology maps (vector tiles, shown on this tab only)
// ---------------------------------------------------------------------------
const soilMaps = { info: null, on: new Set() };

async function initSoilMaps() {
  soilMaps.info = await api("/api/soil/maps");
  const avail = soilMaps.info.maps.filter((m) => m.available);
  $("#soil-maps").innerHTML = avail.length ? avail.map((m) =>
    `<label><input type="checkbox" value="${m.layer}"> ${esc(m.title)} <span class="muted">(${esc(m.source.split(",")[0])})</span></label>`).join("")
    : '<p class="muted">No map yet: run <code>docker compose run --rm ingest soil-maps</code>.</p>';
  $("#soil-maps").addEventListener("change", (e) => {
    const layer = e.target.value;
    if (e.target.checked) soilMaps.on.add(layer); else soilMaps.on.delete(layer);
    showSoilMap(layer, e.target.checked);
    renderSoilMapsLegend();
  });
}

function showSoilMap(layer, on) {
  const id = `soilmap-${layer}`;
  if (on && !map.getSource(id)) {
    map.addSource(id, { type: "vector", maxzoom: 15,
      tiles: [`${location.origin}/api/tiles/${layer}/{z}/{x}/{y}.pbf?site=${encodeURIComponent(aoiInfo.code)}`] });
    map.addLayer({ id: `${id}-fill`, type: "fill", source: id, "source-layer": layer,
      paint: { "fill-color": ["coalesce", ["get", "color"], "#999999"], "fill-opacity": layer === "clay" ? 0.45 : 0.6 } }, soilMapBefore());
    map.addLayer({ id: `${id}-line`, type: "line", source: id, "source-layer": layer,
      paint: { "line-color": "#5f5f5a", "line-width": 0.5, "line-opacity": 0.6 } }, soilMapBefore());
  }
  const vis = on && $("#tab-soil").classList.contains("active") ? "visible" : "none";
  for (const suffix of ["-fill", "-line"]) if (map.getLayer(id + suffix)) map.setLayoutProperty(id + suffix, "visibility", vis);
}

// Above the RPG parcels and the land cover, below the commune outlines and names
const soilMapBefore = () => (map.getLayer("admin-0") ? "admin-0" : undefined);

function renderSoilMapsLegend() {
  $("#soil-maps-legend").innerHTML = soilMaps.info.maps.filter((m) => soilMaps.on.has(m.layer)).map((m) =>
    `<div class="title">${esc(m.title)}</div>` + m.classes.slice(0, 14).map((c) =>
      `<div class="item"><span class="swatch" style="background:${c.color}"></span>${esc(c.label)}<span class="val">${fmt(c.ha)} ha</span></div>`).join("") +
    (m.classes.length > 14 ? `<div class="muted">… ${m.classes.length - 14} more (click the map)</div>` : "") +
    `<div class="muted" style="font-size:11px">${esc(m.source)}</div>`).join("");
}

// ---------------------------------------------------------------------------
// Soils tab: groundwater (Hub'Eau piezometers)
// ---------------------------------------------------------------------------
const gw = { stations: [], data: null, seq: 0 };
const GW_W = 380, GW_H = 190, GW_M = { l: 40, r: 10, t: 10, b: 22 };

async function initGroundwater() {
  const d = await api("/api/groundwater/stations");
  gw.stations = d.stations;
  if (!gw.stations.length) {
    $("#gw-chart").innerHTML = '<p class="muted">No active piezometer within 25 km, or not loaded yet (<code>ingest groundwater</code>).</p>';
    return;
  }
  $("#gw-station").innerHTML = gw.stations.map((s) => `<option value="${esc(s.code_bss)}">${esc(s.commune || s.name)} · ${fmt(s.distance_km, 1)} km · ${s.last_depth !== null ? `${fmt(s.last_depth, 2)} m` : "–"}</option>`).join("");
  $("#gw-station").addEventListener("change", updateGroundwater);
  $("#gw-years").addEventListener("change", updateGroundwater);
  map.addSource("gw-stations", { type: "geojson", data: { type: "FeatureCollection", features: gw.stations.map((s) => ({
    type: "Feature", geometry: { type: "Point", coordinates: [s.lon, s.lat] }, properties: { code: s.code_bss, name: s.commune } })) } });
  map.addLayer({ id: "gw-stations", type: "circle", source: "gw-stations", layout: { visibility: "none" },
    paint: { "circle-radius": 6, "circle-color": "#2a78d6", "circle-stroke-color": "#fff", "circle-stroke-width": 2 } });
  map.on("click", "gw-stations", (e) => { $("#gw-station").value = e.features[0].properties.code; updateGroundwater(); });
}

async function updateGroundwater() {
  if (!gw.stations.length || !$("#tab-soil").classList.contains("active")) return;
  const seq = ++gw.seq, code = $("#gw-station").value, years = $("#gw-years").value;
  const d = await api(`/api/groundwater/series?code_bss=${encodeURIComponent(code)}&years=${years}`);
  if (seq !== gw.seq) return;
  gw.data = d;
  const s = gw.stations.find((x) => x.code_bss === code);
  $("#gw-chart").innerHTML = gwChart(d);
  bindGwHover();
  const c = d.current;
  $("#gw-info").innerHTML = `${esc(s.name || "")} (${esc(code)}), ${fmt(s.distance_km, 1)} km from the site centre, borehole ${s.depth_m ? `${fmt(s.depth_m, 1)} m deep` : "of unknown depth"}${s.aquifers ? `, aquifer: ${esc(s.aquifers)}` : ""}. Record ${esc(d.first)} → ${esc(d.last)}.
    ${c ? `<br><b>${fmt(c.depth_m, 2)} m</b> below ground on ${esc(c.date)}: higher than ${fmt(c.higher_than_pct)} % of the same weeks of ${c.years} earlier years (median ${fmt(c.median_m, 2)} m).` : ""}
    A piezometer measures one aquifer at its place, which may not be the one under the site. Source: Hub'Eau (ADES).`;
}

function gwChart(d) {
  const pts = d.points;
  if (!pts.length) return '<p class="muted">No level over this period.</p>';
  const t = (iso) => Date.parse(`${iso}T00:00:00Z`);
  const x0 = t(pts[0][0]), x1 = Math.max(t(pts[pts.length - 1][0]), x0 + 86400000);
  const doy = (ms) => Math.floor((ms - Date.UTC(new Date(ms).getUTCFullYear(), 0, 1)) / 86400000);
  // The normal (10th-90th percentile and median of the same day in earlier years) along the period
  const days = [];
  if (d.normal) for (let ms = x0; ms <= x1; ms += 86400000 * Math.max(1, Math.round((x1 - x0) / 86400000 / 300))) {
    const k = doy(ms);
    if (d.normal.p50[k] !== null) days.push([ms, d.normal.p10[k], d.normal.p50[k], d.normal.p90[k]]);
  }
  const vals = [...pts.map((p) => p[1]), ...days.flatMap((x) => [x[1], x[3]])];
  const ticks = niceTicks(Math.min(...vals), Math.max(...vals));
  const y0 = ticks[0], y1 = ticks[ticks.length - 1];
  const sx = (ms) => GW_M.l + ((ms - x0) / (x1 - x0)) * (GW_W - GW_M.l - GW_M.r);
  const sy = (v) => GW_M.t + ((v - y0) / (y1 - y0 || 1)) * (GW_H - GW_M.t - GW_M.b);   // depth grows downwards
  gw.scale = { sx, t };
  const line = pts.map((p, i) => `${i ? "L" : "M"}${sx(t(p[0])).toFixed(1)},${sy(p[1]).toFixed(1)}`).join("");
  const band = days.length > 1 ? `<polygon points="${days.map((x) => `${sx(x[0]).toFixed(1)},${sy(x[1]).toFixed(1)}`).join(" ")} ${days.slice().reverse().map((x) => `${sx(x[0]).toFixed(1)},${sy(x[3]).toFixed(1)}`).join(" ")}" fill="#5f5f5a" fill-opacity="0.15"/>` : "";
  const med = days.length > 1 ? `<path d="${days.map((x, i) => `${i ? "L" : "M"}${sx(x[0]).toFixed(1)},${sy(x[2]).toFixed(1)}`).join("")}" fill="none" stroke="#5f5f5a" stroke-width="1.2" stroke-dasharray="4 3"/>` : "";
  const years = (x1 - x0) / (365 * 86400000);
  const xl = Array.from({ length: 5 }, (_, i) => x0 + (i * (x1 - x0)) / 4);
  const fmtX = (ms) => { const s = new Date(ms).toISOString(); return years > 3 ? s.slice(0, 4) : s.slice(0, 7); };
  return `<div class="wx-legend"><span class="k"><span class="line" style="background:#2a78d6;height:3px"></span>Depth of the water below ground (m)</span>
      ${d.normal ? `<span class="k"><span class="line" style="background:#5f5f5a;opacity:.25;height:8px"></span>Same day, earlier years: 10th–90th percentile</span>
      <span class="k"><span class="line" style="background:#5f5f5a;height:2px"></span>median</span>` : ""}</div>
    <div class="wx-plot"><svg viewBox="0 0 ${GW_W} ${GW_H}" role="img" aria-label="Groundwater depth">
    ${ticks.map((v) => `<line x1="${GW_M.l}" x2="${GW_W - GW_M.r}" y1="${sy(v)}" y2="${sy(v)}" stroke="#e6e6e3"/>
      <text x="${GW_M.l - 4}" y="${sy(v) + 3}" text-anchor="end">${fmt(v, 1)}</text>`).join("")}
    ${xl.map((ms, i) => `<text x="${sx(ms)}" y="${GW_H - 6}" text-anchor="${i === 0 ? "start" : i === 4 ? "end" : "middle"}">${fmtX(ms)}</text>`).join("")}
    ${band}${med}
    <path d="${line}" fill="none" stroke="#2a78d6" stroke-width="2" stroke-linejoin="round"/>
    <line class="wx-cross" y1="${GW_M.t}" y2="${GW_H - GW_M.b}" stroke="#8a8a86" visibility="hidden"/>
    <rect class="wx-hit" x="${GW_M.l}" y="0" width="${GW_W - GW_M.l - GW_M.r}" height="${GW_H}" fill="transparent"/>
  </svg><div class="wx-tip" hidden></div></div>`;
}

function bindGwHover() {
  const svg = $("#gw-chart .wx-plot svg");
  if (!svg || !gw.scale) return;
  const { sx, t } = gw.scale, d = gw.data, cross = svg.querySelector(".wx-cross"), tip = $("#gw-chart .wx-tip");
  svg.querySelector(".wx-hit").addEventListener("pointermove", (e) => {
    const x = svgX(svg, e);
    let best = null, bd = Infinity;
    for (const p of d.points) { const dd = Math.abs(sx(t(p[0])) - x); if (dd < bd) { bd = dd; best = p; } }
    if (!best) return;
    const bx = sx(t(best[0]));
    cross.setAttribute("x1", bx); cross.setAttribute("x2", bx); cross.setAttribute("visibility", "visible");
    const ms = t(best[0]), k = Math.floor((ms - Date.UTC(new Date(ms).getUTCFullYear(), 0, 1)) / 86400000);
    tip.hidden = false;
    tip.innerHTML = `<b>${esc(best[0])}</b><br>${fmt(best[1], 2)} m below ground${d.normal && d.normal.p50[k] !== null ? `<br>usual: ${fmt(d.normal.p50[k], 2)} m (${fmt(d.normal.p10[k], 1)}–${fmt(d.normal.p90[k], 1)})` : ""}`;
    placeTip(tip, svg, bx, GW_W);
  });
  svg.querySelector(".wx-hit").addEventListener("pointerleave", () => { cross.setAttribute("visibility", "hidden"); tip.hidden = true; });
}

// ---------------------------------------------------------------------------
// Tabs, popups, start-up
// ---------------------------------------------------------------------------
// ---------------------------------------------------------------------------
// Rain radar on the map (Layers tab): animation of the last hours
// ---------------------------------------------------------------------------
const rr = { coords: null, rv: [], info: null, seq: 0, observed: 0, player: null };
const RR_STEP_MS = 350;

function rrTime(iso) {          // UTC "2026-11-20T15:15" -> local "Fri 16:15"
  const d = new Date(`${iso}Z`);
  const today = new Date().toDateString() === d.toDateString();
  return d.toLocaleString(undefined, { weekday: today ? undefined : "short", hour: "2-digit", minute: "2-digit" });
}

async function initRainRadar() {
  const info = rr.info = await api("/api/radar/info");
  const opts = info.sources.map((s) => `<option value="${s.source}">${esc(s.label)}${s.available ? "" : s.needs ? " — needs a key" : " — no data yet"}</option>`);
  opts.push('<option value="rainviewer">World, RainViewer, about 1 km, every 10 minutes, last 2 hours</option>');
  $("#rr-source").innerHTML = '<option value="">— none —</option>' + opts.join("");
  $("#wx-rsource").innerHTML = info.sources.map((s) => `<option value="${s.source}" ${s.available ? "" : "disabled"}>${esc(s.label.split(",")[0])}${s.available ? "" : " (no data)"}</option>`).join("");
  $("#wx-rsource").value = (info.sources.find((s) => s.available) || info.sources[0]).source;
  rr.player = imagePlayer({ slider: "#rr-frame", button: "#rr-play", stepMs: RR_STEP_MS, show: showRainFrame,
    holdAt: (i) => i === rr.player.frames.length - 1 || i === rr.observed - 1 });     // pauses on "now" and at the end
  for (const id of ["#rr-source", "#rr-hours", "#rr-nowcast"]) $(id).addEventListener("change", loadRainRadar);
  $("#rr-opacity").addEventListener("input", () => rr.player.show(rr.player.i));
  $("#wx-rsource").addEventListener("change", () => updateRadar());
}

function removeRainRadar() {
  rr.player.clear();
  for (const id of ["rr", ...rr.rv.map((_, k) => `rv-${k}`)]) removeLayerAndSource(id);
  rr.rv = [];
}

async function loadRainRadar() {
  const seq = ++rr.seq;
  removeRainRadar();
  const src = $("#rr-source").value;
  $("#rr-controls").hidden = !src;
  $("#rr-status").innerHTML = "";
  if (!src) return;
  if (src === "rainviewer") return loadRainViewer(seq);
  const s = rr.info.sources.find((x) => x.source === src);
  if (!s.available) {
    $("#rr-controls").hidden = true;
    $("#rr-status").innerHTML = s.needs ? `Needs ${esc(s.needs)}.` : "No image yet: it is fetched every 5 minutes when the dataset is in update or live mode (Data tab).";
    return;
  }
  // The nowcast (fetched alongside) continues after the latest radar image, on its own grid
  const [d, nc] = await Promise.all([api(`/api/radar/frames?source=${src}&hours=${$("#rr-hours").value}`),
    $("#rr-nowcast").checked ? api("/api/nowcast/frames").catch(() => null) : null]);
  if (seq !== rr.seq) return;
  const observed = rr.observed = d.frames.length, last = observed - 1;
  const ahead = nc ? nc.frames.filter((f) => f.time > d.frames[last].time).map((f) => ({ ...f, coords: nc.coordinates })) : [];
  const frames = d.frames.concat(ahead);
  rr.coords = d.coordinates;
  rr.preload = preload(frames);
  setImageLayer("rr", frames[last].url, d.coordinates, Number($("#rr-opacity").value));
  rr.player.load(frames, last);          // start on the latest observed image
  renderRainLegend(d.intensity);
  $("#rr-status").innerHTML = `${esc(d.attribution)}. ${observed} images, ${rrTime(d.frames[0].time)} to ${rrTime(d.frames[last].time)} (local time).
    ${ahead.length ? `Then ${ahead.length} forecast frames to ${rrTime(ahead[ahead.length - 1].time)}: ${esc(nc.attribution)}, run ${rrTime(nc.run)}
      (marked “forecast”; a model, not a measurement).` : $("#rr-nowcast").checked ? "No nowcast available (Data tab: “Nowcast”)." : ""}
    The site and its surroundings only.`;
}

async function loadRainViewer(seq) {
  let d;
  try { d = await fetch("https://api.rainviewer.com/public/weather-maps.json").then((r) => r.json()); }
  catch (e) { $("#rr-status").textContent = "RainViewer is not reachable."; return; }
  if (seq !== rr.seq) return;
  const past = d.radar.past;
  rr.rv = past;
  rr.observed = past.length;
  past.forEach((p, k) => {
    map.addSource(`rv-${k}`, { type: "raster", tiles: [`${d.host}${p.path}/256/{z}/{x}/{y}/2/1_1.png`], tileSize: 256, maxzoom: 7,
      attribution: '<a href="https://www.rainviewer.com/">RainViewer</a>' });
    map.addLayer({ id: `rv-${k}`, type: "raster", source: `rv-${k}`,
      paint: { "raster-opacity": 0, "raster-fade-duration": 0 } }, firstVectorLayer);
  });
  const frames = past.map((p) => ({ time: new Date(p.time * 1000).toISOString().slice(0, 16) }));
  rr.player.load(frames, frames.length - 1);
  $("#rr-legend").innerHTML = "";
  $("#rr-status").innerHTML = `RainViewer (worldwide composite, free tiles up to zoom 7: coarse at the plot scale), ${past.length} images, ${rrTime(frames[0].time)} to ${rrTime(frames[past.length - 1].time)}. The period is always the last 2 hours.`;
}

function showRainFrame(f, i) {
  $("#rr-time").innerHTML = `${esc(rrTime(f.time))}${f.forecast ? ' <span class="rr-fc">forecast</span>' : ""}`;
  const op = Number($("#rr-opacity").value);
  if (rr.rv.length) {
    rr.rv.forEach((_, k) => map.getLayer(`rv-${k}`) && map.setPaintProperty(`rv-${k}`, "raster-opacity", k === i ? op : 0));
  } else if (map.getSource("rr")) {
    map.getSource("rr").updateImage({ url: f.url, coordinates: f.coords || rr.coords });
    map.setPaintProperty("rr", "raster-opacity", op);
  }
}

function renderRainLegend(intensity, target = "#rr-legend", title = "Rain intensity (mm/h)") {
  $(target).innerHTML = `<div class="title">${esc(title)}</div><div class="rr-scale">${intensity.map((c) =>
    `<div><span style="background:${c.color}"></span>${c.min_mm_h < 1 ? c.min_mm_h : fmt(c.min_mm_h)}</div>`).join("")}</div>`;
}

// ---------------------------------------------------------------------------
// Weather tab, radar view: where it rains and has been raining over the last hours
// ---------------------------------------------------------------------------
const RADAR_COLOR = "#2a78d6";
const rad = { info: null, points: [], imageId: null, timer: null };
const RD_W = 380, RD_H = 170, RD_M = { l: 34, r: 8, t: 8, b: 22 };

function removeRadarImage() {
  removeLayerAndSource("radar-image");
  $("#wx-rlegend").innerHTML = "";
}

async function updateRadar() {
  const seq = ++wx.seq;
  clearTimeout(rad.timer);
  $("#wx-compare").innerHTML = "";
  const source = $("#wx-rsource").value || "opera";
  const info = rad.info = await api(`/api/radar/info?source=${source}`);
  if (seq !== wx.seq) return;
  if (!info.available) {
    removeRadarImage();
    $("#wx-chart").innerHTML = `<p class="muted">${info.needs ? `The rain radar needs ${esc(info.needs)}.`
      : info.mode === "frozen" ? "This radar is frozen and nothing is stored." : "No radar image yet: the scheduler fetches them every 5 minutes."}
      Its mode is set on the Data tab.</p>`;
    return;
  }
  const keep = Number($("#wx-rhours").value) || 3;
  $("#wx-rhours").innerHTML = info.hours.map((h) => `<option value="${h}">${h} hour${h > 1 ? "s" : ""}</option>`).join("");
  $("#wx-rhours").value = info.hours.includes(keep) ? keep : info.hours[info.hours.length - 1];
  const hours = Number($("#wx-rhours").value);
  const d = await api(`/api/radar/series?source=${source}&area=${encodeURIComponent(currentArea)}`);
  if (seq !== wx.seq) return;
  const lastT = Date.parse(`${info.last}Z`);
  rad.points = d.points.filter((p) => Date.parse(`${p.time}Z`) > lastT - hours * 3600000);
  const a = areaOf(currentArea);
  $("#wx-location").innerHTML = `Radar rain over <b>${esc(a.name)}</b> (${fmt(d.cells)} cells of 500 m), latest image ${esc(info.last.replace("T", " "))} UTC.`;
  $("#wx-chart").innerHTML = radarChart(hours);
  bindRadarHover();
  $("#wx-compare").innerHTML = `<table class="kv"><tr><th>Last</th><th class="num">Mean over the area</th><th class="num">Wettest cell</th><th class="num">Images</th></tr>
    ${d.totals.map((t) => `<tr><td>${t.hours} h</td><td class="num">${fmt(t.mean_mm, 1)} mm</td><td class="num">${fmt(t.max_mm, 1)} mm</td>
      <td class="num">${t.images} / ${t.expected}</td></tr>`).join("")}</table>
    <div class="muted" style="font-size:11px">${esc(info.attribution)}. Missing images count as dry (Météo-France's advice); the images column shows the coverage.
    ${info.mode === "live" ? `Live mode: the last ${info.keep_hours} hours are kept.` : info.mode === "frozen" ? "Frozen: no new images." : "All images are kept."}</div>`;
  showRadarImage(hours, source);
  api(`/api/nowcast/series?area=${encodeURIComponent(currentArea)}`).then((n) => {
    if (seq === wx.seq) $("#wx-chart").insertAdjacentHTML("beforeend", nowcastChart(n));
  }).catch(() => {});
  rad.timer = setTimeout(() => { if (wxViewActive("radar")) updateRadar(); }, 5 * 60000);
}

function radarChart(hours) {
  const pts = rad.points;
  if (!pts.length) return '<p class="muted">No image over this period.</p>';
  const t = (p) => Date.parse(`${p.time}Z`);
  const x1 = t(pts[pts.length - 1]), x0 = x1 - hours * 3600000;
  const hi = Math.max(0.5, ...pts.map((p) => p.mean));
  const ticks = niceTicks(0, hi);
  const y1 = ticks[ticks.length - 1];
  const sx = (ms) => RD_M.l + ((ms - x0) / (x1 - x0 || 1)) * (RD_W - RD_M.l - RD_M.r);
  const sy = (v) => RD_H - RD_M.b - (v / (y1 || 1)) * (RD_H - RD_M.t - RD_M.b);
  rad.scale = { sx, sy, t };
  const bw = Math.max(1, (RD_W - RD_M.l - RD_M.r) / (hours * 12) - 0.5);
  const xl = Array.from({ length: 5 }, (_, i) => x0 + (i * (x1 - x0)) / 4);
  const hhmm = (ms) => new Date(ms).toISOString().slice(11, 16);
  return `<div class="muted">Rain in each 5-minute image, mean over the area (mm). Times in UTC.</div>
    <div class="wx-plot"><svg viewBox="0 0 ${RD_W} ${RD_H}" role="img" aria-label="Radar rain over the last hours">
    ${ticks.map((v) => `<line x1="${RD_M.l}" x2="${RD_W - RD_M.r}" y1="${sy(v)}" y2="${sy(v)}" stroke="#e6e6e3"/>
      <text x="${RD_M.l - 4}" y="${sy(v) + 3}" text-anchor="end">${fmt(v, 1)}</text>`).join("")}
    ${xl.map((ms, i) => `<text x="${sx(ms)}" y="${RD_H - 6}" text-anchor="${i === 0 ? "start" : i === 4 ? "end" : "middle"}">${hhmm(ms)}</text>`).join("")}
    ${pts.filter((p) => p.mean > 0).map((p) => `<rect x="${(sx(t(p)) - bw).toFixed(1)}" y="${sy(p.mean).toFixed(1)}" width="${bw.toFixed(1)}"
      height="${(sy(0) - sy(p.mean)).toFixed(1)}" fill="${RADAR_COLOR}" rx="1"/>`).join("")}
    <line x1="${RD_M.l}" x2="${RD_W - RD_M.r}" y1="${sy(0)}" y2="${sy(0)}" stroke="#8a8a86"/>
    <line class="wx-cross" y1="${RD_M.t}" y2="${RD_H - RD_M.b}" stroke="#8a8a86" visibility="hidden"/>
    <rect class="wx-hit" x="${RD_M.l}" y="0" width="${RD_W - RD_M.l - RD_M.r}" height="${RD_H}" fill="transparent"/>
  </svg><div class="wx-tip" hidden></div></div>`;
}

// The next 6 hours over the area (AROME-PI): rain per 15 minutes as bars, temperature as a line
function nowcastChart(n) {
  if (!n.rain.length) return "";
  const temp = Object.fromEntries(n.temperature.map((p) => [p.time, p.mean]));
  const gusts = Object.fromEntries(n.gust_kmh.map((g) => [g.time, g.max]));
  const hourly = n.temperature.slice(1).map((p) => {
    const end = Date.parse(`${p.time}Z`);
    const rain = n.rain.filter((q) => { const t = Date.parse(`${q.time}Z`); return t > end - 3600000 && t <= end; })
      .reduce((a, q) => a + (q.mean || 0), 0);
    return `<tr><td>${esc(localTime(p.time, { hour: "2-digit", minute: "2-digit" }))}</td><td class="num">${fmt(rain, 1)}</td>
      <td class="num">${fmt(p.mean, 1)}</td><td class="num">${fmt(gusts[p.time])}</td></tr>`;
  }).join("");
  return `<h3 style="margin:14px 0 2px">Next 6 hours (forecast)</h3>
    <div class="muted">Over the area (${n.cells} cell${n.cells > 1 ? "s" : ""} of 0.01°). ${esc(n.attribution)}, run ${esc(rrTime(n.run))} local time.
      Expected total: ${fmt(n.total.mean_mm, 1)} mm (wettest cell ${fmt(n.total.max_mm, 1)} mm).</div>
    ${timeChart({ times: n.rain.map((p) => p.time), leftUnit: "°C", rightUnit: "mm",
      lines: [{ label: "Temperature at 2 m (°C)", color: "#c2407a", values: n.rain.map((p) => temp[p.time] ?? null) }],
      bars: { label: "Rain in 15 minutes, mean (mm)", color: "#7b4fd6", values: n.rain.map((p) => p.mean) } })}
    <table class="kv"><tr><th>Hour ending</th><th class="num">Rain (mm)</th><th class="num">Temperature (°C)</th><th class="num">Max gust (km/h)</th></tr>
    ${hourly}</table>`;
}

function bindRadarHover() {
  const svg = $("#wx-chart .wx-plot svg");
  if (!svg || !rad.scale) return;
  const { sx, t } = rad.scale, cross = svg.querySelector(".wx-cross"), tip = $("#wx-chart .wx-tip");
  svg.querySelector(".wx-hit").addEventListener("pointermove", (e) => {
    const x = svgX(svg, e);
    let best = null, bd = Infinity;
    for (const p of rad.points) { const dd = Math.abs(sx(t(p)) - x); if (dd < bd) { bd = dd; best = p; } }
    if (!best) return;
    const bx = sx(t(best));
    cross.setAttribute("x1", bx); cross.setAttribute("x2", bx); cross.setAttribute("visibility", "visible");
    tip.hidden = false;
    tip.innerHTML = `<b>${esc(best.time.slice(11))} UTC</b><br>mean ${fmt(best.mean, 2)} mm · wettest cell ${fmt(best.max, 1)} mm<br>
      rain on ${fmt(100 * best.wet_share)} % of the area${best.valid < 0.99 ? `<br>${fmt(100 * (1 - best.valid))} % without data` : ""}`;
    placeTip(tip, svg, bx, RD_W);
  });
  svg.querySelector(".wx-hit").addEventListener("pointerleave", () => { cross.setAttribute("visibility", "hidden"); tip.hidden = true; });
}

async function showRadarImage(hours, source) {
  const info = await api(`/api/radar/accum/info?hours=${hours}&source=${source}`);
  if (!wxViewActive("radar")) return;
  removeRadarImage();
  setImageLayer("radar-image", info.url, info.coordinates, Number($("#wx-ropacity").value));
  $("#wx-rlegend").innerHTML = `<div class="title">Rain over the last ${hours} h (mm), to ${esc(info.end.replace("T", " "))} UTC ·
      ${info.images} / ${info.expected} images · dry areas transparent</div>
    ${rampLegend(info.legend, ["0.1", fmt(info.vmax / 2, 0), `${fmt(info.vmax, 0)}+`])}`;
}

$("#wx-rhours").addEventListener("change", () => updateRadar());
$("#wx-ropacity").addEventListener("input", (e) => {
  if (map.getLayer("radar-image")) map.setPaintProperty("radar-image", "raster-opacity", Number(e.target.value));
});

// ---------------------------------------------------------------------------
// Météo-France: warnings banner, observations, AROME maps, PE-AROME, waves and surge
// ---------------------------------------------------------------------------
const localTime = (iso, opts = { weekday: "short", hour: "2-digit", minute: "2-digit" }) =>
  new Date(`${iso}Z`).toLocaleString(undefined, opts);
const VIG_DAY = { J: "today", J1: "tomorrow" };
// The API's answer, or null after writing why there is none in the chart area
async function loadOrShow(path) {
  try { return await api(path); }
  catch (e) { $("#wx-chart").innerHTML = `<p class="muted">${esc(e.message)}</p>`; return null; }
}

async function initAlerts() {
  let d;
  try { d = await api("/api/mf/vigilance"); } catch (e) { return; }
  const v = d.vigilance;
  const parts = [];
  if (v) {
    for (const dep of v.departements) {
      const days = dep.periods.map((p) => {
        const warn = p.phenomena.filter((x) => x.color.id > 1);
        return `<span class="vig-chip" style="background:${p.max_color.hex}" title="${esc(warn.map((x) => `${x.name}: ${x.color.name}`).join(", ") || "No particular vigilance")}">
          ${esc(VIG_DAY[p.echeance] || p.echeance)}${warn.length ? `: ${esc(warn.map((x) => x.name.toLowerCase()).join(", "))}` : ""}</span>`;
      }).join("");
      parts.push(`<div class="vig-dep"><b>Vigilance ${esc(dep.code)}</b>${dep.coastal ? ' <span class="muted">(with the coast)</span>' : ""} ${days}</div>`);
    }
  } else if (d.vigilance_needs) parts.push(`<div class="muted">Vigilance: ${esc(d.vigilance_needs)}</div>`);
  for (const f of d.forets) {
    if (f.status === "published") {
      parts.push(`<div class="vig-dep"><b>Fire danger ${esc(f.departement)}</b> ${f.levels.map((l) =>
        `<span class="vig-chip" style="background:${l.hex}">${esc(String(l.day ?? ""))} ${esc(l.name)}</span>`).join("")}</div>`);
    } else if (f.status === "none") {
      parts.push(`<div class="vig-dep muted">Fire danger ${esc(f.departement)}: no forecast published (Météo des forêts runs during the fire season)</div>`);
    }
  }
  const texts = v ? v.departements.flatMap((dep) => dep.texts.map((t) => ({ dep: dep.code, ...t }))) : [];
  const details = (v?.comment?.length || texts.length) ? `<details><summary>Bulletins</summary>
      ${v.comment.map((c) => `<p>${esc(c)}</p>`).join("")}
      ${texts.map((t) => `<p><b>${esc(t.dep)} · ${esc(t.hazard || "")} (${esc(VIG_DAY[t.term] || t.term || "")}, ${esc(t.risk || "")})</b><br>
        ${t.paragraphs.map((p) => `${p.title ? `<i>${esc(p.title)}</i> ` : ""}${esc(p.text)}`).join("<br>")}</p>`).join("")}</details>` : "";
  $("#alerts").innerHTML = parts.join("") + (v ? `<div class="muted" style="font-size:11px">Météo-France Vigilance, ${esc(localTime(v.updated.slice(0, 16)))}</div>` : "") + details;
  $("#alerts").hidden = !parts.length;
}

async function initMeteoFrance() {
  am.player = imagePlayer({ slider: "#wx-mframe", button: "#wx-mplay", stepMs: 450, show: (f) => {
    $("#wx-mtime").textContent = localTime(f.time);
    if (map.getSource("arome")) map.getSource("arome").updateImage({ url: f.url, coordinates: am.coords });
  } });
  $("#wx-ostation").addEventListener("change", renderObs);
  $("#wx-ohours").addEventListener("change", updateObs);
  $("#wx-mvar").addEventListener("change", updateMaps);
  $("#wx-mopacity").addEventListener("input", () => map.getLayer("arome") && map.setPaintProperty("arome", "raster-opacity", Number($("#wx-mopacity").value)));
  try {      // the sea view only for a coastal site
    if ((await api("/api/mf/sea")).coastal === false) $('#wx-view option[value="sea"]').remove();
  } catch (e) { /* kept: the view says why */ }
}

// A time chart: bars (right axis) and lines (left axis), local times
const TC_W = 380, TC_H = 180, TC_M = { l: 34, r: 34, t: 8, b: 22 };
function timeChart({ times, lines = [], bars = null, leftUnit = "", rightUnit = "", label = "" }) {
  if (!times.length) return '<p class="muted">No data over this period.</p>';
  const t = times.map((s) => Date.parse(`${s}Z`));
  const x0 = Math.min(...t), x1 = Math.max(...t);
  const lv = lines.flatMap((l) => l.values).filter((v) => v !== null);
  let lo = lv.length ? Math.min(...lv) : 0, hi = lv.length ? Math.max(...lv) : 1;
  if (hi - lo < 1) { lo -= 0.5; hi += 0.5; }
  const lt = niceTicks(lo, hi);
  const bmax = bars ? Math.max(0.5, ...bars.values.filter((v) => v !== null)) : 1;
  const sx = (ms) => TC_M.l + ((ms - x0) / (x1 - x0 || 1)) * (TC_W - TC_M.l - TC_M.r);
  const sy = (v) => TC_H - TC_M.b - ((v - lt[0]) / (lt[lt.length - 1] - lt[0] || 1)) * (TC_H - TC_M.t - TC_M.b);
  const sb = (v) => TC_H - TC_M.b - (v / bmax) * (TC_H - TC_M.t - TC_M.b);
  const bw = Math.max(1.5, (TC_W - TC_M.l - TC_M.r) / times.length - 1);
  // ticks: each midnight over a few days, the first of each month over longer spans
  const monthly = x1 - x0 > 21 * 86400e3;
  const days = [];
  for (let d = new Date(x0); d.getTime() <= x1; monthly ? d.setMonth(d.getMonth() + 1, 1) : d.setHours(24, 0, 0, 0)) {
    if (monthly) d.setHours(0, 0, 0, 0);
    if (d.getTime() > x0) days.push(d.getTime());
  }
  const tick = monthly ? { month: "short" } : { weekday: "short", day: "numeric" };

  return `${label ? `<div class="muted">${label}</div>` : ""}<div class="wx-legend">${lines.filter((l) => l.label).map((l) =>
      `<span class="k"><span class="${l.dots ? "dot" : "line"}" style="background:${l.color}"></span>${esc(l.label)}</span>`).join("")}
    ${bars ? `<span class="k"><span class="line" style="background:${bars.color};height:8px"></span>${esc(bars.label)}</span>` : ""}</div>
    <div class="wx-plot"><svg viewBox="0 0 ${TC_W} ${TC_H}" role="img" aria-label="${esc(label || "Chart")}">
    ${yGrid(lt, sy, TC_M, TC_W)}
    ${bars ? [0, bmax / 2, bmax].map((v) => `<text x="${TC_W - TC_M.r + 3}" y="${sb(v) + 3}" fill="${bars.color}">${fmt(v, bmax < 5 ? 1 : 0)}</text>`).join("") : ""}
    ${days.map((ms) => `<line x1="${sx(ms)}" x2="${sx(ms)}" y1="${TC_M.t}" y2="${TC_H - TC_M.b}" stroke="#c9c9c4" stroke-dasharray="2 3"/>
      <text x="${sx(ms) + 3}" y="${TC_H - 6}">${new Date(ms).toLocaleDateString(undefined, tick)}</text>`).join("")}
    ${bars ? bars.values.map((v, i) => v ? `<rect x="${(sx(t[i]) - bw).toFixed(1)}" y="${sb(v).toFixed(1)}" width="${bw.toFixed(1)}" height="${(sb(0) - sb(v)).toFixed(1)}"
      fill="${bars.color}" opacity="0.8"><title>${esc(localTime(times[i]))}: ${fmt(v, 1)} ${esc(rightUnit)}</title></rect>` : "").join("") : ""}
    ${lines.map((l) => l.dots
      ? l.values.map((v, i) => v === null ? "" : `<circle cx="${sx(t[i]).toFixed(1)}" cy="${sy(v).toFixed(1)}" r="1.6" fill="${l.color}"/>`).join("")
      : `<path d="${svgPath(l.values, (i) => sx(t[i]), sy)}" fill="none" stroke="${l.color}" stroke-width="2" ${l.dash ? `stroke-dasharray="${l.dash}"` : ""}/>`).join("")}
    <text x="${TC_M.l}" y="${TC_M.t + 2}" font-size="9" fill="#6a737d">${esc(leftUnit)}</text>
  </svg></div>`;
}

// ---- Observations of the last days
const mfo = { d: null };
async function updateObs() {
  const seq = ++wx.seq;
  $("#wx-compare").innerHTML = "";
  const d = await loadOrShow(`/api/mf/obs?hours=${$("#wx-ohours").value}`);
  if (!d || seq !== wx.seq) return;
  mfo.d = d;
  const keep = $("#wx-ostation").value;
  $("#wx-ostation").innerHTML = d.stations.map((s) => `<option value="${esc(s.id)}">${esc(s.name)} · ${fmt(s.distance_km, 1)} km</option>`).join("");
  if (d.stations.some((s) => s.id === keep)) $("#wx-ostation").value = keep;
  $("#wx-location").innerHTML = `Hourly observations of the Météo-France stations within ${d.radius_km} km (local times).
    ${d.mode === "live" ? `Live mode: the last ${d.keep_hours} hours are kept.` : ""}`;
  renderObs();
}

function renderObs() {
  const d = mfo.d;
  if (!d) return;
  const s = d.stations.find((x) => x.id === $("#wx-ostation").value) || d.stations[0];
  const se = s.series, kmh = (a) => a && a.map((v) => v === null ? null : v * 3.6);
  const lines = [];
  if (se.t) lines.push({ label: "Temperature (°C)", color: "#c2407a", values: se.t });
  if (se.t_dew) lines.push({ label: "Dew point (°C)", color: "#c2407a", dash: "3 3", values: se.t_dew });
  let html = timeChart({ times: d.times, lines, leftUnit: "°C", rightUnit: "mm", label: `${esc(s.name)}, ${fmt(s.distance_km, 1)} km from the site centre`,
    bars: se.precip ? { label: "Rain in the hour (mm)", color: "#2a78d6", values: se.precip } : null });
  if (se.gust || se.wind) html += timeChart({ times: d.times, leftUnit: "km/h",
    lines: [se.wind && { label: "Mean wind (km/h)", color: "#1baf7a", values: kmh(se.wind) },
            se.gust && { label: "Gusts (km/h)", color: "#eb6834", values: kmh(se.gust) }].filter(Boolean) });
  if (se.rh) html += timeChart({ times: d.times, leftUnit: "%", lines: [{ label: "Relative humidity (%)", color: "#7b4fd6", values: se.rh }] });
  $("#wx-chart").innerHTML = html;
  const L = (st, v, k = 1, scale = 1) => st.latest[v] ? fmt(st.latest[v].value * scale, k) : "–";
  $("#wx-compare").innerHTML = `<table class="kv"><tr><th>Station</th><th>Last</th><th class="num">T °C</th><th class="num">RH %</th>
      <th class="num">Gust km/h</th><th class="num" title="Rain over the last 24 hours">Rain 24 h (mm)</th><th class="num" title="Minimum and maximum over the last 24 hours">Min / max 24 h</th></tr>
    ${d.stations.map((st) => `<tr><td>${esc(st.name)} <span class="muted">${fmt(st.distance_km, 1)} km</span></td>
      <td>${st.latest.t ? esc(localTime(st.latest.t.time, { hour: "2-digit", minute: "2-digit" })) : "–"}</td>
      <td class="num">${L(st, "t")}</td><td class="num">${L(st, "rh", 0)}</td><td class="num">${L(st, "gust", 0, 3.6)}</td>
      <td class="num">${fmt(st.last24h.precip, 1)}</td>
      <td class="num">${fmt(st.last24h.t_min, 1)} / ${fmt(st.last24h.t_max, 1)}</td></tr>`).join("")}</table>
    <div class="muted" style="font-size:11px">${esc(d.source || "")}. Not all stations measure everything (many measure rain and temperature only).</div>`;
}

// ---- AROME forecast maps
const am = { coords: null, player: null };
function removeAromeImage() {
  am.player?.clear();
  removeLayerAndSource("arome");
  $("#wx-mlegend").innerHTML = "";
}

async function updateMaps() {
  const seq = ++wx.seq;
  $("#wx-compare").innerHTML = "";
  $("#wx-chart").innerHTML = "";
  const d = await loadOrShow(`/api/mf/arome/frames?var=${$("#wx-mvar").value}`);
  if (!d) { removeAromeImage(); return; }
  if (seq !== wx.seq || !wxViewActive("maps")) return;
  removeAromeImage();
  am.coords = d.coordinates;
  am.preload = preload(d.frames);
  setImageLayer("arome", d.frames[0].url, d.coordinates, Number($("#wx-mopacity").value));
  am.player.load(d.frames, 0);
  if (d.intensity) renderRainLegend(d.intensity, "#wx-mlegend", "Rain (mm/h; 3-hour means after the first day)");
  else $("#wx-mlegend").innerHTML = `<div class="title">${esc(d.label)}</div>${rampLegend(d.legend, [d.vmin, (d.vmin + d.vmax) / 2, d.vmax].map((v) => fmt(v)))}`;
  $("#wx-location").innerHTML = `${esc(d.attribution)}, run of ${esc(localTime(d.run))} (local time): ${d.frames.length} hours.
    The site and ${d.margin_km} km around; drag the slider or play.`;
}

// ---- PE-AROME probabilities
const probBar = (p) => `<span class="pbar"><span style="width:${Math.round(100 * p)}%"></span></span> ${fmt(100 * p)} %`;
async function updateEnsemble() {
  const seq = ++wx.seq;
  $("#wx-compare").innerHTML = "";
  const e = await loadOrShow("/api/mf/ensemble");
  if (!e || seq !== wx.seq) return;
  const dawn = e.frost[0]?.hours_local || [4, 8];
  $("#wx-location").innerHTML = `${esc(e.source)}, run of ${esc(localTime(e.run.slice(0, 16)))}: ${e.members ?? 25 - e.members_failed.length} members. Over the site's extent
    (mean of the 2.5 km cells, and the wettest or coldest cell).`;
  const rain = e.rain.map((w) => `<h3>Rain from ${esc(localTime(w.start))} to ${esc(localTime(w.end))}</h3>
    <table class="kv"><tr><th>At least</th><th>Area mean</th><th>Wettest cell</th></tr>
    ${e.rain_thresholds_mm.map((t) => `<tr><td>${t} mm</td><td>${probBar(w.area_mean.probability[t])}</td><td>${probBar(w.wettest_cell.probability[t])}</td></tr>`).join("")}
    <tr><td class="muted">Members</td><td colspan="2" class="muted">10 % ${fmt(w.area_mean.p10, 1)} mm · median ${fmt(w.area_mean.p50, 1)} mm · 90 % ${fmt(w.area_mean.p90, 1)} mm (range ${fmt(w.area_mean.min, 1)}–${fmt(w.area_mean.max, 1)})</td></tr></table>`).join("");
  const frost = e.frost.map((f) => `<h3>Frost on the morning of ${esc(new Date(f.morning).toLocaleDateString(undefined, { weekday: "long", day: "numeric", month: "long" }))}</h3>
    <table class="kv"><tr><th>Minimum at most</th><th>Area mean</th><th>Coldest cell</th></tr>
    ${e.frost_thresholds_c.map((t) => `<tr><td>${t} °C</td><td>${probBar(f.area_mean.probability[t])}</td><td>${probBar(f.coldest_cell.probability[t])}</td></tr>`).join("")}
    <tr><td class="muted">Members</td><td colspan="2" class="muted">10 % ${fmt(f.area_mean.p10, 1)} °C · median ${fmt(f.area_mean.p50, 1)} °C · 90 % ${fmt(f.area_mean.p90, 1)} °C</td></tr></table>`).join("");
  $("#wx-chart").innerHTML = rain + frost + `<p class="muted" style="font-size:11px">A probability is the share of the members reaching the threshold.
    Frost: the lowest hourly minimum at 2 m between ${dawn[0] - 1}:00 and ${dawn[dawn.length - 1]}:00 local time. Air temperature: ground frost can occur above 0 °C.</p>`;
}

// ---- Waves and storm surge
async function updateSea() {
  const seq = ++wx.seq;
  $("#wx-compare").innerHTML = "";
  const s = await loadOrShow("/api/mf/sea");
  if (!s || seq !== wx.seq) return;
  if (!s.available) {
    $("#wx-chart").innerHTML = `<p class="muted">${s.coastal === false ? `This site is not on the coast (no sea cell within ${s.max_km} km).` :
      s.needs ? `Needs ${esc(s.needs)}.` : "Not fetched yet: the scheduler reads each new run."}</p>`;
    return;
  }
  const wc = s.wave_cells[0], sc = s.surge_cells[0];
  $("#wx-location").innerHTML = `${esc(s.source)}. Waves at ${s.wave_cells.length} sea cells, the nearest ${fmt(wc[2], 1)} km from the site
    (run ${esc(localTime(s.waves_run))})${sc ? `; surge at ${fmt(sc[2], 1)} km (run ${esc(localTime(s.surge_run))})` : ""}.`;
  $("#wx-chart").innerHTML = timeChart({ times: s.waves.times, leftUnit: "m", label: "Significant wave height (m)",
      lines: [{ label: "Wave height Hs (m)", color: "#2a78d6", values: s.waves.hs }] })
    + (s.surge.times.length ? timeChart({ times: s.surge.times, leftUnit: "m", label: "Storm surge: sea level above the predicted tide (m)",
      lines: [{ label: "Surge (m)", color: "#7b4fd6", values: s.surge.values }] }) : "");
  const arrow = (deg) => deg === null ? "–" : `<span style="display:inline-block;transform:rotate(${deg + 180}deg)">↑</span> ${fmt(deg)}°`;
  $("#wx-compare").innerHTML = `<table class="kv"><tr><th>Time</th><th class="num">Hs (m)</th><th class="num">Peak period (s)</th><th>From</th></tr>
    ${s.waves.times.map((t, i) => `<tr><td>${esc(localTime(t))}</td><td class="num">${fmt(s.waves.hs[i], 2)}</td><td class="num">${fmt(s.waves.tp[i], 1)}</td>
      <td>${arrow(s.waves.dir[i])}</td></tr>`).join("")}</table>
    <div class="muted" style="font-size:11px">The surge is the anomaly due to wind and pressure, to add to the tide (not provided).</div>`;
}

// ---------------------------------------------------------------------------
// Vegetation tab: dry matter productivity (Copernicus, 300 m, every 10 days)
// ---------------------------------------------------------------------------
const dmp = { seq: 0 };
async function updateDMP() {
  const seq = ++dmp.seq;
  if (!currentArea) return;
  let d;
  try {
    const y = $("#dmp-year").value;
    d = await api(`/api/clms/dmp?area=${encodeURIComponent(currentArea)}${y ? `&year=${y}` : ""}`);
  } catch (e) { $("#dmp-chart").innerHTML = `<p class="muted">${esc(e.message)}</p>`; return; }
  if (seq !== dmp.seq) return;
  const keep = d.year;
  $("#dmp-year").innerHTML = [...d.years].reverse().map((y) => `<option value="${y}">${y}</option>`).join("");
  $("#dmp-year").value = keep;
  const times = d.dekads.map((md) => `${d.year}-${md}T00:00`);
  const ry = d.reference_years;
  const refLabel = ry.length ? `${ry[0]}–${ry[ry.length - 1]}` : "other years";
  const cur = d.cumulative.filter((v) => v !== null);
  $("#dmp-chart").innerHTML = timeChart({ times, leftUnit: "kg/ha/d", label: `Biomass growth every 10 days, kg of dry matter per hectare and day (mean of ${d.cells} cell${d.cells > 1 ? "s" : ""} of 300 m)`,
      lines: [{ label: `${d.year}`, color: "#1baf7a", values: d.values },
              { label: `Mean ${refLabel}`, color: "#8a8a86", values: d.reference.mean },
              { label: "Lowest / highest", color: "#c9c9c4", dash: "2 3", values: d.reference.min },
              { label: "", color: "#c9c9c4", dash: "2 3", values: d.reference.max }] })
    + timeChart({ times, leftUnit: "t/ha", label: "Dry matter produced since 1 January (t/ha)",
      lines: [{ label: `${d.year}`, color: "#1baf7a", values: d.cumulative },
              { label: `Mean ${refLabel}`, color: "#8a8a86", values: d.cumulative_reference.mean },
              { label: "Lowest / highest", color: "#c9c9c4", dash: "2 3", values: d.cumulative_reference.min },
              { label: "", color: "#c9c9c4", dash: "2 3", values: d.cumulative_reference.max }] })
    + `<div class="muted" style="font-size:11px">${cur.length ? `${d.year}: ${fmt(cur[cur.length - 1], 1)} t/ha so far. ` : ""}
       Dry matter productivity (DMP), Copernicus Land Monitoring Service, 300 m: an estimate of the vegetation's growth from its
       absorbed light (all vegetation of the cells, not one crop); compare years and areas rather than reading it as a yield.</div>`;
}
$("#dmp-year").addEventListener("change", updateDMP);

// ---------------------------------------------------------------------------
// Data tab: how each dataset is followed for this site, and actions
// ---------------------------------------------------------------------------
const dataTab = { d: null, timer: null };

async function apiSend(method, path, body) {
  const r = await fetch(path, { method, headers: body ? { "Content-Type": "application/json" } : {},
    body: body ? JSON.stringify(body) : undefined });
  if (!r.ok) throw new Error((await r.json().catch(() => ({}))).detail || `${r.status} ${r.statusText}`);
  return r.json();
}

const fmtBytes = (n) => n === null || n === undefined ? "" : n < 1e6 ? `${fmt(n / 1e3)} KB` : n < 1e9 ? `${fmt(n / 1e6, n < 1e7 ? 1 : 0)} MB` : `${fmt(n / 1e9, 1)} GB`;
function fmtAgo(iso) {
  if (!iso) return "never";
  const s = (Date.now() - Date.parse(iso)) / 1000;
  return s < 90 ? "just now" : s < 5400 ? `${Math.round(s / 60)} min ago` : s < 129600 ? `${Math.round(s / 3600)} h ago` : `${Math.round(s / 86400)} days ago`;
}

async function updateData() {
  clearTimeout(dataTab.timer);
  const d = dataTab.d = await api("/api/datasets");
  const byCat = Object.keys(d.categories).map((c) => [c, d.datasets.filter((x) => x.category === c)]);
  $("#data-list").innerHTML = byCat.map(([c, list]) => `<div class="ds-cat">${esc(d.categories[c])}</div>` + list.map((x) => {
    const req = x.requests.find((r) => r.status === "pending" || r.status === "running");
    const lastReq = x.requests.find((r) => r.status === "failed" || r.status === "done");
    const status = [
      x.needs ? `<span class="need">Needs ${esc(x.needs)}.</span>` : "",
      `Last update: ${fmtAgo(x.last_ok)}`,
      x.mode === "update" && x.refresh_days ? `refreshed every ${x.refresh_days} days` : "",
      x.last_error ? `<span class="err" title="${esc(x.last_error.message)}">last attempt failed ${fmtAgo(x.last_error.at)}</span>` : "",
      req ? `<b>${req.action === "delete" ? "deletion" : "update"} ${req.status === "running" ? "running" : "queued"}…</b>` : "",
      !req && lastReq && Date.now() - Date.parse(lastReq.finished_at) < 3600000
        ? `${lastReq.action} ${lastReq.status === "done" ? "done" : `<span class="err">failed</span>`}${lastReq.message ? `: ${esc(lastReq.message)}` : ""}` : "",
      x.shared ? "shared by the sites" : "",
    ].filter(Boolean).join(" · ");
    return `<div class="ds-row" data-code="${x.code}">
      <div class="ds-head"><b>${esc(x.label)}</b><span class="ds-size">${x.size_pending && x.size_bytes === null ? "measuring…" : fmtBytes(x.size_bytes)}</span></div>
      <div class="ds-desc">${esc(x.describe)}</div>
      <div class="ds-ctrl">
        <select class="ds-mode" aria-label="Mode of ${esc(x.label)}">${x.modes.map((m) => `<option value="${m.mode}" ${m.mode === x.mode ? "selected" : ""}>${esc(m.label)}</option>`).join("")}</select>
        ${x.modes.some((m) => m.mode === "live") ? `<label title="Hours kept in live mode" ${x.mode === "live" ? "" : "hidden"} class="ds-keep"><input type="number" min="1" max="744" value="${x.keep_hours}"> h</label>` : ""}
        <button data-act="update" ${req ? "disabled" : ""}>Update now</button>
        ${x.deletable ? `<button data-act="delete" ${req || x.size_bytes === 0 ? "disabled" : ""}>Delete data</button>` : ""}
      </div>
      <div class="ds-status">${status}</div></div>`;
  }).join("")).join("");
  const busy = d.datasets.some((x) => x.requests.some((r) => r.status === "pending" || r.status === "running"));
  if ((busy || d.datasets.some((x) => x.size_pending)) && $("#tab-data").classList.contains("active")) {
    dataTab.timer = setTimeout(updateData, busy ? 5000 : 15000);
  }
}

$("#data-list").addEventListener("change", async (e) => {
  const row = e.target.closest(".ds-row");
  if (!row) return;
  const code = row.dataset.code;
  try {
    if (e.target.classList.contains("ds-mode")) {
      await apiSend("PUT", `/api/datasets/${code}`, { mode: e.target.value });
    } else if (e.target.type === "number") {
      await apiSend("PUT", `/api/datasets/${code}`, { mode: row.querySelector(".ds-mode").value, keep_hours: Number(e.target.value) });
    }
  } catch (err) { alert(err.message); }
  updateData();
});

$("#data-list").addEventListener("click", async (e) => {
  const act = e.target.dataset?.act, row = e.target.closest(".ds-row");
  if (!act || !row) return;
  const x = dataTab.d.datasets.find((y) => y.code === row.dataset.code);
  try {
    if (act === "update") await apiSend("POST", `/api/datasets/${x.code}/update`);
    if (act === "delete") {
      if (!confirm(`Delete the stored ${x.label} data of this site (${fmtBytes(x.size_bytes)})? It is set to frozen, so that it is not downloaded again.`)) return;
      await apiSend("POST", `/api/datasets/${x.code}/delete?confirm=${encodeURIComponent(x.code)}`);
    }
  } catch (err) { alert(err.message); }
  updateData();
});

function showTab(name) {
  document.querySelectorAll("#tabs button").forEach((b) => b.classList.toggle("active", b.dataset.tab === name));
  document.querySelectorAll(".tab").forEach((t) => t.classList.toggle("active", t.id === `tab-${name}`));
  if (name === "s2") showS2Image(s2.date); else { removeS2Image(); removeS2Parcel(); }
  if (name === "s1") showS1Image(s1.date); else removeS1Image();
  if (name === "weather") { const v = WX_VIEWS[$("#wx-view").value]; if (v.clear) v.update(); }      // its map image back
  else Object.values(WX_VIEWS).forEach((v) => v.clear?.());
  if (name === "data") updateData(); else clearTimeout(dataTab.timer);
  if (map.getLayer("soil")) map.setLayoutProperty("soil", "visibility", name === "soil" ? "visible" : "none");
  for (const layer of soilMaps.on) showSoilMap(layer, true);
  if (map.getLayer("gw-stations")) map.setLayoutProperty("gw-stations", "visibility", name === "soil" ? "visible" : "none");
  if (name === "soil") { updateSoilArea(); updateGroundwater().catch(() => {}); }
}
$("#tabs").addEventListener("click", (e) => { if (e.target.dataset.tab) showTab(e.target.dataset.tab); });

map.on("click", (e) => {
  if ($("#tab-s2").classList.contains("active") && $("#s2-view").value === "parcel") {
    s2.season = null;
    updateS2Parcel(e.lngLat);
    return;
  }
  if ($("#tab-soil").classList.contains("active")) {
    soilAtPoint(e.lngLat).catch((err) => { $("#soil-point").innerHTML = `<p class="muted">${esc(err.message)}</p>`; });
    return;
  }
  const f = map.queryRenderedFeatures(e.point, { layers: ["rpg-0"].filter((l) => map.getLayer(l)) })[0];
  if (f) {
    const p = f.properties;
    const ilot = p.ilot === true || p.ilot === "true";
    new maplibregl.Popup().setLngLat(e.lngLat).setHTML(ilot
      ? `<b>Îlot ${esc(p.parcel_id)}, RPG ${esc(p.year)}</b><br>Majority crop: ${esc(p.crop_group || p.code_group)}` +
        `${p.share ? ` (${fmt(100 * Number(p.share))} % of the îlot)` : ""}<br>${fmt(p.surf_ha, 2)} ha`
      : `<b>${esc(p.crop || p.code_cultu)}</b><br>${esc(p.crop_group || "")}<br>${fmt(p.surf_ha, 2)} ha` +
        `${p.bio === true || p.bio === "true" ? " · organic" : ""}`).addTo(map);
  }
  // On the Sentinel tabs a click only shows the parcel popup (the image stays on the map)
  if ($("#tab-s2").classList.contains("active") || $("#tab-s1").classList.contains("active")) return;
  // On the Weather tab, a click moves the weather location; elsewhere it queries every layer
  if ($("#tab-weather").classList.contains("active")) {
    marker.setLngLat(e.lngLat).addTo(map);
    setWeatherLocation(e.lngLat);
    return;
  }
  queryPoint(e.lngLat).catch((err) => { $("#point-info").innerHTML = `<p class="muted">${esc(err.message)}</p>`; });
});
map.on("mousemove", "rpg-0", () => { map.getCanvas().style.cursor = "pointer"; });
map.on("mouseleave", "rpg-0", () => { map.getCanvas().style.cursor = ""; });

// Sites: one server follows several zones; the choice is kept in a cookie and the page reloads
async function initSites() {
  const s = await api("/api/sites");
  $("#site").innerHTML = s.sites.map((x) => `<option value="${esc(x.code)}" ${x.code === s.current ? "selected" : ""}>${esc(x.name)}${x.loaded ? "" : " (no data yet)"}</option>`).join("");
  $("#site-row").hidden = s.sites.length < 2;
  $("#site").addEventListener("change", async (e) => {
    await fetch(`/api/sites?site=${encodeURIComponent(e.target.value)}`);
    history.replaceState(null, "", location.pathname);     // drop the map position of the other site
    location.reload();
  });
  return s;
}

map.on("load", async () => {
  try {
    await initSites();
  } catch (err) { /* single-site server or API not ready: no selector */ }
  try {
    aoiInfo = await api("/api/aoi");
  } catch (err) {
    $("#aoi-title").textContent = "No data yet for this site — run the ingestion first (see README).";
    return;
  }
  if (!startHash) map.fitBounds(aoiInfo.bbox, { padding: 20, duration: 0 });
  if (aoiInfo.farm) VECTOR_LAYERS.unshift({ id: "farm", label: `${esc(aoiInfo.farm.name)} (${aoiInfo.farm.plots})`,
    source: "/api/farm.geojson", on: true, swatch: "#c2407a",
    layers: [{ type: "line", paint: { "line-color": "#c2407a", "line-width": 2.5 } },
             { type: "fill", paint: { "fill-color": "#c2407a", "fill-opacity": 0.08 } }] });
  addVectorLayers();
  initRpgYears().catch(() => {});
  addAreaLayer();
  renderHierarchy();
  renderAreaSelector();
  await Promise.allSettled([initRasters(), initStatsExplorer(), initWeather(), initS2(), initS1(), initRainRadar(),
    initSoilMaps(), initGroundwater(), initAlerts(), initMeteoFrance()]);
  setArea(currentArea || aoiInfo.areas[0].code);     // keep an area chosen while the tabs were loading
});
