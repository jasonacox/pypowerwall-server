/*
 * PWCharts - canvas charts shared by the Console (index.html) and the
 * History page (history.html), so a chart fix lands once.
 *
 *   PWCharts.energyTrend(container, opts) -> { setData, note }
 *       The Energy Trend: solar / home / battery / grid kW (left axis,
 *       translucent fill to zero) plus battery level % (dashed, fixed
 *       0-100 % right axis), legend toggles, hover crosshair + tooltip and a
 *       screen-reader summary. Battery kW is positive when discharging,
 *       grid kW positive when importing.
 *   PWCharts.makeChart(container, short) -> canvas
 *   PWCharts.drawChart(canvas, cfg)
 *       Generic line/bar chart (day or time x-axis, min/max bands).
 *
 * Every chart registers itself; one window resize handler redraws them all,
 * and charts whose canvas has left the page are dropped (PWCharts.prune()
 * runs on each redraw and can be called after re-rendering). Styles live in
 * /static/css/charts.css. No dependencies, so the pages work offline.
 */
(function () {
    'use strict';

    const DAY = 86400;
    const MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];
    const WEEKDAYS = ['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat'];

    // ---------------------------------------------------------------------
    // Helpers
    // ---------------------------------------------------------------------
    const esc = s => String(s).replace(/[&<>"']/g, c => ({
        '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'
    }[c]));
    const pad = n => String(n).padStart(2, '0');
    // Axis label on the 12-hour clock: "1p", "1:30p"
    function fmt12h(d) {
        const h = d.getHours();
        const m = d.getMinutes();
        return `${h % 12 || 12}${m ? ':' + pad(m) : ''}${h >= 12 ? 'p' : 'a'}`;
    }
    function parseDay(s) { const [y, m, d] = s.split('-').map(Number); return new Date(y, m - 1, d); }
    // Day strings are placed at UTC noon so day-axis labels never shift by timezone
    function dayX(s) { const [y, m, d] = s.split('-').map(Number); return Date.UTC(y, m - 1, d) / 1000 + DAY / 2; }
    function xDay(x) { const d = new Date((x - DAY / 2) * 1000); return `${d.getUTCFullYear()}-${pad(d.getUTCMonth() + 1)}-${pad(d.getUTCDate())}`; }
    function fmtDayLong(s) {
        const d = parseDay(s);
        return `${WEEKDAYS[d.getDay()]} ${MONTHS[d.getMonth()]} ${d.getDate()}, ${d.getFullYear()}`;
    }
    // 12-hour clock everywhere, like the Console: "Sep 27, 1:30pm"
    function fmtTimeLong(ts, withSeconds) {
        const d = new Date(ts * 1000);
        const secs = withSeconds ? `:${pad(d.getSeconds())}` : '';
        return `${MONTHS[d.getMonth()]} ${d.getDate()}, ${d.getHours() % 12 || 12}:${pad(d.getMinutes())}${secs}${d.getHours() >= 12 ? 'pm' : 'am'}`;
    }
    function niceStep(range, target) {
        const raw = range / Math.max(1, target);
        const mag = Math.pow(10, Math.floor(Math.log10(raw)));
        const n = raw / mag;
        return (n < 1.5 ? 1 : n < 3 ? 2 : n < 7 ? 5 : 10) * mag;
    }

    // Local-time-aligned ticks between xMin and xMax (epoch seconds): the
    // largest step giving at most maxTicks, starting on a step boundary
    function timeTicks(xMin, xMax, maxTicks) {
        const steps = [300, 600, 900, 1800, 3600, 7200, 10800, 21600, 43200, DAY, 2 * DAY, 7 * DAY, 14 * DAY];
        const step = steps.find(s => (xMax - xMin) / s <= maxTicks) || 30 * DAY;
        const offset = -new Date(xMin * 1000).getTimezoneOffset() * 60;
        const first = Math.ceil((xMin + offset) / step) * step - offset;
        const ticks = [];
        for (let x = first; x <= xMax; x += step) ticks.push({ x, step, date: new Date(x * 1000) });
        return ticks;
    }

    function xTicks(cfg, plotW) {
        const span = cfg.xMax - cfg.xMin;
        const maxTicks = Math.max(2, Math.floor(plotW / 80));
        if (cfg.xMode === 'day') {
            const ticks = [];
            const days = span / DAY;
            const step = [1, 2, 7, 14, 30, 61, 91, 182, 365].find(s => days / s <= maxTicks) || 730;
            const first = Math.ceil((cfg.xMin - DAY / 2) / DAY) * DAY + DAY / 2;
            for (let x = first; x <= cfg.xMax; x += step * DAY) {
                const d = new Date((x - DAY / 2) * 1000);
                const label = days > 400
                    ? `${MONTHS[d.getUTCMonth()]} '${String(d.getUTCFullYear()).slice(2)}`
                    : `${MONTHS[d.getUTCMonth()]} ${d.getUTCDate()}`;
                ticks.push({ x, label });
            }
            return ticks;
        }
        return timeTicks(cfg.xMin, cfg.xMax, maxTicks).map(t => {
            const d = t.date;
            const midnight = d.getHours() === 0 && d.getMinutes() === 0;
            return {
                x: t.x,
                label: t.step >= DAY || midnight
                    ? `${MONTHS[d.getMonth()]} ${d.getDate()}`
                    : fmt12h(d),
            };
        });
    }

    // ---------------------------------------------------------------------
    // Registry: one resize handler for every chart; detached charts dropped
    // ---------------------------------------------------------------------
    const registry = new Map();  // canvas -> { redraw, ... }

    function prune() {
        for (const canvas of registry.keys()) if (!canvas.isConnected) registry.delete(canvas);
    }
    function redrawAll() {
        prune();
        for (const entry of registry.values()) entry.redraw();
    }
    let resizeTimer;
    window.addEventListener('resize', () => {
        clearTimeout(resizeTimer);
        resizeTimer = setTimeout(redrawAll, 100);
    });

    function sizeCanvas(canvas) {
        const rect = canvas.getBoundingClientRect();
        const dpr = window.devicePixelRatio || 1;
        canvas.width = Math.round(rect.width * dpr);
        canvas.height = Math.round(rect.height * dpr);
        const ctx = canvas.getContext('2d');
        ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
        ctx.clearRect(0, 0, rect.width, rect.height);
        return { ctx, w: rect.width, h: rect.height };
    }

    // ---------------------------------------------------------------------
    // Generic chart (History page)
    // ---------------------------------------------------------------------
    function drawChart(canvas, cfg) {
        const wrap = canvas.parentElement;
        if (!canvas.clientWidth || !canvas.clientHeight) return;
        const { ctx, w, h } = sizeCanvas(canvas);

        const padding = { l: 52, r: 14, t: cfg.yUnit ? 22 : 10, b: 26 };
        const plotW = w - padding.l - padding.r;
        const plotH = h - padding.t - padding.b;
        const series = cfg.series.filter(s => s.visible !== false && s.points.length);
        // Min/max bands turn to mud when many series overlap; keep them for 1-2 lines
        const showBands = series.filter(s => s.band).length <= 2;

        let lo = Infinity, hi = -Infinity;
        for (const s of series) for (const p of s.points) {
            for (const v of [p.y, p.lo, p.hi]) if (v != null && isFinite(v)) { lo = Math.min(lo, v); hi = Math.max(hi, v); }
        }
        if (!isFinite(lo)) { lo = 0; hi = 1; }
        if (cfg.zeroBased) { lo = Math.min(0, lo); hi = Math.max(0, hi); }
        if (hi - lo < 1e-9) { hi += 1; if (!cfg.zeroBased) lo -= 1; }
        if (!cfg.zeroBased) { const padY = (hi - lo) * 0.08; lo -= padY; hi += padY; }
        const step = niceStep(hi - lo, Math.max(3, Math.floor(plotH / 55)));
        lo = Math.floor(lo / step) * step;
        hi = Math.ceil(hi / step) * step;

        const X = x => padding.l + (x - cfg.xMin) / (cfg.xMax - cfg.xMin) * plotW;
        const Y = y => padding.t + (1 - (y - lo) / (hi - lo)) * plotH;

        ctx.font = '11px -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif';
        ctx.fillStyle = '#8b949e';
        ctx.strokeStyle = 'rgba(48, 54, 61, 0.8)';
        ctx.lineWidth = 1;
        ctx.textAlign = 'right';
        ctx.textBaseline = 'middle';
        const dp = step < 1 ? (step < 0.1 ? 2 : 1) : 0;
        for (let v = lo; v <= hi + step / 2; v += step) {
            const y = Math.round(Y(v)) + 0.5;
            ctx.beginPath(); ctx.moveTo(padding.l, y); ctx.lineTo(w - padding.r, y); ctx.stroke();
            ctx.fillText(v.toFixed(dp), padding.l - 6, y);
        }
        if (cfg.yUnit) {
            ctx.textAlign = 'left';
            ctx.textBaseline = 'top';
            ctx.fillText(cfg.yUnit, 4, 0);
        }
        ctx.textAlign = 'center';
        ctx.textBaseline = 'top';
        for (const t of xTicks(cfg, plotW)) {
            const x = X(t.x);
            if (x < padding.l - 1 || x > w - padding.r + 1) continue;
            ctx.fillText(t.label, Math.min(Math.max(x, padding.l + 20), w - padding.r - 20), h - padding.b + 7);
        }

        ctx.save();
        ctx.beginPath();
        ctx.rect(padding.l, padding.t - 2, plotW, plotH + 4);
        ctx.clip();

        // Bars (grouped per slot); fall back to lines when too thin to read
        const bars = series.filter(s => s.type === 'bar');
        const slotPx = cfg.slot ? cfg.slot / (cfg.xMax - cfg.xMin) * plotW : 0;
        const barW = bars.length ? slotPx * 0.8 / bars.length : 0;
        const barsAsLines = bars.length && barW < 2;
        if (bars.length && !barsAsLines) {
            const zeroY = Y(Math.max(lo, 0));
            bars.forEach((s, i) => {
                ctx.fillStyle = s.color;
                for (const p of s.points) {
                    if (p.y == null) continue;
                    const x = X(p.x) - slotPx * 0.4 + i * barW;
                    const y = Y(p.y);
                    ctx.fillRect(x, Math.min(y, zeroY), Math.max(1, barW - (barW > 4 ? 1 : 0)), Math.abs(zeroY - y) || 1);
                }
            });
        }

        // Lines (with optional min/max band)
        const lines = series.filter(s => s.type !== 'bar' || barsAsLines);
        const gapLimit = (cfg.gap || cfg.slot || 0) * 2.5;
        for (const s of lines) {
            const segments = [];
            let seg = [];
            for (const p of s.points) {
                if (p.y == null) { if (seg.length) segments.push(seg); seg = []; continue; }
                if (seg.length && gapLimit && p.x - seg[seg.length - 1].x > gapLimit) { segments.push(seg); seg = []; }
                seg.push(p);
            }
            if (seg.length) segments.push(seg);
            if (s.band && showBands) {
                ctx.fillStyle = s.color + '26';
                for (const g of segments) {
                    if (g.length < 2 || g[0].lo == null) continue;
                    ctx.beginPath();
                    g.forEach((p, i) => i ? ctx.lineTo(X(p.x), Y(p.hi)) : ctx.moveTo(X(p.x), Y(p.hi)));
                    for (let i = g.length - 1; i >= 0; i--) ctx.lineTo(X(g[i].x), Y(g[i].lo));
                    ctx.closePath();
                    ctx.fill();
                }
            }
            ctx.strokeStyle = s.color;
            ctx.lineWidth = 1.75;
            ctx.setLineDash(s.dash || []);
            for (const g of segments) {
                ctx.beginPath();
                g.forEach((p, i) => i ? ctx.lineTo(X(p.x), Y(p.y)) : ctx.moveTo(X(p.x), Y(p.y)));
                ctx.stroke();
                if (g.length === 1) {
                    ctx.fillStyle = s.color;
                    ctx.beginPath(); ctx.arc(X(g[0].x), Y(g[0].y), 2.5, 0, Math.PI * 2); ctx.fill();
                }
            }
            ctx.setLineDash([]);
        }

        // Hover crosshair
        const st = registry.get(canvas);
        if (st && st.hoverX != null) {
            ctx.strokeStyle = 'rgba(230, 237, 243, 0.35)';
            ctx.lineWidth = 1;
            const hx = Math.round(X(st.hoverX)) + 0.5;
            ctx.beginPath(); ctx.moveTo(hx, padding.t); ctx.lineTo(hx, padding.t + plotH); ctx.stroke();
        }
        ctx.restore();

        // Sorted union of x values for hover lookup
        const xs = Array.from(new Set(series.flatMap(s => s.points.filter(p => p.y != null).map(p => p.x)))).sort((a, b) => a - b);
        const tipSeries = cfg.tooltipAll ? cfg.series.filter(s => s.points.length) : series;
        registry.set(canvas, {
            cfg, X, padding, plotW, xs, series: tipSeries, wrap,
            hoverX: st ? st.hoverX : null,
            redraw: () => drawChart(canvas, cfg),
        });
    }

    function nearestX(xs, x) {
        if (!xs.length) return null;
        let a = 0, b = xs.length - 1;
        while (b - a > 1) { const m = (a + b) >> 1; if (xs[m] < x) a = m; else b = m; }
        return Math.abs(xs[a] - x) <= Math.abs(xs[b] - x) ? xs[a] : xs[b];
    }

    function attachHover(canvas, tooltip) {
        function move(ev) {
            const st = registry.get(canvas);
            if (!st || !st.xs.length) return;
            const rect = canvas.getBoundingClientRect();
            const px = ev.clientX - rect.left;
            const x = st.cfg.xMin + (px - st.padding.l) / st.plotW * (st.cfg.xMax - st.cfg.xMin);
            const hx = nearestX(st.xs, x);
            st.hoverX = hx;
            drawChart(canvas, st.cfg);
            tooltip.replaceChildren();
            const title = document.createElement('div');
            title.className = 'tt-title';
            title.textContent = st.cfg.formatX
                ? st.cfg.formatX(hx)
                : st.cfg.xMode === 'day' ? fmtDayLong(xDay(hx)) : fmtTimeLong(hx);
            tooltip.appendChild(title);
            for (const s of st.series) {
                const p = s.points.find(q => q.x === hx);
                if (!p || p.y == null) continue;
                const row = document.createElement('div');
                row.className = 'tt-row';
                const dot = document.createElement('span');
                dot.className = 'dot';
                dot.style.background = s.color;
                const label = document.createElement('span');
                label.textContent = s.label;
                const v = document.createElement('span');
                v.className = 'v';
                v.textContent = st.cfg.formatValue(p, s);
                if (s.visible === false) row.style.opacity = '0.6';
                row.append(dot, label, v);
                tooltip.appendChild(row);
            }
            tooltip.hidden = false;
            const wrapRect = st.wrap.getBoundingClientRect();
            const tipW = tooltip.offsetWidth;
            let left = st.X(hx) + 14;
            if (left + tipW > wrapRect.width) left = st.X(hx) - tipW - 14;
            tooltip.style.left = Math.max(0, left) + 'px';
            tooltip.style.top = '8px';
        }
        function leave() {
            const st = registry.get(canvas);
            tooltip.hidden = true;
            if (st) { st.hoverX = null; drawChart(canvas, st.cfg); }
        }
        canvas.addEventListener('pointermove', move);
        canvas.addEventListener('pointerdown', move);
        canvas.addEventListener('pointerleave', leave);
    }

    function makeChart(container, short) {
        const wrap = document.createElement('div');
        wrap.className = 'chart-wrap' + (short ? ' short' : '');
        const canvas = document.createElement('canvas');
        canvas.setAttribute('role', 'img');
        const tooltip = document.createElement('div');
        tooltip.className = 'tooltip';
        tooltip.hidden = true;
        wrap.append(canvas, tooltip);
        container.appendChild(wrap);
        attachHover(canvas, tooltip);
        return canvas;
    }

    // ---------------------------------------------------------------------
    // Energy Trend (Console + History)
    // ---------------------------------------------------------------------
    const TREND_SERIES = [
        { key: 'solar_kw', label: 'Solar', color: '#f0c000', axis: 'kw' },
        { key: 'home_kw', label: 'Home', color: '#58a6ff', axis: 'kw' },
        { key: 'battery_kw', label: 'Battery', color: '#3fb950', axis: 'kw' },
        { key: 'grid_kw', label: 'Grid', color: '#8b949e', axis: 'kw' },
        { key: 'battery_level', label: 'Battery Level', color: '#3fb950', axis: 'pct' },
    ];
    const TREND_PAD = { l: 48, r: 44, t: 12, b: 24 };
    let trendCount = 0;

    function hoverTime(ts) {
        const d = new Date(ts * 1000);
        const h = d.getHours() % 12 || 12;
        const ap = d.getHours() >= 12 ? 'pm' : 'am';
        return `${d.toLocaleDateString(undefined, { month: 'short', day: 'numeric' })} ${h}:${pad(d.getMinutes())}${ap}`;
    }

    /**
     * Build an Energy Trend chart inside `container`.
     * opts.hidden:   iterable of series keys to start hidden
     * opts.onToggle: (key, shown, hiddenSet) => void after a legend click
     */
    function energyTrend(container, opts) {
        opts = opts || {};
        const id = `energy-trend-${++trendCount}`;
        const hidden = new Set(opts.hidden || []);
        let points = null;
        let domain = null;
        let hover = null;

        container.classList.add('energy-trend');
        container.replaceChildren();
        const legend = document.createElement('div');
        legend.className = 'energy-trend-legend';
        const canvas = document.createElement('canvas');
        canvas.className = 'energy-trend-chart';
        canvas.setAttribute('role', 'img');
        canvas.setAttribute('aria-label', 'Energy trend chart');
        canvas.setAttribute('aria-describedby', `${id}-summary`);
        const tooltip = document.createElement('div');
        tooltip.className = 'energy-trend-tooltip';
        const note = document.createElement('div');
        note.className = 'energy-trend-note';
        const summary = document.createElement('p');
        summary.className = 'sr-only';
        summary.id = `${id}-summary`;
        container.append(legend, canvas, tooltip, note, summary);

        for (const s of TREND_SERIES) {
            const key = document.createElement('button');
            key.type = 'button';
            key.className = 'key';
            key.dataset.series = s.key;
            const sw = document.createElement('span');
            sw.className = 'swatch' + (s.axis === 'pct' ? ' dashed' : '');
            if (s.axis !== 'pct') sw.style.background = s.color;
            key.append(sw, s.axis === 'pct' ? 'Battery Level % ' : `${s.label} kW`);
            if (s.axis === 'pct') {
                const muted = document.createElement('span');
                muted.className = 'muted';
                muted.textContent = '(right axis)';
                key.appendChild(muted);
            }
            key.addEventListener('click', e => {
                e.stopPropagation();  // the Console card toggles on click
                if (hidden.has(s.key)) hidden.delete(s.key); else hidden.add(s.key);
                syncLegend();
                draw();
                updateTooltip();
                if (opts.onToggle) opts.onToggle(s.key, !hidden.has(s.key), new Set(hidden));
            });
            legend.appendChild(key);
        }
        function syncLegend() {
            for (const key of legend.children) {
                const shown = !hidden.has(key.dataset.series);
                key.classList.toggle('off', !shown);
                key.setAttribute('aria-pressed', String(shown));
            }
        }
        syncLegend();

        function dom() {
            return domain || { t0: points[0].ts, t1: points[points.length - 1].ts };
        }

        function draw() {
            const { ctx, w: W, h: H } = sizeCanvas(canvas);
            if (!points || points.length < 2 || W < 100) {
                hover = null;
                tooltip.style.display = 'none';
                return;
            }
            const plotW = W - TREND_PAD.l - TREND_PAD.r, plotH = H - TREND_PAD.t - TREND_PAD.b;
            const { t0, t1 } = dom();
            const xSpan = (t1 - t0) || 1;
            const X = ts => TREND_PAD.l + ((ts - t0) / xSpan) * plotW;

            let minKw = 0, maxKw = 0;
            for (const p of points) {
                for (const s of TREND_SERIES) {
                    if (s.axis !== 'kw' || hidden.has(s.key) || p[s.key] == null) continue;
                    minKw = Math.min(minKw, p[s.key]);
                    maxKw = Math.max(maxKw, p[s.key]);
                }
            }
            if (maxKw === minKw) maxKw = minKw + 1;
            const Y = kw => TREND_PAD.t + (1 - (kw - minKw) / (maxKw - minKw)) * plotH;
            const Ypct = pct => TREND_PAD.t + (1 - Math.max(0, Math.min(100, pct)) / 100) * plotH;

            // Gridlines, left kW labels, right % labels
            ctx.font = '11px system-ui, sans-serif';
            ctx.textBaseline = 'middle';
            for (let i = 0; i <= 4; i++) {
                const kw = minKw + ((maxKw - minKw) * i) / 4;
                const y = Y(kw);
                ctx.strokeStyle = 'rgba(139, 148, 158, 0.15)';
                ctx.lineWidth = 1;
                ctx.beginPath(); ctx.moveTo(TREND_PAD.l, y); ctx.lineTo(W - TREND_PAD.r, y); ctx.stroke();
                ctx.fillStyle = 'rgba(139, 148, 158, 0.8)';
                ctx.textAlign = 'right';
                // Skip a label that would collide with the "0" zero-line label
                if (!(minKw < 0 && Math.abs(y - Y(0)) < 12)) ctx.fillText(kw.toFixed(1), TREND_PAD.l - 6, y);
                ctx.textAlign = 'left';
                ctx.fillText(`${Math.round(i * 25)}%`, W - TREND_PAD.r + 6, y);
            }
            if (minKw < 0) {
                const zy = Y(0);
                ctx.strokeStyle = 'rgba(139, 148, 158, 0.4)';
                ctx.setLineDash([4, 4]);
                ctx.beginPath(); ctx.moveTo(TREND_PAD.l, zy); ctx.lineTo(W - TREND_PAD.r, zy); ctx.stroke();
                ctx.setLineDash([]);
                ctx.fillStyle = 'rgba(139, 148, 158, 0.9)';
                ctx.textAlign = 'right';
                ctx.fillText('0', TREND_PAD.l - 6, zy);
            }
            // Time axis: ticks on local clock boundaries ("1p", "1:30p"), ~6 at most
            ctx.fillStyle = 'rgba(139, 148, 158, 0.8)';
            ctx.textAlign = 'center';
            ctx.textBaseline = 'top';
            const maxTicks = Math.max(2, Math.min(6, Math.floor(plotW / 60)));
            for (const t of timeTicks(t0, t1, maxTicks)) {
                const x = X(t.x);
                if (x < TREND_PAD.l - 1 || x > W - TREND_PAD.r + 1) continue;
                ctx.fillText(fmt12h(t.date), x, H - TREND_PAD.b + 6);
            }

            const drawSeries = (key, color, dashed, yScale, fill) => {
                const segments = [];
                let seg = [];
                for (const p of points) {
                    const v = p[key];
                    if (v == null) { if (seg.length > 1) segments.push(seg); seg = []; }
                    else seg.push([X(p.ts), yScale(v)]);
                }
                if (seg.length > 1) segments.push(seg);
                for (const sg of segments) {
                    if (fill) {
                        const zy = Y(0);
                        ctx.fillStyle = color;
                        ctx.globalAlpha = 0.12;
                        ctx.beginPath();
                        ctx.moveTo(sg[0][0], zy);
                        for (const [sx, sy] of sg) ctx.lineTo(sx, sy);
                        ctx.lineTo(sg[sg.length - 1][0], zy);
                        ctx.closePath();
                        ctx.fill();
                        ctx.globalAlpha = 1;
                    }
                    ctx.strokeStyle = color;
                    ctx.lineWidth = 1.8;
                    if (dashed) ctx.setLineDash([5, 4]);
                    ctx.beginPath();
                    ctx.moveTo(sg[0][0], sg[0][1]);
                    for (let i = 1; i < sg.length; i++) ctx.lineTo(sg[i][0], sg[i][1]);
                    ctx.stroke();
                    ctx.setLineDash([]);
                }
            };
            for (const s of TREND_SERIES) {
                if (hidden.has(s.key)) continue;
                drawSeries(s.key, s.color, s.axis === 'pct', s.axis === 'pct' ? Ypct : Y, s.axis === 'kw');
            }

            if (hover) {
                const hx = X(hover.ts);
                ctx.strokeStyle = 'rgba(139, 148, 158, 0.5)';
                ctx.lineWidth = 1;
                ctx.setLineDash([3, 3]);
                ctx.beginPath(); ctx.moveTo(hx, TREND_PAD.t); ctx.lineTo(hx, H - TREND_PAD.b); ctx.stroke();
                ctx.setLineDash([]);
                for (const s of TREND_SERIES) {
                    if (hidden.has(s.key) || hover[s.key] == null) continue;
                    ctx.fillStyle = s.color;
                    ctx.beginPath();
                    ctx.arc(hx, s.axis === 'pct' ? Ypct(hover[s.key]) : Y(hover[s.key]), 3.5, 0, Math.PI * 2);
                    ctx.fill();
                    ctx.strokeStyle = 'rgba(13, 17, 23, 0.9)';
                    ctx.lineWidth = 1.5;
                    ctx.stroke();
                }
            }
        }

        function updateTooltip() {
            if (!hover) { tooltip.style.display = 'none'; return; }
            const rows = [`<div class="tt-time">${esc(hoverTime(hover.ts))}</div>`];
            for (const s of TREND_SERIES) {
                if (hidden.has(s.key) || hover[s.key] == null) continue;
                const v = hover[s.key];
                rows.push(`<div><span class="tt-dot" style="background:${s.color}"></span>${esc(s.label)}: <b>${esc(v.toFixed(s.axis === 'pct' ? 0 : 2))} ${s.axis === 'pct' ? '%' : 'kW'}</b></div>`);
            }
            tooltip.innerHTML = rows.join('');
            tooltip.style.display = 'block';
        }

        // Text alternative for screen readers: range of each series
        function updateSummary() {
            if (!points || points.length < 2) { summary.textContent = 'No energy trend data in this window.'; return; }
            const parts = [];
            for (const s of TREND_SERIES) {
                const vals = points.map(p => p[s.key]).filter(v => v != null);
                if (!vals.length) continue;
                const unit = s.axis === 'pct' ? '%' : ' kW';
                const dp = s.axis === 'pct' ? 0 : 1;
                parts.push(`${s.label} ${Math.min(...vals).toFixed(dp)} to ${Math.max(...vals).toFixed(dp)}${unit}`);
            }
            const { t0, t1 } = dom();
            summary.textContent = `Energy trend from ${hoverTime(t0)} to ${hoverTime(t1)}: ${parts.join('; ')}.`;
        }

        function leave() {
            hover = null;
            tooltip.style.display = 'none';
            draw();
        }
        function move(e) {
            if (!points || points.length < 2) return;
            const rect = canvas.getBoundingClientRect();
            const x = e.clientX - rect.left;
            const plotW = rect.width - TREND_PAD.l - TREND_PAD.r;
            if (plotW <= 0 || x < TREND_PAD.l || x > rect.width - TREND_PAD.r) { leave(); return; }
            const { t0, t1 } = dom();
            const ts = t0 + ((x - TREND_PAD.l) / plotW) * ((t1 - t0) || 1);
            let best = null, bestD = Infinity;
            for (const p of points) {
                const d = Math.abs(p.ts - ts);
                if (d < bestD) { bestD = d; best = p; }
            }
            if (best !== hover) { hover = best; draw(); updateTooltip(); }
            const pr = container.getBoundingClientRect();
            const tipW = tooltip.offsetWidth, tipH = tooltip.offsetHeight;
            let left = e.clientX - pr.left + 14;
            if (left + tipW > pr.width - 4) left = e.clientX - pr.left - tipW - 14;
            let top = e.clientY - pr.top - tipH - 10;
            if (top < 2) top = e.clientY - pr.top + 14;
            tooltip.style.left = `${Math.max(0, left)}px`;
            tooltip.style.top = `${top}px`;
        }
        canvas.addEventListener('pointermove', move);
        canvas.addEventListener('pointerdown', move);
        canvas.addEventListener('pointerleave', leave);

        registry.set(canvas, { redraw: draw });

        return {
            note,
            setData(newPoints, newDomain) {
                points = newPoints && newPoints.length ? newPoints : null;
                domain = newDomain || null;
                if (hover && (!points || !points.includes(hover))) hover = null;
                draw();
                updateTooltip();
                updateSummary();
            },
        };
    }

    // Only what the Console and History pages use
    window.PWCharts = {
        DAY, TREND_SERIES,
        esc, pad, parseDay, dayX, xDay, fmtDayLong, fmtTimeLong,
        makeChart, drawChart, energyTrend, prune,
    };
})();
