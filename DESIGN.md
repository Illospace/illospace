---
version: alpha
name: Illo Brain Constellation
description: A calm, spatial, multiplayer AI workspace design system for Cortex and the Illo Brain product.
colors:
  primary: "#8DB7FF"
  secondary: "#D5A14D"
  neutral: "#050915"
  surface: "#04070D"
  on-surface: "#F0F0FA"
  error: "#F87171"
  success: "#4FBF91"
typography:
  display:
    fontFamily: "'Space Grotesk', 'Inter', sans-serif"
    fontSize: 46px
    fontWeight: 500
    lineHeight: 1.08
    letterSpacing: 0px
  astre:
    fontFamily: "'Space Grotesk', 'Inter', sans-serif"
    fontSize: 40px
    fontWeight: 500
    lineHeight: 1.08
    letterSpacing: 0px
  body:
    fontFamily: "'Inter', 'Helvetica Neue', Arial, sans-serif"
    fontSize: 15px
    fontWeight: 400
    lineHeight: 1.6
    letterSpacing: 0px
  body-small:
    fontFamily: "'Inter', 'Helvetica Neue', Arial, sans-serif"
    fontSize: 12px
    fontWeight: 400
    lineHeight: 1.32
    letterSpacing: 0px
  meta:
    fontFamily: "'IBM Plex Mono', monospace"
    fontSize: 11px
    fontWeight: 500
    lineHeight: 1.32
    letterSpacing: 0.08em
spacing:
  xxs: 4px
  xs: 8px
  sm: 12px
  md: 16px
  lg: 24px
  xl: 32px
  xxl: 48px
  xxxl: 64px
rounded:
  xs: 4px
  sm: 6px
  md: 8px
  lg: 12px
  panel: 16px
  full: 999px
components:
  astre-spectral:
    typography: "{typography.astre}"
    rounded: "{rounded.full}"
    backgroundColor: "{colors.primary}"
    textColor: "{colors.neutral}"
  astre-amber:
    typography: "{typography.astre}"
    rounded: "{rounded.full}"
    backgroundColor: "{colors.secondary}"
    textColor: "{colors.neutral}"
  signal-blob:
    typography: "{typography.body-small}"
    rounded: "{rounded.full}"
    backgroundColor: "{colors.surface}"
    textColor: "{colors.on-surface}"
  panel:
    rounded: "{rounded.panel}"
    backgroundColor: "{colors.neutral}"
    textColor: "{colors.on-surface}"
  button-primary:
    typography: "{typography.meta}"
    rounded: "{rounded.full}"
    backgroundColor: "{colors.on-surface}"
    textColor: "{colors.neutral}"
    padding: 12px
  button-secondary:
    typography: "{typography.meta}"
    rounded: "{rounded.full}"
    backgroundColor: "{colors.surface}"
    textColor: "{colors.on-surface}"
    padding: 12px
  composer:
    typography: "{typography.body}"
    rounded: "{rounded.panel}"
    backgroundColor: "{colors.surface}"
    textColor: "{colors.on-surface}"
  pill:
    typography: "{typography.meta}"
    rounded: "{rounded.full}"
    backgroundColor: "{colors.surface}"
    textColor: "{colors.on-surface}"
  pill-danger:
    typography: "{typography.meta}"
    rounded: "{rounded.full}"
    backgroundColor: "{colors.neutral}"
    textColor: "{colors.error}"
  pill-success:
    typography: "{typography.meta}"
    rounded: "{rounded.full}"
    backgroundColor: "{colors.success}"
    textColor: "{colors.neutral}"
---

# Illo Brain DESIGN.md

## Overview

Illo Brain should feel like intelligent collaboration made visible. The product is a multiplayer AI workspace where humans and agents work around ideas, threads, memory, skills, and generated work surfaces. The design language is called **Constellation**.

The core emotional target is calm shared presence: alive, spatial, and memorable, but never noisy or cryptic. Users should feel less alone than they do in isolated AI chats and less vigilant than they do in busy dashboards.

