import { sveltekit } from '@sveltejs/kit/vite';
import { defineConfig, loadEnv } from 'vite';

export default defineConfig(({ mode }) => {
	const env = loadEnv(mode, '.', '');
	const apiTarget = env.ILLO_API_PROXY_TARGET || env.ILLO_API_ORIGIN || 'http://localhost:8000';
	const wsTarget = apiTarget.replace(/^http/, 'ws');

	return {
		plugins: [sveltekit()],
		server: {
			proxy: {
				'/api': apiTarget,
				'/static/uploads': apiTarget,
				'/ws': { target: wsTarget, ws: true },
			},
		},
	};
});
