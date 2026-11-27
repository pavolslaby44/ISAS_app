module.exports = {
  content: ["./templates/**/*.html"],
  theme: {
    extend: {
      fontFamily: { sans: ['Inter', 'ui-sans-serif', 'system-ui', 'sans-serif'] },
      colors: { brand: 'rgb(var(--brand-rgb) / <alpha-value>)' }
    }
  },
  plugins: [require('@tailwindcss/typography')]
};