This file is the first stop for agents changing UI. It condenses the current design philosophy, token grammar, and approved primitive patterns. When more detail is needed, use these sources in order:

- `../illo-brain-design-system/docs/mcp-surface.md`
- `../illo-brain-design-system/rules/design-language.md`
- `../illo-brain-design-system/approved/constellation/`
- `frontend/src/lib/styles/tokens.css`
- `frontend/src/lib/styles/constellation.css`
- `frontend/src/lib/components/constellation/`

If this file conflicts with the approved design-system repo, the approved `Constellation Editorial` source wins for visual output. If a visual change risks thread opening, composer submission, thread reply, or stream behavior, preserve the existing product behavior until the product owner approves the new interaction.

Core product principles:

- Shared presence over isolated panels.
- Calm signal over alert fatigue.
- Conversation belongs at the center.
- Space should clarify thought.
- Agent work should feel native to collaboration, not like sci-fi decoration.
- Open-source friendliness matters: the system should be legible, adaptable, and not dependent on one-off visual magic.

## Current Polish Direction

This is the active correction for the next design pass.

The Cortex workspace canvas is working. Keep its dark-mode star backdrop, its orbit-first identity, and the Daylight neon/cartographic energy around astres, blobs, and spatial signals. That part is the product's memorable center.

The supporting UI has drifted too atmospheric. Thread surfaces, system buttons, navigation, and the compact chat panel should become calmer, flatter, more legible, and more structural. The workspace can feel celestial; the working surfaces should feel like a precise collaborative tool.

Current correction:

- Keep spatial atmosphere in the canvas, edge context, astres, blobs, and small state signals.
- Make reading and working surfaces opaque. Do not put transcript text, message bodies, or composer text on translucent glass.
- Reduce broad gradients outside the orbit/backdrop system. Gradients should be rare, contained, and functional.
- In Daylight, use clean warm-white or soft-white surfaces for reading and controls, not layered glass or heavy color washes.
- In Daylight, routine chrome should be flat. Do not use gradients on the thread stage, nav rail, toolbar buttons, mini chat hover shell, utility panel, or ordinary controls.
- Replace default grey outlines with a clear border grammar: no border when shape and spacing are enough; warm taupe hairlines for structure; neutral/spectral borders for active or focus; semantic borders only for true status.
- Warm taupe is structural in Daylight, not the secondary accent. Do not turn nav selection, toggles, composer controls, or generic hover states amber.
- Daylight secondary controls should be neutral, not contextual color echoes. Avoid blue, blue-green, sage, or cream button fills that compete with the Cortex background; use the secondary/control tokens.
- Do not draw selected nav items as side bars, clipped inset rails, or extra dots. Daylight should keep the same active contour language as dark mode, but with enough neutral contrast to be visible.
- Tone down user-authored message fills. Ownership color should read as rim, mark, or accent before it becomes a large filled block.
- In thread transcripts, user ownership color should live primarily in the mini astre. Keep user message shells neutral, and avoid repeating the "Illo" author label on every assistant reply.
- Thread headers should be one calm row: title, a small Illo working/idle cue, and the side-panel opener. Do not show verbose run metadata as routine thread chrome.
- Bring the compact chat panel back on-brand using the same surface, type, and control grammar as the thread stage.

What we can learn from Linear without copying it:

- A small neutral surface ladder can do more work than decorative gradients.
- Compact 12-15px UI type often feels more professional than oversized chrome.
- Buttons should be precise controls with restrained radius, not glowing pills everywhere.
- One strong accent at a time reads cleaner than many simultaneous saturated states.
- Dense product UI can still feel premium when hierarchy, spacing, and contrast are disciplined.

Do not copy Linear wholesale. Illo keeps Constellation's spatial canvas, ownership colors, astres, signal blobs, and light theme. The lesson is restraint, density, and surface discipline.

## UI Change Classification

Every UI change should declare one primary category before implementation. The category decides where the change belongs, how much verification it needs, and whether this file should change with the code.

Use these categories:

