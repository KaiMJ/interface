/** @type {import('next').NextConfig} */
const nextConfig = {
  // This app is a stand-in for a legacy back-office system. Reloading is a
  // feature: fault states are toggled at /dev and must take effect immediately.
  reactStrictMode: true,
  // Standalone output: Next traces the modules actually reached and emits a
  // self-contained server. The runtime image then carries no node_modules and no
  // package manager -- roughly 1GB down to ~200MB.
  output: "standalone",
  // Next 16 writes AGENTS.md / CLAUDE.md into the project root on every dev
  // start. Unwanted here: this repo's agent guidance is not per-workspace.
  agentRules: false,
};

export default nextConfig;
