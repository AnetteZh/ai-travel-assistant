import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  // Для Docker: сборка кладёт в .next/standalone минимальный сервер без лишних node_modules.
  output: "standalone",
};

export default nextConfig;
