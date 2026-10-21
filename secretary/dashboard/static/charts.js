'use strict';
// Grafici SVG senza dipendenze. Solo API del DOM, mai markup testuale: compatibili con la CSP della dashboard.
// I colori vengono dal CSS (classi `c1…c4`, `lvl-*`), così seguono il tema chiaro/scuro.
const SVG_NS = 'http://www.w3.org/2000/svg';

function svg(tag, attrs, ...children) {
  const node = document.createElementNS(SVG_NS, tag);
  Object.entries(attrs || {}).forEach(([name, value]) => {
    if (value !== undefined && value !== null && value !== false) node.setAttribute(name, String(value));
  });
  children.flat().forEach(child => {if (child) node.append(child);});
  return node;
}

// Estremo "tondo" dell'asse verticale: 1, 2, 5 × 10^n, mai sotto il valore massimo.
function niceMax(value) {
  if (!(value > 0)) return 1;
  const power = Math.pow(10, Math.floor(Math.log10(value)));
  const fraction = value / power;
  const step = fraction <= 1 ? 1 : fraction <= 2 ? 2 : fraction <= 5 ? 5 : 10;
  return step * power;
}

const compactNumber = value => {
  const number = Number(value) || 0;
  if (number >= 1e6) return `${(number / 1e6).toLocaleString('it', {maximumFractionDigits: 1})} Mln`;
  if (number >= 1e3) return `${(number / 1e3).toLocaleString('it', {maximumFractionDigits: 1})} k`;
  return number.toLocaleString('it');
};

// Linea compatta con area: per i KPI. `values` = numeri già allineati ai giorni.
function sparkline(values, {width = 120, height = 36, label = ''} = {}) {
  const points = values.map(Number).map(value => (Number.isFinite(value) ? value : 0));
  const root = svg('svg', {class: 'spark', viewBox: `0 0 ${width} ${height}`, preserveAspectRatio: 'none',
    role: 'img', 'aria-label': label || `Andamento: ${points.join(', ')}`});
  if (points.length < 2) return root;
  const max = Math.max(...points, 1);
  const pad = 3;
  const x = index => pad + (index * (width - pad * 2)) / (points.length - 1);
  const y = value => height - pad - (value / max) * (height - pad * 2);
  const line = points.map((value, index) => `${index ? 'L' : 'M'}${x(index).toFixed(1)} ${y(value).toFixed(1)}`).join(' ');
  root.append(
    svg('path', {class: 'spark-area', d: `${line} L${x(points.length - 1).toFixed(1)} ${height} L${x(0).toFixed(1)} ${height} Z`}),
    svg('path', {class: 'spark-line', d: line}));
  return root;
}

// Barre impilate per giorno. series = [{key, label, values, cls}], days = ['2026-10-21', …].
// `onhover(index | -1)` aggiorna la riga di lettura sotto al grafico (anche da tastiera).
// `tick(day, isLast)` etichetta l'asse, `describe(day)` il nome per le tecnologie assistive.
const dayTick = (day, isLast) => (isLast ? 'oggi' : String(new Date(`${day}T12:00:00`).getDate()));
const dayName = day => new Date(`${day}T12:00:00`).toLocaleDateString('it', {day: 'numeric', month: 'long'});
function stackedBars(days, series, {width = 640, height = 220, label = 'Grafico a barre', onhover, format = compactNumber,
  tick = dayTick, describe = dayName} = {}) {
  const margin = {top: 10, right: 8, bottom: 26, left: 40};
  const plotW = width - margin.left - margin.right;
  const plotH = height - margin.top - margin.bottom;
  const totals = days.map((_, index) => series.reduce((sum, item) => sum + (Number(item.values[index]) || 0), 0));
  const top = niceMax(Math.max(...totals, 1));
  const slot = plotW / Math.max(days.length, 1);
  const barW = Math.min(34, slot * 0.62);
  const root = svg('svg', {class: 'chart chart-bars', viewBox: `0 0 ${width} ${height}`, role: 'group', 'aria-label': label});
  [0, 0.5, 1].forEach(fraction => {
    const yy = margin.top + plotH - fraction * plotH;
    root.append(svg('line', {class: 'grid', x1: margin.left, x2: width - margin.right, y1: yy, y2: yy}),
      (() => {const t = svg('text', {class: 'axis', x: margin.left - 6, y: yy + 3.5, 'text-anchor': 'end'}); t.textContent = format(top * fraction); return t;})());
  });
  days.forEach((day, index) => {
    const cx = margin.left + slot * index + slot / 2;
    const column = svg('g', {class: 'bar-col', tabindex: 0, role: 'img',
      'aria-label': `${describe(day)}: ${series.map(item => `${item.label} ${item.values[index] || 0}`).join(', ')}`});
    let used = 0;
    column.append(svg('rect', {class: 'bar-hit', x: cx - slot / 2, y: margin.top, width: slot, height: plotH}));
    series.forEach(item => {
      const value = Number(item.values[index]) || 0;
      if (!value) return;
      const barH = (value / top) * plotH;
      column.append(svg('rect', {class: `bar ${item.cls}`, x: cx - barW / 2, y: margin.top + plotH - used - barH,
        width: barW, height: Math.max(barH, 1), rx: 2}));
      used += barH;
    });
    const isToday = index === days.length - 1;
    // Con molte colonne si etichetta un giorno su due, sempre contando da oggi.
    if (days.length <= 10 || (days.length - 1 - index) % 2 === 0) {
      const text = svg('text', {class: `axis${isToday ? ' axis-today' : ''}`, x: cx, y: height - 8, 'text-anchor': 'middle'});
      text.textContent = tick(day, isToday);
      column.append(text);
    }
    if (onhover) {
      column.onmouseenter = () => onhover(index);
      column.onfocus = () => onhover(index);
      column.onmouseleave = () => onhover(-1);
      column.onblur = () => onhover(-1);
    }
    root.append(column);
  });
  return root;
}

