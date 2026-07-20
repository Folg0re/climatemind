import { defineConfig } from "vite";
import { resolve } from "path";

export default defineConfig({
  build: {
    lib: {
      entry: resolve(__dirname, "src/main.ts"),
      name: "ClimateMindPanel",
      formats: ["iife"],
      fileName: () => "climatemind-panel.js",
    },
    outDir: "../custom_components/climatemind/frontend",
    emptyOutDir: false,
    rollupOptions: {
      // No external dependencies – everything is bundled
    },
  },
});
