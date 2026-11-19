"""Fetch bounded public pictures before a page takes its write lock."""

import asyncio
import struct
from datetime import UTC, datetime

import httpx

from illo.connectors.addresses import PublicTransport, public_address
from illo.db import all_rows, one
from illo.tasks.pages import images
from illo.tools.registry import Refusal
from illo.tools.web import normalize_url

MAX_BYTES = 5 * 1024 * 1024
STORE_BYTES = 500 * 1024 * 1024
TIMEOUT = 10
FETCHES = asyncio.Semaphore(2)
ADDRESS = 'The address is not a public HTTPS address.'
TYPE = 'The file is not a PNG, JPEG, WebP, or GIF picture.'
FULL = 'The picture store is full.'
LOCK = 18040001


def refusal(position, reason):
    return Refusal(f'Illo cannot get the picture of block {position + 1}: {reason} '
                   'Give a public HTTPS address of a PNG, JPEG, WebP, or GIF file of 5 MB at most, or remove the block.')


def dimensions(data, kind):
    """Read dimensions from the picture header without a decoder."""
    if kind == 'image/png' and len(data) >= 33 and data[8:16] == b'\x00\x00\x00\rIHDR':
        return struct.unpack('>II', data[16:24])
    if kind == 'image/gif' and len(data) >= 13:
        return struct.unpack('<HH', data[6:10])
    if kind == 'image/jpeg':
        pos = 2
        while pos < len(data):
            if data[pos] != 0xff:
                break
            while pos < len(data) and data[pos] == 0xff:
                pos += 1
            if pos >= len(data):
                break
            marker = data[pos]
            pos += 1
            if marker in {0xd9, 0xda}:
                break
            if marker == 0x01 or 0xd0 <= marker <= 0xd8:
                continue
            if pos + 2 > len(data):
                break
            size = int.from_bytes(data[pos:pos + 2], 'big')
            if size < 2 or pos + size > len(data):
                break
            if marker in {0xc0, 0xc1, 0xc2, 0xc3, 0xc5, 0xc6, 0xc7, 0xc9, 0xca, 0xcb, 0xcd, 0xce, 0xcf}:
                if size >= 8:
                    height, width = struct.unpack('>HH', data[pos + 3:pos + 7])
                    return width, height
                break
            pos += size
    if kind == 'image/webp' and len(data) >= 20:
        end = int.from_bytes(data[4:8], 'little') + 8
        pos = 12
        while pos + 8 <= min(end, len(data)):
            tag = data[pos:pos + 4]
            size = int.from_bytes(data[pos + 4:pos + 8], 'little')
            start = pos + 8
            if start + size > min(end, len(data)):
                break
            chunk = data[start:start + size]
            if tag == b'VP8 ' and len(chunk) >= 10 and chunk[3:6] == b'\x9d\x01\x2a':
                width, height = struct.unpack('<HH', chunk[6:10])
                return width & 0x3fff, height & 0x3fff
            if tag == b'VP8L' and len(chunk) >= 5 and chunk[0] == 0x2f:
                bits = int.from_bytes(chunk[1:5], 'little')
                return (bits & 0x3fff) + 1, ((bits >> 14) & 0x3fff) + 1
            if tag == b'VP8X' and len(chunk) >= 10:
                return int.from_bytes(chunk[4:7], 'little') + 1, int.from_bytes(chunk[7:10], 'little') + 1
            pos = start + size + size % 2
    raise ValueError(TYPE)


