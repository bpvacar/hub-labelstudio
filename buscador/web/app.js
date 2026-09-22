// Buscador por especie: grilla de detecciones ordenadas por parecido.
// Lo confirmado se registra en Label Studio; lo descartado queda solo en el buscador.

const $ = (s) => document.querySelector(s);
const estado = {
  usuario: null, especies: [], estaciones: [], anios: [],
  consulta: "", taxon: null, resultados: [],
  seleccion: new Set(), cursor: -1, corte: -1, panel: null,
};

const numero = (n) => n.toLocaleString("es");
const nombreComun = (cientifico) =>
  (estado.especies.find((e) => e.cientifico === cientifico) || {}).valor || cientifico;
const fechaLegible = (iso) => (iso ? iso.replace("T", " ").slice(0, 19) : "sin fecha");

// ------------------------------------------------------------------- red --
async function api(ruta, opciones = {}) {
  const r = await fetch(ruta, { credentials: "same-origin", ...opciones });
  if (r.status === 401) { mostrarEntrada(); throw new Error("sesión"); }
  if (!r.ok) throw new Error((await r.json().catch(() => ({}))).error || r.statusText);
  return r.json();
}

const enviar = (ruta, cuerpo) => api(ruta, {
  method: "POST", headers: { "Content-Type": "application/json" },
  body: JSON.stringify(cuerpo),
});

function avisar(texto, malo = false) {
  const nodo = document.createElement("div");
  nodo.className = "aviso" + (malo ? " malo" : "");
  nodo.textContent = texto;
  $("#avisos").append(nodo);
  setTimeout(() => nodo.remove(), 6000);
}

// --------------------------------------------------------------- entrada --
function mostrarEntrada() {
  $("#entrada").hidden = false;
  $("#app").hidden = true;
}

$("#formEntrada").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  $("#errorEntrada").textContent = "";
  try {
    await enviar("/api/sesion", { token: $("#token").value });
    $("#token").value = "";
    await iniciar();
  } catch (e) {
    $("#errorEntrada").textContent = e.message;
  }
});

// ---------------------------------------------------------------- inicio --
async function iniciar() {
  const e = await api("/api/estado");
  Object.assign(estado, {
    usuario: e.usuario, especies: e.especies,
    estaciones: e.estaciones, anios: e.anios,
  });
  $("#entrada").hidden = true;
  $("#app").hidden = false;
  $("#usuario").textContent = e.usuario;
  $("#infoIndice").textContent =
    `${numero(e.indice.cajas)} detecciones · ${numero(e.indice.etiquetadas)} identificadas · ${e.indice.modelo}`;
  for (const [sel, valores] of [["#estacion", e.estaciones], ["#anio", e.anios]]) {
    for (const v of valores) $(sel).append(new Option(v, v));
  }
  pintarEspecies("");
}

function pintarEspecies(filtro) {
  const sinTildes = (s) => (s || "").normalize("NFD").replace(/\p{Diacritic}/gu, "").toLowerCase();
  const q = sinTildes(filtro);
  const lista = $("#especies");
  lista.textContent = "";
  for (const esp of estado.especies) {
    if (q && !sinTildes(esp.valor + " " + (esp.cientifico || "")).includes(q)) continue;
    const li = document.createElement("li");
    const b = document.createElement("button");
    b.type = "button";
    b.setAttribute("aria-current", String(estado.consulta === esp.valor));
    b.innerHTML = `<span class="nombre">${esp.valor}</span>
      <span class="cuenta">${numero(esp.etiquetadas)}</span>
      <span class="cientifico${esp.confianza === "low" ? " revisar" : ""}">${esp.cientifico || "—"}${esp.confianza === "low" ? " · revisar" : ""}</span>`;
    b.addEventListener("click", () => buscar(esp.valor));
    li.append(b);
    lista.append(li);
  }
}

$("#filtroEspecie").addEventListener("input", (ev) => pintarEspecies(ev.target.value));
$("#refrescar").addEventListener("click", async () => {
  const r = await enviar("/api/refrescar", {});
  avisar(`${numero(r.etiquetadas)} detecciones identificadas en Label Studio`);
  if (estado.consulta) buscar(estado.consulta);
});

// --------------------------------------------------------------- consulta --
$("#formBuscar").addEventListener("submit", (ev) => {
  ev.preventDefault();
  buscar($("#texto").value.trim() || estado.consulta);
});
$("#identificadas").addEventListener("change", () => {
  if (estado.consulta) buscar(estado.consulta);
});