- **Primitive change**: updates a reusable Constellation component, adapter, or shared class in `frontend/src/lib/components/constellation/`, `frontend/src/lib/design-system/`, or shared style files. Use this when the same behavior or visual treatment should appear in multiple places. Check approved design-system sources before inventing a new primitive. Update `DESIGN.md` when the primitive introduces or changes product-level grammar.
- **Token or theme change**: changes semantic colors, type scale, radius, elevation, spacing, focus, or light/dark behavior in `frontend/src/lib/styles/tokens.css` or `frontend/src/lib/styles/constellation.css`. This usually requires a `DESIGN.md` update unless the code is only correcting drift back to an already documented rule.
- **Documented design-rule change**: changes the design language, surface ladder, typography guidance, interaction grammar, or promotion rules. Update `DESIGN.md` first or in the same patch, then make code follow it.
- **Page layout change**: rearranges a route, panel, or feature view using existing primitives and tokens. Keep it local to the page or feature folder. Update `DESIGN.md` only if the layout establishes a reusable page pattern or changes product-level hierarchy.
- **Local component styling change**: tunes a one-off feature component, state, or responsive edge case. Keep styles scoped. Do not promote to tokens or primitives unless the same need appears in multiple places.
- **Behavior-sensitive visual change**: touches Cortex orbit, thread opening, composer submission, thread reply, stream rendering, generated app surfaces, or workspace motion. Preserve behavior first, then polish. Verify the affected workflow before finishing.
- **MagicPath exploration**: creates or edits a MagicPath canvas component as a design proposal. Treat it as reference until ported into the Svelte app. When porting, classify the production change using the categories above.

Promotion rules:

- If a value repeats across unrelated components, prefer a token or primitive instead of page-local CSS.
- If a visual rule affects color, typography, spacing, elevation, radius, or light/dark behavior across the product, update `DESIGN.md`.
- If a change affects only composition inside one route and uses existing primitives correctly, keep it local.
- If a local style starts carrying product language, promote it before it spreads.
- If a MagicPath component is accepted, do not blindly add it to the app. Translate the intent into existing Svelte primitives, or create a new primitive only after the approved-source check.

Before a UI patch, write down:

- category
- files expected to change
- whether `DESIGN.md` needs an update
- visual/workflow checks required before done

## Colors

The canonical Constellation palette is a deep observatory field with two primary ownership families:

- **Space**: `neutral` and `surface` in this file map to `space-blue-black` and `space-deep` in the approved Constellation tokens.
- **Reading light**: `on-surface` maps to the quiet high-contrast `text-primary`.
- **Spectral**: `primary` is the blue signal family for one owner or cool activity.
- **Amber**: `secondary` is the warm signal family for one owner or warm activity.
- **Danger**: `error` is reserved for risk, failure, or destructive action. It is not user-pickable ownership color.
- **Positive**: `success` is reserved for health, success, or complete state.

The front matter keeps colors as hex values for `DESIGN.md` compatibility. The actual product uses richer CSS variables for alpha, shadows, gradients, state colors, light mode, and component materials. Do not recreate those values locally; use `frontend/src/lib/styles/tokens.css` and `frontend/src/lib/styles/constellation.css`.

Theme naming rules:

- `Constellation` is the named dark product theme, not merely "dark mode".
- `Daylight` is the named light product theme.
- `data-theme` stores the named theme id, currently `constellation` or `daylight`.
- `data-color-scheme` stores the contrast axis, currently `dark` or `light`; light-specific CSS must target this axis rather than `data-theme='light'`.
- Components should consume semantic `--constellation-*` variables. Do not add local hardcoded dark colors plus a matching light override unless the value is first promoted to a shared token or a clearly scoped component variable.
- In `frontend/src/lib/styles/constellation.css`, Daylight differences belong in the single `:root[data-color-scheme='light']` token boundary; primitive selectors below it must remain mode-agnostic.
- If a color feels wrong in more than one page, promote the correction to semantic `--constellation-*` tokens before patching local selectors.

Surface color rules:

