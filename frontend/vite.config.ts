import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import vue from "@vitejs/plugin-vue";
import Icons from "unplugin-icons/vite";
import { FileSystemIconLoader } from "unplugin-icons/loaders";
import IconsResolver from "unplugin-icons/resolver";
import Components from "unplugin-vue-components/vite";
import { fileURLToPath, URL } from "node:url";

export default defineConfig({
  plugins: [
    react(),
    vue(),
    Components({
      dirs: [],
      resolvers: [
        IconsResolver({
          prefix: "i",
          customCollections: ["custom"],
        }),
      ],
    }),
    Icons({
      compiler: "vue3",
      autoInstall: false,
      collectionsNodeResolvePath: fileURLToPath(new URL("./node_modules", import.meta.url)),
      customCollections: {
        custom: FileSystemIconLoader(fileURLToPath(new URL("./src/pptist/assets/icons", import.meta.url))),
      },
      scale: 1,
      defaultClass: "i-icon",
    }),
  ],
  css: {
    preprocessorOptions: {
      scss: {
        additionalData: `
          @import '@/assets/styles/variable.scss';
          @import '@/assets/styles/mixin.scss';
        `,
      },
    },
  },
  resolve: {
    alias: {
      "@": fileURLToPath(new URL("./src/pptist", import.meta.url)),
    },
  },
  server: {
    host: "127.0.0.1",
    port: 5173,
    strictPort: true,
    proxy: {
      "/api": {
        target: "http://127.0.0.1:8000",
        changeOrigin: true,
        ws: true,
      },
      "/ws": {
        target: "ws://127.0.0.1:8000",
        changeOrigin: true,
        ws: true,
      },
      "/healthz": {
        target: "http://127.0.0.1:8000",
        changeOrigin: true,
      },
    },
  },
});