async function buscar(consulta) {
  if (!consulta) return;
  estado.consulta = consulta;
  $("#vacio").hidden = true;
  $("#estadoConsulta").textContent = "Buscando…";
  const p = new URLSearchParams({
    consulta, n: "600",
    texto: $("#soloTexto").checked ? "1" : "0",
    identificadas: $("#identificadas").value,
    estacion: $("#estacion").value, anio: $("#anio").value,
  });
  const r = await api("/api/buscar?" + p);
  Object.assign(estado, {
    taxon: r.taxon, resultados: r.resultados,
    seleccion: new Set(), cursor: r.resultados.length ? 0 : -1, corte: -1,
  });
  const como = r.modo === "ejemplos"
    ? `${numero(r.ejemplos)} ejemplos identificados`
    : "búsqueda por texto, sin ejemplos";
  const cuales = { no: "sin identificar", si: "ya identificadas", todas: "en total" }[
    $("#identificadas").value];
  $("#estadoConsulta").textContent =
    `${consulta} · ${como} · ${numero(r.total)} detecciones ${cuales} · ${r.ms} ms`;
  pintarEspecies($("#filtroEspecie").value);
  pintarGrilla();
}

// ---------------------------------------------------------------- grilla --
function pintarGrilla() {
  const g = $("#grilla");
  g.textContent = "";
  estado.resultados.forEach((r, i) => {
    if (i === estado.corte + 1 && estado.corte >= 0) g.append(lineaDeCorte());
    const b = document.createElement("button");
    b.type = "button";
    b.className = "celda"
      + (i === estado.cursor ? " cursor" : "")
      + (r.identificacion ? " conIdentificacion" : "");
    b.setAttribute("aria-pressed", String(estado.seleccion.has(r.deteccion)));
    b.dataset.i = i;
    const marca = r.identificacion
      ? `<span class="identificada">${nombreComun(r.identificacion.cientifico)}</span>` : "";
    b.title = [r.identificacion ? nombreComun(r.identificacion.cientifico) : "sin identificar",
               r.estacion || "sin estación", fechaLegible(r.fecha),
               `similitud ${r.similitud.toFixed(3)}`].join(" · ");
    b.innerHTML = `<img loading="lazy" src="/recorte/${r.deteccion}.jpg" alt="">
      <span class="puesto">${r.puesto}</span>
      <span class="sim">${r.similitud.toFixed(3)}</span>${marca}`;
    b.addEventListener("click", (ev) => {
      if (ev.shiftKey && estado.cursor >= 0) marcarRango(estado.cursor, i);
      else { alternar(i); estado.cursor = i; }
      pintarGrilla();
    });
    b.addEventListener("dblclick", () => abrirPanel(i));
    b.addEventListener("contextmenu", (ev) => { ev.preventDefault(); abrirPanel(i); });
    g.append(b);
  });
  if (!estado.resultados.length) {
    $("#vacio").hidden = false;
    $("#vacio").textContent = "No hay detecciones para esta búsqueda.";
  }
  actualizarAcciones();
}

function lineaDeCorte() {
  const n = estado.corte + 1;
  const d = document.createElement("div");
  d.className = "corte";
  d.innerHTML = `<span class="dato">Línea de corte · ${n} ${n === 1 ? "detección" : "detecciones"} por encima</span>`;
  const b = document.createElement("button");
  b.className = "confirmar";
  b.textContent = n === 1 ? "Confirmar la detección por encima" : `Confirmar las ${n} por encima`;
  b.addEventListener("click", () => confirmar(estado.resultados.slice(0, n)));
  d.append(b);
  return d;
}

const alternar = (i) => {
  const d = estado.resultados[i].deteccion;
  estado.seleccion.has(d) ? estado.seleccion.delete(d) : estado.seleccion.add(d);
};

function marcarRango(desde, hasta) {
  const [a, b] = desde <= hasta ? [desde, hasta] : [hasta, desde];
  for (let i = a; i <= b; i++) estado.seleccion.add(estado.resultados[i].deteccion);
  estado.cursor = hasta;
}

function actualizarAcciones() {
  const n = estado.seleccion.size;
  $("#acciones").hidden = n === 0;
  $("#cuentaSeleccion").textContent = `${n} seleccionada${n === 1 ? "" : "s"}`;
  $("#nombreAceptar").textContent = estado.taxon ? estado.taxon.label : "—";
  $("#aceptar").disabled = !estado.taxon;
  $("#rechazar").disabled = !estado.taxon;
}

// --------------------------------------------------------------- acciones --
const seleccionadas = () =>
  estado.resultados.filter((r) => estado.seleccion.has(r.deteccion));