- Dark working surfaces should lean toward solid near-black, graphite, and deep slate materials with subtle borders.
- Daylight working surfaces should lean toward opaque warm white, soft white, and pale gray with crisp readable text.
- Avoid blue-purple tinted glass as the default material for thread, nav, mini chat, buttons, and secondary pages.
- Inputs, nested cards, utility rows, and menu surfaces in Daylight should use neutral surface tokens, not pale blue fills copied from the workspace canvas.
- Use spectral and amber as accents, ownership cues, and state signals; do not let them flood large reading surfaces.
- `Danger`, `success`, warning, and info colors stay semantic. They are never general decoration.

Ownership color rules:

- User color should appear as rim, bloom, halo, small accents, and authored identity marks before it becomes a full surface fill.
- `Astre` is the brightest point in a cluster.
- `SignalBlob` can approach astre brightness only when working, and should remain a darker sibling in the same hue family.
- User color is identity, not status. Working, unread, inside, attention, and risk are layered cues, not replacements for ownership color.
- Light mode keeps blobs and mini astres mostly warm neutral or glasslike, using user colors as subtle rims, glows, and accents.

## Typography

Constellation uses:

- `Space Grotesk` for display, astre glyphs, and high-identity moments.
- `Inter` for UI and readable body copy.
- `IBM Plex Mono` for compact metadata, instrument labels, URLs, timings, model labels, and operational facts.

Typography should stay quieter than the spatial metaphor. Use hero-scale type only for actual screen-level introductions or astre glyphs. Thread titles are conversational, not heroic. Dense chrome uses small mono labels, often uppercase, but avoid turning whole screens into technical telemetry.

Product UI density:

- The live app is not globally too large. Most shipped UI already sits in the `9px` to `14px` band.
- The main typography problem is inconsistent local sizing and loud surrounding chrome, not message body size.
- Default interface text should generally live around `13px` to `15px`, depending on reading length and surface density.
- Metadata, pills, timestamps, and compact controls can live around `10px` to `12px`.
- Long-form thread responses should prioritize comfortable reading over compact dashboard density.
- Page and panel headings should usually be restrained. Avoid large hero typography inside operational pages, thread headers, utility panels, and mini chat.
- `Space Grotesk` is for identity moments and astre/spatial expression; do not use it to make every secondary page feel like a poster.

Target type scale:

| Role | Target | Use |
| --- | --- | --- |
| `caption` | `10px` / `0.625rem` | tiny metadata, timestamps, table hints, eyebrow labels |
| `meta` | `11px` / `0.6875rem` | pills, status chips, compact labels, nav labels |
| `control` | `12px` / `0.75rem` | dense buttons, tabs, menu items, compact form labels |
| `ui` | `13px` / `0.8125rem` | rows, secondary descriptions, utility panel body |
| `body` | `14px` / `0.875rem` | default product copy, chat body, lists, table text |
| `reading` | `15px` / `0.9375rem` | longer Illo responses, composer text, comfortable thread reading |
| `title-sm` | `16px` / `1rem` | compact thread titles, card/panel titles |
| `title` | `18px` / `1.125rem` | page section titles, important modal titles |
| `page-title` | `22px` to `24px` / `1.375rem` to `1.5rem` | secondary route titles and major page headers |
| `display` | `32px+` | rare brand, onboarding, or spatial identity moments only |

Unit rules:

- Shared typography tokens should be expressed in `rem` so browser text scaling works.
- Unitless line height is preferred.
- Component-local `px` is acceptable only for very small optical corrections; promote repeated values to tokens.
- Do not use `em` for core font sizes because nested controls compound unpredictably.
- `em` is acceptable for icon sizing, glyph offsets, and spacing that should follow a local label.
- Do not scale product UI text with viewport width. Avoid `vw` and `clamp()` for routine app typography.
- `clamp()` is acceptable for rare brand/display moments, astre glyphs, or responsive preview artifacts, not for thread body, nav, buttons, or secondary-page chrome.

Current audit implications:

- Thread and mini-chat body text at `14px` is basically right.
- Composer text at `15px` is right if the surrounding dock becomes calmer.
- Pills and dispatch metadata should stay closer to `10px` to `11px`; do not enlarge them to compensate for weak hierarchy.
- Nav labels and compact buttons should feel `11px` to `12px`, with state shown by contrast and accent, not size.
- Secondary pages should converge on `13px` to `14px` body, `18px` section titles, and `22px` to `24px` page titles.
- If a surface feels too large, first reduce padding, gradients, borders, and color weight before shrinking readable text.

Reading rules:

- Long-form Illo responses optimize for comfort, not visual novelty.
- Thread message body copy should use a calm reading rhythm and enough line height to avoid dashboard fatigue.
- Metadata collapses into one quiet signal line when possible.
- Do not use negative letter spacing in new UI. Use `0` unless an existing token says otherwise.

## Layout

The core layout is spatial first, panel second. Cortex is not a generic SaaS dashboard with a decorative canvas. The canvas is the product's main orientation surface.

Primary surfaces:

- **Workspace**: orbit-first scene with astres, signal blobs, toolbar, nav rail, and persistent composer.
- **Thread stage**: selected idea becomes the central collaboration surface. The surrounding orbit remains as softened spatial context.
- **Utility panel**: summoned side context for Browser, Activity, details, and future tools. It is not part of the primary reading path by default.
- **Generated app surfaces**: compact live objects or facades on the workspace, then richer interactive surfaces when opened.

Spatial rules:

- Users and agents are anchors. Ideas orbit anchors.
- Selecting a thought should feel like entering a collaboration space, not inspecting a dashboard row.
- Nearby activity may remain visible as soft edge motion during thread work.
- The persistent composer is a workspace-level command surface, not a disposable text box.
- On smaller screens, support panels may overlay. The conversation remains the primary surface.

Spacing rules:

- Use the `4px` based spacing scale from the token layer.
- Prefer roomy composition around the canvas and thread reading path.
- Keep dense operational controls compact and grouped.
- Do not nest cards inside cards. Use bands, panels, rows, and direct layout hierarchy instead.

## Elevation & Depth

Depth is atmospheric and relational, not heavy card stacking.

Use depth for:

- floating composer docks
- summoned utility panels
- browser or preview frames
- thread-stage reveal and reading core
- active controls and overlays

Avoid depth for:

- ordinary page sections
- repeated generic cards
- decorative panels that do not own interaction
- stacked sidebars that compete with the thread reading path

Surface rules:

- Panels use solid or near-solid dark or warm-light materials with restrained borders and inset highlights.
- Shadows should imply distance from the canvas, not glossy marketing polish.
- The thread stage should be visually connected to the workspace, but its reading and input surfaces must be opaque.
- Light mode should feel like sunlit celestial cartography, not a washed-out version of dark mode.

Transparency rules:

- Do not place primary transcript text, message bodies, user input, or utility content on transparent glass.
- Do not rely on backdrop blur to make text readable.
- If surrounding orbit motion remains visible during thread work, keep it at the periphery or behind opaque stage chrome, never directly behind the reading path.
- The illusion of movement behind the thread is optional; legibility is mandatory.
- Mini chat is the exception because it is ambient workspace chrome: collapsed idle state may be fully transparent with no blur or shell, but hover, focus, or foreground state must restore readable panel and input surfaces.
- Allowed transparency: ambient edge context, hover glows, orbit halos, collapsed idle mini chat, subtle overlays outside the reading core, and temporary transition effects.
- Not allowed: translucent thread-stage reading core, translucent message cards, translucent composer text field, always-glassy mini chat transcript, or glassy system navigation as the default.

Surface ladder:

| Level | Constellation | Daylight | Use |
| --- | --- | --- | --- |
| 0 | observatory field | neon cartography field | workspace canvas and spatial backdrop |
| 1 | near-black solid | warm white solid | main thread and page reading surface |
| 2 | graphite / deep slate | soft white / pale gray | panels, utility surfaces, nav, mini chat |
| 3 | restrained border / inset | no border or warm taupe hairline | buttons, inputs, rows, compact controls |
| Signal | spectral / amber / semantic colors | spectral / amber / semantic colors | ownership, status, focus, warnings |

