/** @type {import('next').NextConfig} */
const nextConfig = {
  reactStrictMode: true,
  // Fully static client-side app (no SSR/API routes) -> export to web/out so it can
  // be served as plain static files anywhere, with no serverless runtime or secrets.
  output: "export",
};

export default nextConfig;
