/* Geomon — first product web map. No build step: plain JavaScript + MapLibre GL JS. */
"use strict";

const api = (path) => fetch(path).then((r) => {
  if (!r.ok) throw new Error(`${path}: ${r.status}`);
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

const VECTOR_LAYERS = [
  { id: "osm-landuse", label: "OSM land use", source: "/api/osm/landuse.geojson", on: false,
    layers: [{ type: "fill", paint: { "fill-color": ["match", ["get", "kind"],
      ["forest", "wood"], "#7fb77e", ["meadow", "grassland"], "#c5e1a5", ["farmland"], "#f3e9c6",
      ["orchard", "vineyard"], "#d9a9a0", ["residential"], "#e0d6d6", "#d6dcc7"], "fill-opacity": 0.45 } }],
    swatch: "#7fb77e" },
  { id: "rpg", label: "RPG 2025 parcels", source: "/api/rpg.geojson", on: true,
    layers: [
      { type: "fill", paint: { "fill-color": rpgColorExpr,
        "fill-opacity": ["case", ["get", "in_commune"], 0.6, 0.3] } },
      { type: "line", paint: { "line-color": "#3d3d3d", "line-width": 0.4, "line-opacity": 0.6 } },
    ], swatch: "#57a773" },
  { id: "osm-water", label: "OSM water", source: "/api/osm/water.geojson", on: true,
    layers: [{ type: "fill", paint: { "fill-color": "#6aa9d8", "fill-opacity": 0.7 } }], swatch: "#6aa9d8" },
  { id: "osm-waterways", label: "OSM waterways", source: "/api/osm/waterways.geojson", on: true,
    layers: [{ type: "line", paint: { "line-color": "#3a86c8",
      "line-width": ["match", ["get", "kind"], ["river"], 2.5, ["stream"], 1.2, 0.8] } }], swatch: "#3a86c8" },
  { id: "osm-buildings", label: "OSM buildings", source: "/api/osm/buildings.geojson", on: false,
    layers: [{ type: "fill", paint: { "fill-color": "#8d7b6a", "fill-opacity": 0.8 } }], swatch: "#8d7b6a" },
  { id: "osm-roads", label: "OSM roads", source: "/api/osm/roads.geojson", on: false,
    layers: [{ type: "line", paint: { "line-color": ["match", ["get", "kind"],
      ["motorway", "trunk", "primary"], "#d1495b", ["secondary", "tertiary"], "#edae49", "#6c757d"],
      "line-width": ["match", ["get", "kind"], ["motorway", "trunk", "primary"], 3,
        ["secondary", "tertiary"], 2, ["track", "path", "footway"], 0.6, 1.2] } }], swatch: "#edae49" },
  { id: "osm-railways", label: "OSM railways", source: "/api/osm/railways.geojson", on: false,
    layers: [{ type: "line", paint: { "line-color": "#333", "line-width": 1.5, "line-dasharray": [3, 2] } }],
    swatch: "#333" },
  { id: "osm-pois", label: "OSM places & POIs", source: "/api/osm/pois.geojson", on: false,
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

function addVectorLayers() {
  for (const v of VECTOR_LAYERS) {
    map.addSource(v.id, { type: "geojson", data: v.source });
    v.layers.forEach((l, i) => {
      const id = `${v.id}-${i}`;
      if (!firstVectorLayer) firstVectorLayer = id;
      map.addLayer({ id, source: v.id, ...l, layout: { ...(l.layout || {}), visibility: v.on ? "visible" : "none" } });
    });
  }
  $("#vector-layers").innerHTML = VECTOR_LAYERS.map((v) =>
    `<label><input type="checkbox" value="${v.id}" ${v.on ? "checked" : ""}>
     <span class="swatch" style="background:${v.swatch}"></span> ${v.label}</label>`).join("");
  $("#vector-layers").addEventListener("change", (e) => {
    const v = VECTOR_LAYERS.find((x) => x.id === e.target.value);
    v.layers.forEach((_, i) => map.setLayoutProperty(`${v.id}-${i}`, "visibility", e.target.checked ? "visible" : "none"));
  });
}

// ---------------------------------------------------------------------------
// Raster overlays (DEM, soil) rendered by the API
// ---------------------------------------------------------------------------
let rasterCatalogue = [];

async function initRasters() {
  rasterCatalogue = await api("/api/rasters");
  const sel = $("#raster-var");
  for (const ds of rasterCatalogue) {
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
  sel.addEventListener("change", updateRaster);
  $("#raster-depth").addEventListener("change", updateRaster);
  $("#raster-opacity").addEventListener("input", (e) => {
    if (map.getLayer("raster")) map.setPaintProperty("raster", "raster-opacity", Number(e.target.value));
  });
}

async function updateRaster() {
  const value = $("#raster-var").value;
  if (map.getLayer("raster")) map.removeLayer("raster");
  if (map.getSource("raster")) map.removeSource("raster");
  $("#raster-legend").innerHTML = "";
  if (!value) { $("#depth-row").hidden = true; return; }

  const [name, variable] = value.split(":");
  const ds = rasterCatalogue.find((d) => d.name === name);
  const meta = ds.variables.find((v) => v.name === variable);
  const depthSel = $("#raster-depth");
  if (meta.has_depth) {
    if (depthSel.dataset.for !== name) {
      depthSel.innerHTML = ds.depths.map((d, i) => `<option value="${i}">${d}</option>`).join("");
      depthSel.dataset.for = name;
    }
    $("#depth-row").hidden = false;
  } else {
    $("#depth-row").hidden = true;
  }
  const depth = meta.has_depth ? Number(depthSel.value || 0) : 0;
  const q = `depth=${depth}`;
  const info = await api(`/api/raster/${name}/${variable}/info?${q}`);
  map.addSource("raster", { type: "image", url: `/api/raster/${name}/${variable}.png?${q}`,
    coordinates: info.coordinates });
  map.addLayer({ id: "raster", type: "raster", source: "raster",
    paint: { "raster-opacity": Number($("#raster-opacity").value), "raster-resampling": "nearest" } },
    firstVectorLayer);
  renderRasterLegend(info, meta);
}

function renderRasterLegend(info, meta) {
  const u = unit(info.units);
  let html = `<div class="title">${esc(info.long_name)}${u ? ` (${esc(u)})` : ""}</div>`;
  if (meta.categorical) {
    html += info.legend.map((l) => `<div class="item"><span class="swatch" style="background:${l.color}"></span>${esc(l.label)}</div>`).join("");
  } else if (meta.name !== "hillshade") {
    const stops = info.legend.map((l, i) => `${l.color} ${(100 * i) / (info.legend.length - 1)}%`).join(",");
    const digits = info.vmax - info.vmin < 1 ? 3 : info.vmax - info.vmin < 20 ? 1 : 0;
    html += `<div class="ramp" style="background:linear-gradient(to right,${stops})"></div>
      <div class="ramp-labels"><span>${fmt(info.vmin, digits)}</span><span>${fmt((info.vmin + info.vmax) / 2, digits)}</span><span>${fmt(info.vmax, digits)}</span></div>
      <div class="muted">Colour scale: 2nd–98th percentile over the area.</div>`;
  }
  $("#raster-legend").innerHTML = html;
}

// ---------------------------------------------------------------------------
// RPG summary
// ---------------------------------------------------------------------------
async function updateRpgSummary() {
  const s = await api(`/api/rpg/summary?area=${encodeURIComponent(currentArea)}`);
  $("#rpg-summary").innerHTML =
    `<div class="muted">Declared crops in ${esc(s.area.name)}, ${s.year} — total ${fmt(s.total_ha, 1)} ha</div>` +
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

function renderHierarchy() {
  $("#hierarchy").innerHTML = aoiInfo.hierarchy.map((h) =>
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
  $("#area").innerHTML = aoiInfo.areas.map((a) => `<option value="${esc(a.code)}">${esc(areaLabel(a))}</option>`).join("");
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
  updateAreaSummary().catch((err) => { $("#area-summary").innerHTML = `<p class="muted">${esc(err.message)}</p>`; });
  if (wx.catalogue) setWeatherLocation(wx.lngLat, !wx.lngLat);  // the area average follows the area
  updateS2().catch((err) => { $("#s2-status").innerHTML = esc(err.message); });
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
const WX_SERIES = [
  { key: "mf", label: "Météo-France AROME/ARPEGE", color: "#2a78d6", product: "om_mf_hist" },
  { key: "era5land", label: "ERA5-Land / ERA5", color: "#eb6834", product: "om_era5land" },
  { key: "agera5", label: "AgERA5", color: "#c2407a", product: "agera5" },
  { key: "sim2", label: "SIM2 / SAFRAN", color: "#1baf7a", product: "sim2" },
];
const WX_GROUPS = [
  ["Temperature", /^t_/], ["Precipitation and water", /^(precip|rain|snowfall|et0|evap|effective_rain|drainage|runoff)/],
  ["Radiation", /^(radiation|longwave)/], ["Humidity", /^(rh_|vp|q_)/], ["Wind and clouds", /^(wind|cloud)/],
  ["Snow", /^(snow_depth|swe)/], ["Soil", /^(soil_|swi|wg_)/],
];
const wx = { catalogue: null, lngLat: null, isCentre: true, seq: 0 };

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
      <span class="swatch" style="background:${s.color}"></span> ${esc(meta?.label || s.label)}</label>`;
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
    ERA5-Land 0.1°; rain, wind, radiation and ET0 from ERA5 0.25° (not served for ERA5-Land). Forecast: AROME/ARPEGE
    up to ~4 days, then ECMWF IFS 9 km to 15 days. AgERA5 (Copernicus CDS): ERA5 corrected to 0.1°, about 10 days behind. Daily values over the local day (${esc(wx.catalogue.local_time_zone)});
    SIM2 keeps Météo-France's day definitions (rain and ET over 06–06 UTC).
    Updated: ${["om_mf_hist", "om_forecast", "om_era5land", "agera5", "sim2"].map((n) => p[n]?.updated ? `${n} ${p[n].updated.slice(0, 16).replace("T", " ")} UTC` : "").filter(Boolean).join(" · ")}.`;
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

async function updateWeather() {
  if (!currentArea || !$("#wx-var").value) return;
  if ($("#wx-view").value === "normal") return updateNormals();
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
    return `<path d="${p}" fill="none" stroke="${s.color}" stroke-width="2" stroke-linejoin="round" stroke-linecap="round"/>`;
  }).join("");
  const legend = series.length > 1 ? `<div class="wx-legend">${series.map((s) =>
    `<span class="k"><span class="line" style="background:${s.color}"></span>${esc(s.label)}
     <span class="muted">${s.point ? `· ${fmt(s.point.distance_km, 1)} km` : ""}</span></span>`).join("")}</div>` : "";
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
      rows.map((r) => `<div class="r"><span class="swatch" style="background:${r.s.color}"></span>${esc(r.s.label)}<b>${r.v === null ? "–" : `${fmt(r.v, 1)} ${esc(u)}`}</b></div>`).join("");
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
    return `<tr><td><span class="swatch" style="background:${s.color}"></span> ${esc(s.label)}</td>
      <td class="num">${pts.length}</td><td class="num">${fmt(mean, 2)}</td><td class="num">${diff || "–"}</td><td class="num">${corr || "–"}</td>
      <td class="num">${s.point ? `${fmt(s.point.distance_km, 1)} km` : s.cells ? `${s.cells.length} cell${s.cells.length > 1 ? "s" : ""}` : "–"}</td></tr>`;
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
  const normal = $("#wx-view").value === "normal";
  $("#wx-sources-controls").hidden = normal;
  $("#wx-normal-controls").hidden = !normal;
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
    map.addLayer({ id: `wx-${s.key}-line`, type: "line", source: `wx-${s.key}`, paint: { "line-color": s.color, "line-width": 1.5 } });
  }
}

// ---------------------------------------------------------------------------
// Sentinel-2 tab: index time series over the selected area, index image per date
// ---------------------------------------------------------------------------
const S2_COLOR = "#2a78d6";      // single series: categorical slot 1
const s2 = { info: null, points: [], date: null, seq: 0 };
const S2_W = 380, S2_H = 190, S2_M = { l: 40, r: 10, t: 10, b: 22 };

async function initS2() {
  s2.info = await api("/api/s2/info");
  if (!s2.info.available) {
    $("#s2-status").innerHTML = `No Sentinel-2 data yet. ${esc(s2.info.hint || "")}`;
    return;
  }
  const i = s2.info;
  $("#s2-index").innerHTML = i.indices.map((x) => `<option value="${x.name}">${esc(x.long_name)}</option>`).join("");
  $("#s2-year").innerHTML = `<option value="">All years (${i.years[0]}–${i.years[i.years.length - 1]})</option>` +
    [...i.years].reverse().map((y) => `<option value="${y}">${y}</option>`).join("");
  const missing = i.missing_indices.map((x) => `${x.name.toUpperCase()} (add ${x.bands.filter((b) => !i.bands.some((h) => h.name === b)).join(", ")})`);
  $("#s2-sources").innerHTML = `Sentinel-2 L2A (Sen2Cor), ${esc(i.collection)}, openEO Copernicus Data Space · ${fmt(i.resolution_m)} m, ${esc(i.crs)} ·
    ${i.dates} dates ${esc(i.first)} → ${esc(i.last)} · bands ${i.bands.map((b) => b.name).join(", ")} + SCL ·
    scenes with tile cloud cover ≤ ${fmt(i.max_cloud_cover)} %. Clear pixels: SCL ${Object.values(i.clear_classes).map(esc).join(", ").toLowerCase()}, with red reflectance below ${fmt(i.max_clear_red, 2)} (removes fog and low cloud classified as bare soil).
    ${missing.length ? `Other indices: ${missing.map(esc).join("; ")} to GEOMON_S2_BANDS.` : ""}`;
  for (const id of ["#s2-index", "#s2-year", "#s2-clear"]) $(id).addEventListener("change", updateS2);
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

async function updateS2() {
  if (!s2.info?.available || !currentArea) return;
  const view = $("#s2-view").value;
  $("#s2-area-controls").hidden = view !== "area";
  $("#s2-season-controls").hidden = view === "area";
  $("#s2-group-row").hidden = view !== "crop";
  $("#s2-own-row").hidden = view !== "parcel";
  $("#s2-compare").innerHTML = "";
  $("#s2-history").innerHTML = "";
  if (view !== "parcel") removeS2Parcel();
  if (view === "crop") return updateS2Crop();
  if (view === "parcel") return updateS2Parcel();
  const seq = ++s2.seq;
  const index = $("#s2-index").value, year = $("#s2-year").value, minClear = $("#s2-clear").value;
  const a = areaOf(currentArea);
  $("#s2-status").innerHTML = `Mean over <b>${esc(a.name)}</b>, clear pixels only. Click a point to show its image.`;
  const d = await api(`/api/s2/series?index=${index}&area=${encodeURIComponent(currentArea)}&min_clear=${minClear}`);
  if (seq !== s2.seq) return;
  s2.points = d.points.filter((p) => p.mean !== null && (!year || p.date.startsWith(year)));
  $("#s2-chart").innerHTML = s2.points.length ? s2Chart(index) : '<p class="muted">No date with enough clear sky.</p>';
  bindS2Hover();
  $("#s2-date").innerHTML = [...s2.points].reverse().map((p) =>
    `<option value="${p.date}">${p.date} · ${fmt(100 * p.clear_fraction)} % clear · ${fmt(p.mean, 2)}</option>`).join("");
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

function s2Chart(index) {
  const ix = s2.info.indices.find((x) => x.name === index);
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
    tip.innerHTML = `<b>${p.date}</b><br>mean ${fmt(p.mean, 3)} · median ${fmt(p.median, 3)}<br>p10–p90 ${fmt(p.p10, 2)}–${fmt(p.p90, 2)}<br>${fmt(100 * p.clear_fraction)} % of the area clear`;
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
  const index = $("#s2-index").value, masked = $("#s2-mask").checked;
  const q = `date=${date}&masked=${masked}`;
  const info = await api(`/api/s2/${index}/info?${q}`);
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
  $("#s2-legend").innerHTML = `<div class="title">${esc(index.toUpperCase())}, ${esc(date)}${masked ? " (clouds hidden)" : ""}</div>
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
  const index = $("#s2-index").value, area = encodeURIComponent(currentArea);
  let c;
  try { c = await api(`/api/s2/crops?index=${index}&area=${area}${s2.season ? `&season=${s2.season}` : ""}`); }
  catch (e) { $("#s2-chart").innerHTML = '<p class="muted">No parcel series yet: run <code>docker compose run --rm ingest sentinel2-parcels</code>.</p>'; return; }
  if (seq !== s2.seq) return;
  fillSeasons(c.seasons, c.latest_rpg);
  if (!c.seasons.includes(c.season)) s2.season = c.season;
  if (!s2.group || !c.groups.some((g) => g.code === s2.group)) s2.group = (c.groups.find((g) => g.parcels) || c.groups[0])?.code;
  $("#s2-group").innerHTML = c.groups.map((g) => `<option value="${g.code}" ${g.code === s2.group ? "selected" : ""}>${esc(g.label)} · ${g.parcels ? `${g.parcels} parcels, ${fmt(g.ha)} ha${g.status === "assumed" ? " (assumed)" : ""}` : "none this season"}</option>`).join("");
  if (!s2.group) { $("#s2-chart").innerHTML = '<p class="muted">No parcel in this area.</p>'; return; }
  // The farm's plots: references from the whole group (default) or from the plots' own earlier seasons
  const isFarm = areaOf(currentArea).kind === "farm";
  $("#s2-refarea-row").hidden = !isFarm;
  if (isFarm && !$("#s2-refarea").options.length) {
    $("#s2-refarea").innerHTML = `<option value="${esc(aoiInfo.code)}">The whole group (${esc(aoiInfo.name)})</option>
      <option value="farm">My plots, earlier seasons</option>`;
  }
  const refArea = isFarm ? $("#s2-refarea").value : currentArea;
  const d = await api(`/api/s2/crop?index=${index}&weight=${$("#s2-weight").value}&area=${area}&ref_area=${encodeURIComponent(refArea)}&group=${encodeURIComponent(s2.group)}&season=${s2.season}`);
  if (seq !== s2.seq) return;
  const a = areaOf(currentArea);
  $("#s2-status").innerHTML = `<b>${esc(d.label)}</b> in <b>${esc(a.name)}</b>: ${d.weight === "pixels" ? "mean of the parcels' daily curves, weighted by their interior area" : "mean of the parcels' daily curves, one parcel one vote"}
    (${d.smoothing === "whittaker" ? "Whittaker-smoothed" : "interpolated between clear dates"})
    (each parcel's interior, clear on at least ${fmt(100 * d.min_clear)} % of it). References: the seasons before ${d.season}${d.ref_area !== d.area ? `, in ${esc(areaOf(d.ref_area)?.name || d.ref_area)}` : ""}.
    ${d.note ? `<br>${esc(d.note)}` : ""}`;
  s2.seasonData = { kind: "crop", d, current: d.current?.mean, spread: d.current && [d.current.q25, d.current.q75],
    obs: d.observations.map((o) => ({ day: o.day, date: o.date, v: o.mean, used: true, n: o.parcels })),
    refs: d.refs, own: null, title: `${d.season}${d.current ? ` · ${d.current.parcels} parcels, ${fmt(d.current.ha)} ha` : ""}` };
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
    $("#s2-chart").innerHTML = ""; removeS2Parcel(); return;
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
    : `${esc(p.crop)}${p.status === "assumed" ? ` (assumed from RPG ${p.rpg_year})` : ""}`;
  $("#s2-status").innerHTML = `Parcel ${esc(p.parcel_id)} (RPG ${p.rpg_year}), ${fmt(p.surf_ha, 2)} ha, season ${d.season}: <b>${crop}</b>.
    Compared with <b>${esc(d.reference_group_label)}</b> in ${esc(areaOf(area)?.name || area)} (mean of its parcels).`;
  s2.seasonData = { kind: "parcel", d, current: d.curve, spread: null,
    obs: d.observations.map((o) => ({ day: o.day, date: o.date, v: o.mean, used: o.used, clear: o.clear_fraction })),
    refs: g.refs, own: d.own, groupLabel: d.reference_group_label, title: `${d.season} · parcel ${p.parcel_id}` };
  showS2Parcel(d.geometry);
  renderSeason();
  renderHistory(d);
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
    label: sd.kind === "crop" ? `${d.season}, mean of the parcels` : `${d.season}, this parcel` });
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
    ${sd.spread ? `<span class="k"><span class="line" style="background:#2a78d6;opacity:.2;height:8px"></span>${d.season}, middle half of the parcels</span>` : ""}
    <span class="k"><span class="dot" style="background:#2a78d6"></span>${sd.kind === "crop" ? "Clear date (mean of the parcels)" : "Clear date"}</span>
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
      (o ? `<div class="d" style="margin-top:3px">Observed ${o.date}: <b>${fmt(o.v, 2)}</b>${o.n ? ` · ${o.n} parcels` : ""}${o.clear !== undefined ? ` · ${fmt(100 * o.clear)} % clear${o.used ? "" : ", not used"}` : ""}</div>` : "");
    placeTip(tip, svg, x, SN_W);
  });
  hit.addEventListener("pointerleave", () => hideCrosshair(cross, dots, tip));
  hit.addEventListener("click", (e) => { const o = nearObs(dayAt(e)); if (o) showS2Image(o.date); });
}

function renderSeason() {
  const sd = s2.seasonData;
  $("#s2-chart").innerHTML = seasonChart(sd);
  bindSeasonHover();
  // Comparison: on the latest observation (season in progress), at the peak (1 Jan - 31 Aug) and
  // averaged from 1 January (to the latest observation, or over the year when the season is over)
  const cur = sd.current;
  const days = cur ? cur.map((v, i) => (v === null ? -1 : i)).filter((i) => i >= 0) : [];
  if (!days.length) { $("#s2-compare").innerHTML = ""; return; }
  const d = sd.d, jan = d.jan1_day, last = days[days.length - 1];
  const lastObs = s2.info?.last ? Math.round((Date.parse(`${s2.info.last}T00:00:00Z`) - Date.parse(`${d.start}T00:00:00Z`)) / 86400000) : -1;
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
      <td>${h.season}</td><td>${h.status === "unknown" ? '<span class="muted">not published yet</span>' : `${esc(h.crop)}${h.status === "assumed" ? ' <span class="muted">(assumed)</span>' : ""}`}</td>
      <td class="num">${fmt(h.surf_ha, 2)}</td><td class="num">${fmt(h.peak, 2)}</td><td class="num">${h.peak_date ? h.peak_date.slice(5) : "–"}</td><td class="num">${h.n_obs ?? "–"}</td></tr>`).join("")}
  </table></div><div class="muted" style="font-size:11px">Each season uses that year's RPG parcel at the clicked point (outlines and identifiers change every year). Click a row to show its season.</div>`;
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
  if (soil) {
    const v = soil.variables;
    html += `<div class="card"><h3>Soil profile (SoilGrids, ${fmt(soil.resolution_m)} m)</h3>
      <div class="meta">Available water capacity 0–200 cm: <b>${fmt(v.awc_total.value)} mm</b> (estimate)</div>
      <div class="table-wrap"><table class="kv soil"><tr><th>Depth</th>${SOIL_COLUMNS.map(([k, l]) =>
        `<th class="num" title="${esc(v[k]?.long_name)}">${l}${v[k] && unit(v[k].units) ? `<br><span class="muted">${esc(unit(v[k].units))}</span>` : ""}</th>`).join("")}</tr>
      ${soil.depths.map((d, i) => `<tr><td>${d}</td>${SOIL_COLUMNS.map(([k]) => {
        if (!v[k]) return "<td></td>";
        if (k === "texture_usda") return `<td class="num">${esc(v[k].labels?.[i] ?? "–")}</td>`;
        const digits = ["theta_fc", "theta_wp", "bdod"].includes(k) ? 2 : 1;
        return `<td class="num">${fmt(v[k].values[i], digits)}</td>`;
      }).join("")}</tr>`).join("")}
      </table></div>
      <div class="muted" style="font-size:11px;margin-top:4px">θFC, θWP, Ksat and AWC: Saxton &amp; Rawls (2006) pedotransfer estimates.</div>
    </div>`;
  }
  $("#point-info").innerHTML = html;
}

// ---------------------------------------------------------------------------
// Tabs, popups, start-up
// ---------------------------------------------------------------------------
function showTab(name) {
  document.querySelectorAll("#tabs button").forEach((b) => b.classList.toggle("active", b.dataset.tab === name));
  document.querySelectorAll(".tab").forEach((t) => t.classList.toggle("active", t.id === `tab-${name}`));
  if (name === "s2") showS2Image(s2.date); else { removeS2Image(); removeS2Parcel(); }
}
$("#tabs").addEventListener("click", (e) => { if (e.target.dataset.tab) showTab(e.target.dataset.tab); });

map.on("click", (e) => {
  if ($("#tab-s2").classList.contains("active") && $("#s2-view").value === "parcel") {
    s2.season = null;
    updateS2Parcel(e.lngLat);
    return;
  }
  const f = map.queryRenderedFeatures(e.point, { layers: ["rpg-0"].filter((l) => map.getLayer(l)) })[0];
  if (f) {
    const p = f.properties;
    new maplibregl.Popup().setLngLat(e.lngLat).setHTML(
      `<b>${esc(p.crop || p.code_cultu)}</b><br>${esc(p.crop_group || "")}<br>${fmt(p.surf_ha, 2)} ha` +
      `${p.bio === true || p.bio === "true" ? " · organic" : ""}`).addTo(map);
  }
  // On the Sentinel-2 tab a click only shows the parcel popup (the image stays on the map)
  if ($("#tab-s2").classList.contains("active")) return;
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
  if (!location.hash) map.fitBounds(aoiInfo.bbox, { padding: 20, duration: 0 });
  if (aoiInfo.farm) VECTOR_LAYERS.unshift({ id: "farm", label: `${esc(aoiInfo.farm.name)} (${aoiInfo.farm.plots})`,
    source: "/api/farm.geojson", on: true, swatch: "#c2407a",
    layers: [{ type: "line", paint: { "line-color": "#c2407a", "line-width": 2.5 } },
             { type: "fill", paint: { "fill-color": "#c2407a", "fill-opacity": 0.08 } }] });
  addVectorLayers();
  addAreaLayer();
  renderHierarchy();
  renderAreaSelector();
  await Promise.allSettled([initRasters(), initStatsExplorer(), initWeather(), initS2()]);
  setArea(aoiInfo.areas[0].code);
});
