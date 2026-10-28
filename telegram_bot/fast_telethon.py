# Parallel-chunk downloader for Telethon.
#
# Adapted (download-only) from:
#   https://gist.github.com/painor/7e74de80ae0c819d3e9abcf9989a8dd6
# Which is itself based on parallel_file_transfer.py from mautrix-telegram,
# shared with permission to distribute under the MIT license.
#   Copyright (C) 2021 Tulir Asokan - https://github.com/tulir/mautrix-telegram
#
# Telethon normally downloads a file over a single connection, one chunk at a
# time. This opens several extra connections to the same Telegram data
# center and downloads chunks in parallel, then writes them back in order.
#
# NOTE: this relies on a few Telethon internals (client._call, client._get_dc,
# client._init_request, client._connection, client._proxy) that aren't part
# of the public API and could change between Telethon versions. Callers
# should catch exceptions from download_file() and fall back to the normal
# event.download_media() if it fails.

import asyncio
import inspect
import logging
import math
import os
from typing import Optional, List, AsyncGenerator, Union, Awaitable, BinaryIO

from telethon import utils, TelegramClient
from telethon.crypto import AuthKey
from telethon.network import MTProtoSender
from telethon.tl.alltlobjects import LAYER
from telethon.tl.functions import InvokeWithLayerRequest
from telethon.tl.functions.auth import ExportAuthorizationRequest, ImportAuthorizationRequest
from telethon.tl.functions.upload import GetFileRequest
from telethon.tl.types import (Document, InputFileLocation, InputDocumentFileLocation,
                                InputPhotoFileLocation, InputPeerPhotoFileLocation)

log = logging.getLogger("fast_telethon")

TypeLocation = Union[Document, InputDocumentFileLocation, InputPeerPhotoFileLocation,
                      InputFileLocation, InputPhotoFileLocation]

# Per-file cap - how many connections a single download tries to open.
DEFAULT_MAX_CONNECTIONS = 8

# GLOBAL cap - the real safety limit. Shared across every download happening
# at the same time, so 3 simultaneous large files can't add up to 24 open
# connections. Whichever download requests a slot first gets it; the rest
# wait until a slot frees up. Raise only if you've watched the logs for a
# while and see no FloodWaitErrors at the current value.
GLOBAL_MAX_CONNECTIONS = int(os.environ.get('FAST_DOWNLOAD_GLOBAL_MAX_CONNECTIONS', 12))

_global_semaphore: Optional[asyncio.Semaphore] = None


def _get_global_semaphore() -> asyncio.Semaphore:
    global _global_semaphore
    if _global_semaphore is None:
        _global_semaphore = asyncio.Semaphore(GLOBAL_MAX_CONNECTIONS)
    return _global_semaphore


class DownloadSender:
    def __init__(self, client: TelegramClient, sender: MTProtoSender, file: TypeLocation,
                 offset: int, limit: int, stride: int, count: int,
                 semaphore: asyncio.Semaphore) -> None:
        self.client = client
        self.sender = sender
        self.request = GetFileRequest(file, offset=offset, limit=limit)
        self.stride = stride
        self.remaining = count
        self.semaphore = semaphore

    async def next(self) -> Optional[bytes]:
        if not self.remaining:
            return None
        result = await self.client._call(self.sender, self.request)
        self.remaining -= 1
        self.request.offset += self.stride
        return result.bytes

    async def disconnect(self) -> None:
        try:
            await self.sender.disconnect()
        finally:
            # Always give the slot back, even if disconnect itself errors,
            # so a failed download can't permanently eat a global slot.
            self.semaphore.release()


