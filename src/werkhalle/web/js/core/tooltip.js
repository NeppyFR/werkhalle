// One tooltip element for the whole page. Any element, SVG included, gets one by setting data-tip="Text".
let tip = null;
let current = null;
let timer = 0;

function place(target) {
  const box = target.getBoundingClientRect();
  tip.hidden = false;
  const own = tip.getBoundingClientRect();
  const margin = 8;
  let top = box.bottom + 6;
  if (top + own.height > window.innerHeight - margin) top = box.top - own.height - 6;
  let left = box.left + box.width / 2 - own.width / 2;
  left = Math.max(margin, Math.min(left, window.innerWidth - own.width - margin));
  tip.style.top = `${Math.round(Math.max(margin, top))}px`;
  tip.style.left = `${Math.round(left)}px`;
}

function show(target) {
  current = target;
  clearTimeout(timer);
  timer = setTimeout(() => {
    if (current !== target || !target.isConnected) return;
    tip.textContent = target.getAttribute('data-tip');
    place(target);
  }, 280);
}

function hide() {
  current = null;
  clearTimeout(timer);
  tip.hidden = true;
}

export function initTooltips(element) {
  tip = element;
  document.addEventListener('mouseover', (event) => {
    const target = event.target.closest?.('[data-tip]');
    if (target && target.getAttribute('data-tip') && target !== current) show(target);
    else if (!target && current) hide();
  });
  document.addEventListener('focusin', (event) => {
    const target = event.target.closest?.('[data-tip]');
    if (target && target.getAttribute('data-tip')) show(target);
  });
  document.addEventListener('focusout', hide);
  document.addEventListener('keydown', (event) => {
    if (event.key === 'Escape' && current) hide();
  });
  document.addEventListener('scroll', hide, true);
}
