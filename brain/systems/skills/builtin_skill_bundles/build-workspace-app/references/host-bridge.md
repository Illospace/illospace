# Host Bridge

Generated apps may use:

- `await window.illo.getState()` to read state.
- `await window.illo.setState(nextState)` to replace state.
- `await window.illo.updateState(patch)` for shallow patches.
- `window.addEventListener('illo:state', handler)` for host-pushed state.

Never persist user app source as repo files. Use `manage_workspace_app`.
