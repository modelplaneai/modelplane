// LightningCSS settings
// https://lightningcss.dev/
//
// Support browsers with at least 0.25% usage from browserslist:
// https://browserslist.dev/?q=bGFzdCAyIHZlcnNpb25z
const postcssLightningcss = require("postcss-lightningcss")({
  browsers: ">= .25%",
  lightningcssOptions: {},
});

// PurgeCSS settings
// https://purgecss.com/
//
// Load hugo_stats.json to know what elements are in use.
const purgecss = require("@fullhuman/postcss-purgecss")({
  content: ["./hugo_stats.json"],
  variables: true,
  defaultExtractor: (content) => {
    const els = JSON.parse(content).htmlElements;
    return [
      ...(els.tags || []),
      ...(els.classes || []),
      ...(els.ids || []),
    ];
  },
  // Classes added/injected by JS at runtime never appear in hugo_stats.json,
  // so keep their rules from being purged: the floating copy button we hide
  // (bd-clipboard/btn-clipboard) and the code-card copy "copied" state.
  safelist: ["bd-clipboard", "btn-clipboard", "copied"],
});

// PostCSS Media sort
// https://github.com/yunusga/postcss-sort-media-queries
//
// Sort CSS to prioritize desktop users.
const mediasort = require("postcss-sort-media-queries")({
  sort: "desktop-first",
});

module.exports = {
  plugins: [purgecss, mediasort, postcssLightningcss],
};