class ParallelTransferrer:
    def __init__(self, client: TelegramClient, dc_id: Optional[int] = None,
                 semaphore: Optional[asyncio.Semaphore] = None) -> None:
        self.client = client
        self.loop = self.client.loop
        self.dc_id = dc_id or self.client.session.dc_id
        self.auth_key = (None if dc_id and self.client.session.dc_id != dc_id
                          else self.client.session.auth_key)
        self.senders: Optional[List[DownloadSender]] = None
        self.semaphore = semaphore or _get_global_semaphore()

    async def _cleanup(self) -> None:
        await asyncio.gather(*[sender.disconnect() for sender in self.senders])
        self.senders = None

    @staticmethod
    def _get_connection_count(file_size: int, max_count: int = DEFAULT_MAX_CONNECTIONS,
                               full_size: int = 100 * 1024 * 1024) -> int:
        if file_size > full_size:
            return max_count
        return max(1, math.ceil((file_size / full_size) * max_count))

    async def _init_download(self, connections: int, file: TypeLocation, part_count: int,
                              part_size: int) -> None:
        minimum, remainder = divmod(part_count, connections)

        def get_part_count() -> int:
            nonlocal remainder
            if remainder > 0:
                remainder -= 1
                return minimum + 1
            return minimum

        # The first sender may need to export/import cross-DC authorization,
        # so it must be created before any of the others.
        self.senders = [
            await self._create_download_sender(file, 0, part_size, connections * part_size,
                                                get_part_count()),
            *await asyncio.gather(
                *[self._create_download_sender(file, i, part_size, connections * part_size,
                                                get_part_count())
                  for i in range(1, connections)])
        ]

    async def _create_download_sender(self, file: TypeLocation, index: int, part_size: int,
                                       stride: int, part_count: int) -> DownloadSender:
        sender = await self._create_sender()
        return DownloadSender(self.client, sender, file,
                               index * part_size, part_size, stride, part_count,
                               self.semaphore)

    async def _create_sender(self) -> MTProtoSender:
        # Wait for a free slot in the GLOBAL budget (shared across every
        # concurrent download in the process) before opening a connection.
        await self.semaphore.acquire()
        try:
            dc = await self.client._get_dc(self.dc_id)
            sender = MTProtoSender(self.auth_key, loggers=self.client._log)
            await sender.connect(self.client._connection(
                dc.ip_address, dc.port, dc.id,
                loggers=self.client._log, proxy=self.client._proxy))
            if not self.auth_key:
                log.debug(f"Exporting auth to DC {self.dc_id}")
                auth = await self.client(ExportAuthorizationRequest(self.dc_id))
                self.client._init_request.query = ImportAuthorizationRequest(id=auth.id, bytes=auth.bytes)
                req = InvokeWithLayerRequest(LAYER, self.client._init_request)
                await sender.send(req)
                self.auth_key = sender.auth_key
            return sender
        except Exception:
            # Setup failed before a DownloadSender/disconnect() exists to
            # release this slot, so release it here instead.
            self.semaphore.release()
            raise

    async def download(self, file: TypeLocation, file_size: int,
                        part_size_kb: Optional[float] = None,
                        connection_count: Optional[int] = None) -> AsyncGenerator[bytes, None]:
        connection_count = connection_count or self._get_connection_count(file_size)
        part_size = (part_size_kb or utils.get_appropriated_part_size(file_size)) * 1024
        part_count = math.ceil(file_size / part_size)
        log.debug(f"Starting parallel download: {connection_count} conns, "
                  f"{part_size} B/part, {part_count} parts")
        await self._init_download(connection_count, file, part_count, part_size)

        part = 0
        while part < part_count:
            tasks = [self.loop.create_task(sender.next()) for sender in self.senders]
            for task in tasks:
                data = await task
                if not data:
                    break
                yield data
                part += 1

        log.debug("Parallel download finished, cleaning up connections")
        await self._cleanup()


async def download_file(client: TelegramClient,
                         location: TypeLocation,
                         out: BinaryIO,
                         progress_callback: Optional[callable] = None,
                         connection_count: Optional[int] = None) -> BinaryIO:
    """
    Download `location` (e.g. a Message's .document) into the writable
    binary file object `out`, using several parallel connections.
    """
    size = location.size
    dc_id, input_location = utils.get_input_location(location)
    downloader = ParallelTransferrer(client, dc_id)
    async for chunk in downloader.download(input_location, size, connection_count=connection_count):
        out.write(chunk)
        if progress_callback:
            r = progress_callback(out.tell(), size)
            if inspect.isawaitable(r):
                await r
    return out
