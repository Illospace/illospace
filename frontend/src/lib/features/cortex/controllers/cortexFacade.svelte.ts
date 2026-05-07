import { cortex } from '$lib/stores/cortex.svelte';

export type CortexFacadeStore = typeof cortex;

export const cortexFacade: CortexFacadeStore = cortex;

export { cortex };

export default cortexFacade;
