import type { ConstellationTone } from '$lib/components/constellation';
import type { MentionAutocompleteOption } from '$lib/features/composer/domain/mentionAutocomplete';

export type ChatPresenceState = 'active' | 'idle' | 'away' | 'offline';
export type ChatMessageKind = 'message' | 'root-summary' | 'system';
export type ChatAttachmentKind = 'file' | 'image' | 'link';
export type ChatNoticeTone = 'neutral' | 'info' | 'success' | 'warning' | 'danger';

export type ChatPresenceMember = {
  id?: string;
  label: string;
  tone?: ConstellationTone;
  state?: ChatPresenceState;
  style?: string;
};

export type ChatTypingIndicator = {
  label?: string;
  participants: ChatPresenceMember[];
};

export type ChatAttachmentItem = {
  id?: string;
  kind?: ChatAttachmentKind;
  label: string;
  detail?: string;
  url?: string;
  previewUrl?: string;
};

export type ChatThreadReference = {
  id: string;
  label?: string;
  accentColor?: string;
  replyCount?: number;
  unreadCount?: number;
  lastReplyLabel?: string;
  participants?: ChatPresenceMember[];
  onOpen?: (threadId: string) => void;
};

export type ChatMessageItem = {
  id: string;
  kind?: ChatMessageKind;
  role?: 'user' | 'illo' | 'system';
  author: string;
  timestamp?: string;
  tag?: string;
  tone?: ConstellationTone;
  body?: string;
  html?: string;
  summary?: string;
  accentColor?: string;
  coreColor?: string;
  ownerColor?: string;
  statusLabel?: string;
  pending?: boolean;
  error?: boolean;
  startsUnread?: boolean;
  attachments?: ChatAttachmentItem[];
  thread?: ChatThreadReference | null;
};

export type ChatComposerModel = {
  tone?: ConstellationTone;
  value?: string;
  defaultValue?: string;
  placeholder?: string;
  hint?: string;
  modeLabel?: string;
  replyContextLabel?: string;
  variant?: 'room' | 'thread';
  primaryActionLabel?: string;
  workingLabel?: string;
  stopLabel?: string;
  attachLabel?: string;
  disabled?: boolean;
  loading?: boolean;
  canSubmit?: boolean;
  attachments?: ChatAttachmentItem[];
  typing?: ChatTypingIndicator | null;
  mentionOptions?: MentionAutocompleteOption[];
  onValueChange?: (value: string) => void;
  onSubmit?: (value: string) => void;
  onAttach?: () => void;
  onStop?: () => void;
  onPaste?: (event: ClipboardEvent) => void;
  onKeydown?: (event: KeyboardEvent) => void;
  onRemoveAttachment?: (index: number) => void;
};