Gradients belong mostly to Level 0 and Signal layers. Level 1 and Level 2 should usually be flat or nearly flat.
In Daylight, a grey border should be treated as a bug unless it represents data chrome, not product chrome. Prefer `transparent`, `rgba(126, 92, 52, 0.08-0.12)` for structure, or neutral/spectral/semantic state color. Do not promote warm taupe into a broad active fill.

## Shapes

The shape language mixes orbital organic markers with restrained instrument chrome.

Canonical shapes:

- `Astre`: circular source marker with off-axis rings, calm core, and breathing halo.
- `SignalBlob`: irregular organic contour using `alpha`, `beta`, `gamma`, or `delta`.
- `PresenceSeed`: tiny authored identity mark descended from `Astre`.
- `Composer`: anchored rounded dock, never a generic textarea card.
- `Panel`: 16px rounded summoned surface.
- `Pill`, `SelectChip`, `IconButton`: pill or compact instrument control.

Rules:

- Organic blobs are for spatial ideas and thread nodes, not generic cards.
- Rectangular chrome should be restrained and purposeful.
- Avoid using `Astre` as a generic avatar, badge, or status pill.
- Avoid turning `SignalBlob` into a rounded rectangle.
- Blobs should not touch or overlap astres or other blobs; keep visible orbital gaps.

## Components

Use approved Constellation primitives before creating local UI. In this Svelte app, the main adapter layer is `frontend/src/lib/components/constellation/`. The sibling design system remains the approved React/Storybook reference.

Approved or ported core primitives:

- `Astre`: user or agent source marker on the orbit canvas.
- `SignalBlob`: thought, request, or thread node on the workspace.
- `ConstellationPresenceSeed`: compact identity marker for messages, rows, and stacks.
- `ConstellationAstrePalette`: ownership color picker rendered as mini astres.
- `ConstellationComposer`: primary workspace and thread input surface.
- `ConstellationButton`: authored text button for workspace, toolbar, and thread actions.
- `ConstellationIconButton`: compact icon-only utility control.
- `ConstellationPill`: state, model, dispatch, and metadata label.
- `ConstellationSelectChip`: compact dropdown control for composer settings or modes.
- `ConstellationToolbar`: workspace search, filtering, summary, and view toggles.
- `ConstellationNavRail`: overlay navigation rail, collapsed by default.
- `ConstellationScreenFrame`: secondary-screen shell.
- `ConstellationThreadShell`: thread-stage reveal and spatial continuity wrapper.
- `ConstellationThreadHeader`: compact thread identity and utility access.
- `ConstellationThreadMessage`: authored user signal and readable Illo output.
- `ConstellationThinkingState`: temporary active-work insert.
- `ConstellationDispatchInsert`: in-thread agent work container.
- `ConstellationUtilityPanel`: resizable summoned side panel for thread tools.
- `ConstellationBrowserPane`: browser-only preview shell when a dedicated pane is needed.
- `ConstellationVisualReplyBlock`: rich artifact block inside a thread.
- `ConstellationDataTable`, `ConstellationEntityList`, `ConstellationActivityFeed`, `ConstellationWorkerLanes`, `ConstellationPipelinePhaseStrip`, `ConstellationToolCallSummary`: dense factual and runtime views.

Component rules:

- Buttons are instrument controls, not glossy CTAs. Keep labels concise and use icons where useful.
- Icon buttons need tooltips or clear positional meaning.
- The composer trailing action defines the primary action feel. Generic primary buttons should align with its simple, elegant shape and contrast, without metallic gradients.
- Button hover states should stay flat unless a state genuinely needs elevation. Do not reintroduce gradient or shine effects through hover-only rules.
- Secondary buttons and icon buttons should consume the shared secondary/control tokens. Do not hardcode cream, amber, blue, or sage variants locally.
- Utility panel tab switching belongs inside one side panel, not in separate stacked columns.
- Browser and Activity coexist as utility panel tabs in the approved thread stage.
- Visual reply blocks stay conversational. They may render charts, diffs, diagrams, code, or previews, but they should not feel like a separate app jammed into the transcript.
- Data tables are for factual secondary screens, not the main expression of the workspace.
- Popovers and menus should reuse floating/select-chip menu tokens for border, background, shadow, active row, and supporting text. Local menu accents should be promoted only when they establish a reusable state.
- Compact two-pane menus should align option lists near the top edge. Avoid hidden vertical slack between the category list and its active option panel.

Thread-stage rules:

- The thread stage reading core must be opaque in both Constellation and Daylight.
- Daylight thread stage should read as clean white or warm-white workspace, not translucent glass over neon.
- Dark thread stage should preserve the current calm near-black read, but avoid excessive gradients behind long text.
- User messages should be distinct but quiet: use presence seed, rim, narrow accent, or subtle tinted shell before a saturated full-card fill.
- Illo responses should stay mostly unboxed or very lightly framed, optimized for reading.
- Dispatch inserts and tool summaries can use stronger structure, but should still feel conversation-native rather than dashboard widgets.
- Header icon buttons must have enough local inset for borders, focus rings, and hover transforms. If a border clips, fix the row or button-group geometry before changing color or border width.
- Dividers need follow-through spacing. When a section introduces a horizontal separator, the next heading or control group should not touch it; encode the spacing in the section primitive when the pattern repeats.

Navigation and system-button rules:

- Nav and system controls should use solid neutral surfaces, compact typography, and small focused accents.
- Avoid broad gradients on the nav rail, utility controls, secondary-page buttons, and toolbar buttons.
- In Daylight, nav, toolbar, thread-stage, and icon-button borders should follow the same grammar: transparent by default, warm structural line only where needed, neutral or spectral for active/focus.
- Active nav should avoid selected-item rails and extra status dots. Use a full, quiet rounded contour plus stronger glyph/text contrast so light and dark modes share the same selection grammar.
- Use pill shapes only when the control is truly a pill, chip, or status label. Ordinary buttons can use tighter `6px` to `8px` radii.
- Hover and active states should change contrast, border, or small accent marks before adding glow.

Mini chat panel rules:

- The compact chat panel should feel like the same product as the thread stage.
- Preserve its compact size and layout if they work, but align its surface, typography, composer, message treatment, and controls to Constellation primitives.
- Do not let mini chat become a separate dark SaaS widget or generic messenger skin.
- At rest, it should behave like ambient game chat: no visible panel background, no message-box fill, and no blur. On hover, focus, or foreground state, bring back a readable near-solid shell and input surface.
- Tabs should feel like compact product tabs, not a `Team · Name` text label. Keep the type small, steady, and quiet.
- The top edge may be user-resizable. Keep the resize affordance quiet: invisible at rest, visible on hover/focus/drag, with the user's height preference persisted locally.

Later polish queue:

- Run a dedicated light/dark border audit across nav, thread stage, right dock, composer, utility controls, and secondary panels. The goal is consistent contour grammar, not identical borders everywhere.
- Review every active, hover, focus, and disabled control state in Daylight to make sure warm taupe remains structural and amber/spectral are only used for authored signal or true state.
- Revisit the composer action button in all runtime states after live usage: idle, preview, streaming, stopping, disabled, and error should feel related without inheriting generic button chrome.
- Continue mini chat visual QA with real messages, longer names, tabs, typing state, and hover/focus transitions in both themes.
- Do a second typography pass once the surface noise is lower, especially compact labels, code pills, dispatch headers, and right-panel tabs.

## Do's and Don'ts

Do:

