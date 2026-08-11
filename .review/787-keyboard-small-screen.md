# #787 — slice 6 verification (keyboard operability + small-screen rollback)

Verified by the R3 orchestrator on 2026-08-11, in a real browser, on this branch.
The implementing worker could **not** run a browser (its localhost bind returned
`EPERM`), so every result below was produced by the orchestrator afterwards, not
copied from the worker's report.

## How to reproduce

```
export PATH="/Users/redamjahed/.nvm/versions/node/v22.22.0/bin:$PATH"   # Node 22 — see note
npm --prefix frontend run dev
```
Open `http://localhost:5173/cycles?preview=1`. **No backend is needed** — the page
renders from `frontend/src/routes/cycles/previewBehaviorPolicy.ts`.

## Keyboard — what was driven, and the result

All of these were exercised against the live page by dispatching real `KeyboardEvent`s
at the focused element, plus genuine mouse clicks.

**Read this caveat before trusting the table.** The browser-automation harness used here
delivers text input but does **not** deliver OS-level key events to the page — verified
directly: with capture-phase listeners armed on the focused element, pressing `Return`
and pressing `a` both recorded **zero** events, while typing text into the search box
worked. So the keyboard results below prove the component's *handlers* behave correctly;
they do not exercise the browser's own native activation. One specific interaction is
therefore unproven: on a real `Enter`, the browser fires the keydown (which opens the
menu) and then natively activates the `<button>`. `handleTriggerKeydown` calls
`preventDefault()`, which should suppress that activation — but a human should confirm
Enter on a closed select opens it and leaves it open. It is the one step worth two
seconds of a reviewer's time.

The mouse path was verified end to end with real click events: click opens the menu and
moves focus to the selected option; click again closes it and returns focus to the
trigger.

| Path | Result |
|---|---|
| Activate **Edit behavior** | Editor opens, focus moves to the **Mission prompt** textarea |
| **Enter** on a policy select | Menu opens, focus lands on the **currently selected** option (`aria-selected="true"`) |
| **ArrowDown** inside the menu | Moves to the next enabled option (`Workspace default` → `None`) |
| **End** inside the menu | Jumps to the last option (`xHigh`) |
| **Escape** inside the menu | Menu closes, focus returns to the trigger |
| **Tab** inside the menu | Menu closes, focus advances to the next control (`Model override` → `Thinking override`) |
| **Shift+Tab** inside the menu | Menu closes, focus moves **backwards** to the previous control |
| Activate **Review change** | Focus moves to the `Review behavior change` heading |

Accessible names on the two history Revert buttons are now version-specific —
`"Revert behavior version 3"` and `"Revert behavior version 2"`. Before this change
both were announced only as "Revert", which made them indistinguishable to a screen
reader user choosing which version to roll back to.

## Small screen — 375 × 812

- The page does **not** scroll horizontally: `documentElement.scrollWidth === clientWidth === 375`.
- Checked in three states: the read-only effective policy, the open draft editor, and
  the review diff. All three pass.
- The before/after diff stacks into a single readable column and wraps; no sideways
  scroll, nothing clipped.

### Two overflows that are NOT from this change

Recorded so the next reader does not re-investigate them:

1. `SECTION.constellation-page-frame` reports `scrollWidth` 359 vs `clientWidth` 343
   (16px). That is the shared app shell, untouched by this branch.
2. Three `<small>` elements in the cycle **list rows** overflow their box, by design —
   they carry `white-space: nowrap; text-overflow: ellipsis` and are one-line previews
   of each cycle's description. Pre-existing list styling, not the policy surface.

## Gates (re-run by the orchestrator, serially, at load average 2.4)

```
node --version   v22.22.0
npm test         272/272 pass, 0 fail
npm run check    6877 files, 0 errors, 0 warnings
npm run build    clean
```

**Node 22 is required.** Under Node 20, 41 of the 45 frontend test files fail —
including files this branch never touches. That red is the Node version, not the code.

## Console

The only console errors are repeated `500`s from `/api/health` polling an absent
backend — expected when running the preview without the API, and present regardless of
this branch. This diff adds no network calls.

## Scope note — a shared component changed

`frontend/src/lib/components/constellation/ConstellationSelect.svelte` gained the
keyboard behavior, rather than the cycles feature growing its own select. That is the
correct call under the project's reuse rule, and it means **three surfaces outside this
ticket inherit the improvement**: the workspace composer, the system runtime select,
and the vault page.

**Those three were NOT driven in a browser.** `/system` and `/vault` redirect to
`/login`, and the preview harness only covers the Cycles surface, so exercising them
needs a running backend. What is known: the two new props (`ariaDescribedby`,
`ariaInvalid`) are optional, so no existing call site changes shape; `svelte-check`
passes across all four callers; and the behavior change is additive keyboard handling
on a component that previously had almost none. The residual risk is behavioral, not
structural — a caller that relied on the old click-to-close leaving focus where it was
will now see focus return to the trigger. Worth a glance on staging when this merges.