async function confirmar(filas) {
  if (!estado.taxon || !filas.length) return;
  const especie = estado.taxon.scientific_name || estado.taxon.label;
  const dets = filas.map((f) => f.deteccion);
  marcarYendo(dets);
  try {
    const r = await enviar("/api/aceptar", { especie, detecciones: dets });
    const nuevas = r.aceptadas.filter((a) => a.resultado !== "ya tenía especie asignada").length;
    avisar(`${nuevas} ${nuevas === 1 ? "detección confirmada" : "detecciones confirmadas"} como ${estado.taxon.label}` +
      (r.fallas.length ? ` · ${r.fallas.length} con error` : ""), r.fallas.length > 0);
    if (r.fallas.length) console.warn("errores al registrar en Label Studio", r.fallas);
    aplicar(new Set(r.aceptadas.map((a) => a.deteccion)),
            { cientifico: especie, por: estado.usuario, cuando: new Date().toISOString() });
    const esp = estado.especies.find((e) => e.valor === estado.taxon.label);
    if (esp) { esp.etiquetadas += nuevas; pintarEspecies($("#filtroEspecie").value); }
  } catch (e) {
    avisar("No se pudo registrar en Label Studio: " + e.message, true);
    pintarGrilla();
  }
}

async function descartar(filas) {
  if (!estado.taxon || !filas.length) return;
  const especie = estado.taxon.scientific_name || estado.taxon.label;
  const dets = filas.map((f) => f.deteccion);
  marcarYendo(dets);
  await enviar("/api/rechazar", { especie, detecciones: dets });
  avisar(`${dets.length} ${dets.length === 1 ? "detección descartada" : "detecciones descartadas"} para ${estado.taxon.label}`);
  aplicar(new Set(dets), null);
}

function marcarYendo(dets) {
  const conjunto = new Set(dets);
  for (const b of document.querySelectorAll(".celda")) {
    if (conjunto.has(estado.resultados[+b.dataset.i].deteccion)) b.classList.add("yendo");
  }
}

/** Con el filtro "sin identificar", lo resuelto sale de la grilla; en las otras
 *  vistas se queda, con su identificación al día. */
function aplicar(dets, identificacion) {
  if ($("#identificadas").value === "no") {
    estado.resultados = estado.resultados.filter((r) => !dets.has(r.deteccion));
    estado.resultados.forEach((r, i) => { r.puesto = i + 1; });
    estado.corte = -1;
  } else if (identificacion) {
    for (const r of estado.resultados) {
      if (dets.has(r.deteccion)) r.identificacion = identificacion;
    }
  }
  estado.seleccion = new Set();
  estado.cursor = Math.min(estado.cursor, estado.resultados.length - 1);
  pintarGrilla();
  if (estado.panel && dets.has(estado.panel)) cerrarPanel();
}

$("#aceptar").addEventListener("click", () => confirmar(seleccionadas()));
$("#rechazar").addEventListener("click", () => descartar(seleccionadas()));
$("#limpiar").addEventListener("click", () => { estado.seleccion = new Set(); pintarGrilla(); });

// ------------------------------------------------------------------ panel --
async function abrirPanel(i) {
  const r = estado.resultados[i];
  if (!r) return;
  estado.cursor = i;
  pintarGrilla();
  const d = await api(`/api/deteccion/${r.deteccion}`);
  estado.panel = r.deteccion;
  $("#cuerpo").classList.add("conPanel");
  $("#panel").hidden = false;
  $("#panelFoto").src = `/foto/${r.deteccion}.jpg`;
  $("#panelTitulo").innerHTML = d.identificacion
    ? `${nombreComun(d.identificacion.cientifico)} <span class="cientifico">${d.identificacion.cientifico}</span>`
    : "Sin identificar";

  const f = d.foto, caja = d.caja;
  const filas = [
    ["Estación", f.estacion || "sin estación"],
    ["Fecha", fechaLegible(f.fecha)],
    ["Despliegue", f.despliegue || "—"],
    ["Cámara", f.camara || "—"],
    ["Coordenadas", f.latitud != null ? `${f.latitud}, ${f.longitud}` : "sin GPS"],
    ["Detector", `${caja.categoria} · ${caja.confianza ? (+caja.confianza).toFixed(2) : "—"}`],
    ["Caja", `${caja.x1}, ${caja.y1} → ${caja.x2}, ${caja.y2} (${f.ancho}×${f.alto})`],
    ["Evento", `${d.evento.detecciones} ${d.evento.detecciones === 1 ? "detección" : "detecciones"} en ±30 min`],
    ["Archivo", f.ruta.split("/").slice(-2).join("/")],
  ];
  if (d.identificacion) {
    filas.unshift(["Identificó", d.identificacion.por || "—"],
                  ["Cuándo", fechaLegible(d.identificacion.cuando)]);
  }
  $("#panelDatos").innerHTML = filas.map(([k, v]) => `<dt>${k}</dt><dd>${v}</dd>`).join("");

  $("#panelMismaFoto").innerHTML = tira("Otras detecciones en esta foto", d.evento.misma_foto);
  $("#panelEvento").innerHTML = tira("El resto del evento", d.evento.otras_fotos);
  for (const b of document.querySelectorAll("#panel .tira button")) {
    b.addEventListener("click", () => irADeteccion(b.dataset.deteccion));
  }
  $("#panelEnlace").innerHTML = d.label_studio
    ? `<a href="${d.label_studio}" target="_blank" rel="noopener">Abrir la foto en Label Studio</a>`
    : "";
  $("#panelConfirmar").textContent = estado.taxon
    ? `Confirmar como ${estado.taxon.label}` : "Confirmar";
  $("#panelConfirmar").disabled = !estado.taxon;
  $("#panelDescartar").disabled = !estado.taxon;
}

