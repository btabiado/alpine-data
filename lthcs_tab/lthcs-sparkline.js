/**
 * LTHCS Sparkline — vanilla SVG renderer for per-ticker score history.
 *
 * Produces a minimalist sparkline suitable for both:
 *   - Small per-card sparklines (~120x24px) on the LTHCS leaderboard.
 *   - Larger detail-modal charts (~600x220px) with axes and band guides.
 *
 * Pure DOM module — no innerHTML, no frameworks. Returns an SVGElement.
 */

const SVG_NS = "http://www.w3.org/2000/svg";

/**
 * Default band-color map. Mirrors weights.json `band_colors`.
 * Score buckets are inclusive on the lower edge, exclusive on the upper
 * (except the elite bucket which is fully inclusive of 100).
 */
const DEFAULT_BAND_COLORS = {
  elite: "#1F3A5F",        // 90 - 100
  high: "#4A8F5F",         // 80 - 89
  constructive: "#C9A227", // 70 - 79
  monitor: "#D89148",      // 60 - 69
  weakening: "#B85A3E",    // 50 - 59
  review: "#7A2E1F",       //  0 - 49
};

/**
 * Resolve the band color for a given score, matching weights.json bands.
 * Returns a CSS color string from the band_colors map or null if score is non-numeric.
 *
 * @param {number} score
 * @param {Object<string,string>|null} [bandColors] Optional override map keyed by band name.
 * @returns {string|null}
 */
export function bandColorForScore(score, bandColors = null) {
  if (typeof score !== "number" || !Number.isFinite(score)) return null;
  const colors = bandColors || DEFAULT_BAND_COLORS;
  if (score >= 90) return colors.elite || DEFAULT_BAND_COLORS.elite;
  if (score >= 80) return colors.high || DEFAULT_BAND_COLORS.high;
  if (score >= 70) return colors.constructive || DEFAULT_BAND_COLORS.constructive;
  if (score >= 60) return colors.monitor || DEFAULT_BAND_COLORS.monitor;
  if (score >= 50) return colors.weakening || DEFAULT_BAND_COLORS.weakening;
  return colors.review || DEFAULT_BAND_COLORS.review;
}

/**
 * Render a sparkline as an SVGElement.
 *
 * @param {Array<{date: string, score: number, band?: string}>} history
 *        Newest-first or oldest-first; function detects and sorts ascending internally.
 * @param {Object} [options]
 * @param {number}  [options.width=120]       Pixel width (also used for viewBox).
 * @param {number}  [options.height=24]       Pixel height (also used for viewBox).
 * @param {boolean} [options.showBands=false] Faint horizontal guides at 50/70/80/90.
 * @param {boolean} [options.showAxes=false]  Y-tick labels (0/25/50/75/100) and first/last date labels.
 * @param {boolean} [options.showLastDot=true] Filled circle on the most-recent point.
 * @param {string}  [options.strokeColor="currentColor"] Line color.
 * @param {string|null} [options.fillColor=null] Area fill below the line (e.g. "currentColor").
 * @returns {SVGElement}
 */
