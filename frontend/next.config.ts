import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  async redirects() {
    // Short recruiter URL from the Phase 4 brief -> the workspace route.
    return [
      {
        source: "/jobs/:jobId/pipeline",
        destination: "/workspace/jobs/:jobId/pipeline",
        permanent: false,
      },
    ];
  },
};

export default nextConfig;
