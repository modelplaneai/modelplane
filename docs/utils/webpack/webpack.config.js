const path = require("path")

// Plugins
const { WebpackManifestPlugin } = require("webpack-manifest-plugin");

module.exports = {
  cache: true,

  devtool: "source-map",

  entry:{
    main: [path.resolve("src", "js", "globalScripts.js")]
  },

  output: {
    filename: "[name]-[contenthash:8].bundle.min.js",
    chunkFilename: "[name]-[contenthash:8].chunk.min.js",
    path: path.join(__dirname, "../../themes/geekboot/assets/js"),
    clean: true
  },

  optimization: {
    moduleIds: 'deterministic',
    splitChunks: {},
  },

  plugins: [
    new WebpackManifestPlugin({
      fileName: "../../data/assets.json",
      publicPath: "js",
      writeToFileEmit: true,
      generate(seed, files) {
        let manifest = {}

        files.forEach(function (element, index) {
          if (element.name.endsWith("VERSION")) return
          if (element.name.endsWith(".svg")) return
          if (element.name.startsWith("fonts/")) return
          if (element.name.startsWith("/favicon")) return

          Object.assign(manifest, {
            [element.name]:  { src: element.path }
          })
        })

        return manifest
      }
    })]
}