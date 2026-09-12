#!/usr/bin/env python3
"""
Talwar File Recovery - deleted file recovery for FAT32 / exFAT memory cards (Windows).

Pick a drive, scan, tick the files you want, recover them to a folder.
The source drive is only ever opened for reading.

Scan modes
  Quick  - walks the file system and lists entries that are marked deleted
           (names, sizes and dates intact). Also descends into deleted folders.
  Deep   - reads every free cluster and rebuilds photos / videos / documents
           from their file signatures (works even when the folder entry is gone).
"""

import os
import sys
import struct
import string
import ctypes
import bisect
import threading
import queue
import datetime
import shutil
import subprocess
from ctypes import wintypes

import tkinter as tk
from tkinter import ttk, filedialog, messagebox

APP_NAME = "Talwar File Recovery"
SECTOR = 512
CHUNK_MIN = 8 * 1024 * 1024

# --------------------------------------------------------------------------
# Raw volume access
# --------------------------------------------------------------------------

k32 = ctypes.windll.kernel32
k32.CreateFileW.restype = wintypes.HANDLE
k32.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
                            wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
k32.ReadFile.argtypes = [wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD,
                         ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p]
k32.SetFilePointerEx.argtypes = [wintypes.HANDLE, ctypes.c_longlong,
                                 ctypes.POINTER(ctypes.c_longlong), wintypes.DWORD]
k32.DeviceIoControl.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD,
                                ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p]
k32.CloseHandle.argtypes = [wintypes.HANDLE]
INVALID_HANDLE = ctypes.c_void_p(-1).value


