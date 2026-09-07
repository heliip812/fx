// REQ-005 (demoted to Could by the trader review, kept because it is three lines):
// 1..5 jump to a page, r refreshes the snapshot. Ignored while typing in a field.
document.addEventListener("keydown", function (e) {
  var t = e.target || {};
  var tag = (t.tagName || "").toUpperCase();
  if (tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT" || t.isContentEditable) return;
  if (e.metaKey || e.ctrlKey || e.altKey) return;
  var routes = {"1": "/", "2": "/surface", "3": "/gamma-map", "4": "/book", "5": "/data"};
  if (routes[e.key] !== undefined) {
    var link = document.querySelector('a[href="' + routes[e.key] + '"]');
    if (link) { link.click(); e.preventDefault(); }
  } else if (e.key === "r") {
    var btn = document.getElementById("refresh-btn");
    if (btn) { btn.click(); e.preventDefault(); }
  }
});
