/*!
 * Liquid Glass Kit v1
 * Apple-style liquid glass for the web: real refraction in Chromium (Chrome, Edge, Opera, Arc),
 * frosted-glass fallback everywhere else. No dependencies.
 *
 * Usage: include liquid-glass.css + this file. Everything with the right markup is set up
 * automatically, including elements added later (React/Vue/etc. are fine). See DESIGN.md.
 *
 *   LiquidGlass.set({ refraction: 1, liquidity: .6, chroma: true })
 *   el.addEventListener('lg-change', e => e.detail)   // { index } | { checked } | { value }
 *   el.lg.select(i) / el.lg.set(value)                 // drive a component from code
 *   LiquidGlass.refresh()                              // after switching theme classes
 */
(() => {
  const S = { refraction: 1, liquidity: .6, chroma: true };
  if (matchMedia('(prefers-reduced-motion: reduce)').matches) S.liquidity = 0;

  // url() in backdrop-filter is Chromium-only; others keep the CSS frosted fallback
  const refract = !!window.chrome && !/Firefox/.test(navigator.userAgent);
  document.documentElement.classList.add(refract ? 'lg-refract' : 'lg-frost');

  const NS = 'http://www.w3.org/2000/svg';
  let defs;
  const getDefs = () => {
    if (defs) return defs;
    const svg = document.createElementNS(NS, 'svg');
    svg.setAttribute('width', 0); svg.setAttribute('height', 0); svg.setAttribute('aria-hidden', 'true');
    svg.style.position = 'absolute';
    defs = document.createElementNS(NS, 'defs');
    svg.append(defs); document.body.append(svg);
    return defs;
  };

  /* ============================== refraction engine ============================== */

  const filters = new Map(); // ponytail: one per element size, never evicted; only grows on resizes
  // bend strength scales with the element's short side, so small glass doesn't scramble text
  const bendFor = (kind, w, h) => {
    const m = Math.min(w, h);
    return { panel: Math.min(40, m * .3), bar: Math.min(60, m * .7), pill: m * .08, drop: m * .45, lens: m * .32 }[kind] ?? m * .3;
  };

  // Displacement map (R/G = x/y offset, 128 = none), drawn small and stretched: the field is smooth.
  //  panel / bar: only the rim bends, like a thick slab of glass (bar: deeper rim, clearer middle)
  //  lens / pill / drop: the whole shape is a droplet dome that magnifies toward the middle
  function buildMap(w, h, r, kind) {
    const s = Math.min(1, 160 / Math.max(w, h));
    const cw = Math.max(4, Math.round(w * s)), ch = Math.max(4, Math.round(h * s));
    const c = document.createElement('canvas'); c.width = cw; c.height = ch;
    const ctx = c.getContext('2d'), img = ctx.createImageData(cw, ch), d = img.data;
    const hw = w / 2, hh = h / 2, dome = kind === 'lens' || kind === 'pill' || kind === 'drop';
    const bezel = kind === 'bar' ? Math.min(30, hw, h * .32) : Math.min(26, hw, hh);
    for (let j = 0; j < ch; j++) for (let i = 0; i < cw; i++) {
      const sx = (i + .5) / cw * w - hw, sy = (j + .5) / ch * h - hh;
      let nx, ny, m;
      if (dome) {
        // squircle "radius" and its gradient: smooth direction field, no creases at corners
        const u = sx / hw, v = sy / hh, n = 4;
        const rho = Math.min((Math.abs(u) ** n + Math.abs(v) ** n) ** (1 / n), 1);
        nx = Math.sign(u) * Math.abs(u) ** (n - 1) / hw; ny = Math.sign(v) * Math.abs(v) ** (n - 1) / hh;
        const l = Math.hypot(nx, ny) || 1; nx /= l; ny /= l;
        m = rho ** 2.2;
      } else {
        // signed distance to the rounded rect, normal points outward
        const px = Math.abs(sx) - (hw - r), py = Math.abs(sy) - (hh - r);
        let dist;
        if (px > 0 && py > 0) { const l = Math.hypot(px, py); dist = l - r; nx = px / l; ny = py / l; }
        else if (px > py) { dist = px - r; nx = 1; ny = 0; } else { dist = py - r; nx = 0; ny = 1; }
        nx *= Math.sign(sx) || 1; ny *= Math.sign(sy) || 1;
        m = (1 - Math.min(Math.max(-dist / bezel, 0), 1)) ** (kind === 'bar' ? 2 : 3);
      }
      // sample inward: the browser only gives a backdrop for the element's own area
      const k = (j * cw + i) * 4;
      d[k] = 128 - nx * m * 127; d[k + 1] = 128 - ny * m * 127; d[k + 2] = 128; d[k + 3] = 255;
    }
    ctx.putImageData(img, 0, 0);
    return c.toDataURL();
  }

  function makeFilter(id, w, h, r, kind, dpr, frost) {
    // ponytail: the rainbow (3-channel) chain inflates alpha over see-through backdrops, which greys them out.
    // So only short-lived droplets (lens, drop) get it; the resting pill uses the plain chain.
    // Exact fix would need per-channel alpha, which SVG filters can't express.
    const W = w * dpr, H = h * dpr, slab = kind === 'panel' || kind === 'bar' || kind === 'pill';
    const head = `<filter id="${id}" x="0" y="0" width="${W}" height="${H}" filterUnits="userSpaceOnUse" primitiveUnits="userSpaceOnUse" color-interpolation-filters="sRGB">
      <feImage href="${buildMap(w, h, r, kind)}" x="0" y="0" width="${W}" height="${H}" preserveAspectRatio="none" result="map"/>`;
    getDefs().insertAdjacentHTML('beforeend', slab ? head + `
      <feGaussianBlur in="SourceGraphic" stdDeviation="${frost * dpr}" edgeMode="duplicate" result="src"/>
      <feDisplacementMap in="src" in2="map" xChannelSelector="R" yChannelSelector="G" result="d"/>
      <feColorMatrix in="d" type="saturate" values="1.7"/></filter>` : head + `
      <feGaussianBlur in="SourceGraphic" stdDeviation="${frost * dpr}" edgeMode="duplicate" result="src"/>
      <feDisplacementMap in="src" in2="map" xChannelSelector="R" yChannelSelector="G" result="dr"/>
      <feDisplacementMap in="src" in2="map" xChannelSelector="R" yChannelSelector="G" result="dg"/>
      <feDisplacementMap in="src" in2="map" xChannelSelector="R" yChannelSelector="G" result="db"/>
      <feColorMatrix in="dr" type="matrix" values="1 0 0 0 0  0 0 0 0 0  0 0 0 0 0  0 0 0 1 0" result="r"/>
      <feColorMatrix in="dg" type="matrix" values="0 0 0 0 0  0 1 0 0 0  0 0 0 0 0  0 0 0 1 0" result="g"/>
      <feColorMatrix in="db" type="matrix" values="0 0 0 0 0  0 0 0 0 0  0 0 1 0 0  0 0 0 1 0" result="b"/>
      <feBlend in="r" in2="g" mode="screen" result="rg"/><feBlend in="rg" in2="b" mode="screen" result="rgb"/>
      <feColorMatrix in="rgb" type="saturate" values="1.3"/></filter>`);
    const f = { base: bendFor(kind, w, h), mult: 1, dpr, maps: [...defs.lastElementChild.querySelectorAll('feDisplacementMap')] };
    setScales(f);
    return f;
  }
  function setScales(f) {
    const s = f.base * S.refraction * f.dpr * f.mult, spread = S.chroma ? [1, .95, .9] : [1, 1, 1];
    f.maps.forEach((m, i) => m.setAttribute('scale', s * spread[i]));
  }

  function applyGlass(el) {
    const w = el.offsetWidth, h = el.offsetHeight;
    if (!w || !h) return;
    // Chrome runs url() filters in device pixels
    const kind = el.dataset.glass, dpr = devicePixelRatio, cs = getComputedStyle(el);
    const r = Math.min(parseFloat(cs.borderTopLeftRadius) || 0, w / 2, h / 2);
    // frost (blur radius) comes from the --lg-frost token, so themes and .lg-clear can change it
    const frost = parseFloat(cs.getPropertyValue('--lg-frost')) || 0;
    const id = `lg-${kind}-${w}-${h}-${Math.round(r)}-${dpr}-${frost}`.replace(/\./g, '_');
    if (!filters.has(id)) filters.set(id, makeFilter(id, w, h, r, kind, dpr, frost));
    el.dataset.filter = id;
    if (kind !== 'drop') el.style.backdropFilter = `url(#${id})`; // tabs switch the drop's filter on and off
  }
  // live bend multiplier for one element
  function bend(el, k) {
    const f = filters.get(el.dataset.filter);
    if (f && Math.abs(f.mult - k) > .005) { f.mult = k; setScales(f); }
  }

  const glassRO = refract && new ResizeObserver(es => es.forEach(e => applyGlass(e.target)));
  if (refract) addEventListener('resize', () => window.LiquidGlass.refresh()); // dpr changes

  const emit = (el, detail) => el.dispatchEvent(new CustomEvent('lg-change', { detail, bubbles: true }));
  const raf = fn => { let id = 0, last = 0; const loop = now => { const dt = Math.min((now - last) / 16.67, 3); last = now; id = fn(dt) ? requestAnimationFrame(loop) : 0; }; return () => { if (!id) { last = performance.now(); id = requestAnimationFrame(loop); } }; };

  /* ============================== tabs ==============================
     A selector pill that glides between items, can be dragged, and behaves like a drop of
     liquid on the liquid bar: at rest it sits under crisp icons and only lightly bends the bar;
     while it slides it bends the icons it passes over, then settles back.
     The real items are masked out under the pill and the pill shows its own copy of them,
     because glass is drawn over what's behind it, not instead of it (otherwise you'd see both). */
  function tabs(group) {
    const wrap = group.querySelector('.lg-items'), items = [...wrap.querySelectorAll('[data-item]')];
    const pill = document.createElement('div');
    pill.className = 'lg-pill'; pill.dataset.glass = 'pill';
    pill.innerHTML = '<div class="lg-drop" data-glass="drop"></div>';
    group.append(pill);
    const drop = pill.firstChild;
    let copy;
    const refreshCopy = () => {
      copy?.remove(); copy = wrap.cloneNode(true);
      copy.setAttribute('aria-hidden', 'true'); copy.inert = true; copy.removeAttribute('id');
      copy.style.width = wrap.offsetWidth + 'px';
      drop.append(copy);
    };
    let idx = Math.max(0, items.findIndex(i => i.classList.contains('active')));
    let x = 0, y = 0, vx = 0, vy = 0, tx = 0, ty = 0, lift = 1, liftV = 0, flow = 0;
    let dragging = false, moved = false, downX = 0, downY = 0;
    const pos = i => [items[i].offsetLeft, items[i].offsetTop];

    function render() {
      const L = S.liquidity, c = .0015 * L, max = .06 * L;
      const ex = Math.min(Math.abs(vx) * c, max), ey = Math.min(Math.abs(vy) * c, max);
      const sx = lift * (1 + ex - ey * .5), sy = lift * (1 + ey - ex * .5);
      pill.style.transform = `translate(${x}px, ${y}px) scale(${sx}, ${sy})`;
      const w = pill.offsetWidth, h = pill.offsetHeight;
      copy.style.transform = `translate(${wrap.offsetLeft - x}px, ${wrap.offsetTop - y}px)`;
      wrap.style.setProperty('--lg-mx', x + w / 2 - w * sx / 2 - wrap.offsetLeft + 'px');
      wrap.style.setProperty('--lg-my', y + h / 2 - h * sy / 2 - wrap.offsetTop + 'px');
      wrap.style.setProperty('--lg-mw', w * sx + 'px');
      wrap.style.setProperty('--lg-mh', h * sy + 'px');
      // the copy bends only while sliding; at rest the filter is off so icons are pin-sharp
      if (drop.dataset.filter) { drop.style.filter = flow > .01 ? `url(#${drop.dataset.filter})` : ''; bend(drop, flow); }
    }
    const wake = raf(dt => {
      const L = S.liquidity, k = .16, damp = .58 + .1 * L; // near critical damping: glides in, barely overshoots
      vx = (vx + (tx - x) * k * dt) * damp ** dt; vy = (vy + (ty - y) * k * dt) * damp ** dt;
      x += vx * dt; y += vy * dt;
      liftV = (liftV + ((dragging ? 1.05 : 1) - lift) * .25 * dt) * .7 ** dt; lift += liftV * dt;
      const want = dragging || Math.abs(vx) + Math.abs(vy) > .4 ? 1 : 0; // rises fast, fades slowly
      flow = Math.min(1, Math.max(0, flow + (want - flow) * (want ? .35 : .1) * dt));
      const moving = dragging || Math.abs(tx - x) + Math.abs(ty - y) + Math.abs(vx) + Math.abs(vy) + (Math.abs(liftV) + Math.abs(lift - 1)) * 50 + flow > .1;
      if (!moving) { x = tx; y = ty; vx = vy = liftV = flow = 0; lift = 1; }
      render();
      return moving;
    });
    function snap() {
      pill.style.width = items[0].offsetWidth + 'px'; pill.style.height = items[0].offsetHeight + 'px';
      refreshCopy();
      [tx, ty] = pos(idx); x = tx; y = ty; vx = vy = 0; render();
    }
    function select(i, fire = true) {
      idx = i;
      items.forEach((it, j) => { it.classList.toggle('active', j === i); it.setAttribute('aria-selected', j === i); });
      refreshCopy();
      [tx, ty] = pos(i); wake();
      if (fire) emit(group, { index: i, item: items[i] });
    }
    // where the finger is (tx/ty), not where the pill has got to, so fast flicks land right
    const nearest = () => items.reduce((b, it, i) => Math.hypot(it.offsetLeft - tx, it.offsetTop - ty) < Math.hypot(items[b].offsetLeft - tx, items[b].offsetTop - ty) ? i : b, 0);

    group.addEventListener('pointerdown', e => {
      if (!e.target.closest('[data-item]')) return;
      dragging = true; moved = false; downX = e.clientX; downY = e.clientY;
      group.setPointerCapture(e.pointerId); wake();
    });
    group.addEventListener('pointermove', e => {
      if (!dragging) return;
      if (!moved && Math.hypot(e.clientX - downX, e.clientY - downY) > 5) moved = true;
      if (!moved) return;
      const r = group.getBoundingClientRect(), [x0, y0] = pos(0), [x1, y1] = pos(items.length - 1);
      tx = Math.min(Math.max(e.clientX - r.left - pill.offsetWidth / 2, Math.min(x0, x1)), Math.max(x0, x1));
      ty = Math.min(Math.max(e.clientY - r.top - pill.offsetHeight / 2, Math.min(y0, y1)), Math.max(y0, y1));
    });
    const up = e => {
      if (!dragging) return;
      dragging = false;
      if (moved) return select(nearest());
      const hit = document.elementFromPoint(e.clientX, e.clientY)?.closest('[data-item]');
      select(items.includes(hit) ? items.indexOf(hit) : idx);
    };
    group.addEventListener('pointerup', up);
    group.addEventListener('pointercancel', up);
    // keyboard: Enter/Space fire click with detail 0; arrows move between items
    items.forEach((it, i) => it.addEventListener('click', e => { if (e.detail === 0) select(i); }));
    group.addEventListener('keydown', e => {
      const d = { ArrowRight: 1, ArrowDown: 1, ArrowLeft: -1, ArrowUp: -1 }[e.key];
      if (!d) return;
      e.preventDefault(); const n = (idx + d + items.length) % items.length; items[n].focus(); select(n);
    });

    new ResizeObserver(snap).observe(group);
    select(idx, false);
    group.lg = { select, get index() { return idx; } };
  }

  /* ============================== switch ==============================
     The knob turns into a glass droplet while pressed (CSS does the motion). */
  function toggle(sw) {
    if (!sw.querySelector('.lg-knob')) sw.insertAdjacentHTML('beforeend', '<span class="lg-knob" data-glass="lens"></span>');
    if (!sw.hasAttribute('aria-checked')) sw.setAttribute('aria-checked', 'false');
    const set = (on, fire = true) => { sw.setAttribute('aria-checked', on); if (fire) emit(sw, { checked: on }); };
    sw.addEventListener('click', () => set(sw.getAttribute('aria-checked') !== 'true'));
    // show the glass "pressed" look briefly on keyboard toggles too
    sw.addEventListener('keydown', e => { if (e.key === ' ' || e.key === 'Enter') { sw.classList.add('lg-pressed'); setTimeout(() => sw.classList.remove('lg-pressed'), 250); } });
    sw.lg = { set, get checked() { return sw.getAttribute('aria-checked') === 'true'; } };
  }

  /* ============================== slider ==============================
     Thumb glides after the finger on a near-critically-damped spring, stretches slightly
     with speed, and turns into glass while dragged. Transforms only: no layout per frame. */
  function slider(el) {
    if (!el.querySelector('.lg-thumb')) el.innerHTML = '<div class="lg-track"><div class="lg-fill"></div></div><div class="lg-thumb" data-glass="lens"></div>';
    const fill = el.querySelector('.lg-fill'), thumb = el.querySelector('.lg-thumb');
    const min = +(el.dataset.min ?? 0), max = +(el.dataset.max ?? 100), step = +(el.dataset.step ?? 1);
    let target = ((+(el.dataset.value ?? min)) - min) / (max - min); // 0..1, not rounded
    let p = target, pv = 0, s = 1, sv = 0, left = 0, span = 1, tw = 40, sent = NaN, dragging = false;
    const measure = () => { const r = el.getBoundingClientRect(); tw = thumb.offsetWidth; left = r.left; span = Math.max(1, r.width - tw); };
    const value = () => Math.min(max, Math.max(min, Math.round((min + target * (max - min)) / step) * step));

    function render() {
      const st = 1 + Math.min(Math.abs(pv) * span * (.004 + .01 * S.liquidity), .06 + .12 * S.liquidity);
      thumb.style.transform = `translateX(${p * span}px) scale(${s * st}, ${s / Math.sqrt(st)})`;
      fill.style.transform = `scaleX(${p})`;
    }
    const wake = raf(dt => {
      const L = S.liquidity;
      pv = (pv + (target - p) * .28 * dt) * (.5 + .1 * L) ** dt; p += pv * dt;
      const sT = dragging ? 1.4 : 1;
      sv = (sv + (sT - s) * .22 * dt) * (.62 + .12 * L) ** dt; s += sv * dt;
      const v = value();
      if (v !== sent) { sent = v; el.setAttribute('aria-valuenow', v); emit(el, { value: v }); } // at most once a frame
      const moving = dragging || (Math.abs(target - p) + Math.abs(pv)) * span + (Math.abs(s - sT) + Math.abs(sv)) * 50 > .05;
      if (!moving) { p = target; pv = sv = 0; s = sT; }
      render();
      return moving;
    });
    const setTarget = n => { target = Math.min(1, Math.max(0, n)); wake(); };
    const fromEvent = e => setTarget((e.clientX - left - tw / 2) / span);

    el.addEventListener('pointerdown', e => { el.setPointerCapture(e.pointerId); measure(); dragging = true; el.classList.add('lg-dragging'); fromEvent(e); });
    el.addEventListener('pointermove', e => { if (dragging) fromEvent(e); });
    const up = () => { dragging = false; el.classList.remove('lg-dragging'); wake(); };
    el.addEventListener('pointerup', up); el.addEventListener('pointercancel', up);
    el.addEventListener('keydown', e => {
      const d = { ArrowRight: 1, ArrowUp: 1, ArrowLeft: -1, ArrowDown: -1, PageUp: 10, PageDown: -10 }[e.key];
      if (d) { e.preventDefault(); setTarget((value() + d * step - min) / (max - min)); }
      if (e.key === 'Home') { e.preventDefault(); setTarget(0); }
      if (e.key === 'End') { e.preventDefault(); setTarget(1); }
    });
    if (!el.hasAttribute('tabindex')) el.tabIndex = 0;
    el.setAttribute('role', 'slider'); el.setAttribute('aria-valuemin', min); el.setAttribute('aria-valuemax', max);
    new ResizeObserver(() => { measure(); render(); }).observe(el);
    wake();
    el.lg = { set: v => setTarget((v - min) / (max - min)), get value() { return value(); } };
  }

  /* ============================== auto setup ============================== */
  const done = new WeakSet();
  const once = (el, fn) => { if (!done.has(el)) { done.add(el); fn(el); } };
  function init(root = document) {
    const all = sel => [...(root.matches?.(sel) ? [root] : []), ...root.querySelectorAll(sel)];
    // components first: they inject their own glass elements
    all('.lg-tabs').forEach(el => once(el, tabs));
    all('.lg-switch').forEach(el => once(el, toggle));
    all('.lg-slider').forEach(el => once(el, slider));
    if (refract) all('[data-glass]').forEach(el => once(el, g => glassRO.observe(g)));
  }
  function start() {
    init();
    // pick up glass added later (frameworks, re-rendered lists, etc.)
    new MutationObserver(ms => ms.forEach(m => m.addedNodes.forEach(n => n.nodeType === 1 && init(n))))
      .observe(document.body, { childList: true, subtree: true });
  }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', start); else start();

  window.LiquidGlass = {
    init,
    /** set({ refraction: 0..2 (1 = default), liquidity: 0..1, chroma: bool }) */
    set(opts) { Object.assign(S, opts); filters.forEach(setScales); },
    /** re-read sizes and tokens after a theme/class change that doesn't resize anything */
    refresh() { if (refract) document.querySelectorAll('[data-filter]').forEach(applyGlass); },
    settings: S,
    supported: refract,
  };
})();
