/** @type {import('tailwindcss').Config} */
export default {
  content: ['./index.html', './src/**/*.{ts,tsx}'],
  darkMode: 'class',
  theme: {
    extend: {
      colors: {
        bg: '#0d1117',
        panel: '#161b22',
        edge: '#30363d',
        bull: '#3fb950',
        bear: '#f85149',
        accent: '#388bfd',
        warn: '#d29922',
        muted: '#8b949e',
      },
    },
  },
  plugins: [],
}
