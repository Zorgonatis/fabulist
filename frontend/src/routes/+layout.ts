// The whole frontend is a client-rendered SPA (adapter-static, fallback page):
// the build is a static artifact served by the engine at the same origin, so
// there is no Node/SSR server in the deploy path and no server-only work here.
export const ssr = false;
export const prerender = false;
