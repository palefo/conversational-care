/** @type {import('tailwindcss').Config} */
module.exports = {
  // Scan every template and JS file so used utility classes are generated.
  content: [
    "./ConvAI/templates/**/*.html",
    "./ConvAI/static/**/*.js",
  ],
  // Some utility classes are built dynamically in JS (string concatenation),
  // which the content scanner cannot see. Keep those here.
  safelist: [
    // priority / status badge colours
    { pattern: /(bg|text|border)-(red|amber|yellow|green|emerald|sky|blue|gray|slate)-(50|100|200|300|400|500|600|700|800)/ },
  ],
  theme: {
    extend: {},
  },
  plugins: [
    require("@tailwindcss/forms"),
    require("@tailwindcss/typography"),
    require("@tailwindcss/container-queries"),
  ],
};
