import webview
import threading
import json
import subprocess
from pathlib import Path

# Import the engine you built in steps 2-8
import yt_tagger as engine


GUI_FILE = Path(__file__).parent / "gui" / "index.html"


class Api:
    """Methods on this class are callable from JavaScript as
    window.pywebview.api.<method>().  Anything returned is passed
    back to JS as the resolved promise value."""

    def __init__(self):
        self._window = None
        self._pending = None

    def set_window(self, window):
        self._window = window

    # ---- small helper to call a JS function from Python ----
    def _js(self, fn_call):
        if self._window:
            self._window.evaluate_js(fn_call)

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
    # SINGLE SONG  (probe -> review -> save)
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

    def convert_single(self, url, title, artist, album, genre='', year='', tracknumber=''):
        """Download + tag + art + library, using the (possibly edited)
        metadata from the review screen. Runs on a background thread."""
        def work():
            try:
                self._js("window.onProgress('Downloading…')")
                mp3, thumb, info = engine.download_audio(url)

                dupe = engine.find_duplicate(url, title, artist)
                if dupe and dupe.get("match_type") == "fuzzy":
                    score = int(dupe.get("score", 0) * 100)
                    payload = json.dumps({
                        "title": dupe.get("title"), "artist": dupe.get("artist"),
                        "score": score,
                    })
                    self._js(f"window.onFuzzyDuplicate({payload})")
                    self._pending = (mp3, thumb, url, title, artist, album, genre, year, tracknumber)
                    return

                self._finish_single(mp3, thumb, url, title, artist, album, genre, year, tracknumber)
            except Exception as e:
                self._js(f"window.onError({json.dumps(engine.friendly_error(e))})")

        threading.Thread(target=work, daemon=True).start()
        return {"started": True}

    def _finish_single(self, mp3, thumb, url, title, artist, album, genre='', year='', tracknumber=''):
        self._js("window.onProgress('Tagging…')")
        engine.write_tags(mp3, title=title or None, artist=artist or None,
                          album=album or None, genre=genre or None,
                          year=year or None, tracknumber=tracknumber or None)
        self._js("window.onProgress('Embedding cover art…')")
        engine.embed_cover_art(mp3, thumb)
        engine.add_to_library(mp3, title, artist, url, album or None, thumb=thumb)
        self._js(f"window.onDone({json.dumps(title + ' \u2014 ' + artist)})")

    def resolve_fuzzy(self, keep):
        """Called when the user answers the fuzzy-duplicate prompt."""
        pending = self._pending
        if not pending:
            return {"ok": False}
        mp3, thumb, url, title, artist, album, genre, year, tracknumber = pending
        self._pending = None
        if keep:
            self._finish_single(mp3, thumb, url, title, artist, album, genre, year, tracknumber)
        else:
            try:
                if mp3.exists():
                    mp3.unlink()
            except Exception:
                pass
            self._js("window.onCancelled('Discarded duplicate.')")
        return {"ok": True}

    # ---------------------------------------------------------
    # PLAYLIST  (fully automatic, reports a count)
    # ---------------------------------------------------------
    def convert_playlist(self, url, max_items):
        def work():
            try:
                cap = int(max_items) if str(max_items).strip().isdigit() else None
                self._js("window.onProgress('Reading playlist…')")
                results = engine.download_playlist(url, max_items=cap)
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

    def load_for_edit(self, file_path):
        """Return tags dict for a path (same format as pick_mp3, no dialog)."""
        resolved = self._resolve_mp3_path(file_path)
        tags = engine.read_tags(str(resolved))
        return tags if tags.get("exists") else None


def main():
    api = Api()
    window = webview.create_window(
        title="Sideload",
        url=str(GUI_FILE),
        js_api=api,
        width=900,
        height=620,
        min_size=(760, 520),
        background_color="#14101d",
    )
    api.set_window(window)
    webview.start()


if __name__ == "__main__":
    main()
