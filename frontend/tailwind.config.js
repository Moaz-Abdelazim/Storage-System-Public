/** @type {import('tailwindcss').Config} */
module.exports = {
  darkMode: "class",
  content: ["./public/**/*.html", "./public/js/**/*.js"],
  theme: {
    extend: {
      fontFamily: { sans: ["Tajawal", "Segoe UI", "Tahoma", "system-ui", "sans-serif"] },
      colors: {
        // ألوان الهوية: #10B981 / #1F2937 / #94A3B8 / #F3F4F6
        brand: {
          50: "#ecfdf5", 100: "#d1fae5", 200: "#a7f3d0", 300: "#6ee7b7", 400: "#34d399",
          500: "#10B981", 600: "#059669", 700: "#047857", 800: "#065f46", 900: "#064e3b", 950: "#022c22",
        },
        ink: {
          50: "#F3F4F6", 100: "#E5E7EB", 200: "#CBD5E1", 300: "#94A3B8", 400: "#7B8AA0",
          500: "#5B6B82", 600: "#374151", 700: "#2B3646", 800: "#1F2937", 900: "#161E2B", 950: "#0E141D",
        },
        // ألوان دلالية بتتغير مع الدارك مود (CSS variables)
        app: "rgb(var(--c-app) / <alpha-value>)",
        card: "rgb(var(--c-card) / <alpha-value>)",
        soft: "rgb(var(--c-soft) / <alpha-value>)",
        line: "rgb(var(--c-line) / <alpha-value>)",
        fg: "rgb(var(--c-fg) / <alpha-value>)",
        muted: "rgb(var(--c-muted) / <alpha-value>)",
        side: "rgb(var(--c-side) / <alpha-value>)",
      },
      boxShadow: {
        card: "0 1px 2px rgb(16 24 40 / 0.04), 0 1px 3px rgb(16 24 40 / 0.04)",
        pop: "0 12px 32px -8px rgb(16 24 40 / 0.18), 0 4px 12px -4px rgb(16 24 40 / 0.1)",
      },
    },
  },
  plugins: [],
};
