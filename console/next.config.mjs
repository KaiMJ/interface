/** @type {import('next').NextConfig} */
const nextConfig = {
  reactStrictMode: true,
  // Standalone output: Next traces the modules actually reached and emits a
  // self-contained server. The runtime image then carries no node_modules and no
  // package manager -- roughly 1GB down to ~200MB.
  output: "standalone",
  // Next 16 writes AGENTS.md / CLAUDE.md into the project root on every dev
  // start. Unwanted here: this repo's agent guidance is not per-workspace.
  agentRules: false,
  // noVNC is a browser-only ESM package that touches `window` at import time.
  // It is loaded via dynamic import inside an effect; this keeps the bundler from
  // trying to evaluate it during SSR.
  transpilePackages: ["@novnc/novnc"],
};

export default nextConfig;
