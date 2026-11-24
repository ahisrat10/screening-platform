// Small 3D tilt for cards. Skips big containers (they'd feel seasick) and
// honours prefers-reduced-motion.
(function () {
  if (window.matchMedia('(prefers-reduced-motion: reduce)').matches) return;
  var MAX = 7; // degrees

  function tilt(el, e) {
    var r = el.getBoundingClientRect();
    var x = (e.clientX - r.left) / r.width - 0.5;
    var y = (e.clientY - r.top) / r.height - 0.5;
    el.style.transform = 'perspective(900px) rotateX(' + (-y * MAX).toFixed(2) + 'deg) rotateY(' + (x * MAX).toFixed(2) + 'deg) translateZ(0)';
  }
  function reset(el) { el.style.transform = ''; }

  document.addEventListener('mousemove', function (e) {
    var el = e.target.closest && e.target.closest('.ewu-tilt');
    if (!el || el.offsetWidth > 560) return;
    tilt(el, e);
  }, { passive: true });
  document.addEventListener('mouseout', function (e) {
    var el = e.target.closest && e.target.closest('.ewu-tilt');
    if (el && !el.contains(e.relatedTarget)) reset(el);
  }, { passive: true });

  // Cards added later (lists rendered by fetch) get the class automatically.
  function mark(root) {
    root.querySelectorAll && root.querySelectorAll('article.bg-gray-900, .ewu-card.tilt').forEach(function (n) { n.classList.add('ewu-tilt'); });
  }
  mark(document);
  new MutationObserver(function (m) {
    m.forEach(function (x) { x.addedNodes.forEach(function (n) { if (n.nodeType === 1) mark(n); }); });
  }).observe(document.documentElement, { childList: true, subtree: true });
})();
