function getCrypto(): Crypto | undefined {
  if (typeof globalThis === 'undefined') return undefined;
  return globalThis.crypto;
}

function formatUuidFromBytes(bytes: Uint8Array): string {
  const normalized = new Uint8Array(bytes);
  normalized[6] = (normalized[6] & 0x0f) | 0x40;
  normalized[8] = (normalized[8] & 0x3f) | 0x80;

  const hex = Array.from(normalized, (byte) => byte.toString(16).padStart(2, '0')).join('');
  return [
    hex.slice(0, 8),
    hex.slice(8, 12),
    hex.slice(12, 16),
    hex.slice(16, 20),
    hex.slice(20, 32),
  ].join('-');
}

function fallbackRandomByte(): number {
  return Math.floor(Math.random() * 256);
}

export function createUUID(): string {
  const cryptoApi = getCrypto();

  if (cryptoApi?.randomUUID) {
    return cryptoApi.randomUUID();
  }

  if (cryptoApi?.getRandomValues) {
    return formatUuidFromBytes(cryptoApi.getRandomValues(new Uint8Array(16)));
  }

  return formatUuidFromBytes(Uint8Array.from({ length: 16 }, fallbackRandomByte));
}
