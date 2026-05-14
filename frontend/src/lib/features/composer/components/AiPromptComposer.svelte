<script lang="ts">
  import { tick } from 'svelte';

  import {
    findSlashCommandToken,
    replaceSlashCommandToken,
    type SlashCommandToken,
  } from '$lib/utils/slashCommand';
  import { hasSkillMention } from '$lib/utils/skillMention';

  import SkillMentionOverlay from './SkillMentionOverlay.svelte';
  import SlashAutocomplete from './SlashAutocomplete.svelte';

  type SlashPlacement = 'above' | 'below';

  type Props = {
    value?: string;
    textarea?: HTMLTextAreaElement | undefined;
    placeholder?: string;
    ariaLabel?: string;
    disabled?: boolean;
    rows?: number;
    minHeight?: number;
    maxHeight?: number;
    className?: string;
    textareaClassName?: string;
    submitOnEnter?: boolean;
    slashPlacement?: SlashPlacement;
    onInput?: (event: Event, token: SlashCommandToken | null) => void;
    onCursorChange?: (event: Event, token: SlashCommandToken | null) => void;
    onKeydown?: (event: KeyboardEvent) => void;
    onPaste?: (event: ClipboardEvent) => void;
    onSubmit?: (event: KeyboardEvent) => void;
    onEscape?: (event: KeyboardEvent) => void;
    onSlashTokenChange?: (token: SlashCommandToken | null) => void;
  };

  let {
    value = $bindable(''),
    textarea = $bindable(),
    placeholder = 'Ask Illo...',
    ariaLabel = 'Prompt',
    disabled = false,
    rows = 1,
    minHeight = 40,
    maxHeight = 140,
    className = '',
    textareaClassName = '',
    submitOnEnter = false,
    slashPlacement = 'above',
    onInput,
    onCursorChange,
    onKeydown,
    onPaste,
    onSubmit,
    onEscape,
    onSlashTokenChange,
  }: Props = $props();

  let slashRef: SlashAutocomplete | undefined = $state();
  let slashToken: SlashCommandToken | null = $state(null);
  let textareaScrollTop = $state(0);

  const rootClass = $derived(
    ['ai-prompt-composer', className].filter(Boolean).join(' '),
  );
  const textareaClass = $derived(
    [
      'ai-prompt-textarea',
      hasSkillMention(value) ? 'has-skill-mentions' : '',
      textareaClassName,
    ]
      .filter(Boolean)
      .join(' '),
  );
  const rootStyle = $derived(
    `--ai-prompt-min-height:${minHeight}px;--ai-prompt-max-height:${maxHeight}px;`,
  );

  function setSlashToken(token: SlashCommandToken | null) {
    slashToken = token;
    onSlashTokenChange?.(token);
  }

  function autoGrowTextarea() {
    if (!textarea) return;
    textarea.style.height = 'auto';
    const nextHeight = Math.max(minHeight, Math.min(textarea.scrollHeight, maxHeight));
    textarea.style.height = `${nextHeight}px`;
    textareaScrollTop = textarea.scrollTop;
  }

  async function syncTextareaHeight() {
    await tick();
    autoGrowTextarea();
  }

  $effect(() => {
    value;
    textarea;
    minHeight;
    maxHeight;
    void syncTextareaHeight();
  });

  $effect(() => {
    if (!textarea) return;
    textarea.setAttribute('autocorrect', 'off');
    textarea.setAttribute('autocapitalize', 'off');
  });

  function syncSlashAutocomplete() {
    const token = textarea
      ? findSlashCommandToken(value, textarea.selectionStart ?? value.length)
      : null;
    setSlashToken(token);
    if (token) slashRef?.filter(token.query);
    else slashRef?.clear();
    return token;
  }

  function applySlashCommand(cmd: string) {
    const token = slashToken ?? syncSlashAutocomplete();
    if (!token) {
      value = cmd;
      requestAnimationFrame(() => {
        textarea?.focus();
        autoGrowTextarea();
      });
      return;
    }

    const next = replaceSlashCommandToken(value, token, cmd);
    value = next.value;
    setSlashToken(null);
    slashRef?.clear();
    requestAnimationFrame(() => {
      textarea?.focus();
      textarea?.setSelectionRange(next.cursor, next.cursor);
      autoGrowTextarea();
    });
  }

  function handleInput(event: Event) {
    autoGrowTextarea();
    const token = syncSlashAutocomplete();
    onInput?.(event, token);
  }

  function handleCursorChange(event: Event) {
    const token = syncSlashAutocomplete();
    onCursorChange?.(event, token);
  }

  function handleKeydown(event: KeyboardEvent) {
    if (slashRef?.handleKey(event)) return;

    onKeydown?.(event);
    if (event.defaultPrevented) return;

    if (event.key === 'Escape') {
      onEscape?.(event);
      return;
    }

    if (submitOnEnter && event.key === 'Enter' && !event.shiftKey) {
      event.preventDefault();
      onSubmit?.(event);
    }
  }

  function handleScroll() {
    textareaScrollTop = textarea?.scrollTop ?? 0;
  }