async def fetch(value):
    async with FETCHES:
        try:
            async with asyncio.timeout(TIMEOUT):
                url = public_address(str(normalize_url(value)))
                if url.scheme != 'https' or url.port not in {None, 443}:
                    raise ValueError(ADDRESS)
                transport = PublicTransport(httpx.AsyncHTTPTransport(trust_env=False))
                async with httpx.AsyncClient(transport=transport, trust_env=False, timeout=None,
                                             follow_redirects=False) as client:
                    async with client.stream('GET', url, headers={'User-Agent': 'Illo',
                                                                 'Accept-Encoding': 'identity'}) as response:
                        if 300 <= response.status_code < 400:
                            raise ValueError('The address redirects. Give the final address.')
                        if response.status_code != 200:
                            raise ValueError(f'The server answered {response.status_code}.')
                        body = bytearray()
                        async for chunk in response.aiter_raw():
                            body.extend(chunk[:MAX_BYTES + 1 - len(body)])
                            if len(body) > MAX_BYTES:
                                raise ValueError('The file is larger than 5 MB.')
                        data = bytes(body)
                try:
                    kind = images.media_type(data)
                    width, height = dimensions(data, kind)
                    if not width or not height:
                        raise ValueError(TYPE)
                except (ValueError, struct.error):
                    raise ValueError(TYPE) from None
                if max(width, height) > 8000:
                    raise ValueError('The picture is larger than 8,000 pixels on one side.')
                return data, kind
        except (TimeoutError, httpx.RequestError):
            raise ValueError('The server did not answer in 10 seconds.') from None
        except (ValueError, httpx.InvalidURL, OSError) as exc:
            if str(exc) == 'The connector redirect is not allowed':
                raise ValueError('The address redirects. Give the final address.') from None
            if str(exc) in {ADDRESS, TYPE, 'The file is larger than 5 MB.',
                            'The address redirects. Give the final address.',
                            'The picture is larger than 8,000 pixels on one side.'} or str(exc).startswith('The server answered '):
                raise
            raise ValueError(ADDRESS) from None


def notes(blocks):
    return [f'Illo does not get a picture from an address for its own pages. Block {n + 1} shows as a link.'
            if block['image_link'] == 'own' else
            f'Pictures from an address are off for this team. Block {n + 1} shows as a link.'
            for n, block in enumerate(blocks) if block.get('image_link')]


async def prepare(ctx, blocks, *, store_refs=False, link_only=False):
    files = []
    for position, block in enumerate(blocks):
        image = block.get('image')
        if image:
            if 'url' in image:
                if link_only or not ctx.workspace.config.pages.outside_images:
                    # Keep the write-time choice when the setting changes or files expire.
                    block['image_link'] = 'own' if link_only else True
                else:
                    try:
                        data, kind = await fetch(image['url'])
                    except ValueError as exc:
                        raise refusal(position, str(exc)) from None
                    files.append((position, kind, data))
            else:
                block['image'], data, kind = await images.read(ctx, image)
                if store_refs:
                    files.append((position, kind, data))
        for item in block.get('items', []):
            if item['state'] != 'open' and item['checked_at'] is None:
                item['checked_at'] = datetime.now(UTC).isoformat()
    return files


async def replace(conn, files, *, task_id=None, ask_id=None):
    await conn.execute('DELETE FROM page_files WHERE task_id=%s OR ask_id=%s', (task_id, ask_id))
    if files:
        # One lock for the store, so two writes cannot pass the limit together.
        await conn.execute('SELECT pg_advisory_xact_lock(%s)', (LOCK,))
        used = await one(conn, 'SELECT COALESCE(sum(octet_length(data)),0) AS bytes FROM page_files')
        if used['bytes'] + sum(len(data) for _, _, data in files) > STORE_BYTES:
            raise refusal(files[0][0], FULL)
        for position, kind, data in files:
            await conn.execute('INSERT INTO page_files(task_id,ask_id,position,media_type,data) VALUES (%s,%s,%s,%s,%s)',
                               (task_id, ask_id, position, kind, data))


async def positions(conn, *, task_id=None, ask_id=None):
    return {row['position'] for row in await all_rows(conn,
            'SELECT position FROM page_files WHERE task_id=%s OR ask_id=%s', (task_id, ask_id))}


async def read(conn, position, *, task_id=None, ask_id=None):
    return await one(conn, 'SELECT data,media_type FROM page_files WHERE position=%s AND (task_id=%s OR ask_id=%s)',
                     (position, task_id, ask_id))


async def cleanup(ctx):
    async with ctx.pool.connection() as conn:
        await conn.execute("""DELETE FROM page_files f WHERE
            EXISTS (SELECT 1 FROM asks a WHERE a.id=f.ask_id AND a.state<>'open'
                    AND a.created_at<now()-interval '90 days') OR
            EXISTS (SELECT 1 FROM tasks t WHERE t.id=f.task_id AND t.state IN ('done','cancelled')
                    AND t.updated_at<now()-interval '90 days')""")
