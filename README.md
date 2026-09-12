# Talwar File Recovery

A small, free Windows desktop app that recovers deleted files from SD cards, microSD cards and USB drives formatted as **exFAT** or **FAT32/FAT16**.

One Python file. No third-party packages. The source drive is only ever opened for reading.

![Talwar File Recovery scanning a DJI Pocket 4 card](docs/screenshot.png)

## Why this exists

I deleted wedding footage from the microSD card in my DJI Pocket 4. Every recovery product I looked at wanted $90 to $200 to save a single file, on a "scan free, pay to recover" model. Instead of paying, I looked at the card's raw data with Claude Code, found that the deleted videos were fully intact, and built the narrow tool I actually needed in an evening. The first real test rebuilt a 3.3 GB 4K clip from 612 fragments in 29 seconds, verified frame-accurate. This is that tool, cleaned up and open sourced.

## What it does

1. **Pick a drive** from a dropdown (removable drives are listed first). You can also open a raw disk image (`.img`, `.dd`, `.bin`).
2. **Scan** in one of two modes:
   - **Quick scan** (seconds): walks the file system and lists every entry marked deleted, with the original file name, folder, size and date. It also descends into deleted folders and follows the surviving allocation chain, so fragmented files (very common with cameras that write a video and a proxy at the same time) are reassembled correctly rather than copied as a contiguous block.
   - **Deep scan** (slower): reads every free cluster and rebuilds files from their signatures. Finds JPEG, PNG, GIF, WebP, TIFF/RAW (CR2, NEF, ARW, DNG), MP4/MOV/HEIC/3GP/M4A, AVI, WAV, MP3, PDF, ZIP and Office (docx/xlsx/pptx), plus orphaned directory clusters. Use this when the quick scan doesn't show the file you need.
3. **Review the results.** Every file gets an honest rating before you recover anything:
   - **Good**: no cluster has been reused and the header matches the file type.
   - **Fair**: the allocation chain is gone, so the file is assumed contiguous.
   - **Poor**: some of the file's space has been overwritten by newer files.
   - **None**: nothing to recover (empty, or a rename whose data is still on the card under the new name).
   Filter by name, by type (photos, videos, audio, documents), or hide the poor ones.
4. **Recover** with one click. Select rows (Ctrl+click, Shift+click, or **Select good**), choose a folder, press **Recover selected**. Original folder structure and timestamps are preserved. The app refuses to save onto the drive it is recovering from.

## Requirements

- Windows 10 or 11
- Python 3.10 or newer, with tkinter (included in the standard python.org installer)
- Nothing else. No `pip install`.

## Run it

Double-click `Run Talwar File Recovery.bat`, or from a terminal:

```
py -3 talwar_file_recovery.py
```

If Windows refuses raw access to a drive (rare for removable media, common for internal disks), right-click the `.bat` and choose **Run as administrator**.

## Important advice before you scan

- **Stop using the card.** Every new file written to it can overwrite the data you want back. Take it out of the camera and do not copy anything onto it.
- **Recover to a different drive.** The app enforces this.
- Deleted-file recovery is best effort. A "Good" rating means the space has not been reused and the data looks right; it is not a guarantee that every byte survived.

## How it works, briefly

- Opens the volume with `CreateFileW("\\\\.\\E:")` and reads sectors directly, bypassing the file system driver.
- Parses the exFAT or FAT boot sector, loads the FAT and (for exFAT) the allocation bitmap.
- Walks the directory tree. On exFAT, deleted entries are the ones with the in-use bit cleared (`0x05` / `0x40` / `0x41`); on FAT they start with `0xE5`. Long names are reconstructed from the LFN entries.
- For each deleted file it follows the FAT chain from the first cluster. If the chain is intact (it usually is, most drivers do not clear it), the file is rebuilt fragment by fragment. If the chain was cleared, it falls back to a contiguous read.
- The rating comes from checking every cluster of the file against the allocation bitmap / FAT and comparing the first bytes against the expected signature for the extension.
- Deep scan checks each cluster boundary in free space for known file signatures and parses the container format (JPEG markers, PNG chunks, MP4 atoms, ZIP local headers, and so on) to find the exact end of the file.

## Tested on

- 512 GB exFAT microSD card from a DJI Pocket 4 (128 KB clusters, heavily fragmented video files). Quick scan found 1,215 deleted entries in about 7 seconds and correctly identified renamed files, overwritten files and 26 fully recoverable videos.

The FAT32 / FAT16 code paths follow the Microsoft specification but have had less real-world testing. If you try it on a FAT32 card, please open an issue with what you saw, good or bad.

## Limitations

- Windows only (raw volume access uses Win32 APIs).
- NTFS, APFS, ext4 and HFS+ are not supported. Most memory cards are exFAT or FAT32, which are.
- No preview pane yet.
- Deep scan of a large card takes a while (it reads all free space). You can stop it at any time and keep what it has found.
- Deleted files whose space has been reused cannot be brought back by any software.

## Contributing

Issues and pull requests are welcome. Keep it a single file with no dependencies unless there is a very good reason. If you add a file-type carver, add it to `detect()` and follow the pattern of the existing `carve_*` functions: take a reader and an offset, return `(size, extension)` or `None`.

## License

MIT. See [LICENSE](LICENSE).

Built with [Claude Code](https://claude.com/claude-code).