// Anello di avanzamento (0–1) con valore al centro. `level` = ok | warn | high | full.
function ring(ratio, level, {size = 112, thickness = 11, text = '', caption = ''} = {}) {
  const radius = (size - thickness) / 2;
  const circumference = 2 * Math.PI * radius;
  const safe = Number.isFinite(ratio) ? Math.min(1, Math.max(0, ratio)) : 1;
  // `svg()` ignora i figli falsi: con 0% niente tratto (il capo arrotondato disegnerebbe comunque un punto).
  const root = svg('svg', {class: 'ring', viewBox: `0 0 ${size} ${size}`, role: 'img', 'aria-label': caption || text},
    svg('circle', {class: 'ring-track', cx: size / 2, cy: size / 2, r: radius, 'stroke-width': thickness, fill: 'none'}),
    safe > 0 && svg('circle', {class: `ring-value lvl-${level}`, cx: size / 2, cy: size / 2, r: radius, 'stroke-width': thickness, fill: 'none',
      'stroke-linecap': 'round', 'stroke-dasharray': `${(safe * circumference).toFixed(2)} ${circumference.toFixed(2)}`,
      transform: `rotate(-90 ${size / 2} ${size / 2})`}));
  const label = svg('text', {class: 'ring-text', x: size / 2, y: size / 2 + 5, 'text-anchor': 'middle'});
  label.textContent = text;
  root.append(label);
  return root;
}

// Ciambella a segmenti. parts = [{label, value, cls}].
function donut(parts, {size = 132, thickness = 16, center = '', label = 'Ripartizione'} = {}) {
  const radius = (size - thickness) / 2;
  const circumference = 2 * Math.PI * radius;
  const total = parts.reduce((sum, part) => sum + (Number(part.value) || 0), 0);
  const root = svg('svg', {class: 'donut', viewBox: `0 0 ${size} ${size}`, role: 'img',
    'aria-label': `${label}: ${parts.map(part => `${part.label} ${part.value}`).join(', ')}`});
  root.append(svg('circle', {class: 'ring-track', cx: size / 2, cy: size / 2, r: radius, 'stroke-width': thickness, fill: 'none'}));
  let offset = 0;
  if (total > 0) {
    parts.forEach(part => {
      const value = Number(part.value) || 0;
      if (!value) return;
      const length = (value / total) * circumference;
      root.append(svg('circle', {class: `donut-part ${part.cls}`, cx: size / 2, cy: size / 2, r: radius,
        'stroke-width': thickness, fill: 'none',
        'stroke-dasharray': `${Math.max(length - 1.5, 0.5).toFixed(2)} ${(circumference - Math.max(length - 1.5, 0.5)).toFixed(2)}`,
        'stroke-dashoffset': (-offset).toFixed(2), transform: `rotate(-90 ${size / 2} ${size / 2})`}));
      offset += length;
    });
  }
  const text = svg('text', {class: 'ring-text', x: size / 2, y: size / 2 + 6, 'text-anchor': 'middle'});
  text.textContent = center;
  root.append(text);
  return root;
}
