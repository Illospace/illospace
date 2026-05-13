<script module lang="ts">
  const SIMILARITY_MATRIX_CACHE_MS = 60_000;
  let similarityMatrixCachedAt = 0;
  let similarityMatrixValue: any = null;
  let similarityMatrixRequest: Promise<any> | null = null;

  function loadSimilarityMatrix() {
    if (similarityMatrixValue && Date.now() - similarityMatrixCachedAt < SIMILARITY_MATRIX_CACHE_MS) {
      return Promise.resolve(similarityMatrixValue);
    }
    if (!similarityMatrixRequest) {
      similarityMatrixRequest = fetch('/api/cortex/similarity-matrix')
        .then((response) => response.json())
        .then((data) => {
          similarityMatrixCachedAt = Date.now();
          similarityMatrixValue = data;
          return data;
        })
        .finally(() => {
          similarityMatrixRequest = null;
        });
    }
    return similarityMatrixRequest;
  }
</script>

<script lang="ts">
  /**
   * WorkspaceScene — SVG canvas with D3 force simulation.
   * Direct port of the old dashboard's cortex-canvas.js + cortex-interactions.js + cortex-physics.js.
   * The D3 logic is kept nearly verbatim from the old code.
   */
  import { onMount, onDestroy, untrack } from 'svelte';
  import type { WorkspaceAppRead } from '$lib/features/workspace-apps/api/workspaceAppsApi';
  import type { WorkspacePinRead } from '$lib/features/workspace-scene/api/workspacePinsApi';
  import { cortex } from '$lib/stores/cortex.svelte';
  import { auth } from '$lib/stores/auth.svelte';
  import { presence as presenceStore } from '$lib/stores/presence.svelte';
  import { theme } from '$lib/stores/theme.svelte';
  import * as d3 from 'd3';
  import { buildAstrePrimitiveStyle } from '$lib/components/constellation';
  import WorkspaceOrbitPrimitives from '../renderers/WorkspaceOrbitPrimitives.svelte';
  import './WorkspaceScene.css';
  import type {
    ConstellationActivity,
    ConstellationAstrePresence,
    ConstellationScale,
    ConstellationShape,
    ConstellationSignalCue,
    ConstellationSignalIcon,
    ConstellationSignalState,
    ConstellationSignalTreatment,
    ConstellationTone,
  } from '$lib/components/constellation/constellationTypes';
  import {
    createAttractors, buildLookup, getAttractionTarget, clearAttractionCache,
    multiSunRepulsion, multiSunOrbit, ATTRACTOR_CFG, estimateClusterExtent, computeAttractorViewportTransform,
    orbitAnchorKey, orbitAnchorRefForAttractor, orbitAnchorTargetWithinRadius, orbitPhysicsProfileForAttractor,
    sortTeamMembersForSharedAttractorLayout,
    type Attractor, type AttractorLookup, type AttractorLayoutOptions, type OrbitAnchorRef, type TeamMember,
  } from '$lib/utils/attractors';
  import {
    CORTEX_ORBIT_BANDS,
    cortexOrbitBubbleCollisionRadius,
    cortexOrbitDynamicRadius,
    cortexOrbitPerformanceProfile,
  } from '$lib/utils/cortexOrbitPhysics';
  import {
    primitiveStyle,
    semanticZoomLevelForScale,
    type PrimitiveAppVisual,
    type PrimitiveAstreVisual,
    type PrimitiveBlobVisual,
    type PrimitiveOrbitLaneDot,
    type PrimitiveOrbitLaneRing,
    type PrimitiveOrbitLaneSpoke,
    type PrimitiveOrbitLaneVisual,
    type PrimitivePinVisual,
    type SemanticZoomLevel,
    type WorkspaceAppCollisionObstacle,
  } from '$lib/utils/cortexOrbitPrimitives';
  import type { CortexWorkspacePoint } from '$lib/features/workspace-scene/domain/workspacePoint';
  import {
    applyIdeaSnapshotToSceneNode,
    clearOwnerOrbitLayout,
    createOrbitNodeFromIdea,
    orbitNodeCoords,
    type OrbitNode,
  } from '../domain/workspaceSceneState';
  import {
    birthRenderPosition,
    collapseBirthAnimation,
    startBirthLifecycle as beginBirthLifecycle,
  } from '../engine/birthLifecycle';
  import {
    applyPrimitiveOverlayTransform,
    primitiveOverlayTransformStyle as primitiveOverlayTransformStyleForTransform,
    workspacePointFromClientRect,
  } from '../engine/viewportController';
  import {
    collectSceneIdeaPositions,
    persistSceneIdeaPositions,
  } from '../persistence/scenePositionPersistence';
  import {
    workspaceAppOrbitOrder,
    workspaceAppStoredPosition,
  } from '../renderers/primitiveVisualMapper';
  import {
    ASTRE_ARCHIVE_DOT_PRESETS,
    BUBBLE_SHELL_COLOR,
    CONSTELLATION_TEXT_COLOR,
    archiveDotPosition,
    bubbleCue,
    clamp,
    generateBlobPath,
    interpolateNumber,
    lightenColor,
    mixHex,
    normalizeHexColor,
    primitiveBlobIcon,
    primitiveBlobScale,
    primitiveBlobShape,
    primitiveBlobTreatment,
    primitiveTone,
    rgbTriplet,
    seedHash,
    seededRandom,
    semanticSummaryText,
    statusColor,
    toneVars,
    visualStatus,
    withAlpha,
    wrapTextForBubble,
  } from '$lib/utils/cortexSvgVisuals';

  let {
    apps = [],
    pins = [],
    activeAppId = null,
    onthreadopen,
    onappopen,
    onappmove,
    onapparchive,
    onpinmenu,
    onpinmove,
    onpindelete,
    onworkspacecontext,
    onworkspacecontextmenu,
    onownastreclick,
    onarchivedragstate,
  }: {
    apps?: WorkspaceAppRead[];
    pins?: WorkspacePinRead[];
    activeAppId?: string | null;
    onthreadopen?: (origin: { x: number; y: number; id: string }) => void;
    onappopen?: (origin: { x: number; y: number; appId: string }) => void;
    onappmove?: (move: { appId: string; x: number; y: number }) => void | Promise<void>;
    onapparchive?: (archive: { appId: string }) => void | Promise<void>;
    onpinmenu?: (origin: { x: number; y: number; pinId: string }) => void;
    onpinmove?: (move: { pinId: string; x: number; y: number }) => void | Promise<void>;
    onpindelete?: (deletePin: { pinId: string }) => void | Promise<void>;
    onworkspacecontext?: (point: CortexWorkspacePoint) => void;
    onworkspacecontextmenu?: (point: CortexWorkspacePoint) => void;
    onownastreclick?: (origin: { x: number; y: number; userId: string }) => void;
    onarchivedragstate?: (state: { active: boolean; over: boolean }) => void;
  } = $props();

  let containerEl: HTMLDivElement;

  // ── Constants (from cortex-core.js) ────────────────────────
  const ILLO_AGENT_ROLES = ['illo', 'agent', 'assistant'];

  // Ownership accents follow the orbit anchor: the astre/pin a thread belongs to.
  function ownerColor(d: any): string {
    const anchorKey = itemOrbitAnchorKey(d);
    const anchor = orbitAnchorLookup?.byId?.get(anchorKey)
      ?? orbitAnchorLookup?.byAnchorKey?.get(anchorKey)
      ?? attractorLookup?.byId?.get(anchorKey);
    const anchorColor = normalizeHexColor(anchor?.color);
    if (anchorColor) return anchorColor;

    const currentUserColor = d.user_id === auth.user?.id
      ? normalizeHexColor(auth.user?.color)
      : null;
    if (currentUserColor) return currentUserColor;

    if (d.user_id && attractorLookup?.byId?.has(d.user_id)) {
      const attractorColor = normalizeHexColor(attractorLookup.byId.get(d.user_id)!.color);
      if (attractorColor) return attractorColor;
    }

    const explicitUserColor = normalizeHexColor(d.user_color);
    if (explicitUserColor) return explicitUserColor;

    return '#f0f0fa';
  }

  function bubbleStrokeColor(d: any): string {
    const state = visualStatus(d.status);
    return withAlpha(ownerColor(d), state === 'done' ? 0.24 : 0.42);
  }

  function bubbleStrokeWidth(status: string | undefined): number {
    const state = visualStatus(status);
    return state === 'done' ? 1 : 1.15;
  }

  const bubbleOpacity = (s: string) => {
    const state = visualStatus(s);
    return state === 'working' ? 0.95 : 1;
  };

  function bubbleCollisionRadius(d: any): number {
    return cortexOrbitBubbleCollisionRadius(d, {
      canvasW,
      panelOpen: cortex.panelOpen,
      selectedIdeaId: cortex.selectedIdeaId,
    });
  }

  const ORBIT_BANDS = CORTEX_ORBIT_BANDS;

  const PRIMITIVE_HANDOFF_HIT_LAYER_ONLY = true;
  const USE_D3_SHADOW_SCENE = !PRIMITIVE_HANDOFF_HIT_LAYER_ONLY;
  const STATUS_ORBIT_BIAS: Record<string, number> = {
    working: -14,
    done: 6,
  };
  const BLOB_SHAPES: ConstellationShape[] = ['alpha', 'beta', 'gamma', 'delta'];
  const CLUSTER_OUTER_PADDING = 56;
  const CLUSTER_FIRST_RING_CAPACITY = 4;
  const CLUSTER_RING_CAPACITY_STEP = 2;
  const WORKSPACE_APP_DEFAULT_RADIUS = ATTRACTOR_CFG.coreRadius + 176;
  const WORKSPACE_APP_COLLISION_RADIUS = 82;
  const WORKSPACE_APP_COLLISION_GAP = 18;

  const CORE_RADIUS = 80;
  const CORE_ASTRE_INNER_RADIUS = CORE_RADIUS * 0.86;
  const HANDOFF_THRESHOLD = Math.max(CORE_RADIUS + 48, ATTRACTOR_CFG.coreRadius * 1.75);

  // ── Sizing helpers (from cortex-core.js) ───────────────────
  function dynamicRadius(salience: number, title: string, threadCount = 0): number {
    return cortexOrbitDynamicRadius(salience, title, threadCount);
  }
  const fontSize = (r: number) => Math.max(10, Math.min(15, r * 0.26));

  function bubblePresence(d: any): 'none' | 'inside' {
    for (const entry of cortex.typingUsers.values()) {
      if (entry.idea_id === d.id && entry.user_id !== auth.user?.id) {
        return 'inside';
      }
    }
    return 'none';
  }

  function applyToneVars(selection: d3.Selection<any, any, any, any>, prefix: 'bubble' | 'sun' | 'core', accent: string) {
    const vars = toneVars(accent);
    selection
      .style(`--${prefix}-accent`, vars.accent)
      .style(`--${prefix}-accent-rgb`, vars.accentRgb)
      .style(`--${prefix}-core`, vars.core)
      .style(`--${prefix}-core-rgb`, vars.coreRgb)
      .style(`--${prefix}-owner`, vars.owner)
      .style(`--${prefix}-owner-rgb`, vars.ownerRgb);
  }

  function applyAstreToneVars(selection: d3.Selection<any, any, any, any>, accent: string) {
    const owner = mixHex(accent, CONSTELLATION_TEXT_COLOR, 0.82);
    const coreBackground = [
      `radial-gradient(circle at 42% 26%, color-mix(in srgb, ${accent} 12%, rgba(255, 242, 218, 0.028)) 0%, transparent 18%)`,
      `radial-gradient(circle at 50% 54%, color-mix(in srgb, ${accent} 9%, rgba(22, 19, 18, 0.99)) 0%, rgba(5, 7, 12, 0.99) 78%)`,
    ].join(', ');

    selection
      .style('--astre-tone-color', accent)
      .style('--astre-before-border', withAlpha(accent, 0.14))
      .style('--astre-halo-border', withAlpha(accent, 0.12))
      .style('--astre-halo-inner-border', withAlpha(accent, 0.06))
      .style('--astre-ring-shadow', `drop-shadow(0 0 14px ${withAlpha(accent, 0.14)}) drop-shadow(0 0 28px ${withAlpha(accent, 0.06)})`)
      .style('--astre-core-background', coreBackground)
      .style('--astre-core-color', owner)
      .style('--astre-core-border-color', withAlpha(owner, 0.16))
      .style('--astre-core-inner-stroke', withAlpha(owner, 0.08))
      .style('--astre-core-glow-strong', withAlpha(accent, 0.22))
      .style('--astre-core-glow-soft', withAlpha(accent, 0.1))
      .style('--astre-label-color', withAlpha(owner, 0.78));
  }

  function curvedLinkPath(d: any): string {
    const sx = typeof d.source === 'object' ? d.source.x : 0;
    const sy = typeof d.source === 'object' ? d.source.y : 0;
    const tx = typeof d.target === 'object' ? d.target.x : 0;
    const ty = typeof d.target === 'object' ? d.target.y : 0;
    const mx = (sx + tx) / 2, my = (sy + ty) / 2;
    const dx = tx - sx, dy = ty - sy;
    const dist = Math.sqrt(dx * dx + dy * dy) || 1;
    const offset = dist * 0.15;
    return `M${sx},${sy} Q${mx + (-dy / dist) * offset},${my + (dx / dist) * offset} ${tx},${ty}`;
  }

  function connectionPathClass(d: any) {
    return [
      'connection-path',
      d.sourceUserId && d.targetUserId && d.sourceUserId !== d.targetUserId ? 'is-cross-user' : 'is-same-user',
    ].join(' ');
  }

  function connectionPathStyle(d: any) {
    const sourceAccent = normalizeHexColor(d.sourceAccent) ?? CONSTELLATION_TEXT_COLOR;
    const targetAccent = normalizeHexColor(d.targetAccent) ?? sourceAccent;

    return [
      `--connection-source: ${sourceAccent}`,
      `--connection-target: ${targetAccent}`,
    ].join('; ');
  }

  // ── Shared bubble rendering helpers ──────────────────────────────────
  // Used by both renderCanvas() and addBubbleBirth() to avoid duplication.

  function ideaRadius(d: any): number {
    return dynamicRadius(d.salience_score, d.display_title || d.title, d.thread_count);
  }

  function orbitBand(d: any) {
    return ORBIT_BANDS[visualStatus(d.status)] || ORBIT_BANDS[d.status] || { min: 200, max: 350 };
  }

  function clusterRingCapacity(ringIndex: number) {
    return CLUSTER_FIRST_RING_CAPACITY + ringIndex * CLUSTER_RING_CAPACITY_STEP;
  }

  function clusterSlotForRank(rank: number, total: number) {
    let ringIndex = 0;
    let ringStart = 0;
    let capacity = clusterRingCapacity(ringIndex);

    while (rank >= ringStart + capacity) {
      ringStart += capacity;
      ringIndex += 1;
      capacity = clusterRingCapacity(ringIndex);
    }

    return {
      ringIndex,
      slotIndex: rank - ringStart,
      slotCount: Math.max(1, Math.min(capacity, total - ringStart)),
    };
  }

  function sortByRecency(a: any, b: any) {
    return new Date(b.updated_at || b.created_at || 0).getTime() - new Date(a.updated_at || a.created_at || 0).getTime();
  }

  function pinAnchorId(pinId: string) {
    return orbitAnchorKey('pin', pinId) ?? `pin:${pinId}`;
  }

  function workspacePinAnchor(pin: WorkspacePinRead): Attractor {
    const rawId = String(pin.id);
    const label = String(pin.label || 'Pin').trim() || 'Pin';
    return {
      id: pinAnchorId(rawId),
      kind: 'pin',
      anchorId: rawId,
      name: label,
      color: normalizeHexColor(pin.color) ?? '#57CFA0',
      initial: '',
      x: Number(pin.position_x),
      y: Number(pin.position_y),
    };
  }

  function workspacePinAnchors() {
    return pins
      .filter((pin) => !pin.archived_at && Number.isFinite(pin.position_x) && Number.isFinite(pin.position_y))
      .map(workspacePinAnchor);
  }

  function rebuildOrbitAnchors() {
    orbitAnchors = [
      ...attractors,
      ...workspacePinAnchors(),
    ];
    orbitAnchorLookup = buildLookup(orbitAnchors);
  }

  function itemOrbitAnchorKey(item: any) {
    const explicit = orbitAnchorKey(item?.orbit_anchor_type, item?.orbit_anchor_id);
    if (explicit) {
      if (item?.orbit_anchor_type === 'pin') {
        const pinExists = pins.some((pin) => !pin.archived_at && pin.id === item.orbit_anchor_id);
        if (pinExists) return explicit;
      } else if (
        item?.orbit_anchor_type === 'user'
        || orbitAnchorLookup.byAnchorKey.has(explicit)
        || attractorLookup.byId.has(explicit)
      ) {
        return explicit;
      }
    }
    return item?.user_id || auth.user?.id || '__cortex-ownerless__';
  }

  function clusterBaseAngleForAnchor(anchorId: string) {
    if (!orbitAnchorLookup.byId.size) return -Math.PI / 2;
    const ownerSun = orbitAnchorLookup.byId.get(anchorId) ?? orbitAnchorLookup.byAnchorKey.get(anchorId);
    if (!ownerSun) return -Math.PI / 2;

    if (ownerSun.kind === 'pin') {
      return Math.atan2(ownerSun.y - coreY, ownerSun.x - coreX);
    }

    if (attractors.length === 2) {
      const peerSun = attractors.find((sun) => sun.id !== ownerSun.id);
      if (peerSun) {
        return Math.atan2(ownerSun.y - peerSun.y, ownerSun.x - peerSun.x);
      }
    }

    return Math.atan2(ownerSun.y - coreY, ownerSun.x - coreX);
  }

  function clusterSeedAngle(baseAngle: number, ringIndex: number, slotIndex: number, slotCount: number, anchorId: string) {
    if (slotCount <= 1) return baseAngle;
    if (attractors.length <= 1 && !anchorId.startsWith('pin:')) {
      const normalizedSlot = (slotIndex + ringIndex * 0.42) / slotCount;
      return -Math.PI / 2 + normalizedSlot * Math.PI * 2;
    }
    const spread = Math.min(Math.PI * 1.12, Math.PI * 0.72 + ringIndex * 0.16 + (slotCount - 1) * 0.18);
    const offset = slotCount === 1 ? 0 : slotIndex / (slotCount - 1) - 0.5;
    return baseAngle + offset * spread;
  }

  function orbitAnchorForKey(anchorId: string | null | undefined): Attractor | null {
    if (!anchorId) return null;
    return orbitAnchorLookup.byId.get(anchorId)
      ?? orbitAnchorLookup.byAnchorKey.get(anchorId)
      ?? null;
  }

  function ownerOrbitRadius(item: any, ringIndex: number, ownerId = itemOrbitAnchorKey(item)) {
    const profile = orbitPhysicsProfileForAttractor(orbitAnchorForKey(ownerId));
    return Math.max(
      profile.threadOrbitMinRadius,
      profile.threadOrbitBaseRadius + ringIndex * profile.threadOrbitRingGap + (STATUS_ORBIT_BIAS[visualStatus(item.status)] || 0),
    );
  }

  function applyClusterMetrics(items: any[]) {
    const sortedGlobal = [...items].sort(sortByRecency);
    sortedGlobal.forEach((item, idx) => {
      item._recencyRank = idx;
    });

    const byOwner = new Map<string, any[]>();
    for (const item of items) {
      const key = itemOrbitAnchorKey(item);
      const group = byOwner.get(key);
      if (group) {
        group.push(item);
      } else {
        byOwner.set(key, [item]);
      }
    }

    for (const [ownerId, group] of byOwner.entries()) {
      group.sort(sortByRecency);
      const baseAngle = clusterBaseAngleForAnchor(ownerId);
      group.forEach((item, idx) => {
        const slot = clusterSlotForRank(idx, group.length);
        item._ownerRank = idx;
        item._ownerCount = group.length;
        item._ownerRingIndex = slot.ringIndex;
        item._ownerSlotIndex = slot.slotIndex;
        item._ownerSlotCount = slot.slotCount;
        item._ownerOrbitRadius = ownerOrbitRadius(item, slot.ringIndex, ownerId);
        item._ownerSeedAngle = clusterSeedAngle(baseAngle, slot.ringIndex, slot.slotIndex, slot.slotCount, ownerId);
      });
    }
  }

  function clusterExtentByUser(ideas: any[]) {
    const extents: Record<string, number> = {};
    const loadByUserId = computeUserThreadLoadById(ideas);
    const byOwner = new Map<string, any[]>();
    for (const idea of ideas) {
      const key = itemOrbitAnchorKey(idea);
      const group = byOwner.get(key);
      if (group) {
        group.push(idea);
      } else {
        byOwner.set(key, [idea]);
      }
    }

    for (const [ownerId, group] of byOwner.entries()) {
      group.sort(sortByRecency);
      const lastSlot = clusterSlotForRank(Math.max(0, group.length - 1), group.length);
      const maxBubbleRadius = Math.max(...group.map((idea) => ideaRadius(idea)), ATTRACTOR_CFG.coreRadius);
      const physicalExtent =
        ownerOrbitRadius({ status: 'idle' }, lastSlot.ringIndex, ownerId) + maxBubbleRadius + CLUSTER_OUTER_PADDING;
      const load = loadByUserId[ownerId] ?? group.length;
      extents[ownerId] = Math.max(
        physicalExtent,
        estimateClusterExtent(group.length, load, maxBubbleRadius),
      );
    }

    return extents;
  }

  function orbitDistance(d: any, totalIdeas: number) {
    if (typeof d._ownerOrbitRadius === 'number') {
      return d._ownerOrbitRadius;
    }
    const band = orbitBand(d);
    const rank = d._recencyRank ?? totalIdeas;
    const t = Math.min(rank / Math.max(totalIdeas, 1), 1);
    return band.min + t * (band.max - band.min) + (STATUS_ORBIT_BIAS[visualStatus(d.status)] || STATUS_ORBIT_BIAS[d.status] || 0);
  }

  function resolveIdeaOrbitPoint(d: any, totalIdeas: number) {
    const target = getAttractionTarget(d, orbitAnchorLookup, coreX, coreY, auth.user?.id);
    const rng = seededRandom(Math.abs(seedHash(String(d.id || d.title || 'idea'))) + 1);
    const angle = typeof d._ownerSeedAngle === 'number' ? d._ownerSeedAngle : rng() * Math.PI * 2;
    const jitter = (rng() - 0.5) * 28;
    const profile = orbitPhysicsProfileForAttractor(target.suns[0]);
    const radius = Math.max(profile.threadOrbitMinRadius, orbitDistance(d, totalIdeas) + jitter);
    return {
      x: target.x + Math.cos(angle) * radius,
      y: target.y + Math.sin(angle) * radius,
      target,
    };
  }

  function workspaceBirthPoint() {
    const userId = auth.user?.id;
    const focusSun = userId ? attractorLookup.byId.get(userId) : null;
    const baseX = focusSun?.x ?? coreX;
    const baseY = focusSun?.y ?? coreY;
    const visibleCount = cortex.filteredIdeas.filter((idea: any) => !idea.archived_at).length;
    const angle = -Math.PI / 2 + ((visibleCount % 7) / 7) * Math.PI * 2;
    const radius = Math.max(ATTRACTOR_CFG.glowRadius + 90, 210);
    return {
      x: baseX + Math.cos(angle) * radius,
      y: baseY + Math.sin(angle) * radius,
    };
  }

  function isPinnedToAttractorCore(d: any) {
    if (d.x == null || d.y == null) return true;
    if (d.position_x != null && d.position_y != null) return false;
    const target = getAttractionTarget(d, orbitAnchorLookup, coreX, coreY, auth.user?.id);
    const dx = d.x - target.x;
    const dy = d.y - target.y;
    return Math.sqrt(dx * dx + dy * dy) < ATTRACTOR_CFG.coreRadius * 0.8;
  }

  function renderLabelTspans(textEl: any, d: any, r: number) {
    const fs = fontSize(r);
    const lines = wrapTextForBubble(d.display_title || d.title, r);
    const lineHeight = fs * 1.25;
    const totalHeight = lines.length * lineHeight;
    const startY = -totalHeight / 2 + lineHeight * 0.7;
    textEl.attr('font-size', fs);
    lines.forEach((line: string, i: number) => {
      textEl.append('tspan').attr('x', 0).attr('dy', i === 0 ? startY : lineHeight).text(line);
    });
  }

  function computeQueuePositions(ideas: any[]): Map<string, number> {
    return new Map<string, number>();
  }

  function bubbleHaloAxes(r: number) {
    const width = Math.max(110, Math.min(276, Math.round(r * 2.3)));
    const height = Math.max(94, Math.min(232, Math.round(r * 1.95)));
    return {
      rx: width / 2 + 18,
      ry: height / 2 + 18,
    };
  }

  function applyBubbleVisualState(bubbleEl: d3.Selection<SVGGElement, any, any, any>, d: any) {
    const state = visualStatus(d.status);
    const cue = bubbleCue(d.status);
    const presence = bubblePresence(d);
    const accent = ownerColor(d);

    applyToneVars(bubbleEl, 'bubble', accent);

    bubbleEl
      .classed('state-idle', state === 'idle')
      .classed('state-working', state === 'working')
      .classed('state-done', state === 'done')
      .classed('cue-attention', cue === 'attention')
      .classed('cue-risk', cue === 'risk')
      .classed('presence-inside', presence === 'inside')
      .classed('presence-none', presence !== 'inside')
      .classed('cue-none', cue === 'none');
  }

  function syncStatusAnchor(bubbleEl: d3.Selection<SVGGElement, any, any, any>, d: any, r: number) {
    bubbleEl.selectAll('.bubble-status-anchor').remove();
    if (PRIMITIVE_HANDOFF_HIT_LAYER_ONLY) return;

    const cue = bubbleCue(d.status);
    const presence = bubblePresence(d);
    if (cue === 'none' && presence === 'none') return;

    const anchor = bubbleEl.append('g')
      .attr(
        'class',
        `bubble-status-anchor ${
          presence === 'inside'
            ? 'bubble-status-anchor-inside'
            : cue === 'risk'
              ? 'bubble-status-anchor-risk'
              : 'bubble-status-anchor-attention'
        }`,
      )
      .attr('transform', `translate(${r * 0.56},${-r * 0.56})`);

    if (presence === 'inside') {
      anchor.append('rect')
        .attr('class', 'bubble-status-pill')
        .attr('x', -12)
        .attr('y', -8)
        .attr('rx', 8)
        .attr('ry', 8)
        .attr('width', 24)
        .attr('height', 16);

      for (const offset of [-5, 0, 5]) {
        anchor.append('circle')
          .attr('class', 'bubble-status-presence-dot')
          .attr('cx', offset)
          .attr('cy', 0)
          .attr('r', 1.8);
      }
      return;
    }

    anchor.append('circle').attr('class', 'bubble-status-dot-shell').attr('r', cue === 'risk' ? 7.5 : 7);
    anchor.append('circle').attr('class', 'bubble-status-dot-core').attr('r', cue === 'risk' ? 4.3 : 4);
  }

  function renderBubbleHitContent(bubble: d3.Selection<SVGGElement, any, any, any>) {
    bubble.each(function(d: any) {
      const el = d3.select(this as Element);
      const r = ideaRadius(d);
      applyBubbleVisualState(el as any, d);

      el.append('ellipse').attr('class', 'bubble-cloud')
        .attr('rx', r * 1.02)
        .attr('ry', r * 0.88)
        .attr('opacity', bubbleOpacity(d.status));

      el.append('title').text(d.display_title ? `${d.display_title}\n—\n${d.title}` : d.title);
    });
  }

  function renderBubbleContent(bubble: d3.Selection<SVGGElement, any, any, any>, queuePosMap?: Map<string, number>) {
    if (PRIMITIVE_HANDOFF_HIT_LAYER_ONLY) {
      renderBubbleHitContent(bubble);
      return;
    }

    bubble.each(function(d: any) {
      const el = d3.select(this as Element);
      const r = ideaRadius(d);
      const halo = bubbleHaloAxes(r);
      applyBubbleVisualState(el as any, d);

      el.append('ellipse').attr('class', 'bubble-halo')
        .attr('rx', halo.rx)
        .attr('ry', halo.ry);
      el.append('ellipse').attr('class', 'bubble-state-bloom')
        .attr('cx', -r * 0.06).attr('cy', -r * 0.08)
        .attr('rx', Math.max(18, r * 0.58)).attr('ry', Math.max(12, r * 0.44));
      el.append('ellipse').attr('class', 'bubble-presence-bloom bubble-presence-bloom-a')
        .attr('cx', -r * 0.16).attr('cy', -r * 0.18)
        .attr('rx', Math.max(18, r * 0.46)).attr('ry', Math.max(10, r * 0.26));
      el.append('ellipse').attr('class', 'bubble-presence-bloom bubble-presence-bloom-b')
        .attr('cx', r * 0.2).attr('cy', r * 0.16)
        .attr('rx', Math.max(14, r * 0.36)).attr('ry', Math.max(8, r * 0.2));
      el.append('path').attr('class', 'bubble-cloud')
        .attr('d', generateBlobPath(r, d.id))
        .attr('filter', 'url(#bubble-shadow)')
        .attr('opacity', bubbleOpacity(d.status));

      const clipId = `clip-${d.id.replace(/[^a-zA-Z0-9]/g, '')}`;
      if (defs.select(`#${clipId}`).empty()) {
        defs.append('clipPath').attr('id', clipId)
          .append('path').attr('d', generateBlobPath(r * 0.90, d.id));
      }

      const labelG = el.append('g').attr('class', 'bubble-label-group').attr('clip-path', `url(#${clipId})`);
      const textEl = labelG.append('text').attr('class', 'bubble-label');
      renderLabelTspans(textEl, d, r);

      el.append('title').text(d.display_title ? `${d.display_title}\n—\n${d.title}` : d.title);

      if (d.attachments && d.attachments.length > 0) {
        el.append('text').attr('class', 'bubble-attachment-icon')
          .attr('y', r * 0.55).attr('font-size', '10px').attr('text-anchor', 'middle')
          .text('📎');
      }

      syncStatusAnchor(el as any, d, r);

      const pos = queuePosMap?.get(d.id);
      if (pos) {
        el.append('circle').attr('class', 'queue-badge')
          .attr('cx', -r * 0.55).attr('cy', -r * 0.55).attr('r', 8)
          .attr('fill', 'rgba(124,185,232,0.85)').attr('stroke', '#1a1a2e').attr('stroke-width', 1);
        el.append('text').attr('class', 'queue-badge-text')
          .attr('x', -r * 0.55).attr('y', -r * 0.55 + 3)
          .attr('text-anchor', 'middle').attr('fill', '#fff')
          .attr('font-size', '8px').attr('font-weight', 'bold').text('#' + pos);
      }
    });
  }

  function updateBubbleVisuals(bubbleEl: any, d: any, queuePosMap: Map<string, number>) {
    const r = ideaRadius(d);
    const halo = bubbleHaloAxes(r);
    const cloud = bubbleEl.select('.bubble-cloud');
    applyBubbleVisualState(bubbleEl, d);

    cloud
      .interrupt('bubble-update')
      .transition('bubble-update')
      .duration(400)
      .attr('d', generateBlobPath(r, d.id))
      .attr('transform', 'scale(1)')
      .attr('stroke-dasharray', null as any)
      .attr('opacity', bubbleOpacity(d.status));
    bubbleEl.select('title').text(d.display_title ? `${d.display_title}\n—\n${d.title}` : d.title);

    if (PRIMITIVE_HANDOFF_HIT_LAYER_ONLY) return;

    bubbleEl.select('.bubble-halo')
      .attr('rx', halo.rx)
      .attr('ry', halo.ry);
    bubbleEl.select('.bubble-state-bloom')
      .attr('cx', -r * 0.06).attr('cy', -r * 0.08)
      .attr('rx', Math.max(18, r * 0.58)).attr('ry', Math.max(12, r * 0.44));
    bubbleEl.select('.bubble-presence-bloom-a')
      .attr('cx', -r * 0.16).attr('cy', -r * 0.18)
      .attr('rx', Math.max(18, r * 0.46)).attr('ry', Math.max(10, r * 0.26));
    bubbleEl.select('.bubble-presence-bloom-b')
      .attr('cx', r * 0.2).attr('cy', r * 0.16)
      .attr('rx', Math.max(14, r * 0.36)).attr('ry', Math.max(8, r * 0.2));

    const clipId = `clip-${d.id.replace(/[^a-zA-Z0-9]/g, '')}`;
    defs.select(`#${clipId}`).select('path').attr('d', generateBlobPath(r * 0.90, d.id));

    const label = bubbleEl.select('.bubble-label');
    label.selectAll('tspan').remove();
    renderLabelTspans(label, d, r);

    bubbleEl.select('title').text(d.display_title ? `${d.display_title}\n—\n${d.title}` : d.title);

    bubbleEl.selectAll('.bubble-attachment-icon').remove();
    if (d.attachments && d.attachments.length > 0) {
      bubbleEl.append('text').attr('class', 'bubble-attachment-icon')
        .attr('y', r * 0.55).attr('font-size', '10px').attr('text-anchor', 'middle')
        .text('📎');
    }

    syncStatusAnchor(bubbleEl, d, r);

    bubbleEl.selectAll('.queue-badge, .queue-badge-text').remove();
    const pos = queuePosMap.get(d.id);
    if (pos) {
      bubbleEl.append('circle').attr('class', 'queue-badge')
        .attr('cx', -r * 0.55).attr('cy', -r * 0.55).attr('r', 8)
        .attr('fill', 'rgba(124,185,232,0.85)').attr('stroke', '#1a1a2e').attr('stroke-width', 1);
      bubbleEl.append('text').attr('class', 'queue-badge-text')
        .attr('x', -r * 0.55).attr('y', -r * 0.55 + 3)
        .attr('text-anchor', 'middle').attr('fill', '#fff')
        .attr('font-size', '8px').attr('font-weight', 'bold').text('#' + pos);
    }
  }

  function syncBubblePulseScale(bubbleEl: any, scale: number) {
    bubbleEl.select('.bubble-cloud').attr('transform', `scale(${scale})`);
    bubbleEl.select('.bubble-label-group').attr('transform', `scale(${scale})`);
    bubbleEl.selectAll('.bubble-state-bloom, .bubble-presence-bloom, .bubble-attachment-icon, .queue-badge, .queue-badge-text')
      .attr('transform', `scale(${scale})`);
  }

  function sceneNodeById(id: string | null | undefined): OrbitNode | null {
    if (!id) return null;
    return orbitSceneNodesById.get(id) ?? null;
  }

  function renderedIdeaPosition(d: OrbitNode, now = performance.now()) {
    const isSelectedSource = cortex.panelOpen && d.id === cortex.selectedIdeaId;
    if (isSelectedSource && typeof d.x === 'number' && typeof d.y === 'number') {
      return { x: d.x, y: d.y };
    }
    const birthPosition = birthRenderPosition(d, now);
    if (birthPosition) {
      return birthPosition;
    }
    return {
      x: typeof d.x === 'number' ? d.x : d.position_x ?? coreX,
      y: typeof d.y === 'number' ? d.y : d.position_y ?? coreY,
    };
  }

  function sceneTransform(node: OrbitNode, now = performance.now()) {
    const { x, y } = renderedIdeaPosition(node, now);
    return `translate(${x},${y})`;
  }

  function mergeIdeaIntoSceneNode(node: OrbitNode, idea: any) {
    applyIdeaSnapshotToSceneNode(node, idea, {
      usePersistedCoordsWhenUnpositioned: orbitAnchors.length === 0,
    });
  }

  function createSceneNode(idea: any, totalIdeas: number): OrbitNode {
    const persistedCoords = orbitAnchors.length === 0
      ? orbitNodeCoords({
          x: idea.position_x ?? undefined,
          y: idea.position_y ?? undefined,
          position_x: idea.position_x,
          position_y: idea.position_y,
        })
      : null;
    const initialCoords = persistedCoords ?? resolveIdeaOrbitPoint(idea, totalIdeas);

    return createOrbitNodeFromIdea(idea, initialCoords);
  }

  function startBirthLifecycle(node: OrbitNode, totalIdeas: number) {
    const birthFrom = getSunPosition(node.user_id);
    const orbitPoint = orbitNodeCoords(node) ?? resolveIdeaOrbitPoint(node, totalIdeas);
    beginBirthLifecycle(node, birthFrom, orbitPoint);
  }

  function ensureSceneNode(idea: any, totalIdeas: number, options: { birth?: boolean } = {}) {
    let node = orbitSceneNodesById.get(idea.id);
    const isNew = !node;

    if (!node) {
      node = createSceneNode(idea, totalIdeas);
      orbitSceneNodesById.set(idea.id, node);
    } else {
      mergeIdeaIntoSceneNode(node, idea);
    }

    clearAttractionCache([node]);

    if (options.birth && isNew) {
      startBirthLifecycle(node, totalIdeas);
    }

    return node;
  }

  function syncSceneNodes(ideas: any[]) {
    const visibleIdeas = ideas.filter((idea: any) => !idea?.archived_at);
    applyClusterMetrics(visibleIdeas as any[]);
    const nextIds = new Set<string>();
    const nodes = visibleIdeas.map((idea: any) => {
      const node = ensureSceneNode(idea, visibleIdeas.length);
      nextIds.add(node.id);
      return node;
    });

    for (const id of Array.from(orbitSceneNodesById.keys())) {
      if (!nextIds.has(id)) {
        orbitSceneNodesById.delete(id);
      }
    }

    applyClusterMetrics(nodes as any[]);
    return nodes;
  }

  function updateSemanticZoomLevel(scale: number) {
    const nextLevel = semanticZoomLevelForScale(scale || 1);
    if (semanticZoomLevel !== nextLevel) {
      semanticZoomLevel = nextLevel;
    }
  }

  function semanticBlobText(blob: PrimitiveBlobVisual): string {
    if (semanticZoomLevel === 'detail') return blob.text;
    if (semanticZoomLevel === 'summary') return semanticSummaryText(blob.text, 3, 26);
    return semanticSummaryText(blob.text, 2, 18);
  }

  function primitiveBlobSemanticDimensions(blob: PrimitiveBlobVisual) {
    if (semanticZoomLevel === 'detail') {
      return { width: blob.width, height: blob.height };
    }
    if (semanticZoomLevel === 'summary') {
      return {
        width: Math.min(blob.width, 156),
        height: Math.min(blob.height, 104),
      };
    }
    if (semanticZoomLevel === 'symbol') {
      return { width: 92, height: 78 };
    }
    return { width: 68, height: 62 };
  }

  function primitiveBlobScreenScale() {
    const zoomScale = currentZoomTransform.k || 1;
    if (semanticZoomLevel === 'detail') return clamp(zoomScale, 0.58, 1.45);
    if (semanticZoomLevel === 'summary') return clamp(zoomScale, 0.76, 1.18);
    if (semanticZoomLevel === 'symbol') return clamp(zoomScale, 0.72, 0.92);
    return clamp(zoomScale, 0.74, 0.82);
  }

  function primitiveAppSemanticCounterScale() {
    if (semanticZoomLevel === 'glyph') return 2.35;
    if (semanticZoomLevel === 'symbol') return 1.75;
    if (semanticZoomLevel === 'summary') return 1.16;
    return 1;
  }

  function bubblePrimitiveVisual(d: OrbitNode): PrimitiveBlobVisual | null {
    const { x, y } = renderedIdeaPosition(d);
    if (!Number.isFinite(x) || !Number.isFinite(y)) return null;

    const text = d.display_title || d.title;
    const accent = ownerColor(d);
    const radius = ideaRadius(d);
    const titleLength = Math.min(30, String(text || '').length);
    const attachmentCount = Array.isArray(d.attachments) ? d.attachments.length : 0;
    const targetWidth = clamp(132 + Math.sqrt(Math.max(1, titleLength)) * 12 + (attachmentCount ? 8 : 0), 154, 216);
    const targetHeight = clamp(104 + (String(text || '').includes(' ') ? 18 : 10), 116, 152);

    return {
      id: d.id,
      text,
      accent,
      x,
      y,
      width: Math.round(targetWidth),
      height: Math.round(targetHeight),
      tone: primitiveTone(accent),
      shape: primitiveBlobShape(d.id),
      scale: primitiveBlobScale(radius),
      state: visualStatus(d.status) as ConstellationSignalState,
      cue: bubbleCue(d.status),
      presence: bubblePresence(d),
      treatment: primitiveBlobTreatment(d),
      icon: primitiveBlobIcon(d),
      attachmentCount,
    };
  }

  function astrePrimitiveVisual(a: Attractor): PrimitiveAstreVisual {
    return {
      id: a.id,
      letter: a.initial,
      owner: a.name,
      accent: a.color,
      x: a.x,
      y: a.y,
      size: Math.round(ATTRACTOR_CFG.coreRadius * 2.26),
      tone: primitiveTone(a.color),
      scale: 'hero',
      activity: attractorActivity(a.id),
      presence: attractorPresence(a.id),
      archivedCount: archivedDotsForUser(a.id),
    };
  }

  function pinPrimitiveVisual(anchor: Attractor): PrimitivePinVisual | null {
    if (anchor.kind !== 'pin' || !anchor.anchorId) return null;
    const sourcePin = pins.find((pin) => pin.id === anchor.anchorId);
    const localPosition = localPinPositions.get(anchor.anchorId);
    return {
      id: anchor.id,
      pinId: anchor.anchorId,
      label: anchor.name,
      accent: anchor.color,
      createdByUserId: sourcePin?.created_by_user_id ?? null,
      canEdit: Boolean(sourcePin?.created_by_user_id && sourcePin.created_by_user_id === auth.user?.id),
      x: localPosition?.x ?? anchor.x,
      y: localPosition?.y ?? anchor.y,
    };
  }

  function workspaceAppAnchorRef(app: WorkspaceAppRead): OrbitAnchorRef {
    const explicitAnchorType = String(app.visual_spec?.orbit_anchor_type ?? app.metadata?.orbit_anchor_type ?? '').trim();
    const explicitAnchorId = String(app.visual_spec?.orbit_anchor_id ?? app.metadata?.orbit_anchor_id ?? '').trim();
    const explicitKind = explicitAnchorType === 'pin' ? 'pin' : explicitAnchorType === 'user' ? 'user' : null;
    const explicitAnchorKey = orbitAnchorKey(explicitKind, explicitAnchorId);
    if (explicitKind && explicitAnchorId && explicitAnchorKey) {
      return { kind: explicitKind, id: explicitAnchorId, key: explicitAnchorKey };
    }

    const userId = app.anchor_user_id || app.created_by_user_id || auth.user?.id || attractors[0]?.id || 'cortex-core';
    return { kind: 'user', id: userId, key: orbitAnchorKey('user', userId) ?? userId };
  }

  function workspaceAppAnchorId(app: WorkspaceAppRead): string {
    return workspaceAppAnchorRef(app).key;
  }

  function workspaceAppAccent(app: WorkspaceAppRead, anchor?: Attractor | null): string {
    return normalizeHexColor(app.visual_spec?.accent)
      ?? normalizeHexColor(anchor?.color)
      ?? normalizeHexColor(auth.user?.color)
      ?? '#57CFA0';
  }

  function workspaceAppDefaultPosition(
    app: WorkspaceAppRead,
    index: number,
    count: number,
    anchor?: Attractor | null,
  ) {
    const centerX = anchor?.x ?? coreX;
    const centerY = anchor?.y ?? coreY;
    const seed = seededRandom(Math.abs(seedHash(`${app.id}:free-app-position`)) + 1)();
    const laneIndex = Math.floor(index / 6);
    const slotIndex = index % 6;
    const slotCount = Math.min(6, Math.max(1, count - laneIndex * 6));
    const angle = -Math.PI / 2 + (slotIndex / slotCount) * Math.PI * 2 + seed * 0.34;
    const radius = WORKSPACE_APP_DEFAULT_RADIUS + laneIndex * 112 + seed * 22;
    return {
      x: centerX + Math.cos(angle) * radius,
      y: centerY + Math.sin(angle) * radius,
    };
  }

  function workspaceAppPrimitiveVisual(
    app: WorkspaceAppRead,
    index: number,
    count: number,
    anchor?: Attractor | null,
  ): PrimitiveAppVisual {
    const anchorRef = workspaceAppAnchorRef(app);
    const localPosition = localAppPositions.get(app.id);
    const storedPosition = workspaceAppStoredPosition(app);
    const defaultPosition = workspaceAppDefaultPosition(app, index, count, anchor);
    const x = localPosition?.x ?? storedPosition?.x ?? defaultPosition.x;
    const y = localPosition?.y ?? storedPosition?.y ?? defaultPosition.y;
    const active = activeAppId === app.id;
    const floatSeed = seededRandom(Math.abs(seedHash(`${app.id}:float`)) + 1);
    const floatAngle = floatSeed() * Math.PI * 2;
    const floatAmplitude = 3.4 + floatSeed() * 2.4;

    let opacity = active || primitiveAppDragState?.appId === app.id ? 1 : 0.94;
    if (cortex.panelOpen) opacity = active ? 0.8 : 0.34;
    if (flowState) opacity = Math.min(opacity, active ? 0.86 : 0.52);

    return {
      id: app.id,
      name: app.name,
      description: app.description,
      rendererKey: app.renderer_key,
      visualSpec: app.visual_spec || {},
      stateKey: String(app.active_version?.manifest?.state_key || app.visual_spec?.state_key || 'default'),
      accent: workspaceAppAccent(app, anchor),
      anchorKey: anchorRef.key,
      anchorType: anchorRef.kind,
      anchorId: anchorRef.id,
      x,
      y,
      opacity,
      active,
      floatX: Math.cos(floatAngle) * floatAmplitude,
      floatY: Math.sin(floatAngle) * floatAmplitude,
      floatDuration: 13.8 + floatSeed() * 3.6,
      floatDelay: -floatSeed() * 14,
    };
  }

  function workspaceAppPrimitiveVisuals(): PrimitiveAppVisual[] {
    const visibleApps = apps.filter((app) => !app.archived_at);
    if (visibleApps.length <= 0) return [];

    const appGroups = new Map<string, WorkspaceAppRead[]>();
    for (const app of visibleApps) {
      const anchorId = workspaceAppAnchorId(app);
      appGroups.set(anchorId, [...(appGroups.get(anchorId) ?? []), app]);
    }

    const visuals: PrimitiveAppVisual[] = [];
    for (const [anchorId, anchorApps] of appGroups) {
      const anchor = orbitAnchorLookup.byId.get(anchorId)
        ?? orbitAnchorLookup.byAnchorKey.get(anchorId)
        ?? attractorLookup.byId.get(anchorId)
        ?? (auth.user?.id ? attractorLookup.byId.get(auth.user.id) : undefined)
        ?? attractors[0]
        ?? null;
      const sorted = [...anchorApps].sort((a, b) => {
        const orderA = workspaceAppOrbitOrder(a);
        const orderB = workspaceAppOrbitOrder(b);
        if (orderA !== null || orderB !== null) {
          return (orderA ?? Number.MAX_SAFE_INTEGER) - (orderB ?? Number.MAX_SAFE_INTEGER)
            || a.created_at.localeCompare(b.created_at)
            || a.id.localeCompare(b.id);
        }
        return a.created_at.localeCompare(b.created_at) || a.id.localeCompare(b.id);
      });
      sorted.forEach((app, index) => {
        visuals.push(workspaceAppPrimitiveVisual(app, index, sorted.length, anchor));
      });
    }
    return visuals;
  }

  function repelNodesFromWorkspaceApps(nodes: OrbitNode[], alpha: number) {
    const obstacles = primitiveAppCollisionObstacles;
    if (obstacles.length <= 0) return;

    const strength = 0.032 + alpha * 0.085;
    for (const node of nodes) {
      if (!Number.isFinite(node.x) || !Number.isFinite(node.y)) continue;
      const nodeRadius = bubbleCollisionRadius(node);

      for (const app of obstacles) {
        const dx = node.x - app.x;
        const dy = node.y - app.y;
        const distance = Math.hypot(dx, dy) || 1;
        const minDistance = nodeRadius + WORKSPACE_APP_COLLISION_RADIUS + WORKSPACE_APP_COLLISION_GAP;
        const overlap = minDistance - distance;
        if (overlap <= 0) continue;

        const fallbackAngle = seededRandom(Math.abs(seedHash(`${node.id}:${app.id}:app-collision`)) + 1)() * Math.PI * 2;
        const ux = distance > 1 ? dx / distance : Math.cos(fallbackAngle);
        const uy = distance > 1 ? dy / distance : Math.sin(fallbackAngle);
        node.vx = (node.vx ?? 0) + ux * overlap * strength;
        node.vy = (node.vy ?? 0) + uy * overlap * strength;
      }
    }
  }

  function activeIdeasForAttractor(a: Attractor) {
    return cortex.ideas.filter((idea: any) => !idea?.archived_at && itemOrbitAnchorKey(idea) === a.id);
  }

  function orbitLaneCountForAttractor(ideas: any[]) {
    if (ideas.length <= 0) return 1;

    const threadLoad = ideas.reduce((sum, idea: any) => {
      const rawThreadCount = Number(idea.thread_count ?? 0);
      const normalizedThreadCount = Number.isFinite(rawThreadCount) ? rawThreadCount : 0;
      return sum + Math.max(1, normalizedThreadCount);
    }, 0);
    const workingCount = ideas.filter((idea: any) => visualStatus(idea.status) === 'working').length;
    const densityScore =
      ideas.length
      + Math.max(0, threadLoad - ideas.length) * 0.3
      + workingCount * 0.8;

    if (densityScore <= 2.4) return 2;
    if (densityScore <= 8.75) return 3;
    return 4;
  }

  function orbitLaneRadiiForAttractor(a: Attractor): PrimitiveOrbitLaneRing[] {
    const authoredIdeas = activeIdeasForAttractor(a);
    const authoredCount = authoredIdeas.length;
    const isPin = a.kind === 'pin';
    const baseRingCount = orbitLaneCountForAttractor(authoredIdeas);
    const pinRingLimit = authoredCount > 8 ? 3 : 2;
    const ringCount = Math.max(1, isPin ? Math.min(baseRingCount, pinRingLimit) : baseRingCount);
    const isSolo = orbitAnchors.length <= 1;
    const isCurrent = a.id === auth.user?.id;
    let radii: number[];
    if (isPin) {
      radii = [88, 136, 188];
    } else if (isSolo) {
      radii = [132, 228, 318, 408];
    } else if (isCurrent) {
      radii = [116, 206, 292, 374];
    } else {
      radii = [108, 190, 270, 348];
    }
    const densityBoost = Math.min(Math.max(0, authoredCount - 3) * (isPin ? 2.8 : 7), isPin ? 10 : 22);

    return radii.slice(0, ringCount).map((radius, index) => ({
      id: `${a.id}-ring-${index}`,
      rx: radius + (index === ringCount - 1 ? densityBoost : densityBoost * 0.42),
      ry: radius + (index === ringCount - 1 ? densityBoost : densityBoost * 0.42),
      opacity: isPin
        ? Math.max(0.08, 0.18 - index * 0.045)
        : Math.max(0.1, 0.34 - index * 0.065),
      role: 'thread' as const,
    }));
  }

  function orbitLaneGuideSpokesForAttractor(a: Attractor, rings: PrimitiveOrbitLaneRing[]): PrimitiveOrbitLaneSpoke[] {
    if (rings.length <= 0) return [];

    const normalizeAngle = (angle: number) => ((angle % 360) + 360) % 360;

    const shuffleCompassPairs = <T,>(items: T[], rng: () => number) => {
      const shuffled = [...items];
      for (let index = shuffled.length - 1; index > 0; index -= 1) {
        const swapIndex = Math.floor(rng() * (index + 1));
        [shuffled[index], shuffled[swapIndex]] = [shuffled[swapIndex], shuffled[index]];
      }
      return shuffled;
    };

    const rng = seededRandom(Math.abs(seedHash(`${a.id}:orbit-guides`)) + 1);
    const mapRotation = (rng() - 0.5) * 7;
    const compassPairs = [
      { id: 'ew', angles: [0, 180] },
      { id: 'ns', angles: [270, 90] },
      { id: 'ne-sw', angles: [315, 135] },
      { id: 'nw-se', angles: [225, 45] },
    ];

    const isPin = a.kind === 'pin';
    const boundaries = [isPin ? 54 : ATTRACTOR_CFG.coreRadius + 10, ...rings.map((ring) => ring.rx)];
    const spokes: PrimitiveOrbitLaneSpoke[] = [];

    for (let index = 0; index < rings.length; index += 1) {
      const stepRng = seededRandom(Math.abs(seedHash(`${a.id}:orbit-guide-step:${index}`)) + 1);
      const fromRadius = boundaries[index] + (index === 0 ? 0 : 7);
      const toRadius = boundaries[index + 1] - 7;
      if (toRadius - fromRadius < 18) continue;

      const anchorPairId = index % 2 === 0 ? 'ns' : 'ew';
      const anchorPair = compassPairs.find((pair) => pair.id === anchorPairId)!;
      const remainingPairs = compassPairs.filter((pair) => pair.id !== anchorPairId);
      const shuffledPairs = shuffleCompassPairs(remainingPairs, stepRng);
      const pairCount = isPin
        ? (index === 0 ? 1 : (stepRng() > 0.72 ? 2 : 1))
        : clamp(
            (index <= 1 ? 3 : 2) + (stepRng() > 0.54 ? 1 : 0),
            index <= 1 ? 3 : 2,
            compassPairs.length,
          );
      const activePairs = [anchorPair, ...shuffledPairs].slice(0, pairCount);

      activePairs.forEach((pair, pairIndex) => {
        pair.angles.forEach((angle, angleIndex) => {
          spokes.push({
            id: `${a.id}-guide-${index}-${pair.id}-${angleIndex}`,
            angle: normalizeAngle(angle + mapRotation),
            fromRadius,
            toRadius,
            opacity: isPin
              ? Math.max(0.08, 0.16 - index * 0.026 - pairIndex * 0.014)
              : Math.max(0.17, 0.34 - index * 0.032 - pairIndex * 0.012),
          });
        });
      });
    }

    return spokes;
  }

  function orbitLaneGuideDotsForAttractor(
    a: Attractor,
    rings: PrimitiveOrbitLaneRing[],
    spokes: PrimitiveOrbitLaneSpoke[],
  ): PrimitiveOrbitLaneDot[] {
    const dots: PrimitiveOrbitLaneDot[] = [];
    const seenDots = new Map<string, PrimitiveOrbitLaneDot>();
    const isPin = a.kind === 'pin';

    for (const spoke of spokes) {
      const angle = (spoke.angle * Math.PI) / 180;
      const cos = Math.cos(angle);
      const sin = Math.sin(angle);

      rings.forEach((ring, ringIndex) => {
        const isStart = Math.abs(ring.rx - spoke.fromRadius) < 10;
        const isEnd = Math.abs(ring.rx - spoke.toRadius) < 10;
        if (!isStart && !isEnd) return;

        const angleKey = Math.round(spoke.angle * 10);
        const key = `${ringIndex}-${angleKey}`;
        const dotRoll = seededRandom(Math.abs(seedHash(`${a.id}:orbit-dot:${key}`)) + 1)();
        const keepChance = isPin
          ? clamp(0.34 - ringIndex * 0.09, 0.14, 0.34)
          : clamp(0.64 - ringIndex * 0.08, 0.38, 0.64);
        if (dotRoll > keepChance) return;

        const intensityRoll = seededRandom(Math.abs(seedHash(`${a.id}:orbit-dot-intensity:${key}`)) + 1)();
        const isBrightMarker = intensityRoll > 0.68;
        const size = isPin
          ? (isEnd ? 1.9 : 1.6) + intensityRoll * 0.38
          : (isEnd ? 2.7 : 2.3) + intensityRoll * 0.7;
        const opacity = isPin
          ? 0.34 + intensityRoll * 0.18
          : (isBrightMarker ? 0.94 : 0.68 + intensityRoll * 0.16);
        const dot = {
          id: `${a.id}-guide-dot-${spoke.id}-${ringIndex}-${isStart ? 'start' : 'end'}`,
          x: cos * ring.rx,
          y: sin * ring.ry,
          size,
          opacity,
          state: 'idle',
          cue: 'none',
        } satisfies PrimitiveOrbitLaneDot;

        const existing = seenDots.get(key);
        if (existing) {
          existing.size = Math.max(existing.size, dot.size);
          existing.opacity = Math.max(existing.opacity, dot.opacity);
          return;
        }

        seenDots.set(key, dot);
        dots.push(dot);
      });
    }

    return dots;
  }

  function orbitLanePrimitiveVisual(a: Attractor): PrimitiveOrbitLaneVisual {
    const rings = orbitLaneRadiiForAttractor(a);
    const outer = rings[rings.length - 1] ?? { rx: 220, ry: 180 };
    const spokes = orbitLaneGuideSpokesForAttractor(a, rings);
    const dots = orbitLaneGuideDotsForAttractor(a, rings, spokes);

    return {
      id: a.id,
      kind: a.kind === 'pin' ? 'pin' : 'user',
      accent: a.color,
      x: a.x,
      y: a.y,
      outerRx: outer.rx,
      outerRy: outer.ry,
      rings,
      spokes,
      dots,
    };
  }

  function fallbackCorePrimitiveVisual(): PrimitiveAstreVisual {
    return {
      id: 'cortex-core',
      letter: 'C',
      owner: 'Cortex',
      accent: '#f0f0fa',
      x: coreX,
      y: coreY,
      size: Math.round(CORE_RADIUS * 2.18),
      tone: 'spectral',
      scale: 'hero',
      activity: 'idle',
      presence: 'online',
      archivedCount: 3,
    };
  }

  function primitiveBlobWorldPosition(blob: PrimitiveBlobVisual) {
    const node = orbitSceneNodesById.get(blob.id);
    if (node) return renderedIdeaPosition(node);
    return { x: blob.x, y: blob.y };
  }

  function primitiveBubbleOpacity(blob: PrimitiveBlobVisual) {
    const isSelectedSource = cortex.panelOpen && blob.id === cortex.selectedIdeaId;
    if (isSelectedSource) return 0.36;

    let opacity = blob.state === 'working' ? 0.95 : 1;

    if (flowState) {
      opacity = Math.min(opacity, 0.38);
    }

    return opacity;
  }

  function primitiveAstreIsLit(astre: PrimitiveAstreVisual) {
    return astre.id === hoveredAstreId || astre.id === dragTargetAnchorId;
  }

  function primitiveAstreClass(astre: PrimitiveAstreVisual) {
    return [
      astre.id === auth.user?.id ? 'constellation-astre-own' : '',
      primitiveAstreIsLit(astre) ? 'constellation-astre-emphasis' : '',
      astre.id === dragTargetAnchorId ? 'constellation-astre-drop-target' : '',
    ]
      .filter(Boolean)
      .join(' ');
  }

  function primitiveBubbleScreenStyle(blob: PrimitiveBlobVisual) {
    const isLightMode = theme.mode === 'light';
    const tone = toneVars(blob.accent, isLightMode ? 'light' : 'dark');
    const motion = primitiveBubbleMotion(blob);
    const isHovered = hoveredIdeaId === blob.id;
    const dimensions = primitiveBlobSemanticDimensions(blob);
    const lightSurfaceShadow = isLightMode
      ? [
          'inset 0 0 0 1px rgba(255, 255, 255, 0.74)',
          '0 8px 15px rgba(26, 39, 49, 0.032)',
          `0 0 10px color-mix(in srgb, ${tone.accent} 5%, transparent)`,
        ].join(', ')
      : null;
    const lightWorkingSurfaceShadow = isLightMode
      ? [
          'inset 0 0 0 1px rgba(255, 255, 255, 0.74)',
          '0 9px 16px rgba(26, 39, 49, 0.038)',
          `0 0 14px color-mix(in srgb, ${tone.accent} 8%, transparent)`,
          `0 0 24px color-mix(in srgb, ${tone.accent} 3%, transparent)`,
        ].join(', ')
      : null;
    const lightDoneSurfaceShadow = isLightMode
      ? [
          'inset 0 0 0 1px rgba(255, 255, 255, 0.72)',
          '0 8px 15px rgba(26, 39, 49, 0.03)',
          `0 0 10px color-mix(in srgb, ${tone.accent} 4%, transparent)`,
        ].join(', ')
      : null;
    const lightHoverSurfaceShadow = isLightMode
      ? [
          'inset 0 0 0 1px rgba(255, 255, 255, 0.76)',
          '0 10px 18px rgba(26, 39, 49, 0.04)',
          `0 0 16px color-mix(in srgb, ${tone.accent} 9%, transparent)`,
          `0 0 30px color-mix(in srgb, ${tone.accent} 4%, transparent)`,
        ].join(', ')
      : null;

    return primitiveStyle({
      left: '0px',
      top: '0px',
      width: `${dimensions.width}px`,
      height: `${dimensions.height}px`,
      opacity: motion.opacity,
      transform: motion.transform,
      transition: 'width 190ms var(--constellation-motion-ease-lift), height 190ms var(--constellation-motion-ease-lift), opacity 160ms ease, filter 160ms ease',
      'z-index': isHovered ? 30 : 20,
      'pointer-events': 'auto',
      filter: motion.filter,
      '--constellation-signal-blob-surface-background': isLightMode
        ? `color-mix(in srgb, #fffdf7 95%, ${tone.accent} 5%)`
        : null,
      '--constellation-signal-blob-surface-border': isLightMode
        ? `color-mix(in srgb, ${tone.accent} 16%, rgba(255, 253, 247, 0.76))`
        : null,
      '--constellation-signal-blob-surface-shadow': lightSurfaceShadow,
      '--constellation-signal-blob-working-surface-shadow': lightWorkingSurfaceShadow,
      '--constellation-signal-blob-done-surface-shadow': lightDoneSurfaceShadow,
      '--constellation-signal-blob-presence-surface-shadow': lightSurfaceShadow,
      '--constellation-signal-blob-contour-treatment-shadow': lightSurfaceShadow,
      '--constellation-signal-blob-hover-surface-border': isLightMode
        ? `color-mix(in srgb, ${tone.accent} 28%, rgba(255, 253, 247, 0.72))`
        : null,
      '--constellation-signal-blob-hover-surface-shadow': lightHoverSurfaceShadow,
      '--blob-core': tone.core,
      '--blob-owner': tone.owner,
      '--blob-seed': tone.accent,
      '--blob-rim': isLightMode
        ? `color-mix(in srgb, ${tone.accent} 34%, rgba(255, 253, 250, 0.58))`
        : `color-mix(in srgb, ${tone.accent} 68%, rgba(240, 250, 248, 0.22))`,
      '--blob-rim-hot': isLightMode
        ? `color-mix(in srgb, ${tone.accent} 48%, #fffaf4 52%)`
        : `color-mix(in srgb, ${tone.accent} 78%, #eafff7 22%)`,
      '--blob-rim-soft': isLightMode
        ? `color-mix(in srgb, ${tone.accent} 22%, rgba(255, 250, 246, 0.64))`
        : `color-mix(in srgb, ${tone.accent} 58%, rgba(240, 250, 248, 0.22))`,
      '--blob-bloom': isLightMode
        ? withAlpha(tone.accent, 0.08)
        : `color-mix(in srgb, ${tone.accent} 30%, transparent)`,
      '--blob-shadow': isLightMode
        ? withAlpha(tone.accent, 0.12)
        : `color-mix(in srgb, ${tone.accent} 42%, transparent)`,
    });
  }

  function primitiveBubbleMotion(blob: PrimitiveBlobVisual) {
    const isLightMode = theme.mode === 'light';
    const position = primitiveBlobWorldPosition(blob);
    const [screenX, screenY] = currentZoomTransform.apply([position.x, position.y]);
    const isSelectedSource = cortex.panelOpen && blob.id === cortex.selectedIdeaId;
    const isHovered = hoveredIdeaId === blob.id;
    const isArchiveDrop = archiveDropIdeaId === blob.id;
    const archiveProgress = archivingIdeaId === blob.id ? archivingIdeaProgress : 0;
    const zoomScale = primitiveBlobScreenScale();
    const hoverScale = isHovered
      ? semanticZoomLevel === 'detail'
        ? 1.075
        : 1.12
      : 1;
    const archiveScale = isArchiveDrop ? 0.76 : Math.max(0.14, 1 - archiveProgress * 0.86);
    const screenScale = zoomScale * hoverScale * archiveScale;
    const filter = isSelectedSource
      ? isLightMode
        ? 'saturate(0.9) brightness(0.92)'
        : 'saturate(1.02) brightness(0.98)'
      : isArchiveDrop || archiveProgress > 0
        ? isLightMode
          ? 'saturate(0.82) brightness(0.96)'
          : 'saturate(0.9) brightness(0.9) drop-shadow(0 0 18px color-mix(in srgb, var(--blob-shadow) 48%, transparent))'
      : isHovered
        ? isLightMode
          ? 'brightness(1.035) drop-shadow(0 0 16px color-mix(in srgb, var(--blob-seed) 10%, transparent))'
          : 'brightness(1.22) saturate(1.28) drop-shadow(0 0 24px color-mix(in srgb, var(--blob-shadow) 72%, transparent))'
        : isLightMode
          ? ''
          : 'brightness(1.08) saturate(1.18) drop-shadow(0 0 14px color-mix(in srgb, var(--blob-shadow) 44%, transparent))';

    return {
      opacity: primitiveBubbleOpacity(blob) * (isArchiveDrop ? 0.58 : 1) * Math.max(0, 1 - archiveProgress),
      transform: `translate3d(${screenX}px, ${screenY}px, 0) translate(-50%, -50%) scale(${screenScale})`,
      filter,
      zIndex: isArchiveDrop || archiveProgress > 0 ? '32' : isHovered ? '30' : '20',
    };
  }

  function primitiveBlobElementMap() {
    const root = containerEl?.parentElement;
    const elements = new Map<string, HTMLElement>();
    if (!root) return elements;

    root.querySelectorAll<HTMLElement>('[data-constellation-signal-id]').forEach((element) => {
      const id = element.dataset.constellationSignalId;
      if (id) elements.set(id, element);
    });
    return elements;
  }

  function applyPrimitiveBubbleMotion(blob: PrimitiveBlobVisual, element: HTMLElement | undefined) {
    if (!element) return;
    const motion = primitiveBubbleMotion(blob);
    element.style.transform = motion.transform;
    element.style.opacity = String(motion.opacity);
    element.style.filter = motion.filter;
    element.style.zIndex = motion.zIndex;
  }

  function syncPrimitiveMotionVisuals(nodes: OrbitNode[] = Array.from(orbitSceneNodesById.values())) {
    const elements = primitiveBlobElementMap();
    if (elements.size <= 0) return;

    for (const node of nodes) {
      const blob = bubblePrimitiveVisual(node);
      if (!blob) continue;
      applyPrimitiveBubbleMotion(blob, elements.get(blob.id));
    }
  }

  function primitiveAstreStyle(astre: PrimitiveAstreVisual) {
    const isLit = primitiveAstreIsLit(astre);
    return buildAstrePrimitiveStyle({
      id: astre.id,
      accent: astre.accent,
      mode: theme.mode,
      activity: astre.activity,
      x: astre.x,
      y: astre.y,
      size: astre.size,
      opacity: 1,
      zIndex: isLit ? 5 : 2,
      emphasis: isLit,
    });
  }

  function primitivePinIsLit(pin: PrimitivePinVisual) {
    return (
      pin.id === dragTargetAnchorId
      || pin.id === hoveredPinId
      || primitivePinDragState?.pinId === pin.pinId
      || archiveDropPinId === pin.pinId
      || archivingPinId === pin.pinId
    );
  }

  function primitivePinStyle(pin: PrimitivePinVisual) {
    const isArchiveDrop = archiveDropPinId === pin.pinId;
    const archiveProgress = archivingPinId === pin.pinId ? archivingPinProgress : 0;
    const archiveScale = isArchiveDrop ? 0.76 : Math.max(0.14, 1 - archiveProgress * 0.86);
    const archiveOpacity = isArchiveDrop ? 0.58 : Math.max(0, 1 - archiveProgress);
    const isArchiving = isArchiveDrop || archiveProgress > 0;
    return primitiveStyle({
      left: `${pin.x}px`,
      top: `${pin.y}px`,
      '--workspace-pin-accent': pin.accent,
      opacity: (primitivePinIsLit(pin) ? 1 : cortex.panelOpen ? 0.58 : 0.92) * archiveOpacity,
      'z-index': isArchiving ? 32 : primitivePinIsLit(pin) ? 8 : 3,
      transform: isArchiving ? `translate(-50%, -50%) scale(${archiveScale})` : null,
      filter: isArchiving
        ? 'saturate(0.82) brightness(0.92) drop-shadow(0 0 18px color-mix(in srgb, var(--workspace-pin-accent) 42%, transparent))'
        : null,
    });
  }

  function activatePrimitivePin(pin: PrimitivePinVisual, event: MouseEvent) {
    event.stopPropagation();
    if (!pin.canEdit) return;
    onpinmenu?.({ x: event.clientX, y: event.clientY, pinId: pin.pinId });
  }

  function primitiveAppIsLit(app: PrimitiveAppVisual) {
    return app.active || primitiveAppDragState?.appId === app.id;
  }

  function primitiveAppObjectStyle(app: PrimitiveAppVisual) {
    const isLit = primitiveAppIsLit(app);
    const isArchiveDrop = archiveDropAppId === app.id;
    const archiveProgress = archivingAppId === app.id ? archivingAppProgress : 0;
    const archiveScale = isArchiveDrop ? 0.76 : Math.max(0.14, 1 - archiveProgress * 0.86);
    const archiveOpacity = isArchiveDrop ? 0.58 : Math.max(0, 1 - archiveProgress);
    return primitiveStyle({
      left: `${app.x}px`,
      top: `${app.y}px`,
      '--workspace-app-accent': app.accent,
      '--workspace-app-opacity': app.opacity * archiveOpacity,
      '--workspace-app-z': isLit ? 11 : 4,
      '--workspace-app-counter-scale': primitiveAppSemanticCounterScale(),
      '--workspace-app-state-scale': archiveScale,
      '--workspace-app-float-x': `${app.floatX.toFixed(2)}px`,
      '--workspace-app-float-y': `${app.floatY.toFixed(2)}px`,
      '--workspace-app-float-duration': `${app.floatDuration.toFixed(2)}s`,
      '--workspace-app-float-delay': `${app.floatDelay.toFixed(2)}s`,
      filter: isArchiveDrop || archiveProgress > 0
        ? 'saturate(0.85) brightness(0.92) drop-shadow(0 0 18px color-mix(in srgb, var(--workspace-app-accent) 38%, transparent))'
        : null,
    });
  }

  function activatePrimitiveApp(app: PrimitiveAppVisual, event: MouseEvent) {
    event.stopPropagation();
    const rect = containerEl?.getBoundingClientRect();
    const [localX, localY] = currentZoomTransform.apply([app.x, app.y]);
    onappopen?.({
      x: (rect?.left ?? 0) + localX,
      y: (rect?.top ?? 0) + localY,
      appId: app.id,
    });
  }

  function activatePrimitiveBlob(blob: PrimitiveBlobVisual) {
    if (primitiveDragState?.moved) return;
    if (isPreviewIdeaId(blob.id)) return;
    const node = orbitSceneNodesById.get(blob.id);
    const position = node ? renderedIdeaPosition(node) : { x: blob.x, y: blob.y };
    const rect = containerEl?.getBoundingClientRect();
    const [localX, localY] = currentZoomTransform.apply([position.x, position.y]);
    onthreadopen?.({
      x: (rect?.left ?? 0) + localX,
      y: (rect?.top ?? 0) + localY,
      id: blob.id,
    });
    cortex.selectIdea(blob.id);
  }

  function refreshShadowBubbleTitle(node: OrbitNode) {
    if (!USE_D3_SHADOW_SCENE || !g) return;
    const r = dynamicRadius(node.salience_score, node.display_title || node.title, node.thread_count);
    const lines = wrapTextForBubble(node.display_title || node.title, r);
    const fs = fontSize(r);
    const lineHeight = fs * 1.25;
    const totalHeight = lines.length * lineHeight;
    const startY = -totalHeight / 2 + lineHeight * 0.7;
    const label = g.selectAll('.bubble-group').filter((d2: any) => d2.id === node.id).select('.bubble-label');
    if (label.empty()) return;
    label.selectAll('tspan').remove();
    label.attr('font-size', fs);
    lines.forEach((line, index) => {
      label.append('tspan').attr('x', 0).attr('dy', index === 0 ? startY : lineHeight).text(line);
    });
  }

  function openTitleEditor(node: OrbitNode) {
    if (!containerEl || isPreviewIdeaId(node?.id)) return;
    const position = renderedIdeaPosition(node);
    const [localX, localY] = currentZoomTransform.apply([position.x, position.y]);

    const overlay = document.createElement('div');
    overlay.style.cssText = `position:absolute;left:${localX - 100}px;top:${localY - 16}px;z-index:200;`;
    overlay.addEventListener('click', (event) => event.stopPropagation());

    const input = document.createElement('input');
    input.type = 'text';
    input.value = node.display_title || node.title;
    input.style.cssText = 'width:200px;padding:4px 8px;font-size:13px;border-radius:6px;border:1px solid rgba(255,255,255,0.1);background:rgba(11,14,22,0.9);color:rgba(255,255,255,0.9);outline:none;';
    overlay.appendChild(input);
    containerEl.appendChild(overlay);
    input.focus();
    input.select();

    const cleanup = () => {
      if (overlay.parentNode) overlay.remove();
    };
    const save = async () => {
      const nextTitle = input.value.trim();
      if (nextTitle && nextTitle !== (node.display_title || node.title)) {
        node.title = nextTitle;
        node.display_title = nextTitle;
        refreshShadowBubbleTitle(node);
        syncPrimitiveOrbitVisuals(simulation?.nodes() as OrbitNode[] ?? Array.from(orbitSceneNodesById.values()));
        cortex.updateIdeaTitle(node.id, nextTitle);
      }
      cleanup();
    };
    input.addEventListener('keydown', (event) => {
      if (event.key === 'Enter') {
        event.preventDefault();
        save();
      } else if (event.key === 'Escape') {
        cleanup();
      }
    });
    input.addEventListener('blur', save);
  }

  function editPrimitiveBlob(blob: PrimitiveBlobVisual) {
    const node = orbitSceneNodesById.get(blob.id);
    if (!node) return;
    openTitleEditor(node);
  }

  function popPrimitiveBlob(id: string) {
    const node = orbitSceneNodesById.get(id);
    if (!node || isPreviewIdeaId(node.id)) return;
    const nodeId = node.id;

    hidePeek();

    if (g) {
      const color = ownerColor(node);
      const particleG = g.append('g').attr('transform', `translate(${node.x},${node.y})`);
      for (let index = 0; index < 10; index += 1) {
        const angle = (index / 10) * Math.PI * 2 + (Math.random() - 0.5) * 0.45;
        const speed = 48 + Math.random() * 72;
        const particle = particleG.append('circle')
          .attr('r', 2.8 + Math.random() * 2.6)
          .attr('fill', color)
          .attr('opacity', 0.78);
        particle.transition().duration(680).ease(d3.easeCubicOut)
          .attr('cx', Math.cos(angle) * speed)
          .attr('cy', Math.sin(angle) * speed)
          .attr('opacity', 0)
          .attr('r', 0)
          .on('end', () => particle.remove());
      }
      setTimeout(() => particleG.remove(), 760);
    }

    const startedAt = performance.now();
    const duration = 220;
    archivingIdeaId = nodeId;
    archivingIdeaProgress = 0;

    function tick(now: number) {
      const progress = Math.min((now - startedAt) / duration, 1);
      archivingIdeaProgress = d3.easeCubicOut(progress);
      syncPrimitiveDragVisuals();
      if (progress < 1) {
        requestAnimationFrame(tick);
        return;
      }
      archivingIdeaId = null;
      archivingIdeaProgress = 0;
      cortex.deleteIdea(nodeId);
    }

    requestAnimationFrame(tick);
  }

  type PrimitiveDragState = {
    id: string;
    node: OrbitNode;
    startClientX: number;
    startClientY: number;
    moved: boolean;
  };

  type PrimitivePinDragState = {
    pinId: string;
    startClientX: number;
    startClientY: number;
    previousX: number;
    previousY: number;
    x: number;
    y: number;
    moved: boolean;
  };

  type PrimitiveAppDragState = {
    appId: string;
    startClientX: number;
    startClientY: number;
    previousX: number;
    previousY: number;
    moved: boolean;
  };

  function primitivePointerWorldPoint(event: PointerEvent) {
    const rect = containerEl?.getBoundingClientRect();
    const localX = event.clientX - (rect?.left ?? 0);
    const localY = event.clientY - (rect?.top ?? 0);
    const [x, y] = currentZoomTransform.invert([localX, localY]);
    return { x, y };
  }

  function workspacePointFromClient(clientX: number, clientY: number): CortexWorkspacePoint | null {
    const rect = containerEl?.getBoundingClientRect();
    if (!rect) return null;
    return workspacePointFromClientRect(rect, currentZoomTransform, clientX, clientY);
  }

  function handleWorkspaceContextMenu(event: MouseEvent) {
    event.preventDefault();
    const point = workspacePointFromClient(event.clientX, event.clientY);
    if (!point) return;
    onworkspacecontextmenu?.(point);
  }

  function syncPrimitiveDragVisuals() {
    cancelPrimitiveOrbitVisualSync();
    primitiveSyncLastAt = performance.now();
    const nodes = simulation?.nodes() as OrbitNode[] ?? Array.from(orbitSceneNodesById.values());
    syncPrimitiveMotionVisuals(nodes);
  }

  function beginPrimitiveBlobDrag(id: string, event: PointerEvent) {
    if (event.button !== 0 || isPreviewIdeaId(id)) return false;
    const node = orbitSceneNodesById.get(id);
    if (!node) return false;

    event.stopPropagation();
    primitiveDragState = {
      id,
      node,
      startClientX: event.clientX,
      startClientY: event.clientY,
      moved: false,
    };
    dragStart({ active: false }, node);
    highlightConns(id);
    hidePeek();
    return true;
  }

  function movePrimitiveBlobDrag(id: string, event: PointerEvent) {
    const state = primitiveDragState;
    if (!state || state.id !== id) return false;

    const dx = event.clientX - state.startClientX;
    const dy = event.clientY - state.startClientY;
    if (!state.moved && Math.sqrt(dx * dx + dy * dy) < 4) return false;

    state.moved = true;
    event.preventDefault();
    event.stopPropagation();

    const point = primitivePointerWorldPoint(event);
    state.node.x = point.x;
    state.node.y = point.y;
    dragging({ active: false, x: point.x, y: point.y, clientX: event.clientX, clientY: event.clientY }, state.node);
    syncPrimitiveDragVisuals();
    if (simulation) simulation.alpha(0.18).restart();
    return true;
  }

  function endPrimitiveBlobDrag(id: string, event: PointerEvent) {
    const state = primitiveDragState;
    if (!state || state.id !== id) return false;

    if (state.moved) {
      const point = primitivePointerWorldPoint(event);
      state.node.x = point.x;
      state.node.y = point.y;
      dragging({ active: false, x: point.x, y: point.y, clientX: event.clientX, clientY: event.clientY }, state.node);
      event.preventDefault();
      event.stopPropagation();
    }

    dragEnd({ active: false, clientX: event.clientX, clientY: event.clientY }, state.node);
    const didMove = state.moved;
    primitiveDragState = null;
    unhighlightConns();
    syncPrimitiveDragVisuals();
    return didMove;
  }

  function beginPrimitivePinDrag(pin: PrimitivePinVisual, event: PointerEvent) {
    if (!pin.canEdit || event.button !== 0) return false;
    event.stopPropagation();
    archiveDropPinId = null;
    setArchiveDragState(true, false);
    primitivePinDragState = {
      pinId: pin.pinId,
      startClientX: event.clientX,
      startClientY: event.clientY,
      previousX: pin.x,
      previousY: pin.y,
      x: pin.x,
      y: pin.y,
      moved: false,
    };
    return true;
  }

  function patchPrimitivePinPosition(pinId: string, x: number, y: number) {
    localPinPositions.set(pinId, { x, y });
    primitivePinVisuals = primitivePinVisuals.map((pin) =>
      pin.pinId === pinId ? { ...pin, x, y } : pin,
    );
  }

  function movePrimitivePinDrag(pin: PrimitivePinVisual, event: PointerEvent) {
    const state = primitivePinDragState;
    if (!state || state.pinId !== pin.pinId) return false;

    const dx = event.clientX - state.startClientX;
    const dy = event.clientY - state.startClientY;
    if (!state.moved && Math.sqrt(dx * dx + dy * dy) < 3) return false;

    const point = primitivePointerWorldPoint(event);
    const archiveTarget = archiveBinTargetFromClient(event.clientX, event.clientY);
    state.moved = true;
    state.x = point.x;
    state.y = point.y;
    event.preventDefault();
    event.stopPropagation();
    patchPrimitivePinPosition(pin.pinId, point.x, point.y);
    archiveDropPinId = archiveTarget ? pin.pinId : null;
    setArchiveDragState(true, Boolean(archiveTarget));
    return true;
  }

  function animatePinDeleteToBin(
    pin: PrimitivePinVisual,
    target: HTMLElement,
    fallbackPosition: { x: number; y: number },
  ) {
    const startX = pin.x;
    const startY = pin.y;
    const archiveTarget = archiveBinCenterWorldPoint(target);
    const duration = 280;
    const startTime = performance.now();
    archivingPinId = pin.pinId;
    archivingPinProgress = 0;

    function finish() {
      archivingPinId = null;
      archivingPinProgress = 0;
      resetArchiveDragInteraction();
      localPinPositions.delete(pin.pinId);
      syncPrimitiveOrbitVisuals();
      if (simulation) simulation.alpha(0.16).restart();
    }

    function archiveTick(t: number) {
      const elapsed = t - startTime;
      const progress = Math.min(elapsed / duration, 1);
      const eased = 1 - (1 - progress) ** 3;
      patchPrimitivePinPosition(
        pin.pinId,
        interpolateNumber(startX, archiveTarget.x, eased),
        interpolateNumber(startY, archiveTarget.y, eased),
      );
      archivingPinProgress = eased;

      if (progress < 1) {
        requestAnimationFrame(archiveTick);
        return;
      }

      if (!onpindelete) {
        patchPrimitivePinPosition(pin.pinId, fallbackPosition.x, fallbackPosition.y);
        finish();
        return;
      }

      Promise.resolve(onpindelete({ pinId: pin.pinId }))
        .catch(() => {
          patchPrimitivePinPosition(pin.pinId, fallbackPosition.x, fallbackPosition.y);
        })
        .finally(finish);
    }

    requestAnimationFrame(archiveTick);
  }

  function endPrimitivePinDrag(pin: PrimitivePinVisual, event: PointerEvent) {
    const state = primitivePinDragState;
    if (!state || state.pinId !== pin.pinId) return false;

    const didMove = state.moved;
    if (state.moved) {
      const point = primitivePointerWorldPoint(event);
      const archiveTarget = archiveBinTargetFromClient(event.clientX, event.clientY);
      state.x = point.x;
      state.y = point.y;
      patchPrimitivePinPosition(pin.pinId, point.x, point.y);
      event.preventDefault();
      event.stopPropagation();
      resetArchiveDragInteraction();
      primitivePinDragState = null;

      if (archiveTarget) {
        animatePinDeleteToBin({ ...pin, x: point.x, y: point.y }, archiveTarget, {
          x: state.previousX,
          y: state.previousY,
        });
        return didMove;
      }

      Promise.resolve(onpinmove?.({ pinId: pin.pinId, x: point.x, y: point.y })).catch(() => {
        patchPrimitivePinPosition(pin.pinId, state.previousX, state.previousY);
      });
      return didMove;
    }

    localPinPositions.delete(pin.pinId);
    resetArchiveDragInteraction();
    primitivePinDragState = null;
    return didMove;
  }

  function patchPrimitiveAppPosition(appId: string, x: number, y: number) {
    localAppPositions.set(appId, { x, y });
    primitiveAppVisuals = primitiveAppVisuals.map((app) =>
      app.id === appId ? { ...app, x, y } : app,
    );
    primitiveAppCollisionObstacles = primitiveAppCollisionObstacles.map((app) =>
      app.id === appId ? { ...app, x, y } : app,
    );
  }

  function settleAppFreePosition(appId: string, x: number, y: number) {
    let nextX = x;
    let nextY = y;
    const minDistance = WORKSPACE_APP_COLLISION_RADIUS * 2 + WORKSPACE_APP_COLLISION_GAP;
    const otherApps = primitiveAppVisuals.filter((app) => app.id !== appId);

    for (let iteration = 0; iteration < 5; iteration += 1) {
      let changed = false;
      for (const app of otherApps) {
        const dx = nextX - app.x;
        const dy = nextY - app.y;
        const distance = Math.hypot(dx, dy) || 1;
        const overlap = minDistance - distance;
        if (overlap <= 0) continue;

        const fallbackAngle = seededRandom(Math.abs(seedHash(`${appId}:${app.id}:free-app-settle`)) + 1)() * Math.PI * 2;
        const ux = distance > 1 ? dx / distance : Math.cos(fallbackAngle);
        const uy = distance > 1 ? dy / distance : Math.sin(fallbackAngle);
        nextX += ux * (overlap + 6);
        nextY += uy * (overlap + 6);
        changed = true;
      }
      if (!changed) break;
    }

    return { x: nextX, y: nextY };
  }

  function beginPrimitiveAppDrag(app: PrimitiveAppVisual, event: PointerEvent) {
    if (event.button !== 0) return false;
    event.stopPropagation();
    archiveDropAppId = null;
    setArchiveDragState(true, false);
    primitiveAppDragState = {
      appId: app.id,
      startClientX: event.clientX,
      startClientY: event.clientY,
      previousX: app.x,
      previousY: app.y,
      moved: false,
    };
    dragTargetAnchorId = null;
    patchPrimitiveAppPosition(app.id, app.x, app.y);
    return true;
  }

  function movePrimitiveAppDrag(app: PrimitiveAppVisual, event: PointerEvent) {
    const state = primitiveAppDragState;
    if (!state || state.appId !== app.id) return false;

    const dx = event.clientX - state.startClientX;
    const dy = event.clientY - state.startClientY;
    if (!state.moved && Math.sqrt(dx * dx + dy * dy) < 4) return false;

    const point = primitivePointerWorldPoint(event);
    const archiveTarget = archiveBinTargetFromClient(event.clientX, event.clientY);
    state.moved = true;
    event.preventDefault();
    event.stopPropagation();
    patchPrimitiveAppPosition(app.id, point.x, point.y);
    dragTargetAnchorId = null;
    archiveDropAppId = archiveTarget ? app.id : null;
    setArchiveDragState(true, Boolean(archiveTarget));
    if (simulation) simulation.alpha(0.08).restart();
    return true;
  }

  function animateAppArchiveToBin(
    app: PrimitiveAppVisual,
    target: HTMLElement,
    fallbackPosition: { x: number; y: number },
  ) {
    const startX = app.x;
    const startY = app.y;
    const archiveTarget = archiveBinCenterWorldPoint(target);
    const duration = 300;
    const startTime = performance.now();
    archivingAppId = app.id;
    archivingAppProgress = 0;

    function archiveTick(t: number) {
      const elapsed = t - startTime;
      const progress = Math.min(elapsed / duration, 1);
      const eased = 1 - (1 - progress) ** 3;
      patchPrimitiveAppPosition(
        app.id,
        interpolateNumber(startX, archiveTarget.x, eased),
        interpolateNumber(startY, archiveTarget.y, eased),
      );
      archivingAppProgress = eased;

      if (progress < 1) {
        requestAnimationFrame(archiveTick);
        return;
      }

      Promise.resolve(onapparchive?.({ appId: app.id }))
        .catch(() => {
          patchPrimitiveAppPosition(app.id, fallbackPosition.x, fallbackPosition.y);
        })
        .finally(() => {
          archivingAppId = null;
          archivingAppProgress = 0;
          resetArchiveDragInteraction();
          localAppPositions.delete(app.id);
          syncPrimitiveOrbitVisuals();
          if (simulation) simulation.alpha(0.16).restart();
        });
    }

    requestAnimationFrame(archiveTick);
  }

  function endPrimitiveAppDrag(app: PrimitiveAppVisual, event: PointerEvent) {
    const state = primitiveAppDragState;
    if (!state || state.appId !== app.id) return false;

    const didMove = state.moved;
    if (state.moved) {
      const point = primitivePointerWorldPoint(event);
      const archiveTarget = archiveBinTargetFromClient(event.clientX, event.clientY);
      const settled = settleAppFreePosition(app.id, point.x, point.y);
      event.preventDefault();
      event.stopPropagation();

      dragTargetAnchorId = null;
      resetArchiveDragInteraction();
      primitiveAppDragState = null;

      if (archiveTarget) {
        patchPrimitiveAppPosition(app.id, point.x, point.y);
        animateAppArchiveToBin({ ...app, x: point.x, y: point.y }, archiveTarget, {
          x: state.previousX,
          y: state.previousY,
        });
        if (simulation) simulation.alpha(0.16).restart();
        return didMove;
      }

      patchPrimitiveAppPosition(app.id, settled.x, settled.y);
      Promise.resolve(onappmove?.({ appId: app.id, x: settled.x, y: settled.y }))
        .catch(() => {
          patchPrimitiveAppPosition(app.id, state.previousX, state.previousY);
        })
        .finally(() => {
          localAppPositions.delete(app.id);
          syncPrimitiveOrbitVisuals();
        });
      if (simulation) simulation.alpha(0.16).restart();

      return didMove;
    }

    localAppPositions.delete(app.id);
    dragTargetAnchorId = null;
    resetArchiveDragInteraction();
    primitiveAppDragState = null;
    syncPrimitiveOrbitVisuals();
    return didMove;
  }

  function primitiveOverlayTransformStyle() {
    return primitiveOverlayTransformStyleForTransform(currentZoomTransform);
  }

  function updatePrimitiveOverlayTransform() {
    applyPrimitiveOverlayTransform(primitiveOverlayEl, currentZoomTransform);
  }

  // ── State ──────────────────────────────────────────────────
  let svg: d3.Selection<SVGSVGElement, unknown, null, undefined>;
  let g: d3.Selection<SVGGElement, unknown, null, undefined>;
  let defs: d3.Selection<SVGDefsElement, unknown, null, undefined>;
  let simulation: d3.Simulation<any, any> | null = null;
  let coreGroup: d3.Selection<SVGGElement, unknown, null, undefined>;
  let zoomBehavior: d3.ZoomBehavior<SVGSVGElement, unknown> | null = null;
  let primitiveOverlayEl = $state<HTMLDivElement | undefined>();
  let canvasW = 0, canvasH = 0, coreX = 0, coreY = 0;
  let currentZoomTransform: d3.ZoomTransform = d3.zoomIdentity;
  let hasUserAdjustedViewport = false;
  let threadFocusedIdeaId = $state<string | null>(null);
  let threadRestoreTransform = $state<d3.ZoomTransform | null>(null);
  let threadAnchorState = $state<{ id: string; fx: number | null; fy: number | null } | null>(null);
  let primitiveBlobVisuals = $state<PrimitiveBlobVisual[]>([]);
  let primitiveAstreVisuals = $state<PrimitiveAstreVisual[]>([]);
  let primitivePinVisuals = $state<PrimitivePinVisual[]>([]);
  let primitiveAppVisuals = $state<PrimitiveAppVisual[]>([]);
  let primitiveAppCollisionObstacles: WorkspaceAppCollisionObstacle[] = [];
  let primitiveOrbitLaneVisuals = $state<PrimitiveOrbitLaneVisual[]>([]);
  let semanticZoomLevel = $state<SemanticZoomLevel>('detail');
  let hoveredIdeaId = $state<string | null>(null);
  let hoveredAstreId = $state<string | null>(null);
  let hoveredPinId = $state<string | null>(null);
  let dragTargetAnchorId = $state<string | null>(null);
  let archiveDropIdeaId = $state<string | null>(null);
  let archiveDropAppId = $state<string | null>(null);
  let archiveDropPinId = $state<string | null>(null);
  let archivingIdeaId = $state<string | null>(null);
  let archivingIdeaProgress = $state(0);
  let archivingAppId = $state<string | null>(null);
  let archivingAppProgress = $state(0);
  let archivingPinId = $state<string | null>(null);
  let archivingPinProgress = $state(0);
  let archiveDragState = { active: false, over: false };
  let primitiveDragState: PrimitiveDragState | null = null;
  let primitivePinDragState = $state<PrimitivePinDragState | null>(null);
  let primitiveAppDragState: PrimitiveAppDragState | null = null;
  let localPinPositions = new Map<string, { x: number; y: number }>();
  let localAppPositions = new Map<string, { x: number; y: number }>();
  let orbitSceneNodesById = new Map<string, OrbitNode>();

  // Multi-user sun attractors
  let attractors: Attractor[] = [];
  let orbitAnchors: Attractor[] = [];
  let attractorLookup: AttractorLookup = { byId: new Map(), byName: new Map(), byAnchorKey: new Map() };
  let orbitAnchorLookup: AttractorLookup = { byId: new Map(), byName: new Map(), byAnchorKey: new Map() };
  let sunLayoutKey = '';
  let pinLayoutKey = '';
  let sunPulseFrame: number | null = null;
  let breathingFrame: number | null = null;
  let particleFrame: number | null = null;
  let renderGeneration = 0;
  let primitiveSyncFrame: number | null = null;
  let primitiveSyncNodes: OrbitNode[] | null = null;
  let primitiveSyncLastAt = 0;
  let simulationPaintLastAt = 0;
  let renderOwnerWarningIssued = false;
  let workspaceMotionSuspended = false;
  let _isDragging = false;
  const PREVIEW_MEMBER_PREFIX = '__cortex-preview-user__';
  const PREVIEW_IDEA_PREFIX = '__cortex-preview-idea__';
  const PRIMITIVE_SYNC_INTERVAL_MS = 16;
  const SIMULATION_PAINT_INTERVAL_MS = 16;

  type SimulationPerformanceProfile = {
    alphaDecay: number;
    alphaMin: number;
    idleAlphaTarget: number;
    collideIterations: number;
    velocityDecay: number;
  };

  function simulationPerformanceProfile(nodeCount: number): SimulationPerformanceProfile {
    return cortexOrbitPerformanceProfile(nodeCount);
  }

  function activeSimulationNodeCount() {
    return simulation?.nodes().length || orbitSceneNodesById.size || cortex.filteredIdeas.length;
  }

  function activeSimulationProfile() {
    return simulationPerformanceProfile(activeSimulationNodeCount());
  }

  function idleOrbitAlphaTarget() {
    return activeSimulationProfile().idleAlphaTarget;
  }

  function primitiveSyncIntervalMs() {
    if (_isDragging) return PRIMITIVE_SYNC_INTERVAL_MS;
    const nodeCount = activeSimulationNodeCount();
    if (nodeCount >= 220) return 32;
    if (nodeCount >= 120) return 24;
    return PRIMITIVE_SYNC_INTERVAL_MS;
  }

  function simulationPaintIntervalMs() {
    if (_isDragging) return SIMULATION_PAINT_INTERVAL_MS;
    return activeSimulationNodeCount() >= 220 ? 24 : SIMULATION_PAINT_INTERVAL_MS;
  }

  function isPreviewMemberId(id: unknown): boolean {
    return typeof id === 'string' && id.startsWith(PREVIEW_MEMBER_PREFIX);
  }

  function isPreviewIdeaId(id: unknown): boolean {
    return typeof id === 'string' && id.startsWith(PREVIEW_IDEA_PREFIX);
  }

  function currentUserFallbackMember(): TeamMember[] {
    const user = auth.user;
    if (!user?.id) return [];
    return [{
      id: String(user.id),
      name: user.name || user.email || 'You',
      email: user.email,
      color: user.color || '#6d46d9',
    }];
  }

  function workspaceAttractorMembers(): TeamMember[] {
    return cortex.teamMembers.length > 0
      ? cortex.teamMembers
      : currentUserFallbackMember();
  }

  function warnIfCompetingVisibleWorkspaceOwnerExists() {
    if (renderOwnerWarningIssued || import.meta.env.PROD || !containerEl?.parentElement) return;
    const host = containerEl.parentElement;
    const competingOverlay = host.querySelector('.cortex-orbit-overlay');
    if (!competingOverlay) return;
    renderOwnerWarningIssued = true;
    console.warn(
      '[cortex] Competing visible workspace renderer detected. Live /cortex must keep a single render owner. Remove the extra orbit overlay or replace WorkspaceScene completely.',
    );
  }

  function cancelPrimitiveOrbitVisualSync() {
    if (primitiveSyncFrame !== null) {
      cancelAnimationFrame(primitiveSyncFrame);
      primitiveSyncFrame = null;
    }
    primitiveSyncNodes = null;
  }

  function stopActiveSimulation() {
    if (!simulation) return;
    simulation.on('tick', null);
    simulation.on('end', null);
    simulation.stop();
    simulation = null;
  }

  function cancelRenderFrames() {
    if (breathingFrame) {
      cancelAnimationFrame(breathingFrame);
      breathingFrame = null;
    }
    if (particleFrame) {
      cancelAnimationFrame(particleFrame);
      particleFrame = null;
    }
    if (sunPulseFrame) {
      cancelAnimationFrame(sunPulseFrame);
      sunPulseFrame = null;
    }
    cancelPrimitiveOrbitVisualSync();
  }

  function suspendWorkspaceMotion() {
    if (workspaceMotionSuspended) return;
    workspaceMotionSuspended = true;
    if (simulation) {
      simulation.alphaTarget(0);
      simulation.stop();
      syncPrimitiveOrbitVisuals(simulation.nodes() as OrbitNode[]);
    }
    cancelRenderFrames();
    g?.selectAll('*').interrupt();
  }

  function resumeWorkspaceMotion() {
    if (!workspaceMotionSuspended) return;
    workspaceMotionSuspended = false;
    if (!simulation) {
      renderCanvas({ preserveViewport: true });
      return;
    }
    const nodes = simulation.nodes() as OrbitNode[];
    clearAttractionCache(nodes);
    simulation
      .alphaTarget(idleOrbitAlphaTarget())
      .alpha(Math.max(simulation.alpha(), 0.14))
      .restart();
    syncPrimitiveMotionVisuals(nodes);
  }

  function resetRenderRuntime() {
    renderGeneration += 1;
    workspaceMotionSuspended = false;
    stopActiveSimulation();
    cancelRenderFrames();
    primitiveSyncLastAt = 0;
    simulationPaintLastAt = 0;
    coreGroup = undefined as any;
  }

  function runWhenBrowserIdle(callback: () => void, timeout = 1500) {
    if (typeof window === 'undefined') return;
    const requestIdle = (window as typeof window & {
      requestIdleCallback?: (cb: () => void, options?: { timeout?: number }) => number;
    }).requestIdleCallback;
    if (requestIdle) {
      requestIdle(callback, { timeout });
      return;
    }
    window.setTimeout(callback, Math.min(timeout, 500));
  }


  function releaseThreadAnchors(nodes?: OrbitNode[]) {
    for (const node of nodes || []) {
      if (!node?._threadAnchorPinned) continue;
      node.fx = null;
      node.fy = null;
      delete node._threadAnchorPinned;
    }
  }

  function emitWorkspaceContext() {
    if (!containerEl) return;
    const rect = containerEl.getBoundingClientRect();
    const userId = auth.user?.id;
    const focusSun = userId ? attractorLookup.byId.get(userId) : null;
    const worldX = focusSun?.x ?? coreX;
    const worldY = focusSun?.y ?? coreY;
    const [localX, localY] = currentZoomTransform.apply([worldX, worldY]);
    const point = {
      worldX,
      worldY,
      screenX: rect.left + localX,
      screenY: rect.top + localY,
    };
    cortex.setBirthContext(workspaceBirthPoint());
    onworkspacecontext?.(point);
  }

  function ideaCoords(idea: any) {
    const sceneNode = sceneNodeById(idea?.id);
    const source = sceneNode ?? idea;
    const x = typeof source?.x === 'number' ? source.x : source?.position_x;
    const y = typeof source?.y === 'number' ? source.y : source?.position_y;
    if (typeof x !== 'number' || typeof y !== 'number') return null;
    return { x, y };
  }

  function threadFocusTransform(idea: any): d3.ZoomTransform | null {
    const coords = ideaCoords(idea);
    if (!coords || !canvasW || !canvasH) return null;
    const baseTransform = untrack(() => currentZoomTransform);
    const targetScale = Math.max(baseTransform.k, canvasW < 900 ? 2.06 : 2.3);
    const focusX = canvasW < 900 ? canvasW * 0.5 : canvasW * 0.34;
    const focusY = canvasW < 900 ? canvasH * 0.47 : canvasH * 0.39;
    return d3.zoomIdentity
      .translate(focusX - coords.x * targetScale, focusY - coords.y * targetScale)
      .scale(targetScale);
  }

  function animateViewport(target: d3.ZoomTransform, duration = 620) {
    if (!svg || !zoomBehavior) return;
    svg.interrupt('thread-camera');
    svg
      .transition('thread-camera')
      .duration(duration)
      .ease(d3.easeCubicOut)
      .call(zoomBehavior.transform as any, target);
  }

  function startupViewportTransform(width = canvasW, height = canvasH): d3.ZoomTransform {
    const fit = computeAttractorViewportTransform(
      orbitAnchors.length ? orbitAnchors : attractors,
      width,
      height,
      currentAttractorLayoutOptions(),
    );

    return d3.zoomIdentity
      .translate(fit.x, fit.y)
      .scale(fit.k);
  }

  function refreshCollisionForce(alpha = 0.16) {
    if (!simulation) return;
    const profile = activeSimulationProfile();
    simulation.force('collide', d3.forceCollide().radius((d: any) => bubbleCollisionRadius(d)).strength(0.84).iterations(profile.collideIterations));
    if (workspaceMotionSuspended) {
      syncPrimitiveOrbitVisuals(simulation.nodes() as OrbitNode[]);
      return;
    }
    simulation.alpha(alpha).restart();
  }

  // ── SVG Defs (from cortex-physics.js createDefs) ───────────
  function createDefs() {
    defs = svg.append('defs');

    // Core glow
    const coreGlow = defs.append('filter').attr('id', 'core-glow')
      .attr('x', '-100%').attr('y', '-100%').attr('width', '300%').attr('height', '300%');
    coreGlow.append('feGaussianBlur').attr('stdDeviation', '12').attr('result', 'blur');
    const cm = coreGlow.append('feMerge');
    cm.append('feMergeNode').attr('in', 'blur');
    cm.append('feMergeNode').attr('in', 'SourceGraphic');

    // Core glow hover
    const coreGlowH = defs.append('filter').attr('id', 'core-glow-hover')
      .attr('x', '-100%').attr('y', '-100%').attr('width', '300%').attr('height', '300%');
    coreGlowH.append('feGaussianBlur').attr('stdDeviation', '18').attr('result', 'blur');
    const cm2 = coreGlowH.append('feMerge');
    cm2.append('feMergeNode').attr('in', 'blur');
    cm2.append('feMergeNode').attr('in', 'SourceGraphic');

    // Depth blur filters for focus mode
    for (const level of [1, 2, 3]) {
      const f = defs.append('filter').attr('id', `depth-blur-${level}`)
        .attr('x', '-10%').attr('y', '-10%').attr('width', '120%').attr('height', '120%');
      f.append('feGaussianBlur').attr('stdDeviation', level * 1.5).attr('result', 'blur');
      f.append('feMerge').append('feMergeNode').attr('in', 'blur');
    }

    // Bubble shadow
    const bShadow = defs.append('filter').attr('id', 'bubble-shadow')
      .attr('x', '-30%').attr('y', '-30%').attr('width', '160%').attr('height', '160%');
    bShadow.append('feGaussianBlur').attr('in', 'SourceAlpha').attr('stdDeviation', '3').attr('result', 'shadow');
    bShadow.append('feOffset').attr('in', 'shadow').attr('dx', '0').attr('dy', '2').attr('result', 'offsetShadow');
    bShadow.append('feFlood').attr('flood-color', 'rgba(0,0,0,0.3)').attr('result', 'color');
    bShadow.append('feComposite').attr('in', 'color').attr('in2', 'offsetShadow').attr('operator', 'in').attr('result', 'colorShadow');
    const bm = bShadow.append('feMerge');
    bm.append('feMergeNode').attr('in', 'colorShadow');
    bm.append('feMergeNode').attr('in', 'SourceGraphic');

    // Neural aurora glow filter for agent-working indicator
    const auroraGlow = defs.append('filter').attr('id', 'aurora-glow')
      .attr('x', '-80%').attr('y', '-80%').attr('width', '260%').attr('height', '260%');
    auroraGlow.append('feGaussianBlur').attr('stdDeviation', '8').attr('result', 'aBlur');
    auroraGlow.append('feGaussianBlur').attr('in', 'SourceGraphic').attr('stdDeviation', '3').attr('result', 'aSharp');
    const am = auroraGlow.append('feMerge');
    am.append('feMergeNode').attr('in', 'aBlur');
    am.append('feMergeNode').attr('in', 'aBlur');
    am.append('feMergeNode').attr('in', 'aSharp');

    // Soft outer halo filter — large diffuse bloom
    const haloFilter = defs.append('filter').attr('id', 'aurora-halo')
      .attr('x', '-100%').attr('y', '-100%').attr('width', '300%').attr('height', '300%');
    haloFilter.append('feGaussianBlur').attr('stdDeviation', '14').attr('result', 'hBlur');
    const hm = haloFilter.append('feMerge');
    hm.append('feMergeNode').attr('in', 'hBlur');
    hm.append('feMergeNode').attr('in', 'hBlur');
  }

  // ── Recency Ranking ────────────────────────────────────────
  function assignRecencyRanks(nodes: any[]) {
    const sorted = [...nodes].sort((a, b) => {
      return new Date(b.updated_at || b.created_at || 0).getTime() - new Date(a.updated_at || a.created_at || 0).getTime();
    });
    sorted.forEach((node, idx) => { node._recencyRank = idx; });
  }

  // ── Force Simulation (from cortex-physics.js createSim) ────
  function createSim(nodes: any[], links: any[], w: number, h: number) {
    const profile = simulationPerformanceProfile(nodes.length);
    return d3.forceSimulation(nodes)
      .alphaDecay(profile.alphaDecay)
      .alphaMin(profile.alphaMin)
      .alphaTarget(profile.idleAlphaTarget)
      .velocityDecay(profile.velocityDecay)
      .force('center', orbitAnchors.length > 0 ? null : d3.forceCenter(w / 2, h / 2).strength(0.02))
      .force('collide', d3.forceCollide().radius((d: any) => bubbleCollisionRadius(d)).strength(0.84).iterations(profile.collideIterations))
      .force('link', d3.forceLink(links)
        .id((d: any) => d.id)
        .distance((d: any) => d.sourceUserId && d.targetUserId && d.sourceUserId !== d.targetUserId ? 380 : 190)
        .strength((d: any) => d.sourceUserId && d.targetUserId && d.sourceUserId !== d.targetUserId ? 0.018 : 0.045))
      .force('charge', d3.forceManyBody().strength(-80).distanceMax(400))
      .force('appCollision', (alpha: number) => {
        repelNodesFromWorkspaceApps(nodes, alpha);
      })
      .force('coreRepel', () => {
        // Multi-sun repulsion: push nodes away from ALL sun cores
        if (orbitAnchors.length > 0) {
          multiSunRepulsion(nodes, orbitAnchors);
        } else {
          // Fallback: single-core repulsion for when team hasn't loaded
          for (const n of nodes) {
            const dx = n.x - coreX, dy = n.y - coreY;
            const dist = Math.sqrt(dx * dx + dy * dy) || 1;
            if (dist < 200) {
              const proximity = 1 - dist / 200;
              const push = proximity * proximity * 8;
              n.vx += (dx / dist) * push;
              n.vy += (dy / dist) * push;
            }
          }
        }
      })
      .force('orbit', (alpha: number) => {
        // Multi-sun orbit: each node orbits its creator's sun.
        if (orbitAnchors.length > 0) {
          multiSunOrbit(nodes, orbitAnchors, orbitAnchorLookup, alpha, coreX, coreY, ORBIT_BANDS, auth.user?.id);
        } else {
          // Fallback: single-core orbit
          const baseStrength = nodes.length <= 3 ? 0.052 : 0.038;
          for (const n of nodes) {
            const band = ORBIT_BANDS[n.status] || { min: 200, max: 350 };
            const targetDist = typeof n._ownerOrbitRadius === 'number'
              ? n._ownerOrbitRadius
              : (() => {
                  const rank = n._recencyRank != null ? n._recencyRank : nodes.length;
                  const t = Math.min(rank / Math.max(nodes.length, 1), 1);
                  return band.min + t * (band.max - band.min);
                })();
            const dx = n.x - coreX, dy = n.y - coreY;
            const dist = Math.sqrt(dx * dx + dy * dy) || 1;
            const diff = dist - targetDist;
            const radialStr = baseStrength * 0.6 + baseStrength * 0.4 * alpha;
            n.vx -= (dx / dist) * diff * radialStr;
            n.vy -= (dy / dist) * diff * radialStr;
            // Tangential orbit velocity
            const tangentStr = ATTRACTOR_CFG.orbitSpinBase + ATTRACTOR_CFG.orbitSpinBoost * alpha;
            n.vx += (-dy / dist) * tangentStr;
            n.vy += (dx / dist) * tangentStr;
          }
        }
      })
      .force('buoyancy', (alpha: number) => {
        for (const n of nodes) {
          // Subtle upward float for high-salience nodes (reduced to avoid directional bias)
          n.vy -= ((n.salience_score || 5) / 10) * 0.03 * alpha;
        }
      })
      .force('brownian', () => {
        for (const n of nodes) {
          // Gentle random perturbation keeps motion organic (not alpha-dependent)
          n.vx += (Math.random() - 0.5) * 0.15;
          n.vy += (Math.random() - 0.5) * 0.15;
        }
      });
  }

  function archivedDotsForUser(userId: string) {
    const count = Math.max(
      cortex.archivedIdeaCountForUser(userId),
      cortex.ideas.filter((idea: any) => idea?.user_id === userId && idea?.archived_at).length,
    );
    return Math.min(ASTRE_ARCHIVE_DOT_PRESETS.length, count);
  }

  function attractorPresence(userId: string): ConstellationAstrePresence {
    if (auth.user?.id === userId) return 'online';
    return presenceStore.viewers.some((viewer) => viewer.user_id === userId) ? 'online' : 'offline';
  }

  function attractorActivity(userId: string): ConstellationActivity {
    const hasWorkingIdea = cortex.filteredIdeas.some(
      (idea: any) => !idea?.archived_at && idea?.user_id === userId && visualStatus(idea.status) === 'working',
    );
    if (hasWorkingIdea) return 'working';

    for (const entry of cortex.typingUsers.values()) {
      if (entry.user_id === userId) return 'working';
      const idea = cortex.ideas.find((candidate: any) => candidate.id === entry.idea_id);
      if (idea?.user_id === userId) return 'working';
    }

    if (attractorPresence(userId) === 'offline') return 'disconnected';

    return 'idle';
  }

  function applySunVisualState(
    sunEl: d3.Selection<SVGGElement, any, any, any>,
    accent: string,
    activity: ConstellationActivity = 'idle',
  ) {
    applyAstreToneVars(sunEl, accent);
    sunEl
      .classed('activity-working', activity === 'working')
      .classed('activity-disconnected', activity === 'disconnected')
      .classed('activity-idle', activity === 'idle');
  }

  function refreshOrbitVisualState() {
    if (!g) return;

    g.selectAll<SVGGElement, any>('.bubble-group').each(function(d: any) {
      const bubbleEl = d3.select(this);
      applyBubbleVisualState(bubbleEl, d);
      syncStatusAnchor(bubbleEl, d, ideaRadius(d));
    });

    g.selectAll<SVGGElement, any>('.sun-group').each(function(a: any) {
      applySunVisualState(d3.select(this), a.color, attractorActivity(a.id));
    });

    syncPrimitiveOrbitVisuals();
  }

  function syncPrimitiveOrbitVisuals(nodes: OrbitNode[] = Array.from(orbitSceneNodesById.values())) {
    primitiveBlobVisuals = nodes
      .map((node) => bubblePrimitiveVisual(node))
      .filter((node): node is PrimitiveBlobVisual => Boolean(node));
    const appVisuals = workspaceAppPrimitiveVisuals();
    primitiveAppVisuals = appVisuals;
    primitiveAppCollisionObstacles = appVisuals.map(({ id, x, y }) => ({ id, x, y }));

    if (orbitAnchors.length > 0) {
      primitiveOrbitLaneVisuals = orbitAnchors.map((attractor) => orbitLanePrimitiveVisual(attractor));
      primitiveAstreVisuals = attractors.map((attractor) => astrePrimitiveVisual(attractor));
      primitivePinVisuals = orbitAnchors
        .map((anchor) => pinPrimitiveVisual(anchor))
        .filter((pin): pin is PrimitivePinVisual => Boolean(pin));
      queueMicrotask(() => syncPrimitiveMotionVisuals(nodes));
      return;
    }

    primitiveOrbitLaneVisuals = [];
    primitiveAstreVisuals = [fallbackCorePrimitiveVisual()];
    primitivePinVisuals = [];
    queueMicrotask(() => syncPrimitiveMotionVisuals(nodes));
  }

  function schedulePrimitiveOrbitVisuals(nodes: OrbitNode[] = Array.from(orbitSceneNodesById.values())) {
    primitiveSyncNodes = nodes;

    if (primitiveSyncFrame !== null) return;

    const run = (now: number) => {
      primitiveSyncFrame = null;

      if (primitiveSyncLastAt && now - primitiveSyncLastAt < primitiveSyncIntervalMs()) {
        primitiveSyncFrame = requestAnimationFrame(run);
        return;
      }

      primitiveSyncLastAt = now;
      const nextNodes = primitiveSyncNodes ?? Array.from(orbitSceneNodesById.values());
      primitiveSyncNodes = null;
      syncPrimitiveMotionVisuals(nextNodes);
    };

    primitiveSyncFrame = requestAnimationFrame(run);
  }

  // ── Multi-sun rendering ──────────────────────────────────────

  function renderMultiSuns(
    parentG: d3.Selection<SVGGElement, unknown, null, undefined>,
    defs: d3.Selection<SVGDefsElement, unknown, null, undefined>,
  ) {
    const sunLayer = parentG.append('g').attr('class', 'sun-layer').attr('pointer-events', 'none');
    const { coreRadius, labelOffset } = ATTRACTOR_CFG;
    const ringOuterRx = coreRadius * 1.26;
    const ringOuterRy = coreRadius * 1.17;
    const haloRx = coreRadius * 1.14;
    const haloRy = coreRadius * 1.1;
    const haloInnerRx = coreRadius * 1.18;
    const haloInnerRy = coreRadius * 1.03;
    const ringRx = coreRadius * 0.98;
    const ringRy = coreRadius * 0.96;
    const archiveFieldRadii = {
      outerRx: ringOuterRx * 1.14,
      outerRy: ringOuterRy * 1.14,
      innerRx: ringRx,
      innerRy: ringRy,
    };

    const sunArchiveDots: Array<Array<(typeof ASTRE_ARCHIVE_DOT_PRESETS)[number]>> = [];

    for (const a of attractors) {
      const sg = sunLayer.append('g').datum(a).attr('class', 'sun-group')
        .attr('transform', `translate(${a.x},${a.y})`).attr('data-user-id', a.id);
      const isCurrentUserAstre = a.id === auth.user?.id;
      sg.classed('is-current-user', isCurrentUserAstre);
      applySunVisualState(sg as any, a.color, attractorActivity(a.id));

      sg.append('ellipse').attr('class', 'sun-root-ring')
        .attr('rx', ringOuterRx).attr('ry', ringOuterRy);
      sg.append('ellipse').attr('class', 'sun-halo')
        .attr('rx', haloRx).attr('ry', haloRy);
      sg.append('ellipse').attr('class', 'sun-halo-inner')
        .attr('rx', haloInnerRx).attr('ry', haloInnerRy);
      sg.append('ellipse').attr('class', 'sun-ring')
        .attr('rx', ringRx).attr('ry', ringRy);

      const archiveDots = ASTRE_ARCHIVE_DOT_PRESETS.slice(0, archivedDotsForUser(a.id));
      sunArchiveDots.push(archiveDots);
      archiveDots.forEach((dot) => {
        sg.append('circle')
          .attr('class', `sun-archive-dot ${dot.layer === 'inner' ? 'sun-archive-dot-inner' : 'sun-archive-dot-outer'} sun-archive-dot-${dot.variant}`)
          .attr('r', dot.size);
      });

      const coreCluster = sg.append('g').attr('class', 'sun-core-cluster');
      coreCluster.append('circle').attr('class', 'sun-core-shell').attr('r', coreRadius * 0.9);
      coreCluster.append('circle').attr('class', 'sun-core').attr('r', coreRadius * 0.86);

      coreCluster.append('text').attr('class', 'sun-initial')
        .attr('text-anchor', 'middle').attr('dominant-baseline', 'central')
        .attr('font-size', `${Math.round(coreRadius * 0.44)}px`).attr('font-weight', '700')
        .text(a.initial);

      coreCluster.append('circle')
        .attr('class', 'sun-hit-area')
        .attr('r', coreRadius * 0.92)
        .on('mouseover', () => {
          hoveredAstreId = a.id;
        })
        .on('mouseout', () => {
          if (hoveredAstreId === a.id) hoveredAstreId = null;
        })
        .on('click', (event: MouseEvent) => {
          if (!isCurrentUserAstre) return;
          event.stopPropagation();
          onownastreclick?.({ x: event.clientX, y: event.clientY, userId: a.id });
        });

      const firstName = a.name.split(/\s+/)[0];
      sg.append('text').attr('class', 'sun-label')
        .attr('y', labelOffset).attr('text-anchor', 'middle')
        .text(`✦ ${firstName}`);

      sg.style('pointer-events', 'none');
    }

    const startTime = performance.now();
    function paintSunArchiveDots(now: number) {
      const elapsedSeconds = (now - startTime) / 1000;

      sunLayer.selectAll<SVGGElement, unknown>('.sun-group').each(function(_, i) {
        const a = attractors[i];
        if (!a) return;
        const sg = d3.select(this);
        const activity = attractorActivity(a.id);
        applySunVisualState(sg as any, a.color, activity);

        const archiveDots = sunArchiveDots[i] ?? [];
        sg.selectAll('.sun-archive-dot').each(function(_: any, dotIndex: number) {
          const dot = archiveDots[dotIndex];
          if (!dot) return;
          const position = archiveDotPosition(dot, elapsedSeconds + i * 0.45, activity, archiveFieldRadii);
          d3.select(this as Element).attr('cx', position.x).attr('cy', position.y);
        });
      });
    }
    paintSunArchiveDots(startTime);

    // Set coreGroup as the first sun for archive drag compat
    coreGroup = sunLayer.select('.sun-group') as any;
  }

  function renderSingleCore(parentG: d3.Selection<SVGGElement, unknown, null, undefined>) {
    coreGroup = parentG.append('g')
      .attr('class', 'core-orb activity-idle')
      .attr('transform', `translate(${coreX},${coreY})`);
    applyAstreToneVars(coreGroup as any, '#f0f0fa');

    const ringOuterRx = CORE_RADIUS * 1.24;
    const ringOuterRy = CORE_RADIUS * 1.16;
    const haloRx = CORE_RADIUS * 1.13;
    const haloRy = CORE_RADIUS * 1.09;
    const haloInnerRx = CORE_RADIUS * 1.17;
    const haloInnerRy = CORE_RADIUS * 1.03;
    const ringRx = CORE_RADIUS * 0.98;
    const ringRy = CORE_RADIUS * 0.96;
    const archiveFieldRadii = {
      outerRx: ringOuterRx * 1.14,
      outerRy: ringOuterRy * 1.14,
      innerRx: ringRx,
      innerRy: ringRy,
    };

    coreGroup.append('ellipse').attr('class', 'core-root-ring').attr('rx', ringOuterRx).attr('ry', ringOuterRy);
    coreGroup.append('ellipse').attr('class', 'core-halo').attr('rx', haloRx).attr('ry', haloRy);
    coreGroup.append('ellipse').attr('class', 'core-halo-inner').attr('rx', haloInnerRx).attr('ry', haloInnerRy);
    coreGroup.append('ellipse').attr('class', 'core-ring').attr('rx', ringRx).attr('ry', ringRy);

    const archiveDots = ASTRE_ARCHIVE_DOT_PRESETS.slice(0, 3);
    archiveDots.forEach((dot) => {
      coreGroup.append('circle')
        .attr('class', `core-archive-dot ${dot.layer === 'inner' ? 'core-archive-dot-inner' : 'core-archive-dot-outer'} core-archive-dot-${dot.variant}`)
        .attr('r', dot.size);
    });

    const coreCluster = coreGroup.append('g').attr('class', 'core-cluster');
    coreCluster.append('circle').attr('class', 'core-shell').attr('r', CORE_RADIUS * 0.9);
    coreCluster.append('circle').attr('class', 'core-circle').attr('r', CORE_ASTRE_INNER_RADIUS).attr('filter', 'url(#core-glow)');
    coreCluster.append('text').attr('class', 'core-initial')
      .attr('text-anchor', 'middle').attr('dominant-baseline', 'central')
      .attr('font-size', '34px').text('C');
    coreCluster.append('circle')
      .attr('class', 'core-hit-area')
      .attr('r', CORE_RADIUS * 0.92)
      .on('mouseover', () => {
        hoveredAstreId = 'cortex-core';
      })
      .on('mouseout', () => {
        if (hoveredAstreId === 'cortex-core') hoveredAstreId = null;
      });

    coreGroup.append('text').attr('class', 'core-label').attr('y', 100).attr('text-anchor', 'middle')
      .text('✦ Cortex');

    coreGroup.style('pointer-events', 'none');

    const startTime = performance.now();
    function paintCoreArchiveDots(now: number) {
      if (!coreGroup?.node()) return;
      const elapsedSeconds = (now - startTime) / 1000;
      coreGroup.selectAll('.core-archive-dot').each(function(_: any, i: number) {
        const dot = archiveDots[i];
        if (!dot) return;
        const position = archiveDotPosition(dot, elapsedSeconds, 'idle', archiveFieldRadii);
        d3.select(this as Element).attr('cx', position.x).attr('cy', position.y);
      });
    }
    paintCoreArchiveDots(startTime);
  }

  /** Get the sun position for a user (for initial placement / birth animation). */
  function getSunPosition(userId?: string): { x: number; y: number } {
    if (userId && attractorLookup.byId.has(userId)) {
      const a = attractorLookup.byId.get(userId)!;
      return { x: a.x, y: a.y };
    }
    return { x: coreX, y: coreY };
  }

  // ── Drag handlers (from cortex-interactions.js) ────────────
  function canArchiveNode(node: OrbitNode | null | undefined): boolean {
    if (!node?.id || isPreviewIdeaId(node.id)) return false;
    return isIdeaControlledByCurrentUser(node);
  }

  function dragClientPoint(eventLike: any): { x: number; y: number } | null {
    const source = eventLike?.sourceEvent;
    const clientX = typeof eventLike?.clientX === 'number'
      ? eventLike.clientX
      : typeof source?.clientX === 'number'
        ? source.clientX
        : null;
    const clientY = typeof eventLike?.clientY === 'number'
      ? eventLike.clientY
      : typeof source?.clientY === 'number'
        ? source.clientY
        : null;
    if (clientX == null || clientY == null) return null;
    return { x: clientX, y: clientY };
  }

  function archiveBinTargetFromClient(clientX: number, clientY: number): HTMLElement | null {
    if (typeof document === 'undefined') return null;
    const elements = document.elementsFromPoint(clientX, clientY);
    for (const element of elements) {
      if (!(element instanceof HTMLElement)) continue;
      const target = element.closest<HTMLElement>('[data-cortex-archive-bin="true"]');
      if (target) return target;
    }
    for (const target of document.querySelectorAll<HTMLElement>('[data-cortex-archive-bin="true"]')) {
      const rect = target.getBoundingClientRect();
      if (
        clientX >= rect.left
        && clientX <= rect.right
        && clientY >= rect.top
        && clientY <= rect.bottom
      ) {
        return target;
      }
    }
    return null;
  }

  function isIdeaControlledByCurrentUser(idea: any): boolean {
    const currentUserId = auth.user?.id;
    if (!currentUserId || !idea) return false;
    if (!idea.user_id || idea.user_id === currentUserId) return true;
    // A returned handoff can be visually assigned by orbit anchor before user_id catches up.
    return idea.orbit_anchor_type === 'user' && String(idea.orbit_anchor_id ?? '') === currentUserId;
  }

  function restoreDraggedNodeToOrigin(node: any) {
    const x = typeof node._dragOrigX === 'number' ? node._dragOrigX : node.x;
    const y = typeof node._dragOrigY === 'number' ? node._dragOrigY : node.y;
    node.x = x;
    node.y = y;
    node.fx = null;
    node.fy = null;
  }

  function archiveBinTargetFromDrag(eventLike: any, node: OrbitNode): HTMLElement | null {
    if (!canArchiveNode(node)) return null;
    const point = dragClientPoint(eventLike);
    if (!point) return null;
    return archiveBinTargetFromClient(point.x, point.y);
  }

  function setArchiveDragState(active: boolean, over: boolean) {
    if (archiveDragState.active === active && archiveDragState.over === over) return;
    archiveDragState = { active, over };
    onarchivedragstate?.(archiveDragState);
  }

  function resetArchiveDragInteraction() {
    archiveDropIdeaId = null;
    archiveDropAppId = null;
    archiveDropPinId = null;
    setArchiveDragState(false, false);
  }

  function archiveBinCenterWorldPoint(target: HTMLElement): { x: number; y: number } {
    const rect = target.getBoundingClientRect();
    const containerRect = containerEl?.getBoundingClientRect();
    const localX = rect.left + rect.width / 2 - (containerRect?.left ?? 0);
    const localY = rect.top + rect.height / 2 - (containerRect?.top ?? 0);
    const [x, y] = currentZoomTransform.invert([localX, localY]);
    return { x, y };
  }

  function orbitDropTargetWithinRadius(x: number, y: number, node: OrbitNode, radius = HANDOFF_THRESHOLD): Attractor | null {
    const currentAnchorKey = itemOrbitAnchorKey(node);
    return orbitAnchorTargetWithinRadius(
      x,
      y,
      orbitAnchors.length ? orbitAnchors : attractors,
      currentAnchorKey,
      radius,
    );
  }

  function patchIdeaOrbitAnchorInStore(ideaId: string, anchorType: string | null, anchorId: string | null) {
    cortex.ideas = cortex.ideas.map((idea: any) =>
      idea.id === ideaId
        ? {
            ...idea,
            orbit_anchor_type: anchorType,
            orbit_anchor_id: anchorId,
          }
        : idea,
    );
  }

  function applyLocalIdeaOrbitAnchor(node: OrbitNode, anchorType: string | null, anchorId: string | null) {
    node.orbit_anchor_type = anchorType;
    node.orbit_anchor_id = anchorId;
    clearOwnerOrbitLayout(node);
    clearAttractionCache([node]);
    patchIdeaOrbitAnchorInStore(node.id, anchorType, anchorId);
  }

  function restartOrbitSettle() {
    if (!simulation) return;
    simulation.alpha(Math.max(simulation.alpha(), 0.22)).restart();
  }

  function dragStart(e: any, d: any) {
    if (isPreviewIdeaId(d?.id)) return;
    _isDragging = true;
    dragTargetAnchorId = null;
    archiveDropIdeaId = null;
    setArchiveDragState(canArchiveNode(d), false);
    if (!e.active && simulation) simulation.alphaTarget(0.1).restart();
    d.fx = d.x; d.fy = d.y;
    d._dragOrigX = d.x; d._dragOrigY = d.y;
  }

  function dragging(e: any, d: any) {
    if (isPreviewIdeaId(d?.id)) return;
    d.fx = e.x; d.fy = e.y;
    const el = g.selectAll('.bubble-group').filter((d2: any) => d2.id === d.id);

    if (!isIdeaControlledByCurrentUser(d)) {
      dragTargetAnchorId = null;
      resetArchiveDragInteraction();
      el.select('.bubble-cloud').attr('transform', 'scale(1)');
      el.attr('opacity', (dd: any) => bubbleOpacity(dd.status));
      if (coreGroup) coreGroup.select('.core-circle').attr('filter', 'url(#core-glow)');
      return;
    }

    const orbitTarget = orbitDropTargetWithinRadius(e.x, e.y, d, HANDOFF_THRESHOLD);
    const archiveTarget = archiveBinTargetFromDrag(e, d);
    dragTargetAnchorId = orbitTarget?.id ?? null;
    archiveDropIdeaId = archiveTarget ? d.id : null;
    setArchiveDragState(canArchiveNode(d), Boolean(archiveTarget));

    if (archiveTarget) {
      el.select('.bubble-cloud').attr('transform', 'scale(0.76)');
      el.attr('opacity', 0.5);
      if (coreGroup) coreGroup.select('.core-circle').attr('filter', 'url(#core-glow)');
    } else if (orbitTarget) {
      el.select('.bubble-cloud').attr('transform', 'scale(1.04)');
      el.attr('opacity', 1);
      if (coreGroup) coreGroup.select('.core-circle').attr('filter', 'url(#core-glow)');
    } else {
      el.select('.bubble-cloud').attr('transform', 'scale(1)');
      el.attr('opacity', (dd: any) => bubbleOpacity(dd.status));
      if (coreGroup) coreGroup.select('.core-circle').attr('filter', 'url(#core-glow)');
    }
  }

  function animateArchiveToBin(d: OrbitNode, target: HTMLElement) {
    const el = g.selectAll('.bubble-group').filter((d2: any) => d2.id === d.id);
    const startX = typeof d.fx === 'number' ? d.fx : d.x;
    const startY = typeof d.fy === 'number' ? d.fy : d.y;
    const archiveTarget = archiveBinCenterWorldPoint(target);
    const duration = 340;
    const startTime = performance.now();
    archivingIdeaId = d.id;
    archivingIdeaProgress = 0;

    function archiveTick(t: number) {
      const elapsed = t - startTime;
      const progress = Math.min(elapsed / duration, 1);
      const eased = 1 - (1 - progress) ** 3;
      d.x = interpolateNumber(startX, archiveTarget.x, eased);
      d.y = interpolateNumber(startY, archiveTarget.y, eased);
      d.fx = d.x;
      d.fy = d.y;
      archivingIdeaProgress = eased;
      syncPrimitiveDragVisuals();

      if (progress < 1) {
        requestAnimationFrame(archiveTick);
        return;
      }

      d.fx = null;
      d.fy = null;
      archivingIdeaId = null;
      archivingIdeaProgress = 0;
      resetArchiveDragInteraction();
      cortex.deleteIdea(d.id);
      el.remove();
      if (simulation) simulation.alpha(0.3).restart();
    }

    requestAnimationFrame(archiveTick);
  }

  function dragEnd(e: any, d: any) {
    if (isPreviewIdeaId(d?.id)) {
      d.fx = null;
      d.fy = null;
      _isDragging = false;
      dragTargetAnchorId = null;
      resetArchiveDragInteraction();
      if (!e.active && simulation) simulation.alphaTarget(idleOrbitAlphaTarget());
      if (simulation) simulation.alpha(0.1).restart();
      return;
    }

    const archiveTarget = archiveBinTargetFromDrag(e, d);
    _isDragging = false;
    dragTargetAnchorId = null;
    resetArchiveDragInteraction();
    if (!e.active && simulation) simulation.alphaTarget(idleOrbitAlphaTarget());
    if (coreGroup) coreGroup.select('.core-circle').attr('filter', 'url(#core-glow)');

    const dragDx = d.x - (d._dragOrigX || d.x);
    const dragDy = d.y - (d._dragOrigY || d.y);
    const dragDist = Math.sqrt(dragDx * dragDx + dragDy * dragDy);

    const dropX = typeof d.fx === 'number' ? d.fx : d.x;
    const dropY = typeof d.fy === 'number' ? d.fy : d.y;
    const el = g.selectAll('.bubble-group').filter((d2: any) => d2.id === d.id);

    if (!isIdeaControlledByCurrentUser(d)) {
      restoreDraggedNodeToOrigin(d);
      el.select('.bubble-cloud').transition().duration(200).attr('transform', 'scale(1)');
      el.transition().duration(200).attr('opacity', (dd: any) => bubbleOpacity(dd.status));
      syncPrimitiveDragVisuals();
      if (simulation) simulation.alpha(0.22).restart();
      return;
    }

    const orbitTarget = dragDist > 30
      ? orbitDropTargetWithinRadius(dropX, dropY, d, HANDOFF_THRESHOLD)
      : null;
    const orbitAnchor = orbitAnchorRefForAttractor(orbitTarget);

    if (archiveTarget && dragDist > 30) {
      animateArchiveToBin(d, archiveTarget);
    } else if (orbitAnchor) {
      d.x = dropX; d.y = dropY;
      d.fx = null; d.fy = null;

      const previousAnchorType = d.orbit_anchor_type ?? null;
      const previousAnchorId = d.orbit_anchor_id ?? null;
      applyLocalIdeaOrbitAnchor(d, orbitAnchor.kind, orbitAnchor.id);
      syncPrimitiveDragVisuals();
      if (isPreviewMemberId(orbitAnchor.id)) {
        restartOrbitSettle();
        return;
      }
      cortex.updateIdeaOrbitAnchor(d.id, orbitAnchor.kind, orbitAnchor.id).catch(() => {
        applyLocalIdeaOrbitAnchor(d, previousAnchorType, previousAnchorId);
        syncPrimitiveDragVisuals();
      });
      restartOrbitSettle();
    } else {
      d.fx = null; d.fy = null;
      el.select('.bubble-cloud').transition().duration(200).attr('transform', 'scale(1)');
      el.transition().duration(200).attr('opacity', (dd: any) => bubbleOpacity(dd.status));
      if (dragDist > 20) {
        cortex.updateIdeaPosition(d.id, d.x, d.y);
      }
      if (simulation) simulation.alpha(0.15).restart();
    }
  }

  // ── Long-press to pop (from cortex-interactions.js) ────────
  function setupLongPress(sel: any) {
    sel.each(function(this: any, d: any) {
      if (isPreviewIdeaId(d?.id)) return;
      const el = d3.select(this);
      let startTime = 0;
      let progressRing: any = null;
      let holdInterval: any = null;
      let popped = false;
      const threshold = visualStatus(d.status) === 'done' ? 500 : 1000;

      el.on('mousedown.pop', (e: MouseEvent) => {
        if (e.button !== 0) return;
        startTime = Date.now();
        popped = false;
        const r = dynamicRadius(d.salience_score, d.display_title || d.title, d.thread_count) + 5;
        const circ = 2 * Math.PI * r;
        progressRing = el.append('circle')
          .attr('r', r).attr('fill', 'none')
          .attr('stroke', 'white').attr('stroke-width', 2).attr('opacity', 0.5)
          .attr('stroke-dasharray', `0 ${circ}`)
          .attr('transform', 'rotate(-90)');

        holdInterval = setInterval(() => {
          const elapsed = Date.now() - startTime;
          const progress = Math.min(elapsed / threshold, 1);
          el.select('.bubble-cloud').attr('transform', `scale(${1 - progress * 0.15})`);
          progressRing.attr('stroke-dasharray', `${progress * circ} ${circ}`);
          if (progress >= 1 && !popped) {
            popped = true;
            clearInterval(holdInterval);
            popBubble(el, d);
          }
        }, 16);
      });

      const release = () => {
        clearInterval(holdInterval);
        if (!popped && progressRing) {
          el.select('.bubble-cloud').transition().duration(300)
            .ease(d3.easeElasticOut.amplitude(1).period(0.4))
            .attr('transform', 'scale(1)');
          progressRing.remove();
          progressRing = null;
        }
      };
      el.on('mouseup.pop', release);
      el.on('mouseleave.pop', release);
    });
  }

  function popBubble(el: any, d: any) {
    const bubbleColor = ownerColor(d);
    el.select('.bubble-cloud').transition().duration(100)
      .attr('transform', 'scale(1.3)')
      .transition().duration(100).attr('transform', 'scale(0)');
    el.select('.bubble-label').transition().duration(100).attr('opacity', 0);

    // Particle burst
    const particleG = g.append('g').attr('transform', `translate(${d.x},${d.y})`);
    for (let i = 0; i < 12; i++) {
      const angle = (i / 12) * Math.PI * 2 + (Math.random() - 0.5) * 0.5;
      const speed = 60 + Math.random() * 80;
      const size = 3 + Math.random() * 3;
      const p = particleG.append('circle').attr('r', size)
        .attr('fill', bubbleColor).attr('opacity', 0.8);
      p.transition().duration(800).ease(d3.easeCubicOut)
        .attr('cx', Math.cos(angle) * speed).attr('cy', Math.sin(angle) * speed)
        .attr('opacity', 0).attr('r', 0).on('end', () => p.remove());
    }
    setTimeout(() => particleG.remove(), 900);

    // Archive via API
    setTimeout(() => {
      cortex.deleteIdea(d.id);
      el.remove();
      if (simulation) { simulation.alpha(0.3).restart(); }
    }, 250);
  }

  // ── Thread peek tooltip ─────────────────────────────────────
  let peekEl: HTMLDivElement | null = null;
  let peekTimeout: any = null;

  function showPeek(d: any, screenX: number, screenY: number) {
    if (!d.thread || d.thread.length === 0) return;
    const last = d.thread[d.thread.length - 1];
    const content = (last.content || '').slice(0, 120) + (last.content?.length > 120 ? '…' : '');
    const role = last.role === 'illo' ? '🎯 Illo' : last.role === 'user' ? '👤 You' : '🤖';

    if (!peekEl) {
      peekEl = document.createElement('div');
      peekEl.className = 'cortex-peek-tooltip';
      containerEl.appendChild(peekEl);
    }
    const safe = content.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
    peekEl.innerHTML = `<span class="peek-role">${role}</span> ${safe}`;
    peekEl.style.left = `${Math.min(screenX + 15, window.innerWidth - 320)}px`;
    peekEl.style.top = `${screenY - 10}px`;
    peekEl.style.display = 'block';
    peekEl.style.opacity = '1';
  }

  function hidePeek() {
    if (peekTimeout) { clearTimeout(peekTimeout); peekTimeout = null; }
    if (peekEl) {
      peekEl.style.opacity = '0';
      setTimeout(() => { if (peekEl) peekEl.style.display = 'none'; }, 200);
    }
  }

  // ── Status ripple animation ─────────────────────────────────
  function renderStatusRipple(node: any, newStatus: string) {
    if (!g || node.x == null) return;
    const c = ownerColor(node);
    const r = dynamicRadius(node.salience_score, node.display_title || node.title, node.thread_count);
    const ripple = g.append('circle')
      .attr('cx', node.x).attr('cy', node.y).attr('r', r)
      .attr('fill', 'none').attr('stroke', c).attr('stroke-width', 3).attr('opacity', 0.7);
    ripple.transition().duration(1500).ease(d3.easeCubicOut)
      .attr('r', r + 60).attr('stroke-width', 0.5).attr('opacity', 0)
      .on('end', () => ripple.remove());
  }

  // ── Workspace focus reset ─────────────────────────────────────
  function handleFocusMode(_transform: any) {
    if (!g) return;
    g.selectAll('.bubble-group').interrupt()
      .attr('opacity', (d: any) => bubbleOpacity(d.status))
      .attr('filter', null as any);
    g.selectAll('.sun-group').interrupt()
      .attr('opacity', 1)
      .attr('filter', null as any);
  }

  // ── Connection highlighting ────────────────────────────────
  function highlightConns(id: string) {
    hoveredIdeaId = id;
    if (!USE_D3_SHADOW_SCENE) return;
    g.selectAll('.connection-path').classed('highlighted', (d: any) => {
      const s = typeof d.source === 'object' ? d.source.id : d.source;
      const t = typeof d.target === 'object' ? d.target.id : d.target;
      return s === id || t === id;
    });
  }
  function unhighlightConns() {
    hoveredIdeaId = null;
    if (!USE_D3_SHADOW_SCENE) return;
    g.selectAll('.connection-path').classed('highlighted', false);
  }

  // ── Breathing animation (from cortex-interactions.js) ──────
  function startBubbleBreathing(sel: any) {
    if (breathingFrame) {
      cancelAnimationFrame(breathingFrame);
      breathingFrame = null;
    }
    const phases = new Map<string, number>();
    const loopGeneration = renderGeneration;
    const startTime = performance.now();

    function tick(now: number) {
      if (loopGeneration !== renderGeneration) return;
      const t = (now - startTime) / 1000;
      const clouds = g ? g.selectAll('.bubble-cloud') : sel.selectAll('.bubble-cloud');
      clouds.each(function(this: any, d: any) {
        if (!phases.has(d.id)) phases.set(d.id, Math.random() * Math.PI * 2);
        const ph = phases.get(d.id) || 0;
        const state = visualStatus(d.status);
        const parent = d3.select((this as SVGElement).parentNode as Element);
        const isThreadSource = cortex.panelOpen && d.id === cortex.selectedIdeaId;

        parent.classed('thread-source-absorbed', isThreadSource);

        if (isThreadSource) {
          parent.selectAll('.working-ring').remove();
          parent.selectAll('.aurora-element').remove();
          syncBubblePulseScale(parent, 1);
          return;
        }

        if (state !== 'working') parent.selectAll('.aurora-element').remove();

        if (d._sceneState === 'birth') {
          parent.selectAll('.working-ring').remove();
          syncBubblePulseScale(parent, 1);
          return;
        }

        if (state === 'idle') {
          const sc = 1 + Math.sin(t / 8 * Math.PI * 2 + ph) * 0.003;
          syncBubblePulseScale(parent, sc);
          return;
        }

        if (state === 'done') {
          parent.selectAll('.working-ring').remove();
          const sc = 1 + Math.sin(t / 6 * Math.PI * 2 + ph) * 0.0025;
          syncBubblePulseScale(parent, sc);
          const cueCore = parent.select('.bubble-status-dot-core');
          if (!cueCore.empty()) {
            cueCore
              .attr('r', 4 + Math.max(0, Math.sin(t * 1.8 + ph)) * 0.25)
              .attr('opacity', 0.98);
          }
          return;
        }

        if (state === 'working') {
          const beatCycle = ((t * 1000 + ph * 140) % 1680) / 1680;
          const beatA = Math.max(0, 1 - Math.abs(beatCycle - 0.16) / 0.09);
          const beatB = Math.max(0, 1 - Math.abs(beatCycle - 0.31) / 0.06);
          const heartBeat = beatA * beatA + beatB * beatB * 0.68;

          parent.selectAll('.working-ring').remove();

          // Match the approved primitive's body pulse more closely.
          const workSc = 1.004 + heartBeat * 0.024;
          syncBubblePulseScale(parent, workSc);
          return; // skip default breathing below
        }

        parent.selectAll('.working-ring').remove();

        const period = 7 + (ph % 3);
        const sc = 1 + Math.sin(t / period * Math.PI * 2 + ph) * 0.005;
        syncBubblePulseScale(parent, sc);
      });
      breathingFrame = requestAnimationFrame(tick);
    }
    breathingFrame = requestAnimationFrame(tick);
  }

  // ── Render Canvas (from cortex-canvas.js renderCanvas) ─────
  function computeUserThreadLoadById(ideas: any[]): Record<string, number> {
    return ideas.reduce((acc: Record<string, number>, idea: any) => {
      if (idea?.archived_at || !idea?.user_id) return acc;
      const rawThreadCount = Number(idea.thread_count ?? 0);
      const normalizedThreadCount = Number.isFinite(rawThreadCount) ? rawThreadCount : 0;
      const ideaLoad = Math.max(1, normalizedThreadCount);
      acc[idea.user_id] = (acc[idea.user_id] || 0) + ideaLoad;
      return acc;
    }, {});
  }

  function computeOrbitAnchorThreadLoadById(ideas: any[]): Record<string, number> {
    return ideas.reduce((acc: Record<string, number>, idea: any) => {
      if (idea?.archived_at) return acc;
      const anchorId = itemOrbitAnchorKey(idea);
      const rawThreadCount = Number(idea.thread_count ?? 0);
      const normalizedThreadCount = Number.isFinite(rawThreadCount) ? rawThreadCount : 0;
      const ideaLoad = Math.max(1, normalizedThreadCount);
      acc[anchorId] = (acc[anchorId] || 0) + ideaLoad;
      return acc;
    }, {});
  }

  function currentAttractorLayoutOptions(): AttractorLayoutOptions {
    const visibleIdeas = cortex.ideas.filter((idea: any) => !idea?.archived_at);
    const clusterExtents = clusterExtentByUser(visibleIdeas);
    const userClusterExtents = Object.fromEntries(
      Object.entries(clusterExtents).filter(([anchorId]) => !anchorId.startsWith('pin:')),
    );
    return {
      clusterExtentByUserId: userClusterExtents,
      clusterExtentByAnchorId: clusterExtents,
      loadByUserId: computeUserThreadLoadById(cortex.ideas),
      loadByAnchorId: computeOrbitAnchorThreadLoadById(cortex.ideas),
    };
  }

  function fixedSunLayoutOptions(): AttractorLayoutOptions {
    return {};
  }

  function buildSunLayoutKey(members: any[]): string {
    return JSON.stringify({
      memberIds: (members || []).map((member: any) => member.id),
    });
  }

  function refreshAttractors(w: number, h: number): boolean {
    const members = sortTeamMembersForSharedAttractorLayout(workspaceAttractorMembers());
    const options = fixedSunLayoutOptions();
    const nextLayoutKey = buildSunLayoutKey(members);
    const changed = nextLayoutKey !== sunLayoutKey;
    attractors = createAttractors(members, w, h, options);
    attractorLookup = buildLookup(attractors);
    rebuildOrbitAnchors();
    sunLayoutKey = nextLayoutKey;
    return changed;
  }

  function syncRenderedSunData() {
    if (!g) return;
    const byId = new Map(attractors.map((attractor) => [attractor.id, attractor] as const));

    g.selectAll<SVGGElement, any>('.sun-group').each(function(a: any) {
      const element = this as SVGGElement;
      const id = typeof a?.id === 'string' ? a.id : element.getAttribute('data-user-id') || '';
      const next = byId.get(id);
      if (!next) return;

      const sg = d3.select(element).datum(next);
      const firstName = String(next.name || '').split(/\s+/)[0] || next.initial;
      sg
        .classed('is-current-user', next.id === auth.user?.id)
        .attr('data-user-id', next.id);
      sg.select('.sun-initial').text(next.initial);
      sg.select('.sun-label').text(`✦ ${firstName}`);
      applySunVisualState(sg as any, next.color, attractorActivity(next.id));
    });
  }

  function renderCanvas({ preserveViewport = false }: { preserveViewport?: boolean } = {}) {
    if (!containerEl) return;
    const preservedTransform = preserveViewport ? untrack(() => currentZoomTransform) : null;
    resetRenderRuntime();
    const renderToken = renderGeneration;
    const rect = containerEl.getBoundingClientRect();
    canvasW = rect.width;
    canvasH = rect.height;
    coreX = canvasW / 2;
    coreY = canvasH / 2;
    containerEl.innerHTML = '';

    svg = d3.select(containerEl).append('svg')
      .attr('width', '100%').attr('height', '100%')
      .attr('viewBox', `0 0 ${canvasW} ${canvasH}`) as any;

    svg.on('contextmenu', (e: MouseEvent) => handleWorkspaceContextMenu(e));
    createDefs();

    // Zoom
    zoomBehavior = d3.zoom<SVGSVGElement, unknown>().scaleExtent([0.25, 4]).on('zoom', (e) => {
      if (e.sourceEvent) {
        hasUserAdjustedViewport = true;
      }
      currentZoomTransform = e.transform;
      updateSemanticZoomLevel(e.transform.k);
      g.attr('transform', e.transform as any);
      updatePrimitiveOverlayTransform();
      syncPrimitiveMotionVisuals(simulation?.nodes() as OrbitNode[] ?? Array.from(orbitSceneNodesById.values()));
      handleFocusMode(e.transform);
      emitWorkspaceContext();
    });
    svg.call(zoomBehavior);
    svg.on('dblclick.zoom', null);

    // Click background to leave the active thread surface.
    svg.on('click', (e: MouseEvent) => {
      if ((e.target as Element).tagName !== 'svg') return;
      cortex.selectIdea(null);
    });

    svg.on('dblclick', (e: MouseEvent) => {
      if ((e.target as Element).tagName === 'svg') e.preventDefault();
    });

    g = svg.append('g');
    // ── User astres; default Cortex core is only a no-user fallback. ──
    refreshAttractors(canvasW, canvasH);

    if (USE_D3_SHADOW_SCENE && attractors.length > 0) {
      renderMultiSuns(g, defs);

      if (!preserveViewport) {
        svg.call(zoomBehavior.transform, startupViewportTransform(canvasW, canvasH));
      }
    } else if (USE_D3_SHADOW_SCENE) {
      // Fallback only for unauthenticated/system contexts.
      renderSingleCore(g);
    } else if (!preserveViewport && attractors.length > 0) {
      svg.call(zoomBehavior.transform, startupViewportTransform(canvasW, canvasH));
    }

    if (preserveViewport && preservedTransform) {
      svg.call(zoomBehavior.transform, preservedTransform);
    }
    emitWorkspaceContext();

    // ── Prepare data ──
    const visible = syncSceneNodes(cortex.filteredIdeas);
    clearAttractionCache(visible);

    const visibleIds = new Set(visible.map((i: any) => i.id));
    const visibleById = new Map(visible.map((idea: any) => [idea.id, idea] as const));
    const linkData = cortex.connections
      .filter((c: any) => visibleIds.has(c.source_id) && visibleIds.has(c.target_id))
      .map((c: any) => {
        const sourceIdea = visibleById.get(c.source_id);
        const targetIdea = visibleById.get(c.target_id);

        return {
          ...c,
          source: c.source_id,
          target: c.target_id,
          sourceUserId: sourceIdea?.user_id,
          targetUserId: targetIdea?.user_id,
          sourceAccent: sourceIdea ? ownerColor(sourceIdea) : undefined,
          targetAccent: targetIdea ? ownerColor(targetIdea) : undefined,
        };
      });

    simulation = createSim(visible, linkData, canvasW, canvasH);

    const linkSel = USE_D3_SHADOW_SCENE
      ? g.selectAll('.connection-path').data(linkData, (d: any) => d.id)
        .join('path')
        .attr('class', connectionPathClass)
        .attr('style', connectionPathStyle)
      : null;

    const bubbleSel = USE_D3_SHADOW_SCENE
      ? g.selectAll('.bubble-group').data(visible, (d: any) => d.id)
        .join('g').attr('class', 'bubble-group entering')
        .call(d3.drag<SVGGElement, any>().on('start', dragStart).on('drag', dragging).on('end', dragEnd) as any)
      : null;

    if (bubbleSel) {
      // Staggered fade-in
      bubbleSel.each(function(_d: any, i: number) {
        setTimeout(() => d3.select(this as Element).classed('entering', false), 100 + i * 50);
      });

      // Render all bubble visual content using shared helper
      const queuePosMap = computeQueuePositions(visible);
      renderBubbleContent(bubbleSel as d3.Selection<SVGGElement, any, any, any>, queuePosMap);
    }
    syncPrimitiveOrbitVisuals(visible);

    if (bubbleSel) {
      // ── Interactions ──
      // Long-press to pop
      setupLongPress(bubbleSel);

      bubbleSel
        .on('click', (e: Event, d: any) => {
          e.stopPropagation();
          if (isPreviewIdeaId(d?.id)) return;
          const mouse = e as MouseEvent;
          onthreadopen?.({ x: mouse.clientX, y: mouse.clientY, id: d.id });
          cortex.selectIdea(d.id);
        })
        .on('mouseover', function(e: any, d: any) {
          d3.select(this).raise();
          highlightConns(d.id);
          peekTimeout = setTimeout(() => showPeek(d, e.clientX, e.clientY), 500);
        })
        .on('mouseout', function() {
          unhighlightConns();
          hidePeek();
        });

      // Double-click edit
      bubbleSel.on('dblclick', (e: Event, d: any) => {
        e.stopPropagation();
        openTitleEditor(d);
      });
    }

    // Simulation tick
    simulation.on('tick', () => {
      if (renderToken !== renderGeneration) return;
      const now = performance.now();
      if (simulationPaintLastAt && now - simulationPaintLastAt < simulationPaintIntervalMs()) return;
      simulationPaintLastAt = now;
      if (linkSel) linkSel.attr('d', curvedLinkPath);
      if (USE_D3_SHADOW_SCENE) {
        const tickBubbleSel = g.selectAll<SVGGElement, any>('.bubble-group');
        tickBubbleSel.attr('transform', (d: OrbitNode) => sceneTransform(d, now));
      }
      schedulePrimitiveOrbitVisuals(simulation?.nodes() as OrbitNode[] ?? Array.from(orbitSceneNodesById.values()));

    });

    const applySemanticClustering = (data: any) => {
      if (renderToken !== renderGeneration) return;
      if (!data.pairs || !simulation) return;
      const nodesById = new Map<string, any>(
        simulation.nodes().map((node: any) => [String(node.id), node]),
      );
      const semanticPairs: Array<{ source: any; target: any; sim: number }> = [];
      for (const p of data.pairs) {
        const sim = Number(p.sim);
        if (!Number.isFinite(sim) || sim < 0.4) continue;
        const source = nodesById.get(String(p.a));
        const target = nodesById.get(String(p.b));
        if (!source || !target || source === target) continue;
        semanticPairs.push({ source, target, sim });
      }
      if (!semanticPairs.length) return;
      simulation.force('semantic', (alpha: number) => {
        for (const pair of semanticPairs) {
          const dx = pair.target.x - pair.source.x;
          const dy = pair.target.y - pair.source.y;
          const dist = Math.sqrt(dx * dx + dy * dy) || 1;
          const strength = (pair.sim - 0.4) * 0.003 * alpha;
          pair.source.vx += (dx / dist) * strength;
          pair.source.vy += (dy / dist) * strength;
          pair.target.vx -= (dx / dist) * strength;
          pair.target.vy -= (dy / dist) * strength;
        }
      });
      simulation.alpha(0.18).restart();
    };

    // Semantic clustering is progressive enhancement: keep the initial orbit
    // interactive first, then hydrate the extra force when the browser is idle.
    const scheduleSemanticClustering = (attempt = 0) => {
      window.setTimeout(() => {
        if (renderToken !== renderGeneration) return;
        if (cortex.panelOpen && attempt < 20) {
          scheduleSemanticClustering(attempt + 1);
          return;
        }
        runWhenBrowserIdle(() => {
          if (renderToken !== renderGeneration || cortex.panelOpen) return;
          loadSimilarityMatrix().then(applySemanticClustering).catch(() => {});
        }, 1800);
      }, attempt === 0 ? 900 : 220);
    };
    scheduleSemanticClustering();

    // Save positions when simulation settles
    simulation.on('end', () => {
      if (renderToken !== renderGeneration) return;
      const positions = collectSceneIdeaPositions(
        simulation?.nodes() as OrbitNode[] ?? [],
        isPreviewIdeaId,
      );
      persistSceneIdeaPositions(positions).catch(() => {});
    });
  }

  // ── Lifecycle ──────────────────────────────────────────────
  let paused = false;
  let flowState = $state(false);
  let flowCheckInterval: any = null;

  function checkFlowState() {
    const panelOpen = cortex.panelOpen;
    const activeInput = document.activeElement?.tagName === 'TEXTAREA';
    if (panelOpen && activeInput && !flowState) {
      flowState = true;
      g?.selectAll('.bubble-group')
        .filter((d: any) => d.id !== cortex.selectedIdeaId)
        .transition().duration(600).attr('opacity', 0.38);
    } else if ((!panelOpen || !activeInput) && flowState) {
      flowState = false;
      g?.selectAll('.bubble-group').transition().duration(600)
        .attr('opacity', (d: any) => bubbleOpacity(d.status));
    }
  }

  function handleFitView() {
    if (!svg || !zoomBehavior) return;
    hasUserAdjustedViewport = false;
    svg.transition().duration(500).call(zoomBehavior.transform as any, startupViewportTransform());
  }

  function handleTogglePause() {
    paused = !paused;
    if (paused) {
      renderGeneration += 1;
      cancelRenderFrames();
      stopActiveSimulation();
    } else {
      renderCanvas({ preserveViewport: true });
    }
  }

  // Track rendered idea IDs to detect new ones
  let renderedIds = new Set<string>();
  let initialIdeaHydrationComplete = false;

  function seedRenderedIdeaTracking(ideas: any[]) {
    renderedIds = new Set();
    nodeSnapshotMap = new Map();
    for (const idea of ideas) {
      renderedIds.add(idea.id);
      nodeSnapshotMap.set(idea.id, ideaSnapshot(idea));
    }
  }

  /**
   * Add a single new bubble to the existing SVG with birth animation from core.
   * Called reactively when cortex.ideas changes.
   */
  function addBubbleBirth(idea: any) {
    if (!g || !simulation) return;

    const totalIdeas = cortex.filteredIdeas.filter((entry: any) => !entry?.archived_at).length;
    const sceneNode = ensureSceneNode(idea, totalIdeas, { birth: true });

    const nodes = simulation.nodes() as OrbitNode[];
    if (!nodes.some((node) => node.id === sceneNode.id)) {
      nodes.push(sceneNode);
      simulation.nodes(nodes);
    }
    if (!workspaceMotionSuspended) {
      simulation.alpha(0.5).restart();
    }

    if (!USE_D3_SHADOW_SCENE) {
      if (workspaceMotionSuspended) {
        collapseBirthAnimation(sceneNode);
      }
      syncPrimitiveOrbitVisuals(Array.from(orbitSceneNodesById.values()));
      syncPrimitiveMotionVisuals(simulation.nodes() as OrbitNode[]);
      return;
    }

    const r = ideaRadius(sceneNode);
    const qPosMap = computeQueuePositions(cortex.filteredIdeas.filter((entry: any) => !entry?.archived_at));

    const bubble = g.append('g')
      .datum(sceneNode)
      .attr('class', 'bubble-group')
      .attr('transform', `translate(${sceneNode.x},${sceneNode.y})`)
      .call(d3.drag<SVGGElement, any>().on('start', dragStart).on('drag', dragging).on('end', dragEnd) as any);

    renderBubbleContent(bubble, qPosMap);
    syncPrimitiveOrbitVisuals(Array.from(orbitSceneNodesById.values()));

    if (workspaceMotionSuspended) {
      bubble.select('.bubble-cloud')
        .attr('opacity', bubbleOpacity(sceneNode.status))
        .attr('transform', 'scale(1)');
      bubble.select('.bubble-label').attr('opacity', 1);
    } else {
      bubble.select('.bubble-cloud')
        .attr('opacity', 0).attr('transform', 'scale(0)')
        .transition('bubble-birth-cloud').duration(600).ease(d3.easeCubicOut)
        .attr('opacity', bubbleOpacity(sceneNode.status))
        .attr('transform', 'scale(1)');
      bubble.select('.bubble-label')
        .attr('opacity', 0)
        .transition('bubble-birth-label').delay(300).duration(400).attr('opacity', 1);
    }

    bubble
      .on('click', (e: Event) => {
        e.stopPropagation();
        if (isPreviewIdeaId(sceneNode?.id)) return;
        const mouse = e as MouseEvent;
        onthreadopen?.({ x: mouse.clientX, y: mouse.clientY, id: sceneNode.id });
        cortex.selectIdea(sceneNode.id);
      })
      .on('mouseover', function(e: any) {
        d3.select(this).raise();
        highlightConns(sceneNode.id);
        peekTimeout = setTimeout(() => showPeek(sceneNode, e.clientX, e.clientY), 500);
      })
      .on('mouseout', function() {
        unhighlightConns(); hidePeek();
      });

    bubble.on('dblclick', (e: Event) => {
      e.stopPropagation();
      openTitleEditor(sceneNode);
    });

    setupLongPress(bubble);

    if (coreGroup) {
      coreGroup.select('.core-circle').transition().duration(150).attr('r', CORE_RADIUS * 1.08)
        .transition().duration(150).attr('r', CORE_ASTRE_INNER_RADIUS);
    }
  }

  onMount(() => {
    warnIfCompetingVisibleWorkspaceOwnerExists();
    renderCanvas();
    // Seed renderedIds with initial ideas
    const initialIdeas = cortex.filteredIdeas.filter((entry: any) => !entry?.archived_at);
    seedRenderedIdeaTracking(initialIdeas);
    initialIdeaHydrationComplete = !cortex.loading || initialIdeas.length > 0;
    window.addEventListener('cortex-fit-view', handleFitView);
    window.addEventListener('cortex-toggle-pause', handleTogglePause);
    flowCheckInterval = setInterval(checkFlowState, 2000);
  });

  // Re-initialize attractors when teamMembers loads or user-owned cluster loads change.
  $effect(() => {
    const members = cortex.teamMembers;
    if (members && members.length > 0 && svg) {
      const previousAttractorMap = new Map(
        attractors.map((a) => [a.id, { x: a.x, y: a.y }]),
      );
      const previousAttractorIds = attractors.map((a) => a.id).sort().join('|');
      const hadNoAttractors = attractors.length === 0;
      const rect = svg.node()?.getBoundingClientRect?.() || { width: canvasW, height: canvasH };
      const w = rect.width || canvasW;
      const h = rect.height || canvasH;
      const layoutChanged = refreshAttractors(w, h);
      const nextAttractorIds = attractors.map((a) => a.id).sort().join('|');
      const topologyChanged = previousAttractorIds !== nextAttractorIds;
      emitWorkspaceContext();
      // Clear cached attraction targets so nodes re-evaluate their sun
      if (simulation) {
        clearAttractionCache(simulation.nodes());
      }
      if (layoutChanged && previousAttractorMap.size > 0) {
        for (const node of orbitSceneNodesById.values()) {
          if (!node.user_id || node._threadAnchorPinned) continue;
          if (itemOrbitAnchorKey(node) !== node.user_id) continue;
          const previous = previousAttractorMap.get(node.user_id);
          const next = attractorLookup.byId.get(node.user_id);
          if (!previous || !next || !Number.isFinite(node.x) || !Number.isFinite(node.y)) continue;
          const dx = next.x - previous.x;
          const dy = next.y - previous.y;
          node.x += dx;
          node.y += dy;
          if (typeof node._birthFromX === 'number') node._birthFromX += dx;
          if (typeof node._birthFromY === 'number') node._birthFromY += dy;
        }
      }
      if (topologyChanged && attractors.length > 0) {
        orbitSceneNodesById = new Map();
      }
      // If we were showing single-sun fallback, do a full re-render to switch
      // to multi-sun visuals, update forceCenter, and reposition nodes.
      // Also re-render when dual-user load changes move the suns.
      if (hadNoAttractors || layoutChanged) {
        renderCanvas({ preserveViewport: !hadNoAttractors && hasUserAdjustedViewport });
      } else {
        syncRenderedSunData();
        refreshOrbitVisualState();
      }
    }
  });

  $effect(() => {
    const nextPinKey = pins
      .filter((pin) => !pin.archived_at)
      .map((pin) => `${pin.id}:${pin.label}:${pin.color}:${Math.round(pin.position_x)}:${Math.round(pin.position_y)}`)
      .sort()
      .join('|');
    if (!svg || nextPinKey === pinLayoutKey) return;
    pinLayoutKey = nextPinKey;
    for (const pin of pins) {
      localPinPositions.delete(pin.id);
    }
    rebuildOrbitAnchors();
    const nodes = simulation?.nodes() as OrbitNode[] | undefined;
    if (nodes) clearAttractionCache(nodes);
    refreshOrbitVisualState();
    if (simulation) simulation.alpha(0.24).restart();
  });

  $effect(() => {
    auth.user?.id;
    if (svg) {
      emitWorkspaceContext();
    }
  });

  $effect(() => {
    const appLayoutKey = apps
      .map((app) => [
        app.id,
        app.updated_at,
        app.anchor_user_id ?? '',
        app.visual_spec?.orbit_anchor_type ?? '',
        app.visual_spec?.orbit_anchor_id ?? '',
        app.visual_spec?.position_x ?? '',
        app.visual_spec?.position_y ?? '',
      ].join(':'))
      .join('|');
    activeAppId;
    if (svg) {
      syncPrimitiveOrbitVisuals();
      if (simulation && !workspaceMotionSuspended) {
        simulation.alpha(Math.max(simulation.alpha(), 0.12)).restart();
      }
    }
  });

  $effect(() => {
    const panelOpen = cortex.panelOpen;
    const selectedId = cortex.selectedIdeaId;
    const selectedIdea = cortex.selectedIdea;

    if (!svg || !zoomBehavior) return;

    if (panelOpen && selectedId && selectedIdea) {
      const target = threadFocusTransform(selectedIdea);
      if (!target) return;

      if (!threadRestoreTransform) {
        threadRestoreTransform = untrack(() => currentZoomTransform);
      }

      if (threadFocusedIdeaId !== selectedId) {
        threadFocusedIdeaId = selectedId;
        animateViewport(target, 680);
      }
      return;
    }

    if (!panelOpen && threadFocusedIdeaId) {
      const restore = threadRestoreTransform ?? d3.zoomIdentity;
      threadFocusedIdeaId = null;
      threadRestoreTransform = null;
      animateViewport(restore, 220);
    }
  });

  $effect(() => {
    const panelOpen = cortex.panelOpen;
    const selectedId = cortex.selectedIdeaId;

    if (!simulation) return;
    const nodes = simulation.nodes() as OrbitNode[];

    if (panelOpen && selectedId) {
      const anchorNode = nodes.find((node) => node.id === selectedId);
      if (!anchorNode) return;
      collapseBirthAnimation(anchorNode);

      if (!threadAnchorState || threadAnchorState.id !== selectedId) {
        const prevAnchorState = threadAnchorState;
        if (prevAnchorState) {
          const prevNode = nodes.find((node) => node.id === prevAnchorState.id);
          if (prevNode) {
            prevNode.fx = prevAnchorState.fx;
            prevNode.fy = prevAnchorState.fy;
            delete prevNode._threadAnchorPinned;
          }
        }

        threadAnchorState = {
          id: selectedId,
          fx: anchorNode.fx ?? null,
          fy: anchorNode.fy ?? null,
        };
        anchorNode.fx = anchorNode.x;
        anchorNode.fy = anchorNode.y;
        anchorNode._threadAnchorPinned = true;
        refreshCollisionForce(0.22);
        handleFocusMode(currentZoomTransform);
      }
      return;
    }

    if (threadAnchorState) {
      const prevNode = nodes.find((node) => node.id === threadAnchorState?.id);
      if (prevNode) {
        prevNode.fx = threadAnchorState.fx;
        prevNode.fy = threadAnchorState.fy;
        delete prevNode._threadAnchorPinned;
      }
      threadAnchorState = null;
      refreshCollisionForce(0.14);
      handleFocusMode(currentZoomTransform);
    }

    releaseThreadAnchors(nodes);
  });

  $effect(() => {
    const panelOpen = cortex.panelOpen;
    const selectedId = cortex.selectedIdeaId;
    if (!svg) return;

    if (panelOpen && selectedId) {
      suspendWorkspaceMotion();
      return;
    }

    resumeWorkspaceMotion();
  });

  // Track node properties for change detection
  let nodeSnapshotMap = new Map<string, {
    status: string;
    title: string;
    display_title?: string;
    salience_score: number;
    attachments_count: number;
    thread_count: number;
    user_id?: string;
    orbit_anchor_type?: string | null;
    orbit_anchor_id?: string | null;
    author_name?: string;
    author_color?: string;
    user_color?: string;
  }>();

  function ideaSnapshot(idea: any) {
    return {
      status: idea.status, title: idea.title, display_title: idea.display_title,
      salience_score: idea.salience_score || 5,
      attachments_count: (idea.attachments || []).length,
      thread_count: idea.thread_count || 0, user_id: idea.user_id,
      orbit_anchor_type: idea.orbit_anchor_type ?? null,
      orbit_anchor_id: idea.orbit_anchor_id ?? null,
      author_name: idea.author_name,
      author_color: idea.author_color,
      user_color: idea.user_color,
    };
  }

  function snapshotChanged(a: any, b: any): boolean {
    return a.status !== b.status || a.title !== b.title || a.display_title !== b.display_title
      || a.salience_score !== b.salience_score
      || a.attachments_count !== b.attachments_count
      || a.thread_count !== b.thread_count || a.user_id !== b.user_id
      || a.orbit_anchor_type !== b.orbit_anchor_type || a.orbit_anchor_id !== b.orbit_anchor_id
      || a.author_name !== b.author_name || a.author_color !== b.author_color
      || a.user_color !== b.user_color;
  }

  $effect(() => {
    const ideas = cortex.filteredIdeas.filter((idea: any) => !idea?.archived_at);

    if (!initialIdeaHydrationComplete && cortex.loading && renderedIds.size === 0) {
      return;
    }

    if (!initialIdeaHydrationComplete && !cortex.loading) {
      initialIdeaHydrationComplete = true;
      if (ideas.length > 0 && renderedIds.size === 0 && svg) {
        renderCanvas({ preserveViewport: hasUserAdjustedViewport });
        seedRenderedIdeaTracking(ideas);
        return;
      }
    }

    const currentIds = new Set(ideas.map((i: any) => i.id));
    syncSceneNodes(ideas);
    const queuePosMap = computeQueuePositions(ideas);

    for (const idea of ideas) {
      if (!renderedIds.has(idea.id)) {
        renderedIds.add(idea.id);
        nodeSnapshotMap.set(idea.id, ideaSnapshot(idea));
        addBubbleBirth(idea);
      } else {
        const sceneNode = ensureSceneNode(idea, ideas.length);
        const prevSnap = nodeSnapshotMap.get(idea.id);
        const currSnap = ideaSnapshot(idea);

        if (prevSnap && snapshotChanged(prevSnap, currSnap)) {
          nodeSnapshotMap.set(idea.id, currSnap);
          if (
            prevSnap.orbit_anchor_type !== currSnap.orbit_anchor_type
            || prevSnap.orbit_anchor_id !== currSnap.orbit_anchor_id
            || prevSnap.user_id !== currSnap.user_id
          ) {
            clearOwnerOrbitLayout(sceneNode);
            clearAttractionCache([sceneNode]);
          }

          if (USE_D3_SHADOW_SCENE) {
            g?.selectAll('.bubble-group')
              .filter((d: any) => d.id === idea.id)
              .each(function (this: any) {
                const bubbleEl = d3.select(this);
                bubbleEl.datum(sceneNode);
                updateBubbleVisuals(bubbleEl, sceneNode, queuePosMap);

                if (prevSnap!.status !== currSnap.status) {
                  renderStatusRipple(sceneNode, idea.status);
                }
              });
          } else if (prevSnap.status !== currSnap.status) {
            renderStatusRipple(sceneNode, idea.status);
          }

          refreshCollisionForce(0.15);
        } else if (USE_D3_SHADOW_SCENE) {
          g?.selectAll('.bubble-group')
            .filter((d: any) => d.id === idea.id)
            .each(function (this: any) {
              const bubbleEl = d3.select(this);
              bubbleEl.datum(sceneNode);
              bubbleEl.selectAll('.queue-badge, .queue-badge-text').remove();
              const pos = queuePosMap.get(sceneNode.id);
              if (pos) {
                const r = ideaRadius(sceneNode);
                bubbleEl.append('circle').attr('class', 'queue-badge')
                  .attr('cx', -r * 0.55).attr('cy', -r * 0.55).attr('r', 8)
                  .attr('fill', 'rgba(124,185,232,0.85)').attr('stroke', '#1a1a2e').attr('stroke-width', 1);
                bubbleEl.append('text').attr('class', 'queue-badge-text')
                  .attr('x', -r * 0.55).attr('y', -r * 0.55 + 3)
                  .attr('text-anchor', 'middle').attr('fill', '#fff')
                  .attr('font-size', '8px').attr('font-weight', 'bold').text('#' + pos);
              }
            });
        }
      }
    }

    refreshOrbitVisualState();

    for (const id of renderedIds) {
      if (!currentIds.has(id)) {
        renderedIds.delete(id);
        nodeSnapshotMap.delete(id);
        orbitSceneNodesById.delete(id);
        if (USE_D3_SHADOW_SCENE) {
          g?.selectAll('.bubble-group').filter((d: any) => d.id === id)
            .transition().duration(300).attr('opacity', 0)
            .on('end', function() { d3.select(this as Element).remove(); });
        }
        if (simulation) {
          const nodes = (simulation.nodes() as OrbitNode[]).filter((n) => n.id !== id);
          simulation.nodes(nodes);
          clearAttractionCache(nodes);
          simulation.alpha(0.2).restart();
        }
      }
    }
  });

  $effect(() => {
    const typingEntries = Array.from(cortex.typingUsers.values()).map((entry) => `${entry.user_id}:${entry.idea_id}`).join('|');
    typingEntries;
    refreshOrbitVisualState();
  });

  onDestroy(() => {
    resetArchiveDragInteraction();
    resetRenderRuntime();
    if (flowCheckInterval) clearInterval(flowCheckInterval);
    cortex.setBirthContext(null);
    window.removeEventListener('cortex-fit-view', handleFitView);
    window.removeEventListener('cortex-toggle-pause', handleTogglePause);
  });
