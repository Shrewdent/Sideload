import webview
import threading
import itertools
import json
import subprocess
from pathlib import Path

# Import the engine you built in steps 2-8
import yt_tagger as engine


GUI_FILE = Path(__file__).parent / "gui" / "index.html"

# Launch background per theme so the window doesn't flash the wrong color
THEME_BG = {
    "violet": "#14101d",
    "nebula": "#07060f",
    "aurora": "#071013",
    "moss":   "#0e1512",
    "tide":   "#0c141f",
}


class Api:
    """Methods on this class are callable from JavaScript as
    window.pywebview.api.<method>().  Anything returned is passed
    back to JS as the resolved promise value."""

    def __init__(self):
        self._window = None
        # ---- download queue state ----
        self._queue = []                       # list of item dicts
        self._queue_lock = threading.Lock()
        self._worker = None
        self._ids = itertools.count(1)
        # ---- fuzzy-duplicate handshake (one at a time; the JS
        # confirm() dialog is modal, so this can never overlap) ----
        self._fuzzy_event = threading.Event()
        self._fuzzy_keep = False

    def set_window(self, window):
        self._window = window

    # ---- small helper to call a JS function from Python ----
    def _js(self, fn_call):
        if self._window:
            self._window.evaluate_js(fn_call)

    # ---------------------------------------------------------
    # THEME
    # ---------------------------------------------------------
    def get_theme(self):
        return engine.get_setting("theme", "violet")

    def set_theme(self, theme):
        engine.set_setting("theme", theme)
        return {"ok": True}

    # ---------------------------------------------------------
    # LIBRARY
    # ---------------------------------------------------------
    def get_library(self):
        """Return the full library list (newest first)."""
        lib = engine.load_library()
        return list(reversed(lib))

    def search(self, query):
        """Return library entries matching a query."""
        return engine.search_library(query)

    def delete_from_library(self, file_name):
        """Remove a library entry by its filename."""
        engine.delete_entry(file_name)
        return {"ok": True}

    def get_clipboard(self):
        """Return the current clipboard text via PowerShell (no browser permission needed)."""
        try:
            result = subprocess.run(
                ["powershell", "-NoProfile", "-command", "Get-Clipboard"],
                capture_output=True, text=True, timeout=3
            )
            return result.stdout.strip() or None
        except Exception:
            return None

    def get_output_dir(self):
        """Return the resolved output folder path."""
        return str(engine.OUTPUT_DIR)

    def pick_output_dir(self):
        """Open a folder picker and update the output directory."""
        result = self._window.create_file_dialog(webview.FOLDER_DIALOG)
        if not result:
            return None
        path = result[0] if isinstance(result, (list, tuple)) else result
        engine.set_output_dir(path)
        return path

    def _resolve_mp3_path(self, file_name, stored_path=None):
        """Resolve the actual path of an MP3.
        Tries stored_path first, then falls back to OUTPUT_DIR / file_name
        (or the absolute path itself if file_name is absolute)."""
        if stored_path:
            p = Path(stored_path)
            if p.exists():
                return p
        p = Path(file_name)
        return p if p.is_absolute() else engine.OUTPUT_DIR / file_name

    def reveal_in_explorer(self, file_name, stored_path=None):
        """Open Windows Explorer with the song file selected."""
        full_path = self._resolve_mp3_path(file_name, stored_path)
        if full_path.exists():
            subprocess.Popen(['explorer', '/select,', str(full_path)])
            return {"ok": True}
        return {"ok": False, "error": f"File not found: {Path(file_name).name}"}

    def send_to_apple_music(self, file_name, stored_path=None):
        """Open the song's MP3 with the system default player."""
        full_path = self._resolve_mp3_path(file_name, stored_path)
        if not full_path.exists():
            return {"ok": False, "error": f"File not found: {Path(file_name).name}"}
        return engine.send_to_apple_music(str(full_path))

    # ---------------------------------------------------------
    # SINGLE SONG  (probe -> review -> queue)
    # ---------------------------------------------------------
    def probe(self, url):
        """Check for a URL duplicate and fetch metadata guesses
        WITHOUT downloading yet. Returns a dict the GUI uses to
        build the review screen."""
        dupe = engine.find_duplicate(url, "", "")
        url_dupe = dupe if (dupe and dupe.get("match_type") == "url") else None

        try:
            import yt_dlp
            opts = {"quiet": True, "noplaylist": True, "skip_download": True}
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=False)
            guess = engine.parse_metadata(info)
            thumb = info.get("thumbnail")
        except Exception as e:
            return {"ok": False, "error": engine.friendly_error(e)}

        return {
            "ok": True,
            "url": url,
            "title": guess.get("title") or "",
            "artist": guess.get("artist") or "",
            "year": (info.get("upload_date") or "")[:4],
            "thumbnail": thumb,
            "url_duplicate": (
                {"title": url_dupe.get("title"), "artist": url_dupe.get("artist")}
                if url_dupe else None
            ),
        }

    # ---------------------------------------------------------
    # QUEUE
    # ---------------------------------------------------------
    def queue_single(self, url, title, artist, album, genre='', year='', tracknumber=''):
        """Add a reviewed song to the download queue and make sure
        the worker thread is running. Returns immediately, so the
        user can go paste the next URL."""
        item = {
            "id": next(self._ids),
            "url": url, "title": title, "artist": artist, "album": album,
            "genre": genre, "year": year, "tracknumber": tracknumber,
            "status": "queued", "progress": 0, "label": "Queued",
        }
        with self._queue_lock:
            self._queue.append(item)
        self._push_queue()
        self._ensure_worker()
        return {"queued": True, "id": item["id"]}

    def clear_finished(self):
        """Drop done / errored / skipped items from the queue display."""
        with self._queue_lock:
            self._queue = [i for i in self._queue
                           if i["status"] in ("queued", "working", "waiting")]
        self._push_queue()
        return {"ok": True}

    def resolve_fuzzy(self, keep):
        """Called when the user answers the fuzzy-duplicate prompt."""
        self._fuzzy_keep = bool(keep)
        self._fuzzy_event.set()
        return {"ok": True}

    def _push_queue(self):
        """Send the queue state (public fields only) to the GUI."""
        with self._queue_lock:
            public = [{k: i[k] for k in ("id", "title", "artist", "status", "progress", "label")}
                      for i in self._queue]
        self._js(f"window.onQueueUpdate({json.dumps(public)})")

    def _ensure_worker(self):
        if self._worker and self._worker.is_alive():
            return
        self._worker = threading.Thread(target=self._drain_queue, daemon=True)
        self._worker.start()

    def _next_queued(self):
        with self._queue_lock:
            for i in self._queue:
                if i["status"] == "queued":
                    return i
        return None

    def _drain_queue(self):
        """Worker: process queued items one at a time until none remain."""
        while True:
            item = self._next_queued()
            if not item:
                break
            self._process_item(item)

    def _process_item(self, item):
        """Download + dupe-check + tag + art + library for one queue item."""

        def hook(d):
            # yt-dlp progress hook -> live percent on the queue row
            try:
                if d.get("status") == "downloading":
                    total = d.get("total_bytes") or d.get("total_bytes_estimate")
                    if total:
                        pct = int(d.get("downloaded_bytes", 0) * 100 / total)
                        if pct != item["progress"]:
                            item["progress"] = pct
                            speed = d.get("speed")
                            if speed:
                                item["label"] = f"Downloading {pct}% \u00b7 {speed/1048576:.1f} MB/s"
                            else:
                                item["label"] = f"Downloading {pct}%"
                            self._push_queue()
                elif d.get("status") == "finished":
                    item["progress"] = 100
                    item["label"] = "Converting to MP3\u2026"
                    self._push_queue()
            except Exception:
                pass  # never let a progress update kill the download

        try:
            item["status"] = "working"
            item["label"] = "Starting\u2026"
            self._push_queue()

            mp3, thumb, info = engine.download_audio(item["url"], progress_hook=hook)

            # Fuzzy duplicate check (URL dupes were already surfaced at review time)
            dupe = engine.find_duplicate(item["url"], item["title"], item["artist"])
            if dupe and dupe.get("match_type") == "fuzzy":
                item["status"] = "waiting"
                item["label"] = "Possible duplicate \u2014 waiting for you"
                self._push_queue()
                payload = json.dumps({
                    "id": item["id"],
                    "title": dupe.get("title"), "artist": dupe.get("artist"),
                    "score": int(dupe.get("score", 0) * 100),
                })
                self._fuzzy_event.clear()
                self._js(f"window.onFuzzyDuplicate({payload})")
                self._fuzzy_event.wait()          # confirm() is modal, resolves fast
                if not self._fuzzy_keep:
                    try:
                        if mp3.exists():
                            mp3.unlink()
                    except Exception:
                        pass
                    item["status"] = "skipped"
                    item["label"] = "Discarded duplicate"
                    self._push_queue()
                    return
                item["status"] = "working"

            item["label"] = "Tagging\u2026"
            self._push_queue()
            engine.write_tags(mp3,
                              title=item["title"] or None, artist=item["artist"] or None,
                              album=item["album"] or None, genre=item["genre"] or None,
                              year=item["year"] or None, tracknumber=item["tracknumber"] or None)

            item["label"] = "Embedding cover art\u2026"
            self._push_queue()
            engine.embed_cover_art(mp3, thumb)
            engine.add_to_library(mp3, item["title"], item["artist"], item["url"],
                                  item["album"] or None, thumb=thumb)

            item["status"] = "done"
            item["progress"] = 100
            item["label"] = "Done"
            self._push_queue()
        except Exception as e:
            item["status"] = "error"
            item["label"] = engine.friendly_error(e)
            self._push_queue()

    # ---------------------------------------------------------
    # PLAYLIST  (fully automatic, reports live per-song counts)
    # ---------------------------------------------------------
    def convert_playlist(self, url, max_items):
        def work():
            try:
                cap = int(max_items) if str(max_items).strip().isdigit() else None
                self._js("window.onProgress('Reading playlist\u2026')")

                def prog(i, total, label):
                    msg = f"Playlist {i}/{total} \u2014 {label}" if label else f"Playlist {i}/{total}"
                    self._js(f"window.onProgress({json.dumps(msg)})")

                results = engine.download_playlist(url, max_items=cap, progress=prog)
                count = len(results) if results else 0
                if count == 0:
                    self._js(f"window.onError({json.dumps('No songs found. Is that a real playlist or set URL?')})")
                else:
                    self._js(f"window.onDone({json.dumps(f'Playlist done \u2014 {count} added')})")
            except Exception as e:
                self._js(f"window.onError({json.dumps(engine.friendly_error(e))})")

        threading.Thread(target=work, daemon=True).start()
        return {"started": True}

    # ---------------------------------------------------------
    # IMPORT & EDIT  (step 12)
    # ---------------------------------------------------------
    def pick_mp3(self):
        """Open a native file picker for one MP3. Returns its current
        tags (via read_tags) or None if cancelled."""
        result = self._window.create_file_dialog(
            webview.OPEN_DIALOG,
            allow_multiple=False,
            file_types=("Audio (*.mp3)", "All files (*.*)"),
        )
        if not result:
            return None
        path = result[0] if isinstance(result, (list, tuple)) else result
        return engine.read_tags(path)

    def pick_image(self):
        """Open a native file picker for a cover image. Returns its path or None."""
        result = self._window.create_file_dialog(
            webview.OPEN_DIALOG,
            allow_multiple=False,
            file_types=("Images (*.jpg;*.jpeg;*.png;*.webp)", "All files (*.*)"),
        )
        if not result:
            return None
        return result[0] if isinstance(result, (list, tuple)) else result

    def preview_image(self, image_path):
        """Return a base64 data URL for a local image file (for live preview)."""
        return engine.read_image_as_data_url(image_path)

    def import_song(self, file_path):
        """Add an existing MP3 (by path) to the library."""
        return engine.import_existing(file_path)

    def save_edits(self, file_path, title, artist, album, new_image_path,
                   genre=None, year=None, tracknumber=None):
        """Write edited tags back into the file + library. Optionally
        replace cover art from a chosen image."""
        try:
            engine.update_entry(file_path, title, artist, album,
                                genre=genre or None, year=year or None,
                                tracknumber=tracknumber or None)
            if new_image_path:
                engine.embed_cover_from_file(file_path, new_image_path)
                new_thumb = engine.read_art_thumb(file_path)
                if new_thumb:
                    engine.update_thumb(file_path, new_thumb)
            tags = engine.read_tags(file_path)
            return {"ok": True, "art_data_url": tags.get("art_data_url")}
        except Exception as e:
            return {"ok": False, "error": engine.friendly_error(e)}

    def find_mp3_by_name(self, filename):
        """Locate an MP3 by bare filename — searches the library and OUTPUT_DIR.
        Used as a fallback when pywebviewFullPath isn't set on drag-drop events."""
        from pathlib import Path as _P
        name = _P(filename).name
        for e in engine.load_library():
            p = e.get("path") or e.get("file") or ""
            if _P(p).name == name and _P(p).exists():
                return str(p)
        candidate = engine.OUTPUT_DIR / name
        if candidate.exists():
            return str(candidate)
        return None

    def load_for_edit(self, file_path):
        """Return tags dict for a path (same format as pick_mp3, no dialog)."""
        resolved = self._resolve_mp3_path(file_path)
        tags = engine.read_tags(str(resolved))
        return tags if tags.get("exists") else None


def main():
    api = Api()
    theme = engine.get_setting("theme", "nebula")
    window = webview.create_window(
        title="Sideload",
        url=str(GUI_FILE),
        js_api=api,
        width=900,
        height=620,
        min_size=(760, 520),
        background_color=THEME_BG.get(theme, "#14101d"),
    )
    api.set_window(window)
    webview.start()


if __name__ == "__main__":
    main()
