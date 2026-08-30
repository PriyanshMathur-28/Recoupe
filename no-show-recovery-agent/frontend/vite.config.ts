import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// The Flask app serves the compiled bundle from /static/clients, so asset URLs
// must be rooted there. `npm run dev` proxies the JSON API to Flask on :5000.
export default defineConfig({
    plugins: [react()],
    base: "/static/clients/",
    build: {
        outDir: "../static/clients",
        emptyOutDir: true,
        sourcemap: false,
    },
    server: {
        port: 5173,
        proxy: {
            "/api": {
                target: "http://127.0.0.1:5000",
                changeOrigin: true,
            },
            // landing.css and global.css are served by Flask straight from
            // source (see the matching routes in dashboard.py) instead of being
            // bundled, so the dev server must proxy them too or the pages
            // render unstyled on :5173.
            "/landing.css": {
                target: "http://127.0.0.1:5000",
                changeOrigin: true,
            },
            "/global.css": {
                target: "http://127.0.0.1:5000",
                changeOrigin: true,
            },
        },
    },
});