function tira(titulo, items) {
  if (!items || !items.length) return "";
  const celdas = items.map((m) => `
    <button type="button" data-deteccion="${m.deteccion}"
            title="${m.identificacion ? nombreComun(m.identificacion.cientifico) : "sin identificar"} · ${fechaLegible(m.fecha)}">
      <img loading="lazy" src="/recorte/${m.deteccion}.jpg" alt="">
      ${m.identificacion ? '<span class="marcaIdent"></span>' : ""}
    </button>`).join("");
  return `<div class="tira"><h3>${titulo} · ${items.length}</h3>
            <div class="miniaturas">${celdas}</div></div>`;
}

/** Una miniatura del panel puede no estar en la grilla: se agrega al final para
 *  poder abrirla igual. */
function irADeteccion(deteccion) {
  const i = estado.resultados.findIndex((r) => r.deteccion === deteccion);
  if (i >= 0) return abrirPanel(i);
  estado.resultados.push({
    puesto: estado.resultados.length + 1, similitud: 0, deteccion,
    estacion: null, fecha: null, ruta: "", confianza: null, identificacion: null,
  });
  abrirPanel(estado.resultados.length - 1);
}

function cerrarPanel() {
  estado.panel = null;
  $("#panel").hidden = true;
  $("#cuerpo").classList.remove("conPanel");
}

$("#panelCerrar").addEventListener("click", cerrarPanel);
$("#panelConfirmar").addEventListener("click", () => {
  const r = estado.resultados.find((x) => x.deteccion === estado.panel);
  if (r) confirmar([r]);
});
$("#panelDescartar").addEventListener("click", () => {
  const r = estado.resultados.find((x) => x.deteccion === estado.panel);
  if (r) descartar([r]);
});

// ---------------------------------------------------------------- teclado --
function columnas() {
  const g = getComputedStyle($("#grilla")).gridTemplateColumns;
  return Math.max(1, g.split(" ").length);
}

document.addEventListener("keydown", (ev) => {
  if ($("#app").hidden) return;
  const escribiendo = ["INPUT", "SELECT", "TEXTAREA"].includes(ev.target.tagName);
  if (escribiendo || ev.metaKey || ev.ctrlKey) return;
  const n = estado.resultados.length;
  if (!n) return;
  const mover = { ArrowRight: 1, ArrowLeft: -1, ArrowDown: columnas(), ArrowUp: -columnas() }[ev.key];

  if (mover !== undefined) {
    ev.preventDefault();
    const antes = estado.cursor;
    estado.cursor = Math.max(0, Math.min(n - 1, estado.cursor + mover));
    if (ev.shiftKey) marcarRango(antes, estado.cursor);
    pintarGrilla();
    document.querySelector(`.celda[data-i="${estado.cursor}"]`)
      ?.scrollIntoView({ block: "nearest" });
    if (estado.panel) abrirPanel(estado.cursor);     // el panel sigue al cursor
    return;
  }
  switch (ev.key.toLowerCase()) {
    case " ": ev.preventDefault(); alternar(estado.cursor); pintarGrilla(); break;
    case "a": confirmar(seleccionadas()); break;
    case "r": descartar(seleccionadas()); break;
    case "z": abrirPanel(estado.cursor); break;
    case "l": estado.corte = estado.cursor; pintarGrilla(); break;
    case "enter":
      if (estado.corte >= 0) confirmar(estado.resultados.slice(0, estado.corte + 1));
      break;
    case "escape":
      if (estado.panel) cerrarPanel();
      else { estado.seleccion = new Set(); pintarGrilla(); }
      break;
  }
});

// Arranque: si ya hay sesión, entra directo.
iniciar().catch(() => mostrarEntrada());
