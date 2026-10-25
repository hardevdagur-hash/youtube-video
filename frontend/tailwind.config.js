/** @type {import('tailwindcss').Config} */

// Apple-style single accent: every former violet/indigo/purple accent maps to this blue.
const appleBlue = {
  50: '#F0F7FF',
  100: '#E0EFFF',
  200: '#B9DBFF',
  300: '#7CBCFF',
  400: '#2997FF',
  500: '#0A84FF',
  600: '#0071E3',
  700: '#0062C4',
  800: '#004F9E',
  900: '#003A75',
  950: '#002347',
};

export default {
  content: ['./index.html', './src/**/*.{js,ts,jsx,tsx}'],
  darkMode: 'class',
  theme: {
    extend: {
      fontFamily: {
        sans: ['-apple-system', 'BlinkMacSystemFont', '"SF Pro Text"', '"SF Pro Display"', 'Inter', '"Helvetica Neue"', 'Helvetica', 'Arial', 'sans-serif'],
        mono: ['"SF Mono"', 'Monaco', 'Consolas', 'monospace'],
      },
      colors: {
        // Apple neutral scale (light: #F5F5F7 canvas, #1D1D1F text; dark: black canvas, #1D1D1F surfaces)
        gray: {
          50: '#FAFAFC',
          100: '#F5F5F7',
          200: '#E8E8ED',
          300: '#D2D2D7',
          400: '#A1A1A6',
          500: '#86868B',
          600: '#6E6E73',
          700: '#424245',
          800: '#2C2C2E',
          900: '#1D1D1F',
          950: '#000000',
        },
        blue: appleBlue,
        violet: appleBlue,
        indigo: appleBlue,
        purple: appleBlue,
        emerald: {
          50: '#EEFBF1',
          100: '#D7F5DE',
          200: '#AEEBBD',
          300: '#7BDD94',
          400: '#30D158',
          500: '#28B84B',
          600: '#1F9D3E',
          700: '#1A8234',
          800: '#16672B',
          900: '#114F22',
        },
      },
      fontSize: {
        'hero': ['56px', '1.07'],
        'section': ['40px', '1.1'],
      },
      spacing: {
        '18': '4.5rem',
      },
      borderRadius: {
        '2xl': '18px',
        '3xl': '28px',
      },
      boxShadow: {
        'glass': 'none',
        'card': 'none',
        'card-hover': '0 4px 24px rgba(0,0,0,0.06)',
        'elevated': '0 4px 24px rgba(0,0,0,0.06)',
      },
      animation: {
        'float': 'float 6s ease-in-out infinite',
        'pulse-slow': 'pulse 4s cubic-bezier(0.4, 0, 0.6, 1) infinite',
      },
      keyframes: {
        float: {
          '0%, 100%': { transform: 'translateY(0px)' },
          '50%': { transform: 'translateY(-12px)' },
        },
      },
    },
  },
  plugins: [],
}
