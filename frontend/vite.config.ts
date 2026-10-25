import adapter from '@sveltejs/adapter-static';
import { sveltekit } from '@sveltejs/kit/vite';
import { defineConfig, loadEnv } from 'vite';

// Node globals are not typed here (no @types/node), so reach `process` through
// globalThis. This config always runs under Node, so it is present.
const nodeProcess = (globalThis as typeof globalThis & {
	process?: {
		cwd(): string;
		env: Record<string, string | undefined>;
		argv?: string[];
		// Node >= 22.3 (dev machines, CI and the Dockerfile all satisfy this).
		// Used to reach `node:fs` without a type package.
		getBuiltinModule?(id: string): unknown;
	};
}).process;

/** The `node:fs` calls this config makes, and nothing else. */
type NodeFileSystem = {
	readdirSync(dir: string): string[];
	rmSync(dir: string, options: { recursive: boolean; force: boolean }): void;
	mkdirSync(dir: string, options: { recursive: boolean }): void;
	// `type` is POSIX-ignored; entries may be files or directories.
	symlinkSync(target: string, path: string, type: 'dir'): void;
};

/**
 * Routes that exist for local dev only. The `/components` gallery renders every
 * openui component on one wall (no model turns) and is the surface
 * `scripts/render_shots.py` screenshots; it has no production purpose.
 */
const DEV_ONLY_ROUTES: readonly string[] = ['components'];

/**
 * Stage `src/routes` minus `DEV_ONLY_ROUTES` into `.svelte-kit/routes-prod`.
 *
 * SvelteKit derives the route table from the filesystem under `files.routes` —
 * there is no per-route build exclusion, and a `$app/environment` `dev` check
 * would still ship the route id and its chunk. So a production build points kit
 * at this overlay instead, while `vite dev` keeps reading the real tree (HMR on
 * the harnesses is untouched). Entries are symlinked, not copied, so the
 * modules resolve to their real paths and nothing can go stale.
 */
function stageProductionRoutes(projectRoot: string): string {
	const fs = nodeProcess?.getBuiltinModule?.('node:fs') as NodeFileSystem | undefined;
	if (!fs) {
		throw new Error(
			'vite.config: process.getBuiltinModule is unavailable (Node >= 22.3 required) — ' +
				'refusing to build a bundle that would ship the dev-only routes'
		);
	}

	const source = `${projectRoot}/src/routes`;
	const staged = `${projectRoot}/.svelte-kit/routes-prod`;
	fs.rmSync(staged, { recursive: true, force: true });
	fs.mkdirSync(staged, { recursive: true });
	for (const entry of fs.readdirSync(source)) {
		if (DEV_ONLY_ROUTES.includes(entry)) continue;
		fs.symlinkSync(`${source}/${entry}`, `${staged}/${entry}`, 'dir');
	}
	return staged;
}

/**
 * A real `vite build` — not `svelte-kit sync`/`svelte-check`, which resolve
 * this same config with the `build` command to generate types for the DEV tree
 * (`./$types` mirrors `src/routes` through `rootDirs`). Only an actual build
 * invocation gets the overlay, so type generation keeps seeing every route.
 */
function isProductionBuild(command: string): boolean {
	return command === 'build' && (nodeProcess?.argv ?? []).includes('build');
}

export default defineConfig(({ command, mode }) => {
	// @openuidev/lang-core reads the telemetry opt-out from the Node process env
	// at runtime (browser runtimes never send). Force it here so dev, build and
	// preview all keep the bundled PostHog path off.
	const env = loadEnv(mode, nodeProcess?.cwd() ?? '', '');
	if (nodeProcess) nodeProcess.env.OPENUI_TELEMETRY_DISABLED = env.OPENUI_TELEMETRY_DISABLED || '1';

	return {
		define: {
			// Belt-and-suspenders for bundled code reading it via dot access.
			'process.env.OPENUI_TELEMETRY_DISABLED': JSON.stringify('1')
		},
		server: {
			watch: {
				// `build/` is adapter-static's OUTPUT (the artifact the engine
				// serves), and it sits inside this dev server's watched root — so
				// every `npm run build` was a watched change and the dev server
				// broadcast `page reload build/index.html` to every open client
				// (vite's html handler in the client environment). The artifact is
				// never an HMR input, and a verification build must not be able to
				// disturb a live dev session, so the watcher ignores it outright.
				// (`.svelte-kit` is deliberately NOT listed: kit's own dev plugin
				// already ignores all of it except `generated/`, which route HMR
				// needs.)
				ignored: ['**/build/**']
			}
		},
		plugins: [
			sveltekit({
				// Production bundles route from the staged overlay; dev reads src/routes.
				...(isProductionBuild(command)
					? { files: { routes: stageProductionRoutes(nodeProcess?.cwd() ?? '') } }
					: {}),
				compilerOptions: {
					// Force runes mode for the project, except for libraries. Can be removed in svelte 6.
					runes: ({ filename }) =>
						filename.split(/[/\\]/).includes('node_modules') ? undefined : true
				},

				// SPA build: `frontend/build/` is a static artifact the FastAPI engine
				// serves at the SAME ORIGIN (src/fabulist/api/app.py mounts it at "/").
				// The fallback page plus `ssr = false` in src/routes/+layout.ts mean
				// every path is answered by build/index.html and the app routes in
				// the browser — no Node server anywhere in the deploy path.
				adapter: adapter({ fallback: 'index.html' })
			})
		]
	};
});
