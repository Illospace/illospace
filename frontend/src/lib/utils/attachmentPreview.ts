export type AttachmentPreviewKind =
  | 'image'
  | 'video'
  | 'pdf'
  | 'text'
  | 'document'
  | 'archive'
  | 'link'
  | 'file';

export const ATTACHMENT_INPUT_ACCEPT = [
  'image/jpeg',
  'image/png',
  'image/webp',
  'image/gif',
  'image/avif',
  'video/mp4',
  'video/webm',
  'video/quicktime',
  'application/pdf',
  'text/plain',
  'text/markdown',
  'text/csv',
  'application/json',
  '.avif',
  '.gif',
  '.jpg',
  '.jpeg',
  '.png',
  '.webp',
  '.m4v',
  '.mov',
  '.mp4',
  '.webm',
  '.doc',
  '.docx',
  '.odt',
  '.pdf',
  '.ppt',
  '.pptx',
  '.rtf',
  '.xls',
  '.xlsx',
  '.txt',
  '.md',
  '.csv',
  '.json',
  '.7z',
  '.rar',
  '.zip',
].join(',');

const IMAGE_ATTACHMENT_EXTENSIONS = new Set(['avif', 'gif', 'jpeg', 'jpg', 'png', 'svg', 'webp']);
const VIDEO_ATTACHMENT_EXTENSIONS = new Set(['m4v', 'mov', 'mp4', 'webm']);
const PDF_ATTACHMENT_EXTENSIONS = new Set(['pdf']);
const TEXT_ATTACHMENT_EXTENSIONS = new Set(['csv', 'json', 'md', 'txt']);
const DOCUMENT_ATTACHMENT_EXTENSIONS = new Set([
  'doc',
  'docx',
  'key',
  'numbers',
  'odt',
  'pages',
  'ppt',
  'pptx',
  'rtf',
  'xls',
  'xlsx',
]);
const ARCHIVE_ATTACHMENT_EXTENSIONS = new Set(['7z', 'rar', 'tar', 'zip']);
const MESSAGE_URL_PATTERN = /https?:\/\/[^\s<>"']+/gi;

export function attachmentUrl(attachment: any): string {
  const url = attachment?.url ?? attachment?.href ?? attachment?.previewUrl;
  return typeof url === 'string' ? url.trim() : '';
}

export function attachmentType(attachment: any): string {
  const type = attachment?.type ?? attachment?.content_type ?? attachment?.contentType ?? attachment?.mime_type;
  return typeof type === 'string' ? type.toLowerCase() : '';
}

export function attachmentLabel(attachment: any): string {
  const label = attachment?.label ?? attachment?.title ?? attachment?.filename ?? attachment?.name;
  if (typeof label === 'string' && label.trim()) return label.trim();

  const url = attachmentUrl(attachment);
  if (!url) return 'Attachment';
  try {
    const parsed = new URL(url, 'http://illo.local');
    const lastSegment = decodeURIComponent(parsed.pathname.split('/').filter(Boolean).pop() ?? '');
    return lastSegment || parsed.hostname || 'Attachment';
  } catch {
    return url.split('/').pop() || 'Attachment';
  }
}

export function attachmentExtension(attachment: any): string {
  const sources = [attachmentLabel(attachment), attachmentUrl(attachment)];
  for (const source of sources) {
    const cleanSource = source.split('?')[0]?.split('#')[0] ?? '';
    const match = cleanSource.match(/\.([a-z0-9]+)$/i);
    if (match?.[1]) return match[1].toLowerCase();
  }
  return '';
}

export function attachmentHost(attachment: any): string {
  const url = attachmentUrl(attachment);
  if (!url) return '';
  try {
    const parsed = new URL(url);
    return parsed.hostname.replace(/^www\./, '');
  } catch {
    return '';
  }
}

export function isExternalAttachmentUrl(attachment: any): boolean {
  return /^https?:\/\//i.test(attachmentUrl(attachment));
}

export function attachmentPreviewKind(attachment: any): AttachmentPreviewKind {
  const explicitKind = typeof attachment?.kind === 'string' ? attachment.kind.toLowerCase() : '';
  if (explicitKind === 'link' || explicitKind === 'url') return 'link';

  const type = attachmentType(attachment);
  const extension = attachmentExtension(attachment);
  if (type.startsWith('image/') || IMAGE_ATTACHMENT_EXTENSIONS.has(extension)) return 'image';
  if (type.startsWith('video/') || VIDEO_ATTACHMENT_EXTENSIONS.has(extension)) return 'video';
  if (type === 'application/pdf' || PDF_ATTACHMENT_EXTENSIONS.has(extension)) return 'pdf';
  if (type === 'text/uri-list') return 'link';
  if (type.startsWith('text/') || TEXT_ATTACHMENT_EXTENSIONS.has(extension)) return 'text';
  if (DOCUMENT_ATTACHMENT_EXTENSIONS.has(extension)) return 'document';
  if (ARCHIVE_ATTACHMENT_EXTENSIONS.has(extension)) return 'archive';
  if (isExternalAttachmentUrl(attachment)) return 'link';
  return 'file';
}

export function attachmentKindLabel(attachment: any): string {
  const kind = attachmentPreviewKind(attachment);
  const extension = attachmentExtension(attachment);
  if (kind === 'link') return attachmentHost(attachment) || 'Link';
  if (extension) return extension.toUpperCase();
  if (kind === 'video') return 'Video';
  if (kind === 'pdf') return 'PDF';
  if (kind === 'text') return 'Text';
  if (kind === 'archive') return 'Archive';
  if (kind === 'document') return 'Document';
  return 'File';
}

export function formatAttachmentBytes(bytes: number): string {
  if (!Number.isFinite(bytes) || bytes <= 0) return '';
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${Math.round(bytes / 1024)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

export function attachmentDetail(attachment: any): string {
  const size = typeof attachment?.size === 'number' ? attachment.size : null;
  const type = attachmentType(attachment);
  const host = attachmentHost(attachment);
  if (attachmentPreviewKind(attachment) === 'link') return host || attachmentUrl(attachment);
  if (size && type) return `${type} • ${formatAttachmentBytes(size)}`;
  if (size) return formatAttachmentBytes(size);
  return type || '';
}

export function attachmentCanOpen(attachment: any): boolean {
  return Boolean(attachmentUrl(attachment));
}

export function attachmentCanEmbed(kind: AttachmentPreviewKind): boolean {
  return kind === 'image' || kind === 'video' || kind === 'pdf' || kind === 'text' || kind === 'link';
}

export function normalizeMessageUrl(rawUrl: string): string {
  return rawUrl.replace(/[),.;!?]+$/g, '');
}

export function messageLinkAttachments(
  body: string | null | undefined,
  attachments: any[] | null | undefined = [],
): any[] {
  const text = typeof body === 'string' ? body : '';
  if (!text) return [];

  const existingUrls = new Set((attachments ?? []).map((attachment) => attachmentUrl(attachment)).filter(Boolean));
  const urls: string[] = [];
  for (const match of text.matchAll(MESSAGE_URL_PATTERN)) {
    const url = normalizeMessageUrl(match[0]);
    if (!url || existingUrls.has(url) || urls.includes(url)) continue;
    urls.push(url);
    if (urls.length >= 2) break;
  }

  return urls.map((url) => ({
    kind: 'link',
    url,
    filename: attachmentHost({ url }) || url,
    type: 'text/uri-list',
  }));
}