</script>

<div class={rootClass} style={rootStyle}>
  <SlashAutocomplete
    bind:this={slashRef}
    visible={Boolean(slashToken)}
    placement={slashPlacement}
    anchor={textarea}
    oninput={applySlashCommand}
  />

  {#if hasSkillMention(value)}
    <SkillMentionOverlay value={value} scrollTop={textareaScrollTop} />
  {/if}

  <textarea
    bind:this={textarea}
    bind:value
    class={textareaClass}
    {rows}
    placeholder={placeholder}
    aria-label={ariaLabel}
    spellcheck="false"
    autocomplete="off"
    disabled={disabled}
    onkeydown={handleKeydown}
    oninput={handleInput}
    onkeyup={handleCursorChange}
    onclick={handleCursorChange}
    onscroll={handleScroll}
    onpaste={onPaste}
  ></textarea>
</div>

<style>
  .ai-prompt-composer {
    position: relative;
    width: 100%;
    min-height: var(--ai-prompt-min-height, 40px);
    box-sizing: border-box;
    --skill-mention-padding: var(--ai-prompt-padding, 0);
    --skill-mention-font-size: var(--ai-prompt-font-size, inherit);
    --skill-mention-line-height: var(--ai-prompt-line-height, inherit);
  }

  .ai-prompt-textarea {
    display: block;
    width: 100%;
    min-height: var(--ai-prompt-min-height, 40px);
    max-height: var(--ai-prompt-max-height, 140px);
    box-sizing: border-box;
    resize: none;
    border: 0;
    outline: 0;
    background: transparent;
    color: var(--ai-prompt-text, var(--constellation-composer-textarea, rgba(240, 240, 250, 0.9)));
    caret-color: var(--ai-prompt-text, var(--constellation-composer-textarea, rgba(240, 240, 250, 0.9)));
    border-radius: inherit;
    padding: var(--ai-prompt-padding, 0);
    font: inherit;
    font-size: var(--ai-prompt-font-size, inherit);
    line-height: var(--ai-prompt-line-height, inherit);
    letter-spacing: 0;
    box-shadow: none;
    overflow-y: auto;
    overscroll-behavior: contain;
  }

  .ai-prompt-textarea.has-skill-mentions {
    color: transparent;
  }

  .ai-prompt-textarea.has-skill-mentions::selection {
    color: var(--ai-prompt-text, var(--constellation-composer-textarea, rgba(240, 240, 250, 0.9)));
    background: var(--constellation-skill-mention-selection, rgba(155, 128, 255, 0.3));
  }

  .ai-prompt-textarea::placeholder {
    color: var(--ai-prompt-placeholder, var(--constellation-composer-placeholder, rgba(240, 240, 250, 0.36)));
  }

  .ai-prompt-textarea:disabled {
    cursor: not-allowed;
    opacity: 0.56;
  }

</style>
