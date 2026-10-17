// Runs before the app loads config or route CSS, so cold loads honor preferences.
;(function () {
  var theme = 'apex-light'
  var reduceMotion = false
  try {
    var saved = localStorage.getItem('apex.theme')
    if (saved === 'system') {
      theme = window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light'
    } else if (['apex-light', 'dark', 'light', 'solarized-dark', 'solarized-light', 'monokai-dimmed'].includes(saved)) {
      theme = saved
    }
    reduceMotion = JSON.parse(localStorage.getItem('apex.preferences.v1') || '{}')?.reduceMotion === true
  } catch { /* Storage can be unavailable; the default theme still applies. */ }
  document.documentElement.dataset.theme = theme
  document.documentElement.dataset.reduceMotion = String(reduceMotion)
})()
