/* Lyst dashboard kit clientside functions (window.dash_clientside.lk). Registered by app.frame /
   state.register_filters; no server round trip. */
window.dash_clientside = Object.assign({}, window.dash_clientside, {
  lk: {
    /* ColorSchemeToggle.computedColorScheme -> the scheme store figure callbacks read. Writes only on a change. */
    scheme: function (computed, current) {
      var s = computed === "dark" || computed === "light" ? computed : (current || "light");
      return s === current ? window.dash_clientside.no_update : s;
    },

    /* Mobile navbar: the burger opens and closes the AppShell navbar. */
    burger: function (opened, navbar) {
      var nb = Object.assign({}, navbar || {});
      nb.collapsed = { mobile: !opened };
      return nb;
    },

    /* URL -> filter store, once, when the page loads with a query string. */
    urlToStore: function (search, spec, data, ctlIds) {
      var nu = window.dash_clientside.no_update;
      var ctlOut = (ctlIds || []).map(function () { return nu; });
      if (!search || !spec) { return [nu, ctlOut]; }
      var params = new URLSearchParams(search);
      var next = Object.assign({}, data || {});
      var changed = false;
      Object.keys(spec).forEach(function (k) {
        if (!params.has(k)) { return; }
        var vals = params.getAll(k).filter(function (v) { return v !== ""; });
        var v = spec[k].multi ? vals : (vals.length ? vals[0] : null);
        if (spec[k].type === "number" && v !== null) { v = spec[k].multi ? v.map(Number) : Number(v); }
        if (JSON.stringify(next[k]) !== JSON.stringify(v)) { next[k] = v; changed = true; }
      });
      if (!changed) { return [nu, ctlOut]; }
      ctlOut = (ctlIds || []).map(function (id) {
        return Object.prototype.hasOwnProperty.call(next, id.dim) ? next[id.dim] : nu;
      });
      return [next, ctlOut];
    },

    /* Filter store -> URL (replaceState: no reload, no history entry per click). Defaults are left out. */
    storeToUrl: function (data, pathname, spec) {
      if (!data || !spec) { return window.dash_clientside.no_update; }
      var params = new URLSearchParams(window.location.search);
      Object.keys(spec).forEach(function (k) {
        params.delete(k);
        var v = data[k], d = spec[k]["default"];
        if (v === null || v === undefined || JSON.stringify(v) === JSON.stringify(d)) { return; }
        (Array.isArray(v) ? v : [v]).forEach(function (x) { params.append(k, x); });
      });
      var qs = params.toString();
      var url = window.location.pathname + (qs ? "?" + qs : "") + window.location.hash;
      if (url !== window.location.pathname + window.location.search + window.location.hash) {
        window.history.replaceState(window.history.state, "", url);
      }
      return window.dash_clientside.no_update;
    }
  }
});

/* AG Grid cell renderer: a bar for the cell's value against cellRendererParams.max, with text from
   cellRendererParams.textField (formatted in Python, so formats match). */
(function () {
  var f = (window.dashAgGridComponentFunctions = window.dashAgGridComponentFunctions || {});
  f.LkBar = function (props) {
    var p = (props.colDef && props.colDef.cellRendererParams) || {};
    var v = Number(props.value) || 0, max = Number(p.max) || 1;
    var w = Math.max(0, Math.min(100, (100 * v) / max));
    var text = p.textField && props.data ? props.data[p.textField] : String(props.value);
    return React.createElement("div", { className: "lk-bar-cell" },
      React.createElement("div", { className: "lk-bar-track" },
        React.createElement("div", { className: "lk-bar", style: { width: w + "%", background: p.colour || null } })),
      React.createElement("span", { className: "lk-bar-text" }, text));
  };
})();
