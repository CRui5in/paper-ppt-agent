import type { Config } from "tailwindcss";
import tailwindcssAnimate from "tailwindcss-animate";

const config: Config = {
  content: ["./index.html", "./src/**/*.{ts,tsx}"],
  darkMode: ["selector", "[data-theme='dark']"],
  theme: {
    extend: {
      colors: {
        background: "hsl(var(--background))",
        foreground: "hsl(var(--foreground))",
        border: {
          DEFAULT: "hsl(var(--border))",
          soft: "var(--border-soft)",
          strong: "var(--border-strong)",
        },
        input: "hsl(var(--input))",
        ring: "hsl(var(--ring))",
        primary: {
          DEFAULT: "hsl(var(--primary))",
          foreground: "hsl(var(--primary-foreground))",
        },
        secondary: {
          DEFAULT: "hsl(var(--secondary))",
          foreground: "hsl(var(--secondary-foreground))",
        },
        muted: {
          DEFAULT: "hsl(var(--muted))",
          foreground: "hsl(var(--muted-foreground))",
          text: "var(--muted-text)",
        },
        accent: {
          DEFAULT: "hsl(var(--accent))",
          foreground: "hsl(var(--accent-foreground))",
        },
        card: {
          DEFAULT: "hsl(var(--card))",
          foreground: "hsl(var(--card-foreground))",
        },
        // Custom semantic colors
        si: {
          accent: "var(--si-accent)",
          muted: "var(--si-muted)",
        },
        heading: "var(--heading-color)",
        body: "var(--body-color)",
        subtle: "var(--subtle-text)",
        surface: {
          DEFAULT: "var(--surface)",
          hover: "var(--surface-hover)",
          inset: "var(--surface-inset)",
          strong: "var(--surface-strong)",
        },
        line: "var(--line)",
      },
      borderRadius: {
        lg: "var(--radius)",
        md: "calc(var(--radius) - 2px)",
        sm: "calc(var(--radius) - 4px)",
      },
      fontFamily: {
        sans: ["Inter", "Manrope", "Segoe UI", "sans-serif"],
        body: ["Manrope", "Inter", "Segoe UI", "sans-serif"],
        mono: ["JetBrains Mono", "Cascadia Code", "monospace"],
      },
      boxShadow: {
        paper: "var(--shadow)",
        panel: "var(--header-shadow), var(--shadow)",
      },
    },
  },
  plugins: [tailwindcssAnimate],
};

export default config;