- Read this file before UI work.
- Use approved Constellation primitives and tokens first.
- Preserve thread opening, thread dismiss, composer submission, attachment flow, thread reply, and stream behavior.
- Keep conversation as the primary surface once a thread is open.
- Use ambient motion and relationship before badges and alerts.
- Keep user colors stable through ownership, handoff, and agent state changes unless the owner actually changes.
- Treat `CortexSVG.svelte` orbit edits as behavior-sensitive, even when changing only visuals.
- Document any visual drift from the approved MCP surface as a temporary parity gap.
- Use Linear as a restraint reference for density, solid surfaces, small controls, and disciplined accents.
- Prefer flat or nearly flat neutral surfaces for work areas, especially in Daylight.

Don't:

- Do not invent a big new local primitive before checking the approved design-system repo or MCP surface.
- Do not create a second visible orbit renderer over the live canvas.
- Do not make the live workspace orbit a decorative background behind dashboard UI.
- Do not use generic SaaS cards as the default unit of layout.
- Do not use gradient-orb decoration, purple AI slop, or ornamental noise to fake intelligence.
- Do not repaint ownership color as status color.
- Do not stack Browser, Activity, Details, and Audit as permanent columns around the thread.
- Do not put full user names inside astres or presence seeds.
- Do not expose unfinished exploration artifacts as if they were approved.
- Do not put primary reading surfaces on transparent glass.
- Do not use broad gradients on system buttons, nav, thread reading surfaces, or mini chat by default.
- Do not let user message color overwhelm the thread.
- Do not leave legacy keyboard shortcuts attached to hidden or deprecated overlays. If a surface is no longer part of the product, remove the shortcut, lazy loader, state, component, and feature API wrapper together.
- Do not patch repeated Daylight color drift page by page when a shared token or primitive is responsible.

## Spatial Interaction Grammar

The Constellation model has three levels:

- **Universe / team view**: astres and their orbiting work create shared presence.
- **Personal astre context**: focus on one person's local orbit without losing team orientation.
- **Idea / thread view**: enter the collaboration surface for a thought.

Thread entry should preserve origin:

- the selected `SignalBlob` is the thread origin
- the workspace softens behind the thread stage
- the composer changes from workspace dock to thread reply mode
- utility access is available but secondary

Workspace creation rules:

- any non-empty workspace composer submission creates a thought and opens its thread
- attachments, `@` mentions, slash commands, and paste or drag-drop flows must remain intact
- newly created thoughts must appear immediately in orbit and not as invisible click targets

## Motion & State

Motion should make work legible, not decorative.

State grammar:

- `Astre.activity`: `idle`, `working`, `disconnected`
- `SignalBlob.state`: `idle`, `working`, `done`
- `SignalBlob.presence`: `none`, `inside`
- `SignalBlob.cue`: `none`, `attention`, `risk`
- `SignalBlob.treatment`: `bloom`, `contour`, `seed`

Motion rules:

- `Astre` halo breathes subtly at rest.
- `Astre` ring motion should feel like unstable gravity, not a spinner.
- `SignalBlob.working` uses a single heartbeat halo for agent work.
- Do not render worker docks, worker chips, agent-count badges, or runner-linked micro-widgets on `SignalBlob`; per-worker detail belongs inside runtime views, not on the orbit canvas.
- `inside` is live occupancy and should stack with working instead of replacing it.
- `attention` and `risk` stay peripheral, usually top-right, and should not repaint the whole blob.
- Reduced-motion fallbacks must freeze looping layers while preserving state clarity.

## Implementation Rules

Use this hierarchy when designing or porting UI:

1. Approved MCP and Storybook surface in `../illo-brain-design-system/approved/constellation/`.
2. Live Svelte Constellation adapters in `frontend/src/lib/components/constellation/`.
3. Token CSS in `frontend/src/lib/styles/tokens.css` and `frontend/src/lib/styles/constellation.css`.

Before adding a primitive:

- check whether it exists in the approved design-system component collection
- port or adapt that primitive first
- if no approved primitive exists, keep the new surface local until the product owner approves promotion

Before finishing UI changes that touch Cortex:

- verify thread open, dismiss, composer submit, thread reply, and visible orbit sanity
- keep thread opening, composer submission, thread reply, and stream behavior intact