</script>

<div class="cortex-svg-container primitive-orbit-handoff">
  <div
    class="cortex-svg-d3-layer"
    data-cortex-workspace-render-owner="workspace-scene-d3"
    bind:this={containerEl}
  ></div>

  <WorkspaceOrbitPrimitives
    bind:overlayEl={primitiveOverlayEl}
    overlayStyle={primitiveOverlayTransformStyle()}
    orbitLaneVisuals={primitiveOrbitLaneVisuals}
    astreVisuals={primitiveAstreVisuals}
    pinVisuals={primitivePinVisuals}
    appVisuals={primitiveAppVisuals}
    blobVisuals={primitiveBlobVisuals}
    panelOpen={cortex.panelOpen}
    {semanticZoomLevel}
    themeMode={theme.mode}
    movingPinId={primitivePinDragState?.pinId ?? null}
    blobText={semanticBlobText}
    astreClass={primitiveAstreClass}
    astreStyle={primitiveAstreStyle}
    pinActive={primitivePinIsLit}
    pinStyle={primitivePinStyle}
    appStyle={primitiveAppObjectStyle}
    blobStyle={primitiveBubbleScreenStyle}
    setHoveredAstre={(id) => {
      hoveredAstreId = id;
    }}
    clearHoveredAstre={(id) => {
      if (hoveredAstreId === id) hoveredAstreId = null;
    }}
    setHoveredPin={(id) => {
      hoveredPinId = id;
    }}
    clearHoveredPin={(id) => {
      if (hoveredPinId === id) hoveredPinId = null;
    }}
    activateAstre={(astre, event) => {
      event.stopPropagation();
      const target = event.currentTarget as HTMLElement | null;
      const rect = target?.getBoundingClientRect();
      onownastreclick?.({
        x: event.clientX || (rect ? rect.left + rect.width / 2 : 0),
        y: event.clientY || (rect ? rect.top + rect.height / 2 : 0),
        userId: astre.id,
      });
    }}
    activatePin={activatePrimitivePin}
    beginPinDrag={beginPrimitivePinDrag}
    movePinDrag={movePrimitivePinDrag}
    endPinDrag={endPrimitivePinDrag}
    activateApp={activatePrimitiveApp}
    beginAppDrag={beginPrimitiveAppDrag}
    moveAppDrag={movePrimitiveAppDrag}
    endAppDrag={endPrimitiveAppDrag}
    activateBlob={activatePrimitiveBlob}
    editBlob={editPrimitiveBlob}
    popBlob={popPrimitiveBlob}
    hoverBlob={(blob) => highlightConns(blob.id)}
    unhoverBlob={unhighlightConns}
    beginBlobDrag={(blob, event) => beginPrimitiveBlobDrag(blob.id, event)}
    moveBlobDrag={(blob, event) => movePrimitiveBlobDrag(blob.id, event)}
    endBlobDrag={(blob, event) => endPrimitiveBlobDrag(blob.id, event)}
  />
</div>
