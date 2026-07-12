/**
 * Optional, purely client-side folder integration built on the File System
 * Access API. Chromium-only (Chrome/Edge/Opera) - Firefox and Safari don't
 * implement showDirectoryPicker(), so isDirectoryPickerSupported() is false
 * there and every caller must treat that as "feature unavailable" rather
 * than erroring, leaving current behavior (zip download) unchanged for
 * those browsers. Two things live here:
 *   - "Skip albums I already have": listExistingAlbumFolders() reads what's
 *     already in the chosen folder to cross off duplicates before download.
 *   - "Save straight to the folder": saveZipToFolder() writes a completed
 *     download's files directly into an <Artist> - <Album> subfolder,
 *     instead of the friend getting a .zip to save/extract by hand.
 * Both need real read/write access, which is why pickDownloadFolder()
 * requests mode: "readwrite" - one picker interaction covers both.
 *
 * Deliberately has no persistence layer of its own: the chosen folder handle
 * lives only in React state for the current page load. Re-picking is a
 * one-click action, and the reason the duplicate-check exists (catching
 * duplicates "even a long time later") is satisfied by reading the real
 * folder fresh each time rather than the app remembering anything about
 * past downloads itself - no server involvement, no expiring cache.
 */

import { unzipSync } from "fflate";

export function isDirectoryPickerSupported(): boolean {
  return typeof window !== "undefined" && "showDirectoryPicker" in window;
}

// Mirrors album_downloader.py's sanitize_filename() exactly, so a name
// computed here matches the folder name the downloader actually writes.
export function sanitizeFilename(name: string): string {
  return name
    .replace(/\\/g, "-")
    .replace(/\//g, "-")
    .replace(/[:*?"<>|]/g, "")
    .trim();
}

export function albumFolderName(artist: string, album: string): string {
  return sanitizeFilename(`${artist} - ${album}`);
}

export async function pickDownloadFolder(): Promise<FileSystemDirectoryHandle> {
  return window.showDirectoryPicker({ id: "album-downloads", mode: "readwrite" });
}

/** Returns the names of every subdirectory directly inside `dirHandle`. */
export async function listExistingAlbumFolders(
  dirHandle: FileSystemDirectoryHandle,
): Promise<Set<string>> {
  const names = new Set<string>();
  for await (const [name, entry] of dirHandle.entries()) {
    if (entry.kind === "directory") names.add(name);
  }
  return names;
}

/**
 * Unzips a completed download's bytes (fetched from GET /download/{job_id} -
 * the server keeps producing the same zip it always has, only the frontend's
 * handling of it changed) and writes each file into an <Artist> - <Album>
 * subfolder of `dirHandle`. The zip's internal layout is flat (no "Artist -
 * Album/" prefix - see album_downloader's web adapter), so unzipped entry
 * names are already the final filenames (e.g. "00 cover.jpg", "01 Title.mp3").
 * `create: true` on both the subfolder and each file means this also just
 * works for a re-download into an already-existing folder (overwrites).
 */
export async function saveZipToFolder(
  dirHandle: FileSystemDirectoryHandle,
  artist: string,
  album: string,
  zipBytes: ArrayBuffer,
): Promise<void> {
  const files = unzipSync(new Uint8Array(zipBytes));
  const albumDir = await dirHandle.getDirectoryHandle(albumFolderName(artist, album), { create: true });
  for (const [name, data] of Object.entries(files)) {
    const fileHandle = await albumDir.getFileHandle(name, { create: true });
    const writable = await fileHandle.createWritable();
    await writable.write(data);
    await writable.close();
  }
}