export function renderSparkline(history, options = {}) {
  const {
    width = 120,
    height = 24,
    showBands = false,
    showAxes = false,
    showLastDot = true,
    strokeColor = "currentColor",
    fillColor = null,
  } = options;

  const svg = document.createElementNS(SVG_NS, "svg");
  svg.setAttribute("width", String(width));
  svg.setAttribute("height", String(height));
  svg.setAttribute("viewBox", `0 0 ${width} ${height}`);
  svg.setAttribute("preserveAspectRatio", "none");
  svg.setAttribute("role", "img");
  // Detail-view margins are larger to leave room for axis labels and band guides.
  const isDetail = showBands || showAxes;
  const marginTop = isDetail ? 12 : 2;
  const marginBottom = isDetail ? (showAxes ? 18 : 12) : 2;
  const marginLeft = showAxes ? 28 : 0;
  const marginRight = showBands ? 20 : 0;

  // Empty-history early return: placeholder text, accessible label.
  if (!Array.isArray(history) || history.length === 0) {
    svg.setAttribute("aria-label", "Score history sparkline; no history yet");
    const txt = document.createElementNS(SVG_NS, "text");
    txt.setAttribute("x", String(width / 2));
    txt.setAttribute("y", String(height / 2));
    txt.setAttribute("text-anchor", "middle");
    txt.setAttribute("dominant-baseline", "middle");
    txt.setAttribute("font-size", String(Math.max(9, Math.min(12, height / 2))));
    txt.setAttribute("fill", "currentColor");
    txt.setAttribute("opacity", "0.5");
    txt.textContent = "no history yet";
    svg.appendChild(txt);
    return svg;
  }

  // Defensive copy + ascending sort by date string (ISO YYYY-MM-DD sorts lexically).
  const points = history
    .filter((p) => p && typeof p.score === "number" && Number.isFinite(p.score))
    .slice()
    .sort((a, b) => (a.date < b.date ? -1 : a.date > b.date ? 1 : 0));

  if (points.length === 0) {
    svg.setAttribute("aria-label", "Score history sparkline; no history yet");
    return svg;
  }

  const plotLeft = marginLeft;
  const plotRight = width - marginRight;
  const plotTop = marginTop;
  const plotBottom = height - marginBottom;
  const plotWidth = Math.max(1, plotRight - plotLeft);
  const plotHeight = Math.max(1, plotBottom - plotTop);

  // Y scale: score 0..100 mapped to plotBottom..plotTop (inverted).
  const yFor = (score) => plotTop + (1 - score / 100) * plotHeight;

  // X scale: index 0..N-1 mapped linearly. For single-point we center it.
  const xFor = (i, n) =>
    n <= 1 ? plotLeft + plotWidth / 2 : plotLeft + (i / (n - 1)) * plotWidth;

  const last = points[points.length - 1];
  svg.setAttribute(
    "aria-label",
    `Score history sparkline; current ${last.score.toFixed(1)}`,
  );

  // --- Band guides (detail mode) --------------------------------------------
  if (showBands) {
    for (const threshold of [50, 70, 80, 90]) {
      const y = yFor(threshold);
      const line = document.createElementNS(SVG_NS, "line");
      line.setAttribute("x1", String(plotLeft));
      line.setAttribute("x2", String(plotRight));
      line.setAttribute("y1", String(y));
      line.setAttribute("y2", String(y));
      line.setAttribute("stroke", "currentColor");
      line.setAttribute("stroke-width", "1");
      line.setAttribute("opacity", "0.1");
      line.setAttribute("stroke-dasharray", "2,3");
      svg.appendChild(line);

      const label = document.createElementNS(SVG_NS, "text");
      label.setAttribute("x", String(plotRight + 2));
      label.setAttribute("y", String(y));
      label.setAttribute("font-size", "9");
      label.setAttribute("fill", "currentColor");
      label.setAttribute("opacity", "0.4");
      label.setAttribute("dominant-baseline", "middle");
      label.textContent = String(threshold);
      svg.appendChild(label);
    }
  }

  // --- Y-axis ticks (detail mode) -------------------------------------------
  if (showAxes) {
    for (const tick of [0, 25, 50, 75, 100]) {
      const y = yFor(tick);
      const label = document.createElementNS(SVG_NS, "text");
      label.setAttribute("x", String(plotLeft - 4));
      label.setAttribute("y", String(y));
      label.setAttribute("font-size", "9");
      label.setAttribute("fill", "currentColor");
      label.setAttribute("opacity", "0.55");
      label.setAttribute("text-anchor", "end");
      label.setAttribute("dominant-baseline", "middle");
      label.textContent = String(tick);
      svg.appendChild(label);
    }

    // First/last date labels along the x-axis.
    const firstDate = points[0].date;
    const lastDate = points[points.length - 1].date;
    const dateY = plotBottom + 12;

    const firstLabel = document.createElementNS(SVG_NS, "text");
    firstLabel.setAttribute("x", String(plotLeft));
    firstLabel.setAttribute("y", String(dateY));
    firstLabel.setAttribute("font-size", "9");
    firstLabel.setAttribute("fill", "currentColor");
    firstLabel.setAttribute("opacity", "0.55");
    firstLabel.setAttribute("text-anchor", "start");
    firstLabel.textContent = firstDate;
    svg.appendChild(firstLabel);

    if (points.length > 1) {
      const lastLabel = document.createElementNS(SVG_NS, "text");
      lastLabel.setAttribute("x", String(plotRight));
      lastLabel.setAttribute("y", String(dateY));
      lastLabel.setAttribute("font-size", "9");
      lastLabel.setAttribute("fill", "currentColor");
      lastLabel.setAttribute("opacity", "0.55");
      lastLabel.setAttribute("text-anchor", "end");
      lastLabel.textContent = lastDate;
      svg.appendChild(lastLabel);
    }
  }

  // --- Single-point case ----------------------------------------------------
  if (points.length === 1) {
    const cx = xFor(0, 1);
    const cy = yFor(points[0].score);
    const dot = document.createElementNS(SVG_NS, "circle");
    dot.setAttribute("cx", String(cx));
    dot.setAttribute("cy", String(cy));
    dot.setAttribute("r", String(isDetail ? 3 : 2));
    dot.setAttribute("fill", strokeColor);
    svg.appendChild(dot);
    return svg;
  }

  // --- Path construction (2+ points) ----------------------------------------
  const coords = points.map((p, i) => ({
    x: xFor(i, points.length),
    y: yFor(p.score),
  }));

  const d = coords
    .map((c, i) => `${i === 0 ? "M" : "L"}${c.x.toFixed(2)},${c.y.toFixed(2)}`)
    .join(" ");

  // Optional area fill — close the path down to the baseline.
  if (fillColor) {
    const areaD =
      d +
      ` L${coords[coords.length - 1].x.toFixed(2)},${plotBottom.toFixed(2)}` +
      ` L${coords[0].x.toFixed(2)},${plotBottom.toFixed(2)} Z`;
    const area = document.createElementNS(SVG_NS, "path");
    area.setAttribute("d", areaD);
    area.setAttribute("fill", fillColor);
    area.setAttribute("stroke", "none");
    svg.appendChild(area);
  }

  const path = document.createElementNS(SVG_NS, "path");
  path.setAttribute("d", d);
  path.setAttribute("fill", "none");
  path.setAttribute("stroke", strokeColor);
  path.setAttribute("stroke-width", isDetail ? "1.5" : "1");
  path.setAttribute("stroke-linecap", "round");
  path.setAttribute("stroke-linejoin", "round");
  svg.appendChild(path);

  // --- Last-point dot -------------------------------------------------------
  if (showLastDot) {
    const lastCoord = coords[coords.length - 1];
    const dot = document.createElementNS(SVG_NS, "circle");
    dot.setAttribute("cx", String(lastCoord.x));
    dot.setAttribute("cy", String(lastCoord.y));
    dot.setAttribute("r", String(isDetail ? 3 : 1.75));
    dot.setAttribute("fill", strokeColor);
    svg.appendChild(dot);
  }

  return svg;
}
