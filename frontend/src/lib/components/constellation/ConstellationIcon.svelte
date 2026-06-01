<script lang="ts">
  import {
    IconArrowLeft,
    IconArrowRight,
    IconArrowUp,
    IconArchive,
    IconBell,
    IconBolt,
    IconBrandOpenai,
    IconBrain,
    IconBrowser,
    IconBrush,
    IconCheck,
    IconChevronLeft,
    IconChevronDown,
    IconChevronRight,
    IconCoins,
    IconCode,
    IconCopy,
    IconCpu,
    IconDatabase,
    IconEdit,
    IconEye,
    IconEyeOff,
    IconExternalLink,
    IconFile,
    IconFileText,
    IconFileTypePdf,
    IconFileZip,
    IconFolder,
    IconFolderPlus,
    IconGitBranch,
    IconLayoutGrid,
    IconLayoutSidebarRight,
    IconLink,
    IconLock,
    IconLockOpen,
    IconListDetails,
    IconLogout,
    IconMenu2,
    IconMicrophone,
    IconMessages,
    IconMessageReply,
    IconMoon,
    IconMoodPlus,
    IconNotes,
    IconPaperclip,
    IconPhoto,
    IconPlus,
    IconPlaylistAdd,
    IconRefresh,
    IconRoute,
    IconSearch,
    IconSettings2,
    IconShieldLock,
    IconSparkles,
    IconSquareFilled,
    IconSun,
    IconTestPipe,
    IconTool,
    IconTrash,
    IconUsers,
    IconVideo,
    IconX,
    IconZoomIn,
    IconZoomOut,
    IconZoomReset,
  } from '@tabler/icons-svelte-runes';

  export type ConstellationIconName =
    | 'activity'
    | 'archive'
    | 'archive-box'
    | 'attach'
    | 'back'
    | 'bolt'
    | 'brush'
    | 'check'
    | 'chat'
    | 'close'
    | 'code'
    | 'copy'
    | 'costs'
    | 'cortex'
    | 'cycles'
    | 'database'
    | 'edit'
    | 'eye'
    | 'eye-off'
    | 'document'
    | 'external-link'
    | 'file'
    | 'folder'
    | 'folder-plus'
    | 'forward'
    | 'git-branch'
    | 'image'
    | 'link'
    | 'lock'
    | 'lock-open'
    | 'logout'
    | 'memory'
    | 'menu'
    | 'mic'
    | 'moon'
    | 'notification'
    | 'overview'
    | 'openai'
    | 'paperclip'
    | 'pdf'
    | 'plus'
    | 'preview'
    | 'queue'
    | 'reaction-add'
    | 'refresh'
    | 'reply-thread'
    | 'route'
    | 'runtime'
    | 'search'
    | 'send'
    | 'settings'
    | 'side-panel'
    | 'skills'
    | 'stop'
    | 'sun'
    | 'system'
    | 'team'
    | 'test'
    | 'tool'
    | 'trash'
    | 'vault'
    | 'video'
    | 'x'
    | 'zoom-in'
    | 'zoom-out'
    | 'zoom-reset'
    | 'chevron-down'
    | 'chevron-left'
    | 'chevron-right';

  const ICONS = {
    activity: IconListDetails,
    archive: IconFileZip,
    'archive-box': IconArchive,
    attach: IconPaperclip,
    back: IconArrowLeft,
    bolt: IconBolt,
    brush: IconBrush,
    check: IconCheck,
    chat: IconMessages,
    close: IconX,
    code: IconCode,
    copy: IconCopy,
    costs: IconCoins,
    cortex: IconBrain,
    cycles: IconRefresh,
    database: IconDatabase,
    edit: IconEdit,
    eye: IconEye,
    'eye-off': IconEyeOff,
    document: IconFileText,
    'external-link': IconExternalLink,
    file: IconFile,
    folder: IconFolder,
    'folder-plus': IconFolderPlus,
    forward: IconArrowRight,
    'git-branch': IconGitBranch,
    image: IconPhoto,
    link: IconLink,
    lock: IconLock,
    'lock-open': IconLockOpen,
    logout: IconLogout,
    memory: IconNotes,
    menu: IconMenu2,
    mic: IconMicrophone,
    moon: IconMoon,
    notification: IconBell,
    overview: IconLayoutGrid,
    openai: IconBrandOpenai,
    paperclip: IconPaperclip,
    pdf: IconFileTypePdf,
    plus: IconPlus,
    preview: IconBrowser,
    queue: IconPlaylistAdd,
    'reaction-add': IconMoodPlus,
    refresh: IconRefresh,
    'reply-thread': IconMessageReply,
    route: IconRoute,
    runtime: IconCpu,
    search: IconSearch,
    send: IconArrowUp,
    settings: IconSettings2,
    'side-panel': IconLayoutSidebarRight,
    skills: IconSparkles,
    stop: IconSquareFilled,
    sun: IconSun,
    system: IconCpu,
    team: IconUsers,
    test: IconTestPipe,
    tool: IconTool,
    trash: IconTrash,
    vault: IconShieldLock,
    video: IconVideo,
    x: IconX,
    'zoom-in': IconZoomIn,
    'zoom-out': IconZoomOut,
    'zoom-reset': IconZoomReset,
    'chevron-down': IconChevronDown,
    'chevron-left': IconChevronLeft,
    'chevron-right': IconChevronRight,
  } as const;

  let {
    name,
    size = 16,
    stroke = 1.8,
    color = 'currentColor',
    className = '',
  }: {
    name: ConstellationIconName;
    size?: number | string;
    stroke?: number | string;
    color?: string;
    className?: string;
  } = $props();

  const iconClass = $derived(['constellation-icon', className].filter(Boolean).join(' '));
  const IconComponent = $derived(ICONS[name]);
</script>

<IconComponent
  size={size}
  stroke={stroke}
  {color}
  class={iconClass}
  aria-hidden="true"
/>
