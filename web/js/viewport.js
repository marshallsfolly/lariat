// Pin #app to the *visual* viewport so the iOS keyboard never pushes,
// bounces or scrolls the page: height = visible height, translateY = how far
// iOS scrolled the visual viewport. One rAF per change, CSS vars only.

let listeners = [];
let settleNow = () => {};

/** Measure again after a view change: removing a focused element sends no focusout. */
export function settleViewport() {
  settleNow();
}

export function onViewport(fn) {
  listeners.push(fn);
}

export function initViewport() {
  const vv = window.visualViewport;
  const root = document.documentElement;
  let raf = 0;
  let last = '';

  // With nothing focused to type into, the keyboard is down, whatever the visual
  // viewport still says: iOS can leave it at the keyboard-up height (e.g. when
  // the focused terminal is removed), which cut the app off above the bottom.
  const typing = () => {
    const el = document.activeElement;
    return !!el && (el.tagName === 'TEXTAREA' || el.tagName === 'INPUT' || el.isContentEditable);
  };
  const apply = () => {
    raf = 0;
    const kb = vv && typing();
    const h = vv ? (kb ? vv.height : Math.max(vv.height, window.innerHeight)) : window.innerHeight;
    const top = kb ? Math.max(0, vv.offsetTop) : 0;
    const key = `${h}:${top}`;
    if (key === last) return;
    last = key;
    root.style.setProperty('--vvh', `${Math.round(h)}px`);
    root.style.setProperty('--vvt', `${Math.round(top)}px`);
    // A visual viewport much shorter than the layout viewport means the keyboard is up.
    root.classList.toggle('kb-open', window.innerHeight - h > 120);
    for (const fn of listeners) fn();
  };
  const schedule = () => {
    if (!raf) raf = requestAnimationFrame(apply);
  };
  // The keyboard animates out for ~250 ms and iOS does not always report the end.
  const settle = () => {
    for (const ms of [50, 350, 800]) setTimeout(schedule, ms);
  };

  vv?.addEventListener('resize', schedule);
  vv?.addEventListener('scroll', schedule);
  window.addEventListener('resize', schedule);
  window.addEventListener('orientationchange', () => setTimeout(schedule, 50));
  // iOS scrolls the focused field into view; the fixed layout already shows it.
  document.addEventListener('focusin', () => {
    window.scrollTo(0, 0);
    schedule();
  });
  settleNow = settle;
  document.addEventListener('focusout', settle);
  document.addEventListener('visibilitychange', () => document.visibilityState === 'visible' && settle());
  window.addEventListener('pageshow', settle);
  // Nothing on the page should scroll the document itself.
  document.addEventListener('scroll', () => window.scrollTo(0, 0), { passive: true });
  apply();
}