class RawVolume:
    """Sector-aligned read access to a volume (\\\\.\\E:) or a disk image file."""

    def __init__(self, path):
        self.path = path
        self.is_device = path.startswith("\\\\.\\")
        self.h = None
        self.f = None
        if self.is_device:
            h = k32.CreateFileW(path, 0x80000000, 3, None, 3, 0, None)
            if h == INVALID_HANDLE or h is None:
                err = k32.GetLastError()
                if err == 5:
                    raise PermissionError("Access denied opening %s. Try running as administrator." % path)
                raise OSError("Cannot open %s (Win32 error %d)" % (path, err))
            self.h = h
            self.size = self._length_ioctl() or 0
        else:
            self.f = open(path, "rb", buffering=0)
            self.size = os.path.getsize(path)
        self._cache_off = -1
        self._cache = b""

    def _length_ioctl(self):
        out = ctypes.c_ulonglong(0)
        ret = wintypes.DWORD(0)
        ok = k32.DeviceIoControl(self.h, 0x0007405C, None, 0, ctypes.byref(out), 8, ctypes.byref(ret), None)
        return out.value if ok else 0

    def close(self):
        if self.h:
            k32.CloseHandle(self.h)
            self.h = None
        if self.f:
            self.f.close()
            self.f = None

    def _raw_read(self, off, n):
        if self.is_device:
            newpos = ctypes.c_longlong(0)
            if not k32.SetFilePointerEx(self.h, off, ctypes.byref(newpos), 0):
                raise OSError("seek failed at %d" % off)
            buf = ctypes.create_string_buffer(n)
            got = wintypes.DWORD(0)
            total = 0
            while total < n:
                if not k32.ReadFile(self.h, ctypes.addressof(buf) + total, n - total, ctypes.byref(got), None):
                    if total == 0:
                        raise OSError("read failed at %d (err %d)" % (off, k32.GetLastError()))
                    break
                if got.value == 0:
                    break
                total += got.value
            return buf.raw[:total]
        else:
            self.f.seek(off)
            return self.f.read(n)

    def read(self, off, n):
        """Read n bytes at off, any alignment, clamped to the volume size."""
        if n <= 0 or off >= self.size:
            return b""
        if off + n > self.size:
            n = self.size - off
        # serve from the scan cache when possible
        if self._cache_off >= 0 and off >= self._cache_off and off + n <= self._cache_off + len(self._cache):
            s = off - self._cache_off
            return self._cache[s:s + n]
        lo = off - (off % SECTOR)
        hi = ((off + n + SECTOR - 1) // SECTOR) * SECTOR
        if hi > self.size:
            hi = self.size
        data = self._raw_read(lo, hi - lo)
        return data[off - lo: off - lo + n]

    def read_chunk_cached(self, off, n):
        data = self.read(off, n)
        self._cache_off = off
        self._cache = data
        return data

    def drop_cache(self):
        self._cache_off = -1
        self._cache = b""


# --------------------------------------------------------------------------
# File system layout (FAT12/16/32 + exFAT)
# --------------------------------------------------------------------------

def u16(b, o): return struct.unpack_from("<H", b, o)[0]
def u32(b, o): return struct.unpack_from("<I", b, o)[0]
def u64(b, o): return struct.unpack_from("<Q", b, o)[0]


def fat_datetime(date, time_):
    try:
        return datetime.datetime(1980 + (date >> 9), (date >> 5) & 15, date & 31,
                                 time_ >> 11, (time_ >> 5) & 63, (time_ & 31) * 2)
    except ValueError:
        return None


def exfat_datetime(v):
    try:
        return datetime.datetime(1980 + (v >> 25), (v >> 21) & 15, (v >> 16) & 31,
                                 (v >> 11) & 31, (v >> 5) & 63, (v & 31) * 2)
    except ValueError:
        return None


class FileSystem:
    def __init__(self, vol):
        self.vol = vol
        self.kind = None
        bs = vol.read(0, 512)
        if len(bs) < 512:
            raise OSError("Volume too small to read a boot sector")
        if bs[3:11] == b"EXFAT   ":
            self._init_exfat(bs)
        elif bs[510:512] == b"\x55\xaa" and bs[11:13] != b"\x00\x00":
            self._init_fat(bs)
        else:
            raise OSError("Unsupported file system (not FAT/exFAT). Deep scan is still possible.")
        if vol.size == 0:
            vol.size = self.volume_bytes
        self.fat = None
        self.bitmap = None

    def _init_exfat(self, bs):
        self.kind = "exFAT"
        self.bps = 1 << bs[108]
        self.spc = 1 << bs[109]
        self.csize = self.bps * self.spc
        self.fat_off = u32(bs, 80) * self.bps
        self.fat_bytes = u32(bs, 84) * self.bps
        self.data_off = u32(bs, 88) * self.bps
        self.cluster_count = u32(bs, 92)
        self.root_cluster = u32(bs, 96)
        self.volume_bytes = u64(bs, 72) * self.bps
        self.root_fixed = None

    def _init_fat(self, bs):
        self.bps = u16(bs, 11)
        self.spc = bs[13]
        if self.bps not in (512, 1024, 2048, 4096) or self.spc == 0:
            raise OSError("Invalid FAT boot sector")
        self.csize = self.bps * self.spc
        reserved = u16(bs, 14)
        nfats = bs[16]
        root_entries = u16(bs, 17)
        total = u16(bs, 19) or u32(bs, 32)
        spf = u16(bs, 22) or u32(bs, 36)
        root_dir_sectors = (root_entries * 32 + self.bps - 1) // self.bps
        first_data = reserved + nfats * spf + root_dir_sectors
        self.cluster_count = (total - first_data) // self.spc
        if self.cluster_count < 4085:
            self.kind = "FAT12"
        elif self.cluster_count < 65525:
            self.kind = "FAT16"
        else:
            self.kind = "FAT32"
        self.fat_off = reserved * self.bps
        self.fat_bytes = spf * self.bps
        self.data_off = first_data * self.bps
        self.volume_bytes = total * self.bps
        if self.kind == "FAT32":
            self.root_cluster = u32(bs, 44)
            self.root_fixed = None
        else:
            self.root_cluster = 0
            self.root_fixed = ((reserved + nfats * spf) * self.bps, root_dir_sectors * self.bps)

    # -- clusters ------------------------------------------------------------

    def cluster_off(self, c):
        return self.data_off + (c - 2) * self.csize

    def off_cluster(self, off):
        return (off - self.data_off) // self.csize + 2

    def valid_cluster(self, c):
        return 2 <= c < self.cluster_count + 2

    def load_tables(self, log=None):
        if self.fat is None:
            n = min(self.fat_bytes, 512 * 1024 * 1024)
            if log:
                log("Loading allocation table (%s)..." % human(n))
            self.fat = self.vol.read(self.fat_off, n)
        if self.kind == "exFAT" and self.bitmap is None:
            self.bitmap = b""
            # the allocation bitmap is described by an entry (type 0x81) in the root directory
            for c in self.chain(self.root_cluster, nofat=False, size=None, limit=64):
                d = self.vol.read(self.cluster_off(c), self.csize)
                for i in range(0, len(d) - 31, 32):
                    if d[i] == 0x81:
                        first = u32(d, i + 20)
                        length = u64(d, i + 24)
                        if self.valid_cluster(first) and 0 < length < 1 << 30:
                            self.bitmap = self.vol.read(self.cluster_off(first), length)
                        break
                if self.bitmap:
                    break

    def fat_entry(self, c):
        f = self.fat
        if self.kind == "exFAT":
            o = c * 4
            return u32(f, o) if o + 4 <= len(f) else 0
        if self.kind == "FAT32":
            o = c * 4
            return (u32(f, o) & 0x0FFFFFFF) if o + 4 <= len(f) else 0
        if self.kind == "FAT16":
            o = c * 2
            return u16(f, o) if o + 2 <= len(f) else 0
        o = c + c // 2
        if o + 2 > len(f):
            return 0
        v = u16(f, o)
        return (v >> 4) if c & 1 else (v & 0xFFF)

    def is_end(self, v):
        return {"exFAT": v >= 0xFFFFFFF8, "FAT32": v >= 0x0FFFFFF8,
                "FAT16": v >= 0xFFF8, "FAT12": v >= 0xFF8}[self.kind]

    def is_free(self, c):
        """True if the cluster is not allocated to any live file."""
        if not self.valid_cluster(c):
            return False
        if self.kind == "exFAT":
            i = c - 2
            if self.bitmap and (i >> 3) < len(self.bitmap):
                return not (self.bitmap[i >> 3] >> (i & 7)) & 1
            return True
        return self.fat_entry(c) == 0

    def allocated_in_range(self, start_c, n, cap=200000):
        cnt = 0
        for c in range(start_c, min(start_c + n, start_c + cap, self.cluster_count + 2)):
            if not self.is_free(c):
                cnt += 1
        return cnt

    def chain(self, start, nofat, size, limit=1 << 22):
        """Yield the clusters of a file/dir. Contiguous when nofat or when the FAT chain is gone."""
        if not self.valid_cluster(start):
            return
        if size is not None:
            n = max(1, (size + self.csize - 1) // self.csize)
        else:
            n = None
        if nofat:
            for i in range(n if n is not None else 1):
                c = start + i
                if not self.valid_cluster(c):
                    return
                yield c
            return
        seen = set()
        c = start
        count = 0
        while self.valid_cluster(c) and c not in seen and count < limit:
            yield c
            seen.add(c)
            count += 1
            if n is not None and count >= n:
                return
            v = self.fat_entry(c) if self.fat else 0
            if self.is_end(v):
                return
            if v == 0:
                # chain lost (deleted) - assume contiguous
                if n is None:
                    return
                c = c + 1
            else:
                c = v


# --------------------------------------------------------------------------
# Found items
# --------------------------------------------------------------------------

class Item:
    __slots__ = ("name", "folder", "ext", "size", "mtime", "start", "source", "chance",
                 "fs_path", "is_dir", "nofat", "start_cluster", "note", "runs")

    def __init__(self, name, folder, size, start, source, mtime=None, fs_path=None,
                 is_dir=False, nofat=True, start_cluster=0, note=""):
        self.name = name
        self.folder = folder
        self.ext = os.path.splitext(name)[1].lower().lstrip(".")
        self.size = size
        self.mtime = mtime
        self.start = start
        self.source = source
        self.chance = "?"
        self.fs_path = fs_path
        self.is_dir = is_dir
        self.nofat = nofat
        self.start_cluster = start_cluster
        self.note = note
        self.runs = []          # list of (byte offset, length) making up the file, in order


# --------------------------------------------------------------------------
# Directory parsing
# --------------------------------------------------------------------------

TRASH_NAMES = ("$recycle.bin", "recycler", ".trash", ".trashes", "trash", "deleted", "recycle",
               "lost.dir", ".deleted", "bin")

SIG_BY_EXT = {
    "jpg": (b"\xff\xd8\xff",), "jpeg": (b"\xff\xd8\xff",), "png": (b"\x89PNG",),
    "gif": (b"GIF8",), "pdf": (b"%PDF",), "zip": (b"PK\x03\x04",), "docx": (b"PK\x03\x04",),
    "xlsx": (b"PK\x03\x04",), "pptx": (b"PK\x03\x04",), "wav": (b"RIFF",), "avi": (b"RIFF",),
    "mp3": (b"ID3", b"\xff\xfb", b"\xff\xf3", b"\xff\xf2"), "cr2": (b"II*\x00",),
    "nef": (b"MM\x00*", b"II*\x00"), "arw": (b"II*\x00",), "dng": (b"II*\x00", b"MM\x00*"),
    "tif": (b"II*\x00", b"MM\x00*"), "tiff": (b"II*\x00", b"MM\x00*"),
}
FTYP_EXTS = ("mp4", "mov", "m4a", "m4v", "3gp", "heic", "heif", "avif", "lrf", "insv", "360")


def lfn_chars(e):
    raw = e[1:11] + e[14:26] + e[28:32]
    s = raw.decode("utf-16-le", "replace")
    cut = s.find("\x00")
    if cut >= 0:
        s = s[:cut]
    return s.replace("￿", "")


def short_checksum(name11):
    s = 0
    for b in name11:
        s = ((s >> 1) | ((s & 1) << 7)) & 0xFF
        s = (s + b) & 0xFF
    return s


def decode_short(e, deleted):
    base = e[0:8]
    ext = e[8:11]
    if deleted:
        base = b"_" + base[1:]
    if base[0] == 0x05:
        base = b"\xe5" + base[1:]
    b = base.decode("cp437", "replace").rstrip()
    x = ext.decode("cp437", "replace").rstrip()
    if e[12] & 0x08:
        b = b.lower()
    if e[12] & 0x10:
        x = x.lower()
    return b + ("." + x if x else "")


def parse_fat_dir(data):
    """Yield dicts for every 8.3 entry in a FAT directory buffer."""
    pending = []
    for i in range(0, len(data) - 31, 32):
        e = data[i:i + 32]
        first = e[0]
        if first == 0x00:
            pending = []
            continue
        attr = e[11]
        if attr == 0x0F:
            pending.append(e)
            continue
        if attr & 0x08:
            pending = []
            continue  # volume label
        deleted = first == 0xE5
        name = None
        if pending:
            csum = short_checksum(e[0:11])
            if all(p[13] == csum for p in pending):
                name = "".join(lfn_chars(p) for p in reversed(pending))
        pending = []
        if not name:
            name = decode_short(e, deleted)
        if name in (".", "..") or not name.strip("_ "):
            continue
        start = (u16(e, 20) << 16) | u16(e, 26)
        yield {"name": name, "deleted": deleted, "is_dir": bool(attr & 0x10), "start": start,
               "size": u32(e, 28), "mtime": fat_datetime(u16(e, 24), u16(e, 22)), "nofat": False}


def parse_exfat_dir(data):
    """Yield dicts for every file/directory entry set in an exFAT directory buffer."""
    n = len(data)
    i = 0
    while i + 32 <= n:
        e = data[i:i + 32]
        t = e[0]
        if t & 0x7F != 0x05:
            i += 32
            continue
        deleted = not (t & 0x80)
        sec = e[1]
        attrs = u16(e, 4)
        mtime = exfat_datetime(u32(e, 12))
        stream = None
        parts = []
        j = i + 32
        for _ in range(sec):
            if j + 32 > n:
                break
            se = data[j:j + 32]
            st = se[0] & 0x7F
            if st == 0x40:
                stream = se
            elif st == 0x41:
                parts.append(se[2:32])
            elif st in (0x20, 0x21, 0x22):
                pass
            else:
                break
            j += 32
        i = max(j, i + 32)
        if stream is None:
            continue
        name_len = stream[3]
        name = b"".join(parts)[:name_len * 2].decode("utf-16-le", "replace").rstrip("\x00")
        if not name:
            continue
        yield {"name": name, "deleted": deleted, "is_dir": bool(attrs & 0x10),
               "start": u32(stream, 20), "size": u64(stream, 24), "mtime": mtime,
               "nofat": bool(stream[1] & 0x02)}


def looks_like_fat_dir(buf):
    return (len(buf) >= 64 and buf[0:11] == b".          " and buf[11] & 0x10
            and buf[32:43] == b"..         ")


EXFAT_TYPES = {0x81, 0x82, 0x83, 0x85, 0xA0, 0xA1, 0xA2, 0xC0, 0xC1,
               0x01, 0x02, 0x03, 0x05, 0x20, 0x21, 0x22, 0x40, 0x41}


def looks_like_exfat_dir(buf):
    if len(buf) < 96:
        return False
    t0, t1, t2 = buf[0], buf[32], buf[64]
    if (t0 & 0x7F) == 0x05 and (t1 & 0x7F) == 0x40 and (t2 & 0x7F) == 0x41:
        return True
    if t0 in (0x83, 0x81, 0x82, 0x03) and t1 in EXFAT_TYPES and t2 in EXFAT_TYPES:
        return True
    return False


# --------------------------------------------------------------------------
# Signature carvers - each returns (size, ext) or None
# --------------------------------------------------------------------------

MAX_JPEG = 120 << 20
MAX_PNG = 300 << 20
MAX_GIF = 200 << 20
MAX_PDF = 512 << 20
MAX_ZIP = 2 << 30
MAX_MP3 = 300 << 20
MAX_RAW = 150 << 20


def carve_jpeg(rd, off):
    pos = off + 2
    end = off + MAX_JPEG
    while pos < end:
        h = rd(pos, 4)
        if len(h) < 4 or h[0] != 0xFF:
            return None
        m = h[1]
        if m == 0xFF:
            pos += 1
            continue
        if m == 0xD8 or 0xD0 <= m <= 0xD7 or m == 0x01:
            pos += 2
            continue
        if m == 0xD9:
            return (pos + 2 - off, "jpg")
        seglen = (h[2] << 8) | h[3]
        if seglen < 2:
            return None
        pos += 2 + seglen
        if m != 0xDA:
            continue
        # entropy-coded data: look for the next real marker
        found = False
        while pos < end and not found:
            buf = rd(pos, 1 << 20)
            if not buf:
                return None
            i = 0
            L = len(buf)
            while True:
                i = buf.find(b"\xff", i)
                if i < 0 or i >= L - 1:
                    break
                nx = buf[i + 1]
                if nx == 0x00 or 0xD0 <= nx <= 0xD7 or nx == 0xFF:
                    i += 1
                    continue
                if nx == 0xD9:
                    return (pos + i + 2 - off, "jpg")
                pos = pos + i
                found = True
                break
            if not found:
                if L < (1 << 20):
                    return None
                pos += L - 1
    return None


def carve_png(rd, off):
    pos = off + 8
    end = off + MAX_PNG
    while pos < end:
        h = rd(pos, 8)
        if len(h) < 8:
            return None
        ln = struct.unpack(">I", h[:4])[0]
        typ = h[4:8]
        if not all(65 <= c <= 122 for c in typ):
            return None
        pos += 12 + ln
        if typ == b"IEND":
            return (pos - off, "png")
    return None


def carve_gif(rd, off):
    hdr = rd(off, 13)
    if len(hdr) < 13:
        return None
    pos = off + 13
    flags = hdr[10]
    if flags & 0x80:
        pos += 3 << ((flags & 7) + 1)
    end = off + MAX_GIF

    def skip_subblocks(p):
        while True:
            b = rd(p, 1)
            if not b:
                return None
            if b[0] == 0:
                return p + 1
            p += 1 + b[0]

    while pos < end:
        b = rd(pos, 1)
        if not b:
            return None
        t = b[0]
        if t == 0x3B:
            return (pos + 1 - off, "gif")
        if t == 0x21:
            pos = skip_subblocks(pos + 2)
        elif t == 0x2C:
            d = rd(pos, 10)
            if len(d) < 10:
                return None
            pos += 10
            if d[9] & 0x80:
                pos += 3 << ((d[9] & 7) + 1)
            pos = skip_subblocks(pos + 1)
        else:
            return None
        if pos is None:
            return None
    return None


KNOWN_ATOMS = {b"ftyp", b"moov", b"mdat", b"free", b"skip", b"wide", b"uuid", b"meta", b"moof",
               b"mfra", b"pdin", b"sidx", b"styp", b"ssix", b"prft", b"junk", b"PICT", b"pnot", b"mvex"}


def carve_mp4(rd, off, limit):
    pos = off
    seen_media = False
    hdr = rd(off, 12)
    brand = hdr[8:12]
    while pos < off + limit:
        h = rd(pos, 16)
        if len(h) < 8:
            break
        size = struct.unpack(">I", h[:4])[0]
        typ = h[4:8]
        if typ not in KNOWN_ATOMS:
            break
        if size == 1:
            if len(h) < 16:
                break
            size = struct.unpack(">Q", h[8:16])[0]
        elif size == 0:
            break
        if size < 8:
            break
        if typ in (b"moov", b"mdat"):
            seen_media = True
        pos += size
    if not seen_media or pos <= off + 8:
        return None
    b = brand.lower()
    if b.startswith(b"qt"):
        ext = "mov"
    elif b in (b"heic", b"heix", b"mif1", b"hevc", b"msf1"):
        ext = "heic"
    elif b == b"avif":
        ext = "avif"
    elif b.startswith(b"3g"):
        ext = "3gp"
    elif b == b"m4a ":
        ext = "m4a"
    else:
        ext = "mp4"
    return (pos - off, ext)


def carve_riff(rd, off):
    h = rd(off, 12)
    if len(h) < 12:
        return None
    size = struct.unpack("<I", h[4:8])[0] + 8
    kind = h[8:12]
    if size < 44 or size > 4 << 30:
        return None
    if kind == b"WAVE":
        return (size, "wav")
    if kind == b"AVI ":
        return (size, "avi")
    if kind == b"WEBP":
        return (size, "webp")
    return None


def carve_pdf(rd, off):
    pos = off
    end = off + MAX_PDF
    last = None
    while pos < end:
        buf = rd(pos, 1 << 20)
        if not buf:
            break
        i = buf.find(b"%%EOF")
        while i >= 0:
            e = pos + i + 5
            tail = rd(e, 2)
            if tail[:2] == b"\r\n":
                e += 2
            elif tail[:1] in (b"\n", b"\r"):
                e += 1
            last = e
            nxt = rd(e, 64).lstrip(b" \r\n\t")
            if not (nxt[:1].isdigit() or nxt.startswith((b"xref", b"trailer", b"startxref", b"%"))
                    ) or nxt.startswith(b"%PDF"):
                return (e - off, "pdf")
            i = buf.find(b"%%EOF", i + 5)
        if len(buf) < (1 << 20):
            break
        pos += len(buf) - 8
    if last:
        return (last - off, "pdf")
    return None


def carve_zip(rd, off):
    pos = off
    names = []
    end = off + MAX_ZIP
    while pos < end:
        h = rd(pos, 30)
        if len(h) < 4:
            return None
        sig = h[:4]
        if sig == b"PK\x03\x04":
            if len(h) < 30:
                return None
            flags = u16(h, 6)
            csize = u32(h, 18)
            nlen = u16(h, 26)
            xlen = u16(h, 28)
            names.append(rd(pos + 30, nlen))
            if flags & 8 and csize == 0:
                # sizes live in a trailing data descriptor: fall back to a search for the end record
                return _zip_search_end(rd, off, pos, names)
            pos += 30 + nlen + xlen + csize
        elif sig == b"PK\x01\x02":
            return _zip_search_end(rd, off, pos, names)
        elif sig == b"PK\x07\x08":
            pos += 16
        else:
            return None
    return None


def _zip_search_end(rd, off, pos, names):
    end = off + MAX_ZIP
    p = pos
    while p < end:
        buf = rd(p, 1 << 20)
        if not buf:
            return None
        i = buf.find(b"PK\x05\x06")
        if i >= 0:
            e = p + i
            rec = rd(e, 22)
            if len(rec) < 22:
                return None
            total = e + 22 + u16(rec, 20) - off
            return (total, _zip_ext(names))
        if len(buf) < (1 << 20):
            return None
        p += len(buf) - 4
    return None


def _zip_ext(names):
    joined = b"|".join(names)
    if b"[Content_Types].xml" in joined:
        if b"word/" in joined:
            return "docx"
        if b"xl/" in joined:
            return "xlsx"
        if b"ppt/" in joined:
            return "pptx"
    if b"META-INF/MANIFEST.MF" in joined:
        return "jar"
    return "zip"


MP3_BITRATES = {
    (3, 1): [0, 32, 64, 96, 128, 160, 192, 224, 256, 288, 320, 352, 384, 416, 448],
    (3, 2): [0, 32, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320, 384],
    (3, 3): [0, 32, 40, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320],
    (2, 1): [0, 32, 48, 56, 64, 80, 96, 112, 128, 144, 160, 176, 192, 224, 256],
    (2, 2): [0, 8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128, 144, 160],
    (2, 3): [0, 8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128, 144, 160],
}
MP3_RATES = {3: [44100, 48000, 32000], 2: [22050, 24000, 16000], 0: [11025, 12000, 8000]}


def mp3_frame_len(h):
    if len(h) < 4 or h[0] != 0xFF or (h[1] & 0xE0) != 0xE0:
        return 0
    ver = (h[1] >> 3) & 3
    layer = 4 - ((h[1] >> 1) & 3)
    if ver == 1 or layer == 4:
        return 0
    br_i = h[2] >> 4
    sr_i = (h[2] >> 2) & 3
    pad = (h[2] >> 1) & 1
    if br_i in (0, 15) or sr_i == 3:
        return 0
    v = 3 if ver == 3 else 2
    br = MP3_BITRATES[(v, layer)][br_i] * 1000
    sr = MP3_RATES[ver][sr_i]
    if layer == 1:
        return (12 * br // sr + pad) * 4
    if layer == 2 or ver == 3:
        return 144 * br // sr + pad
    return 72 * br // sr + pad


def carve_mp3(rd, off):
    pos = off
    h = rd(off, 10)
    if h[:3] == b"ID3":
        sz = ((h[6] & 0x7F) << 21) | ((h[7] & 0x7F) << 14) | ((h[8] & 0x7F) << 7) | (h[9] & 0x7F)
        pos = off + 10 + sz + (10 if h[5] & 0x10 else 0)
    frames = 0
    end = off + MAX_MP3
    while pos < end:
        fh = rd(pos, 4)
        fl = mp3_frame_len(fh)
        if fl == 0:
            if rd(pos, 3) == b"TAG":
                pos += 128
            break
        frames += 1
        pos += fl
    if frames < 20:
        return None
    return (pos - off, "mp3")


def detect(head):
    """Return (kind, ext_hint) for a cluster head, or None."""
    if head[:3] == b"\xff\xd8\xff":
        return "jpeg"
    if head[:8] == b"\x89PNG\r\n\x1a\n":
        return "png"
    if head[4:8] == b"ftyp":
        return "mp4"
    if head[:4] == b"RIFF":
        return "riff"
    if head[:5] == b"%PDF-":
        return "pdf"
    if head[:4] == b"PK\x03\x04":
        return "zip"
    if head[:6] in (b"GIF87a", b"GIF89a"):
        return "gif"
    if head[:3] == b"ID3":
        return "mp3"
    if head[:4] in (b"II*\x00", b"MM\x00*"):
        return "tiff"
    return None


def raw_ext(head):
    if head[8:10] == b"CR":
        return "cr2"
    if b"NIKON" in head[:512]:
        return "nef"
    if b"SONY" in head[:512]:
        return "arw"
    return "tif"


# --------------------------------------------------------------------------
# Scanner
# --------------------------------------------------------------------------

class Cancelled(Exception):
    pass


class Scanner:
    def __init__(self, vol, fs, emit, cancel_event):
        self.vol = vol
        self.fs = fs
        self.emit = emit
        self.cancel = cancel_event
        self.seen_dirs = set()
        self.extents = []       # sorted list of (start, end) byte ranges already claimed
        self.count = 0
        self._nofat_hint = {}
        self._size_hint = {}
        self.live_starts = {}   # start cluster -> (path, size) of files still on the card

    def log(self, text):
        self.emit(("log", text))

    def check_cancel(self):
        if self.cancel.is_set():
            raise Cancelled()

    def claim(self, start, end):
        bisect.insort(self.extents, (start, end))

    def claimed(self, off):
        i = bisect.bisect_right(self.extents, (off, 1 << 62)) - 1
        for j in range(i, max(-1, i - 8), -1):
            s, e = self.extents[j]
            if s <= off < e:
                return True
        return False

    # -- resolve an item's data runs and assess how intact it is -------------

    def resolve_runs(self, it):
        """Fill it.runs. Returns (allocated_clusters, total_clusters, chain_intact)."""
        fs = self.fs
        csize = fs.csize
        n = (it.size + csize - 1) // csize
        if it.source == "Carved" or not it.start_cluster:
            it.runs = [(it.start, it.size)]
            c0 = fs.off_cluster(it.start)
            return fs.allocated_in_range(c0, n), n, True
        start = it.start_cluster
        intact = True
        clusters = []
        if it.nofat:
            clusters = list(range(start, start + n))
        else:
            c = start
            seen = set()
            while fs.valid_cluster(c) and len(clusters) < n:
                clusters.append(c)
                if len(clusters) >= n:
                    break
                if c in seen:
                    intact = False
                    break
                seen.add(c)
                v = fs.fat_entry(c)
                if v == 0:
                    intact = False        # chain cleared: assume the rest is contiguous
                    c = c + 1
                elif fs.is_end(v):
                    intact = False        # chain shorter than the recorded size
                    c = c + 1
                else:
                    c = v
        alloc = 0
        runs = []
        remaining = it.size
        for c in clusters:
            if not fs.valid_cluster(c):
                break
            if not fs.is_free(c):
                alloc += 1
            ln = min(csize, remaining)
            off = fs.cluster_off(c)
            if runs and runs[-1][0] + runs[-1][1] == off:
                runs[-1] = (runs[-1][0], runs[-1][1] + ln)
            else:
                runs.append((off, ln))
            remaining -= ln
        if remaining > 0:
            intact = False
        it.runs = runs
        return alloc, n, intact

    def assess(self, it):
        fs = self.fs
        if it.is_dir:
            it.chance = "-"
            return
        if it.size <= 0 or (it.start is None and not it.fs_path):
            it.chance = "None"
            it.note = "empty"
            return
        if it.fs_path:
            it.chance = "Good"
            return
        if it.start_cluster and it.start_cluster in self.live_starts:
            live_path, live_size = self.live_starts[it.start_cluster]
            if live_size == it.size:
                it.chance = "None"
                it.source = "Renamed (still on card)"
                it.note = "now " + live_path
                it.runs = []
                return
        alloc, n, intact = self.resolve_runs(it)
        if not it.runs:
            it.chance = "None"
            it.note = "outside volume"
            return
        head = self.vol.read(it.runs[0][0], 512)
        sigs = SIG_BY_EXT.get(it.ext)
        sig_ok = None
        if sigs:
            sig_ok = any(head.startswith(s) for s in sigs)
        elif it.ext in FTYP_EXTS:
            sig_ok = head[4:8] == b"ftyp"
        frag = len(it.runs) > 1
        if alloc == 0 and sig_ok is False:
            it.chance = "Poor"
            it.note = "header does not match type"
        elif alloc == 0 and intact:
            it.chance = "Good"
            if frag:
                it.note = "%d fragments, chain intact" % len(it.runs)
        elif alloc == 0:
            it.chance = "Fair"
            it.note = "chain lost, assumed contiguous"
        elif alloc < n:
            it.chance = "Poor"
            it.note = "%d of %d clusters reused by newer files" % (alloc, n)
        else:
            it.chance = "Poor"
            it.note = "space reused by newer files"

    def add(self, it):
        self.assess(it)
        for off, ln in it.runs:
            self.claim(off, off + ln)
        self.count += 1
        self.emit(("item", it))

    # -- quick scan: directory tree ------------------------------------------

    def read_dir(self, start, nofat, size, deleted):
        fs = self.fs
        bufs = []
        if size is None or size == 0:
            size = None
        if deleted:
            # chain is gone: read contiguous clusters while they still look like directory data
            n = (size + fs.csize - 1) // fs.csize if size else 256
            for i in range(n):
                c = start + i
                if not fs.valid_cluster(c):
                    break
                d = self.vol.read(fs.cluster_off(c), fs.csize)
                if i > 0 and size is None and not self._dir_continues(d):
                    break
                bufs.append(d)
        else:
            for c in fs.chain(start, nofat, size, limit=1 << 16):
                bufs.append(self.vol.read(fs.cluster_off(c), fs.csize))
        return b"".join(bufs)

    def _dir_continues(self, d):
        if self.fs.kind == "exFAT":
            return d[0] in EXFAT_TYPES
        return d[0] not in (0x00,) and d[11] in (0x0F, 0x10, 0x20, 0x21, 0x22, 0x23, 0x00, 0x01, 0x02, 0x04, 0x06, 0x07, 0x24, 0x30)

    def parse(self, data):
        return parse_exfat_dir(data) if self.fs.kind == "exFAT" else parse_fat_dir(data)

    def quick_scan(self, drive_root=None):
        fs = self.fs
        fs.load_tables(self.log)
        self.log("Walking %s directory tree..." % fs.kind)
        stack = []
        if fs.root_fixed:
            off, ln = fs.root_fixed
            stack.append(("root-fixed", self.vol.read(off, ln), "", False, 0))
        else:
            stack.append((fs.root_cluster, None, "", False, 0))
        dirs = 0
        found = []
        while stack:
            self.check_cancel()
            key, data, path, in_deleted, depth = stack.pop()
            if data is None:
                if key in self.seen_dirs:
                    continue
                self.seen_dirs.add(key)
                data = self.read_dir(key, self._nofat_hint.get(key, False), self._size_hint.get(key), in_deleted)
            dirs += 1
            if dirs % 20 == 0:
                self.emit(("progress", None, "Scanned %d folders, %d deleted entries so far" % (dirs, len(found))))
            trashy = any(p.lower() in TRASH_NAMES for p in path.split("/") if p)
            for e in self.parse(data):
                deleted = e["deleted"] or in_deleted
                if e["is_dir"]:
                    if depth < 40 and fs.valid_cluster(e["start"]):
                        self._nofat_hint[e["start"]] = e["nofat"]
                        self._size_hint[e["start"]] = e["size"] if fs.kind == "exFAT" else None
                        stack.append((e["start"], None, (path + "/" if path else "") + e["name"], deleted, depth + 1))
                    if e["deleted"]:
                        found.append(Item(e["name"], path, 0, None, "Deleted folder", e["mtime"], is_dir=True,
                                          start_cluster=e["start"]))
                    continue
                start = fs.cluster_off(e["start"]) if fs.valid_cluster(e["start"]) else None
                if deleted:
                    src = "In deleted folder" if in_deleted else "Deleted"
                    found.append(Item(e["name"], path, e["size"], start, src, e["mtime"], nofat=e["nofat"],
                                      start_cluster=e["start"]))
                else:
                    if fs.valid_cluster(e["start"]):
                        self.live_starts[e["start"]] = ((path + "/" if path else "") + e["name"], e["size"])
                    if trashy and drive_root:
                        fp = os.path.join(drive_root, *(path.split("/") + [e["name"]])) if path else os.path.join(drive_root, e["name"])
                        found.append(Item(e["name"], path, e["size"], start, "Existing (trash folder)", e["mtime"], fs_path=fp))
        self.emit(("progress", None, "Checking %d entries..." % len(found)))
        for i, it in enumerate(found):
            if i % 100 == 0:
                self.check_cancel()
            self.add(it)
        self.log("Quick scan done: %d folders walked, %d items." % (dirs, self.count))

    _nofat_hint = {}
    _size_hint = {}

    # -- deep scan: signature carving over free clusters ----------------------

    def deep_scan(self, skip_allocated=True):
        fs = self.fs
        vol = self.vol
        fs.load_tables(self.log)
        csize = fs.csize
        chunk = max(CHUNK_MIN, csize)
        chunk -= chunk % csize
        start_off = fs.data_off
        total = vol.size
        off = start_off
        rd = vol.read
        seq = 0
        raw_open = None  # (start, ext) for TIFF-style files whose size is unknown
        last_ui = 0
        self.log("Deep scan: %s in %s clusters%s" % (human(total - start_off), human(csize),
                                                     " (free space only)" if skip_allocated else ""))
        while off < total:
            self.check_cancel()
            n = min(chunk, total - off)
            n -= n % SECTOR
            if n <= 0:
                break
            c_first = fs.off_cluster(off)
            n_clusters = max(1, n // csize)
            if skip_allocated and all(not fs.is_free(c) for c in range(c_first, c_first + n_clusters)):
                off += n
                continue
            buf = vol.read_chunk_cached(off, n)
            if not buf:
                break
            for k in range(0, len(buf) - 64, csize):
                pos = off + k
                c = c_first + k // csize
                if skip_allocated and not fs.is_free(c):
                    continue
                if self.claimed(pos):
                    continue
                head = buf[k:k + 64]
                kind = detect(head)
                if kind is None:
                    if fs.kind != "exFAT" and looks_like_fat_dir(buf[k:k + 64]):
                        self.orphan_dir(c)
                    elif fs.kind == "exFAT" and looks_like_exfat_dir(buf[k:k + 96]):
                        self.orphan_dir(c)
                    continue
                if raw_open:
                    self._close_raw(raw_open, pos)
                    raw_open = None
                res = None
                try:
                    if kind == "jpeg":
                        res = carve_jpeg(rd, pos)
                    elif kind == "png":
                        res = carve_png(rd, pos)
                    elif kind == "mp4":
                        res = carve_mp4(rd, pos, total - pos)
                    elif kind == "riff":
                        res = carve_riff(rd, pos)
                    elif kind == "pdf":
                        res = carve_pdf(rd, pos)
                    elif kind == "zip":
                        res = carve_zip(rd, pos)
                    elif kind == "gif":
                        res = carve_gif(rd, pos)
                    elif kind == "mp3":
                        res = carve_mp3(rd, pos)
                    elif kind == "tiff":
                        raw_open = (pos, raw_ext(buf[k:k + 512]))
                        continue
                except Cancelled:
                    raise
                except Exception:
                    res = None
                if res:
                    size, ext = res
                    if size > total - pos:
                        size = total - pos
                    seq += 1
                    it = Item("carved_%05d.%s" % (seq, ext), "Carved/" + ext, size, pos, "Carved")
                    self.add(it)
            if raw_open and off + n - raw_open[0] > MAX_RAW:
                self._close_raw(raw_open, raw_open[0] + MAX_RAW)
                raw_open = None
            off += n
            done = off - start_off
            if done - last_ui >= 64 << 20 or off >= total:
                last_ui = done
                self.emit(("progress", done / max(1, total - start_off),
                           "Deep scan %s / %s   |   %d items found" % (human(done), human(total - start_off), self.count)))
        if raw_open:
            self._close_raw(raw_open, min(total, raw_open[0] + MAX_RAW))
        vol.drop_cache()
        self.log("Deep scan finished. %d items total." % self.count)

    def _close_raw(self, raw_open, end):
        start, ext = raw_open
        size = end - start
        if size < 4096:
            return
        it = Item("carved_raw_%012x.%s" % (start, ext), "Carved/" + ext, size, start, "Carved")
        self.assess(it)
        if it.chance == "Good":
            it.chance = "Fair"
        it.note = "size estimated"
        self.claim(it.start, it.start + it.size)
        self.count += 1
        self.emit(("item", it))

    def orphan_dir(self, c):
        if c in self.seen_dirs:
            return
        self.seen_dirs.add(c)
        fs = self.fs
        data = self.read_dir(c, True, None, True)
        label = "[lost folder %d]" % c
        stack = [(data, label, 0)]
        while stack:
            data, path, depth = stack.pop()
            for e in self.parse(data):
                if e["is_dir"]:
                    if depth < 20 and fs.valid_cluster(e["start"]) and e["start"] not in self.seen_dirs:
                        self.seen_dirs.add(e["start"])
                        sub = self.read_dir(e["start"], True, e["size"] if fs.kind == "exFAT" else None, True)
                        stack.append((sub, path + "/" + e["name"], depth + 1))
                    continue
                start = fs.cluster_off(e["start"]) if fs.valid_cluster(e["start"]) else None
                it = Item(e["name"], path, e["size"], start, "In lost folder", e["mtime"], nofat=e["nofat"],
                          start_cluster=e["start"])
                self.add(it)


# --------------------------------------------------------------------------
# Recovery
# --------------------------------------------------------------------------

BAD = '<>:"/\\|?*'


def safe_name(s):
    s = "".join("_" if ch in BAD or ord(ch) < 32 else ch for ch in s).strip(" .")
    return s or "unnamed"


def unique_path(p):
    if not os.path.exists(p):
        return p
    base, ext = os.path.splitext(p)
    i = 1
    while True:
        q = "%s (%d)%s" % (base, i, ext)
        if not os.path.exists(q):
            return q
        i += 1


def recover_items(vol, fs, items, dest, emit, cancel):
    done_bytes = 0
    total = sum(it.size for it in items)
    ok = 0
    for idx, it in enumerate(items):
        if cancel.is_set():
            break
        folder = os.path.join(dest, *[safe_name(p) for p in it.folder.split("/") if p]) if it.folder else dest
        os.makedirs(folder, exist_ok=True)
        target = unique_path(os.path.join(folder, safe_name(it.name)))
        emit(("progress", done_bytes / max(1, total), "Recovering %d/%d: %s" % (idx + 1, len(items), it.name)))
        try:
            if it.fs_path:
                shutil.copy2(it.fs_path, target)
            else:
                runs = it.runs or [(it.start, it.size)]
                with open(target, "wb") as out:
                    for pos, remaining in runs:
                        while remaining > 0 and not cancel.is_set():
                            n = min(4 << 20, remaining)
                            data = vol.read(pos, n)
                            if not data:
                                break
                            out.write(data)
                            pos += len(data)
                            remaining -= len(data)
                            done_bytes += len(data)
                        if cancel.is_set():
                            break
            if it.mtime:
                ts = it.mtime.timestamp()
                os.utime(target, (ts, ts))
            ok += 1
        except Exception as ex:
            emit(("log", "Failed %s: %s" % (it.name, ex)))
    emit(("recovered", ok, len(items), dest))


# --------------------------------------------------------------------------
# Drive enumeration
# --------------------------------------------------------------------------

def list_drives():
    out = []
    mask = k32.GetLogicalDrives()
    for i, letter in enumerate(string.ascii_uppercase):
        if not (mask >> i) & 1:
            continue
        root = letter + ":\\"
        t = k32.GetDriveTypeW(root)
        name = ctypes.create_unicode_buffer(256)
        fsn = ctypes.create_unicode_buffer(256)
        k32.GetVolumeInformationW(root, name, 256, None, None, None, fsn, 256)
        tot = ctypes.c_ulonglong(0)
        k32.GetDiskFreeSpaceExW(root, None, ctypes.byref(tot), None)
        kind = {2: "Removable", 3: "Fixed", 4: "Network", 5: "CD"}.get(t, "Other")
        if t in (4, 5) or tot.value == 0:
            continue
        out.append({"letter": letter, "label": name.value or "(no label)", "fs": fsn.value or "?",
                    "size": tot.value, "type": kind, "removable": t == 2})
    out.sort(key=lambda d: (not d["removable"], d["letter"]))
    return out


def human(n):
    n = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return ("%d %s" % (n, unit)) if unit == "B" else ("%.1f %s" % (n, unit))
        n /= 1024


# --------------------------------------------------------------------------
# GUI
# --------------------------------------------------------------------------

BG = "#1b1f27"
PANEL = "#242a35"
FG = "#e8ebf0"
MUTED = "#8b93a5"
ACCENT = "#3d8bfd"
GOOD = "#3ec46d"
FAIR = "#e0b341"
POOR = "#e5544b"


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title(APP_NAME)
        self.geometry("1180x720")
        self.minsize(900, 560)
        self.configure(bg=BG)
        self.q = queue.Queue()
        self.cancel = threading.Event()
        self.worker = None
        self.vol = None
        self.fs = None
        self.items = []
        self.item_by_iid = {}
        self.drives = []
        self.sort_col = "chance"
        self.sort_rev = False
        self._style()
        self._build()
        self.refresh_drives()
        self.after(100, self._pump)
        self.protocol("WM_DELETE_WINDOW", self.on_close)

    # -- styling ------------------------------------------------------------

    def _style(self):
        st = ttk.Style(self)
        st.theme_use("clam")
        st.configure(".", background=BG, foreground=FG, fieldbackground=PANEL, bordercolor=PANEL,
                     font=("Segoe UI", 10))
        st.configure("TFrame", background=BG)
        st.configure("Panel.TFrame", background=PANEL)
        st.configure("TLabel", background=BG, foreground=FG)
        st.configure("Muted.TLabel", background=BG, foreground=MUTED)
        st.configure("Title.TLabel", background=BG, foreground=FG, font=("Segoe UI Semibold", 16))
        st.configure("TButton", background=PANEL, foreground=FG, padding=(12, 6), borderwidth=0)
        st.map("TButton", background=[("active", "#2f3745"), ("disabled", "#20242c")],
               foreground=[("disabled", MUTED)])
        st.configure("Accent.TButton", background=ACCENT, foreground="white", font=("Segoe UI Semibold", 10))
        st.map("Accent.TButton", background=[("active", "#2f77e0"), ("disabled", "#2b4a75")])
        st.configure("Go.TButton", background=GOOD, foreground="#0b1f12", font=("Segoe UI Semibold", 10))
        st.map("Go.TButton", background=[("active", "#35b061"), ("disabled", "#2c5a3c")])
        st.configure("TCombobox", fieldbackground=PANEL, background=PANEL, foreground=FG, arrowcolor=FG,
                     padding=4)
        st.map("TCombobox", fieldbackground=[("readonly", PANEL)], foreground=[("readonly", FG)])
        st.configure("TEntry", fieldbackground=PANEL, foreground=FG, insertcolor=FG, padding=4)
        st.map("TEntry", fieldbackground=[("!disabled", PANEL)], foreground=[("!disabled", FG)])
        st.configure("TCheckbutton", background=BG, foreground=FG)
        st.map("TCheckbutton", background=[("active", BG)])
        st.configure("TRadiobutton", background=BG, foreground=FG)
        st.map("TRadiobutton", background=[("active", BG)])
        st.configure("Horizontal.TProgressbar", troughcolor=PANEL, background=ACCENT, borderwidth=0, thickness=8)
        st.configure("Treeview", background=PANEL, fieldbackground=PANEL, foreground=FG, rowheight=24,
                     borderwidth=0)
        st.configure("Treeview.Heading", background="#2c3340", foreground=FG, relief="flat",
                     font=("Segoe UI Semibold", 10))
        st.map("Treeview", background=[("selected", ACCENT)], foreground=[("selected", "white")])
        st.map("Treeview.Heading", background=[("active", "#3a4354")])
        self.option_add("*TCombobox*Listbox.background", PANEL)
        self.option_add("*TCombobox*Listbox.foreground", FG)
        self.option_add("*TCombobox*Listbox.selectBackground", ACCENT)

    # -- layout -------------------------------------------------------------

    def _build(self):
        pad = {"padx": 14, "pady": (12, 0)}
        top = ttk.Frame(self)
        top.pack(fill="x", **pad)
        ttk.Label(top, text=APP_NAME, style="Title.TLabel").pack(side="left")
        ttk.Label(top, text="   deleted file recovery for SD cards and USB drives   |   source drive is never written to",
                  style="Muted.TLabel").pack(side="left", pady=(6, 0))

        # step 1: drive
        row1 = ttk.Frame(self)
        row1.pack(fill="x", **pad)
        ttk.Label(row1, text="1. Drive").pack(side="left")
        self.drive_var = tk.StringVar()
        self.drive_box = ttk.Combobox(row1, textvariable=self.drive_var, state="readonly", width=52)
        self.drive_box.pack(side="left", padx=(10, 6))
        ttk.Button(row1, text="Refresh", command=self.refresh_drives).pack(side="left")
        ttk.Button(row1, text="Open image file...", command=self.pick_image).pack(side="left", padx=(6, 0))
        self.drive_info = ttk.Label(row1, text="", style="Muted.TLabel")
        self.drive_info.pack(side="left", padx=(14, 0))

        # step 2: scan
        row2 = ttk.Frame(self)
        row2.pack(fill="x", **pad)
        ttk.Label(row2, text="2. Scan").pack(side="left")
        self.mode = tk.StringVar(value="both")
        for txt, val in (("Quick (deleted entries, seconds)", "quick"),
                         ("Deep (signature carving, slower)", "deep"),
                         ("Both", "both")):
            ttk.Radiobutton(row2, text=txt, value=val, variable=self.mode).pack(side="left", padx=(10, 0))
        self.free_only = tk.BooleanVar(value=True)
        ttk.Checkbutton(row2, text="Deep: free space only", variable=self.free_only).pack(side="left", padx=(16, 0))
        self.stop_btn = ttk.Button(row2, text="Stop", command=self.stop_scan, state="disabled")
        self.stop_btn.pack(side="right")
        self.scan_btn = ttk.Button(row2, text="Start scan", style="Accent.TButton", command=self.start_scan)
        self.scan_btn.pack(side="right", padx=(0, 8))

        prog = ttk.Frame(self)
        prog.pack(fill="x", padx=14, pady=(10, 0))
        self.pbar = ttk.Progressbar(prog, mode="determinate", maximum=1000)
        self.pbar.pack(fill="x")
        self.status = ttk.Label(prog, text="Pick the drive and press Start scan.", style="Muted.TLabel")
        self.status.pack(anchor="w", pady=(4, 0))

        # filter row
        row3 = ttk.Frame(self)
        row3.pack(fill="x", **pad)
        ttk.Label(row3, text="3. Results").pack(side="left")
        ttk.Label(row3, text="Search", style="Muted.TLabel").pack(side="left", padx=(16, 4))
        self.filter_var = tk.StringVar()
        self.filter_var.trace_add("write", lambda *_: self.refill())
        ttk.Entry(row3, textvariable=self.filter_var, width=24).pack(side="left")
        ttk.Label(row3, text="Type", style="Muted.TLabel").pack(side="left", padx=(12, 4))
        self.type_var = tk.StringVar(value="All")
        tb = ttk.Combobox(row3, textvariable=self.type_var, state="readonly", width=12,
                          values=["All", "Photos", "Videos", "Audio", "Documents", "Other"])
        tb.pack(side="left")
        tb.bind("<<ComboboxSelected>>", lambda e: self.refill())
        self.hide_poor = tk.BooleanVar(value=False)
        ttk.Checkbutton(row3, text="Hide poor / empty", variable=self.hide_poor,
                        command=self.refill).pack(side="left", padx=(12, 0))
        self.count_lbl = ttk.Label(row3, text="", style="Muted.TLabel")
        self.count_lbl.pack(side="right")

        # results table
        mid = ttk.Frame(self)
        mid.pack(fill="both", expand=True, padx=14, pady=(8, 0))
        cols = ("name", "folder", "type", "size", "modified", "chance", "found", "details")
        self.tree = ttk.Treeview(mid, columns=cols, show="headings", selectmode="extended")
        heads = {"name": ("File name", 290), "folder": ("Folder", 170), "type": ("Type", 55),
                 "size": ("Size", 85), "modified": ("Modified", 130), "chance": ("Chance", 65),
                 "found": ("Found by", 120), "details": ("Details", 230)}
        for c in cols:
            self.tree.heading(c, text=heads[c][0], command=lambda c=c: self.sort_by(c))
            self.tree.column(c, width=heads[c][1], anchor="e" if c == "size" else "w", stretch=(c in ("name", "folder")))
        vsb = ttk.Scrollbar(mid, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=vsb.set)
        self.tree.pack(side="left", fill="both", expand=True)
        vsb.pack(side="right", fill="y")
        self.tree.tag_configure("Good", foreground=GOOD)
        self.tree.tag_configure("Fair", foreground=FAIR)
        self.tree.tag_configure("Poor", foreground=POOR)
        self.tree.tag_configure("None", foreground=MUTED)
        self.tree.tag_configure("-", foreground=MUTED)
        self.tree.bind("<<TreeviewSelect>>", lambda e: self.update_selection_label())

        # step 4: recover
        row4 = ttk.Frame(self)
        row4.pack(fill="x", padx=14, pady=(10, 12))
        ttk.Label(row4, text="4. Recover").pack(side="left")
        ttk.Button(row4, text="Select all", command=self.select_all).pack(side="left", padx=(10, 0))
        ttk.Button(row4, text="Select good", command=self.select_good).pack(side="left", padx=(6, 0))
        ttk.Button(row4, text="Clear", command=lambda: self.tree.selection_remove(self.tree.selection())).pack(side="left", padx=(6, 0))
        ttk.Label(row4, text="Save to", style="Muted.TLabel").pack(side="left", padx=(16, 4))
        self.dest_var = tk.StringVar(value=os.path.join(os.path.expanduser("~"), "Desktop", "Recovered Files"))
        ttk.Entry(row4, textvariable=self.dest_var, width=40).pack(side="left")
        ttk.Button(row4, text="Browse...", command=self.pick_dest).pack(side="left", padx=(6, 0))
        self.rec_btn = ttk.Button(row4, text="Recover selected", style="Go.TButton", command=self.recover, state="disabled")
        self.rec_btn.pack(side="right")
        self.sel_lbl = ttk.Label(row4, text="", style="Muted.TLabel")
        self.sel_lbl.pack(side="right", padx=(0, 12))

    # -- drives -------------------------------------------------------------

    def refresh_drives(self):
        self.drives = list_drives()
        vals = ["%s:  %s  (%s, %s, %s)" % (d["letter"], d["label"], d["fs"], human(d["size"]), d["type"]) for d in self.drives]
        self.drive_box["values"] = vals
        self.image_path = None
        if vals:
            pick = 0
            for i, d in enumerate(self.drives):
                if d["removable"]:
                    pick = i
                    break
            self.drive_box.current(pick)
        self.drive_box.bind("<<ComboboxSelected>>", lambda e: setattr(self, "image_path", None))

    def pick_image(self):
        p = filedialog.askopenfilename(title="Open disk image", filetypes=[("Disk images", "*.img *.dd *.bin *.raw *.iso"), ("All files", "*.*")])
        if p:
            self.image_path = p
            self.drive_var.set("Image: " + p)

    def current_source(self):
        if getattr(self, "image_path", None):
            return self.image_path, None
        idx = self.drive_box.current()
        if idx < 0 or idx >= len(self.drives):
            return None, None
        d = self.drives[idx]
        return "\\\\.\\%s:" % d["letter"], d["letter"] + ":\\"

    # -- scanning -----------------------------------------------------------

    def start_scan(self):
        path, root = self.current_source()
        if not path:
            messagebox.showwarning(APP_NAME, "Pick a drive first.")
            return
        self.cancel.clear()
        self.items = []
        self.item_by_iid = {}
        self.tree.delete(*self.tree.get_children())
        self.pbar["value"] = 0
        self.scan_btn["state"] = "disabled"
        self.stop_btn["state"] = "normal"
        self.rec_btn["state"] = "disabled"
        self.drive_box["state"] = "disabled"
        mode = self.mode.get()
        free_only = self.free_only.get()
        if self.vol:
            self.vol.close()
        self.vol = None
        self.fs = None

        def work():
            emit = self.q.put
            try:
                vol = RawVolume(path)
                self.vol = vol
                fs = FileSystem(vol)
                self.fs = fs
                emit(("fsinfo", "%s, %s clusters, %s" % (fs.kind, human(fs.csize), human(vol.size))))
                sc = Scanner(vol, fs, emit, self.cancel)
                if mode in ("quick", "both"):
                    sc.quick_scan(root)
                if mode in ("deep", "both"):
                    sc.deep_scan(skip_allocated=free_only)
                emit(("done", "Scan complete: %d items found." % sc.count))
            except Cancelled:
                emit(("done", "Scan stopped."))
            except Exception as ex:
                emit(("error", "%s: %s" % (type(ex).__name__, ex)))

        self.worker = threading.Thread(target=work, daemon=True)
        self.worker.start()

    def stop_scan(self):
        self.cancel.set()
        self.status["text"] = "Stopping..."

    def _pump(self):
        try:
            for _ in range(500):
                msg = self.q.get_nowait()
                self._handle(msg)
        except queue.Empty:
            pass
        self.after(100, self._pump)

    def _handle(self, msg):
        kind = msg[0]
        if kind == "item":
            it = msg[1]
            self.items.append(it)
            if self._passes(it):
                self._insert(it)
            self.count_lbl["text"] = "%d items" % len(self.items)
        elif kind == "progress":
            frac, text = msg[1], msg[2]
            if frac is not None:
                self.pbar["value"] = frac * 1000
            self.status["text"] = text
        elif kind == "log":
            self.status["text"] = msg[1]
        elif kind == "fsinfo":
            self.drive_info["text"] = msg[1]
        elif kind in ("done", "error"):
            self.status["text"] = msg[1]
            if kind == "done":
                self.pbar["value"] = 1000
            else:
                messagebox.showerror(APP_NAME, msg[1])
            self.scan_btn["state"] = "normal"
            self.stop_btn["state"] = "disabled"
            self.drive_box["state"] = "readonly"
            self.rec_btn["state"] = "normal" if self.items else "disabled"
            self.refill()
        elif kind == "recovered":
            ok, n, dest = msg[1], msg[2], msg[3]
            self.pbar["value"] = 1000
            self.status["text"] = "Recovered %d of %d files to %s" % (ok, n, dest)
            self.scan_btn["state"] = "normal"
            self.rec_btn["state"] = "normal"
            self.stop_btn["state"] = "disabled"
            if messagebox.askyesno(APP_NAME, "Recovered %d of %d files to:\n%s\n\nOpen the folder?" % (ok, n, dest)):
                subprocess.Popen(["explorer", dest])

    # -- table --------------------------------------------------------------

    TYPE_GROUPS = {
        "Photos": {"jpg", "jpeg", "png", "gif", "heic", "heif", "avif", "webp", "tif", "tiff", "cr2", "nef", "arw", "dng", "bmp"},
        "Videos": {"mp4", "mov", "m4v", "3gp", "avi", "lrf", "mkv", "insv", "360", "mts"},
        "Audio": {"mp3", "wav", "m4a", "aac", "flac"},
        "Documents": {"pdf", "docx", "xlsx", "pptx", "doc", "xls", "ppt", "txt", "zip", "jar", "csv"},
    }

    def _passes(self, it):
        f = self.filter_var.get().strip().lower()
        if f and f not in it.name.lower() and f not in it.folder.lower():
            return False
        t = self.type_var.get()
        if t != "All":
            if t == "Other":
                if any(it.ext in g for g in self.TYPE_GROUPS.values()):
                    return False
            elif it.ext not in self.TYPE_GROUPS[t]:
                return False
        if self.hide_poor.get() and it.chance in ("Poor", "None", "-"):
            return False
        return True

    def _insert(self, it):
        when = it.mtime.strftime("%Y-%m-%d %H:%M") if it.mtime else ""
        iid = self.tree.insert("", "end", values=(it.name, it.folder, "folder" if it.is_dir else it.ext,
                                                  "" if it.is_dir else human(it.size), when, it.chance, it.source,
                                                  it.note),
                               tags=(it.chance,))
        self.item_by_iid[iid] = it

    def refill(self):
        self.tree.delete(*self.tree.get_children())
        self.item_by_iid = {}
        shown = [it for it in self.items if self._passes(it)]
        if self.sort_col:
            shown.sort(key=self._sort_key, reverse=self.sort_rev)
        for it in shown:
            self._insert(it)
        self.count_lbl["text"] = "%d of %d items" % (len(shown), len(self.items))
        self.update_selection_label()

    def _sort_key(self, it):
        c = self.sort_col
        if c == "size":
            return it.size
        if c == "modified":
            return it.mtime.timestamp() if it.mtime else 0
        if c == "chance":
            return ({"Good": 0, "Fair": 1, "Poor": 2, "None": 3, "-": 4}.get(it.chance, 5), -it.size)
        if c == "type":
            return it.ext
        if c == "folder":
            return it.folder.lower()
        if c == "found":
            return it.source
        if c == "details":
            return it.note
        return it.name.lower()

    def sort_by(self, col):
        if self.sort_col == col:
            self.sort_rev = not self.sort_rev
        else:
            self.sort_col, self.sort_rev = col, False
        self.refill()

    def select_all(self):
        self.tree.selection_set(self.tree.get_children())
        self.update_selection_label()

    def select_good(self):
        good = [iid for iid, it in self.item_by_iid.items() if it.chance in ("Good", "Fair") and not it.is_dir]
        self.tree.selection_set(good)
        self.update_selection_label()

    def update_selection_label(self):
        sel = [self.item_by_iid[i] for i in self.tree.selection() if i in self.item_by_iid]
        files = [it for it in sel if not it.is_dir]
        self.sel_lbl["text"] = "%d selected, %s" % (len(files), human(sum(it.size for it in files))) if files else ""

    # -- recovery -----------------------------------------------------------

    def pick_dest(self):
        p = filedialog.askdirectory(title="Choose where to save recovered files", mustexist=False)
        if p:
            self.dest_var.set(p)

    def recover(self):
        sel = [self.item_by_iid[i] for i in self.tree.selection() if i in self.item_by_iid]
        files = [it for it in sel if not it.is_dir and it.size > 0 and (it.runs or it.fs_path)]
        if not files:
            messagebox.showinfo(APP_NAME, "Select one or more files in the list first (Ctrl+click or Shift+click for several, or use Select good).")
            return
        dest = self.dest_var.get().strip()
        if not dest:
            self.pick_dest()
            dest = self.dest_var.get().strip()
            if not dest:
                return
        src_path, root = self.current_source()
        if root and os.path.splitdrive(os.path.abspath(dest))[0].upper() == root[:2].upper():
            messagebox.showerror(APP_NAME, "Do not save onto the drive you are recovering from. Pick a folder on another drive (for example your Desktop).")
            return
        try:
            os.makedirs(dest, exist_ok=True)
        except Exception as ex:
            messagebox.showerror(APP_NAME, "Cannot create folder:\n%s" % ex)
            return
        self.cancel.clear()
        self.scan_btn["state"] = "disabled"
        self.rec_btn["state"] = "disabled"
        self.stop_btn["state"] = "normal"
        vol, fs = self.vol, self.fs
        t = threading.Thread(target=recover_items, args=(vol, fs, files, dest, self.q.put, self.cancel), daemon=True)
        t.start()

    def on_close(self):
        self.cancel.set()
        if self.vol:
            self.vol.close()
        self.destroy()


def main():
    app = App()
    app.mainloop()


if __name__ == "__main__":
    main()
