/* DiceRatchet docs/html —— 极简交互：当前页高亮 + 滚动入场。
   没有构建步骤、没有外部依赖；禁用 JS 时页面仍然完整可读。 */
(function () {
  "use strict";

  // 1) 侧栏高亮：按 pathname 的 basename 匹配 nav 链接
  var here = location.pathname.split("/").pop() || "index.html";
  document.querySelectorAll(".nav a").forEach(function (a) {
    var href = (a.getAttribute("href") || "").split("/").pop();
    if (href === here) a.classList.add("active");
  });

  // 2) 首屏以下才做入场：首屏内容保持可见（避免截图/打印/JS 失败时整页空白）
  if (!("IntersectionObserver" in window)) return;
  var vh = window.innerHeight || 800;
  var io = new IntersectionObserver(function (entries) {
    entries.forEach(function (entry) {
      if (entry.isIntersecting) {
        entry.target.classList.add("on");
        io.unobserve(entry.target);
      }
    });
  }, { rootMargin: "0px 0px -8% 0px", threshold: 0.05 });
  document.querySelectorAll(".reveal").forEach(function (el) {
    if (el.getBoundingClientRect().top < vh * 0.92) return;
    el.classList.add("pre");
    io.observe(el);
  });
})();
