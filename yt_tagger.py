import os
import base64
import yt_dlp
import re
import json
from datetime import datetime
from pathlib import Path
from io import BytesIO
from PIL import Image
from mutagen.easyid3 import EasyID3
from mutagen.mp3 import MP3
from mutagen.id3 import ID3, APIC, ID3NoHeaderError
import urllib.request
from difflib import SequenceMatcher

CONFIG_PATH = Path(__file__).parent / "config.json"

def load_config() -> dict:
    if CONFIG_PATH.exists():
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, ValueError):
            return {}
    return {}

def save_config(config: dict):
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(config, f, indent=2, ensure_ascii=False)

# Folder where finished MP3s land — defaults to ./output, overridable via config.json
_cfg = load_config()
OUTPUT_DIR = Path(_cfg.get("output_dir", str(Path(__file__).parent / "output")))
OUTPUT_DIR.mkdir(exist_ok=True)

def set_output_dir(new_path: str):
    global OUTPUT_DIR
    OUTPUT_DIR = Path(new_path)
    OUTPUT_DIR.mkdir(exist_ok=True)
    cfg = load_config()
    cfg["output_dir"] = str(OUTPUT_DIR)
    save_config(cfg)

def get_setting(key, default=None):
    """Read a single value out of config.json."""
    return load_config().get(key, default)

def set_setting(key, value):
    """Write a single value into config.json."""
    cfg = load_config()
    cfg[key] = value
    save_config(cfg)

LIBRARY_PATH = Path(__file__).parent / "library.json"

def send_to_apple_music(mp3_path):
    try:
        os.startfile(str(mp3_path))
        return {"ok": True}
    except Exception as e:
        return {"ok": False, "error": str(e)}

def friendly_error(e) -> str:
    """Turn a yt-dlp / network exception into a plain-English message."""
    msg = str(e).lower()
    if "is not a valid url" in msg or "unsupported url" in msg:
        return "That doesn't look like a valid URL."
    if "video unavailable" in msg or "removed" in msg:
        return "That video is unavailable or has been removed."
    if "private" in msg:
        return "That video is private."
    if "age" in msg and "restrict" in msg:
        return "That video is age-restricted and can't be downloaded."
    if "sign in" in msg or "confirm your age" in msg:
        return "That video requires sign-in and can't be downloaded."
    if "getaddrinfo" in msg or "connection" in msg or "timed out" in msg or "network" in msg:
        return "Network problem — check your internet connection."
    if "javascript runtime" in msg or "js runtime" in msg or "deno" in msg:
        return "Missing JavaScript runtime. Install Deno (deno.land) so YouTube downloads work."
    return f"Couldn't process that: {str(e).splitlines()[0][:120]}"

def load_library() -> list:
    """Load the library, or return an empty list if it doesn't exist yet."""
    if LIBRARY_PATH.exists():
        try:
            with open(LIBRARY_PATH, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, ValueError):
            print("⚠️  library.json was corrupted, starting fresh.")
            return []
    return []


def save_library(library: list):
    """Write the library back to disk."""
    with open(LIBRARY_PATH, "w", encoding="utf-8") as f:
        json.dump(library, f, indent=2, ensure_ascii=False)


def add_to_library(mp3_path: Path, title: str, artist: str, url: str,
                   album: str = None, thumb: str = None):
    """Record a converted song in the library."""
    library = load_library()
    entry = {
        "title": title,
        "artist": artist,
        "album": album,
        "url": url,
        "file": str(mp3_path.name),
        "path": str(mp3_path.resolve()),
        "added": datetime.now().isoformat(timespec="seconds"),
    }
    if thumb:
        entry["thumb"] = thumb
    library.append(entry)
    save_library(library)
    print(f"📚 Added to library ({len(library)} total).")


def delete_entry(file_name: str):
    """Remove the library entry whose 'file' field matches file_name."""
    library = load_library()
    library = [e for e in library if e.get("file") != file_name]
    save_library(library)


def search_library(query: str) -> list:
    """Find library entries whose title or artist contains the query."""
    library = load_library()
    q = query.lower().strip()
    return [
        e for e in library
        if q in (e.get("title") or "").lower()
        or q in (e.get("artist") or "").lower()
    ]

def normalize(text: str) -> str:
    """Lowercase, strip punctuation/extra spaces for comparison."""
    if not text:
        return ""
    text = text.lower()
    text = re.sub(r"[^\w\s]", "", text)   # drop punctuation
    text = re.sub(r"\s+", " ", text)      # collapse whitespace
    return text.strip()


def similarity(a: str, b: str) -> float:
    """Return a 0.0–1.0 similarity ratio between two strings."""
    return SequenceMatcher(None, normalize(a), normalize(b)).ratio()


def find_duplicate(url: str, title: str, artist: str, threshold: float = 0.85):
    """Check the library for a duplicate.
    Returns the matching entry dict, or None.
    Sets a 'match_type' key on the returned copy: 'url' or 'fuzzy'."""
    library = load_library()

    # 1. Exact URL match
    for entry in library:
        if entry.get("url") and entry["url"] == url:
            match = dict(entry)
            match["match_type"] = "url"
            return match

    # 2. Fuzzy title + artist match
    combined_new = f"{artist} {title}"
    for entry in library:
        combined_old = f"{entry.get('artist') or ''} {entry.get('title') or ''}"
        score = similarity(combined_new, combined_old)
        if score >= threshold:
            match = dict(entry)
            match["match_type"] = "fuzzy"
            match["score"] = round(score, 2)
            return match

    return None


def download_audio(url: str, progress_hook=None) -> Path:
    """Download a single YouTube URL and convert it to MP3.
    Returns the path to the finished MP3 file."""

    ydl_opts = {
        "format": "bestaudio/best",
        "noplaylist": True,  # only grab the single video, ignore any playlist
        # Save using the video's title as the filename
        "writethumbnail": True,  # save the video thumbnail alongside the audio
        "outtmpl": str(OUTPUT_DIR / "%(title)s.%(ext)s"),
        "postprocessors": [
            {
                "key": "FFmpegExtractAudio",
                "preferredcodec": "mp3",
                "preferredquality": "192",
            }
        ],
        "quiet": False,
        "no_warnings": False,
    }

    if progress_hook:
        ydl_opts["progress_hooks"] = [progress_hook]

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=True)
        base = ydl.prepare_filename(info)
        mp3_path = Path(base).with_suffix(".mp3")
        thumbnail_url = info.get("thumbnail")

    print(f"\n✅ Done: {mp3_path.name}")
    return mp3_path, thumbnail_url, info

def download_playlist(url: str, max_items: int = None, progress=None):
    """Download every video in a playlist as MP3s.
    Processes each one through the full tag + cover art pipeline.
    Set max_items to cap how many to grab (None = all)."""

    # First, extract the playlist's entries WITHOUT downloading,
    # so we know what we're dealing with before committing.
    flat_opts = {
        "extract_flat": True,
        "quiet": True,
        "noplaylist": False,
        "ignoreerrors": True,
        "in_playlist": True,
    }
    with yt_dlp.YoutubeDL(flat_opts) as ydl:
        playlist_info = ydl.extract_info(url, download=False)

    entries = playlist_info.get("entries", [])
    print(f"DEBUG: extract_flat returned {len(entries)} entries")
    if not entries:
        print("⚠️  No playlist entries found. Is this actually a playlist URL?")
        return

    total = len(entries)
    if max_items:
        entries = entries[:max_items]

    print(f"\n📃 Playlist: {playlist_info.get('title', 'Unknown')}")
    print(f"   {total} videos found, downloading {len(entries)}.\n")

    results = []
    skipped = 0
    for i, entry in enumerate(entries, start=1):
        video_url = entry.get("url") or entry.get("id")
        if not video_url:
            continue
        if not video_url.startswith("http"):
            video_url = f"https://www.youtube.com/watch?v={video_url}"

        print(f"\n[{i}/{len(entries)}] ───────────────────────────")

        if progress:
            try:
                progress(i, len(entries), entry.get("title") or "")
            except Exception:
                pass

        # URL dupe check BEFORE downloading
        existing = find_duplicate(video_url, "", "")
        if existing and existing["match_type"] == "url":
            print(f"⏭️  Skipping, already in library: "
                  f"{existing['artist']} – {existing['title']}")
            skipped += 1
            continue

        try:
            mp3, thumb, info = download_audio(video_url)
            guess = parse_metadata(info)

            # Fuzzy dupe check AFTER metadata
            dupe = find_duplicate(video_url, guess["title"], guess["artist"])
            if dupe and dupe["match_type"] == "fuzzy":
                print(f"⏭️  Skipping likely duplicate ({int(dupe['score']*100)}%): "
                      f"{dupe['artist']} – {dupe['title']}")
                if mp3.exists():
                    mp3.unlink()
                skipped += 1
                continue

            write_tags(mp3, title=guess["title"], artist=guess["artist"])
            embed_cover_art(mp3, thumb)
            add_to_library(mp3, guess["title"], guess["artist"], video_url)
            results.append(mp3)
        except Exception as e:
            print(f"❌ Skipped (error): {e}")
            continue

    print(f"\n✅ Playlist done: {len(results)} downloaded, {skipped} skipped as duplicates.")
    return results

def write_tags(mp3_path: Path, title: str = None, artist: str = None,
               album: str = None, genre: str = None, year: str = None,
               tracknumber: str = None):
    """Write ID3 tags into an MP3."""
    try:
        audio = EasyID3(mp3_path)
    except ID3NoHeaderError:
        # File has no ID3 header yet — create one
        audio = MP3(mp3_path, ID3=EasyID3)
        audio.add_tags()
        audio = EasyID3(mp3_path)

    if title:
        audio["title"] = title
    if artist:
        audio["artist"] = artist
    if album:
        audio["album"] = album
    if genre:
        audio["genre"] = genre
    if year:
        audio["date"] = year
    if tracknumber:
        audio["tracknumber"] = tracknumber

    audio.save()
    print(f"🏷️  Tagged: title={title!r}, artist={artist!r}, album={album!r}, genre={genre!r}, year={year!r}, track={tracknumber!r}")

def make_square(img: Image.Image) -> Image.Image:
    """Crop an image to a centered square."""
    width, height = img.size
    side = min(width, height)
    left = (width - side) // 2
    top = (height - side) // 2
    return img.crop((left, top, left + side, top + side))


def embed_cover_art(mp3_path: Path, thumbnail_url: str):
    """Download the thumbnail, square it, and embed it as cover art."""
    if not thumbnail_url:
        print("⚠️  No thumbnail available, skipping cover art.")
        return

    # Download the thumbnail into memory
    with urllib.request.urlopen(thumbnail_url) as resp:
        raw = resp.read()

    # Open, square it, re-encode as JPEG
    img = Image.open(BytesIO(raw)).convert("RGB")
    img = make_square(img)
    buffer = BytesIO()
    img.save(buffer, format="JPEG", quality=90)
    cover_data = buffer.getvalue()

    # Embed into the MP3's ID3 tags
    try:
        audio = ID3(mp3_path)
    except ID3NoHeaderError:
        audio = ID3()

    audio.setall("APIC", [APIC(
        encoding=3,
        mime="image/jpeg",
        type=3,            # 3 = front cover
        desc="Cover",
        data=cover_data,
    )])
    audio.save(mp3_path)
    # Remove the loose thumbnail file yt-dlp left behind
    for ext in (".jpg", ".webp", ".png"):
        leftover = mp3_path.with_suffix(ext)
        if leftover.exists():
            leftover.unlink()
    print("🖼️  Cover art embedded.")

# Junk phrases to strip from titles, case-insensitive
JUNK_PATTERNS = [
    r"\(official\s*(music\s*)?video\)",
    r"\[official\s*(music\s*)?video\]",
    r"\(official\s*audio\)",
    r"\[official\s*audio\]",
    r"\(official\s*lyric\s*video\)",
    r"\(lyric[s]?\s*video\)",
    r"\(lyric[s]?\)",
    r"\[lyric[s]?\]",
    r"\(audio\)",
    r"\(visualizer\)",
    r"\(official\)",
    r"\(hd\)",
    r"\(4k\)",
    r"\(4k\s*remaster(ed)?\)",
    r"\(remaster(ed)?\s*\d*\)",
    r"\bofficial\s*video\b",
    r"\bofficial\s*audio\b",
]


def clean_title(raw: str) -> str:
    """Strip common junk phrases and tidy whitespace."""
    cleaned = raw
    for pattern in JUNK_PATTERNS:
        cleaned = re.sub(pattern, "", cleaned, flags=re.IGNORECASE)
    # Collapse multiple spaces and trim stray separators
    cleaned = re.sub(r"\s{2,}", " ", cleaned)
    cleaned = cleaned.strip(" -–—|")
    return cleaned.strip()


def parse_metadata(info: dict) -> dict:
    """Guess artist and title from the video info.
    Returns a dict with 'title' and 'artist' (either may be None)."""
    raw_title = info.get("title", "")
    uploader = info.get("uploader") or info.get("channel") or ""

    cleaned = clean_title(raw_title)

    artist = None
    title = cleaned

    # Most common pattern: "Artist - Song"
    # Split on the first dash with spaces around it
    if re.search(r"\s[-–—]\s", cleaned):
        left, right = re.split(r"\s[-–—]\s", cleaned, maxsplit=1)
        artist = left.strip()
        title = right.strip()
    else:
        # No dash — fall back to channel name as artist
        # Strip common channel suffixes like "VEVO" or "- Topic"
        fallback = re.sub(r"vevo$", "", uploader, flags=re.IGNORECASE)
        fallback = re.sub(r"\s*-\s*topic$", "", fallback, flags=re.IGNORECASE)
        artist = fallback.strip() or None

    return {"title": title or None, "artist": artist}


def read_tags(mp3_path) -> dict:
    """Read existing tags + cover art out of an MP3 on disk.
    Returns a dict with title/artist/album and the embedded art (if any)."""
    mp3_path = Path(mp3_path)
    result = {
        "title": "", "artist": "", "album": "", "genre": "", "year": "", "tracknumber": "",
        "has_art": False, "art_data_url": None,
        "file": str(mp3_path), "exists": mp3_path.exists(),
    }
    if not mp3_path.exists():
        return result

    # Text tags via EasyID3 (tolerant if the file has no tags yet)
    try:
        easy = EasyID3(mp3_path)
        result["title"] = (easy.get("title") or [""])[0]
        result["artist"] = (easy.get("artist") or [""])[0]
        result["album"] = (easy.get("album") or [""])[0]
        result["genre"] = (easy.get("genre") or [""])[0]
        result["year"] = (easy.get("date") or [""])[0]
        result["tracknumber"] = (easy.get("tracknumber") or [""])[0]
    except ID3NoHeaderError:
        pass
    except Exception:
        pass

    # Cover art via raw ID3, returned as a data URL the GUI can show
    try:
        id3 = ID3(mp3_path)
        apics = [f for f in id3.values() if getattr(f, "FrameID", "") == "APIC"]
        if apics:
            # Use the largest frame — guards against stale low-quality duplicates
            pic = max(apics, key=lambda f: len(f.data))
            mime = pic.mime or "image/jpeg"
            b64 = base64.b64encode(pic.data).decode("ascii")
            result["has_art"] = True
            result["art_data_url"] = f"data:{mime};base64,{b64}"
    except Exception:
        pass

    return result


def embed_cover_from_file(mp3_path, image_path):
    """Square an image file from disk and embed it as cover art.
    Accepts any format Pillow can open (JPEG, PNG, WEBP, BMP, …).
    Raises on failure so callers can surface the error."""
    mp3_path = Path(mp3_path)
    image_path = Path(image_path)
    if not image_path.exists():
        raise FileNotFoundError(f"Image not found: {image_path.name}")

    try:
        img = Image.open(image_path)
    except Exception as e:
        raise ValueError(f"Cannot open image ({image_path.suffix or 'unknown type'}): {e}") from e

    # Composite RGBA/LA/palette images onto white so transparency doesn't go black
    if img.mode in ("RGBA", "LA"):
        bg = Image.new("RGB", img.size, (255, 255, 255))
        bg.paste(img, mask=img.split()[-1])
        img = bg
    elif img.mode != "RGB":
        img = img.convert("RGB")

    img = make_square(img)
    buffer = BytesIO()
    img.save(buffer, format="JPEG", quality=90)
    cover_data = buffer.getvalue()

    try:
        audio = ID3(mp3_path)
    except ID3NoHeaderError:
        audio = ID3()
    audio.setall("APIC", [APIC(encoding=3, mime="image/jpeg", type=3,
                               desc="Cover", data=cover_data)])
    audio.save(mp3_path)
    print("🖼️  Cover art replaced.")


def read_image_as_data_url(image_path, max_size: int = 500) -> str | None:
    """Open any image file and return a base64 JPEG data URL for preview.
    Handles RGBA/palette modes and resizes to max_size to keep it lightweight."""
    try:
        img = Image.open(Path(image_path))
        if img.mode == "RGBA":
            bg = Image.new("RGB", img.size, (255, 255, 255))
            bg.paste(img, mask=img.split()[3])
            img = bg
        elif img.mode != "RGB":
            img = img.convert("RGB")
        img.thumbnail((max_size, max_size), Image.LANCZOS)
        buf = BytesIO()
        img.save(buf, format="JPEG", quality=85)
        return "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode("ascii")
    except Exception:
        return None


def read_art_thumb(mp3_path, size: int = 64) -> str | None:
    """Read embedded cover art and return a compact data URL thumbnail, or None."""
    try:
        id3 = ID3(Path(mp3_path))
        apics = [f for f in id3.values() if getattr(f, "FrameID", "") == "APIC"]
        if not apics:
            return None
        img = Image.open(BytesIO(apics[0].data))
        if img.mode not in ("RGB", "L"):
            img = img.convert("RGB")
        img.thumbnail((size, size), Image.LANCZOS)
        buf = BytesIO()
        img.save(buf, format="JPEG", quality=70)
        return "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode("ascii")
    except Exception:
        return None


def import_existing(mp3_path) -> dict:
    """Add an MP3 already on disk to the library (by full path),
    reading its current tags. Returns the result + library entry."""
    mp3_path = Path(mp3_path)
    tags = read_tags(mp3_path)

    library = load_library()
    full = str(mp3_path.resolve())
    # Skip if this exact file is already in the library
    for e in library:
        if e.get("file") == full or e.get("file") == mp3_path.name:
            return {"ok": False, "reason": "already in library", "entry": e}

    entry = {
        "title": tags["title"] or mp3_path.stem,
        "artist": tags["artist"],
        "album": tags["album"],
        "url": None,
        "file": full,            # full path for imported files
        "added": datetime.now().isoformat(timespec="seconds"),
        "imported": True,
    }
    thumb = read_art_thumb(mp3_path)
    if thumb:
        entry["thumb"] = thumb
    library.append(entry)
    save_library(library)
    return {"ok": True, "entry": entry}


def _entry_matches(e: dict, file_path: str) -> bool:
    """True if a library entry corresponds to file_path.
    Handles three storage formats:
      - imported songs:      e['file'] is the full path
      - new converted songs: e['path'] is the full path, e['file'] is filename
      - old converted songs: e['file'] is filename only, no 'path' key
    """
    name = Path(file_path).name
    return (
        e.get("file") == file_path or   # imported / exact match
        e.get("path") == file_path or   # new converted (has 'path' field)
        e.get("file") == name           # old converted (filename only)
    )


def update_entry(file_path, title, artist, album, genre=None, year=None, tracknumber=None):
    """Write edited tags back into the MP3 AND update the library record."""
    # 1. Write tags into the actual file
    target = Path(file_path)
    if not target.is_absolute():
        target = OUTPUT_DIR / file_path     # old-style filename in output/
    if target.exists():
        write_tags(target, title=title or None, artist=artist or None,
                   album=album or None, genre=genre or None,
                   year=year or None, tracknumber=tracknumber or None)

    # 2. Update the matching library record and bump to top
    library = load_library()
    for i, e in enumerate(library):
        if _entry_matches(e, file_path):
            e["title"] = title
            e["artist"] = artist
            e["album"] = album
            e["added"] = datetime.now().isoformat(timespec="seconds")
            library.append(library.pop(i))  # move to end → appears first after reverse
            break
    save_library(library)
    return {"ok": True}


def update_thumb(file_path: str, thumb: str):
    """Update the stored thumbnail for a library entry."""
    library = load_library()
    for e in library:
        if _entry_matches(e, file_path):
            if thumb:
                e["thumb"] = thumb
            break
    save_library(library)


if __name__ == "__main__":
    url = input("Paste a YouTube URL: ").strip()

    mode = input("Single song or playlist? [s/p]: ").strip().lower()

    if mode == "p":
        cap = input("Max songs to grab (blank = all): ").strip()
        cap = int(cap) if cap.isdigit() else None
        download_playlist(url, max_items=cap)
    else:
        # Quick URL check before downloading anything
        existing = find_duplicate(url, "", "")
        if existing and existing["match_type"] == "url":
            print(f"\n⛔ Already in library (same URL): "
                  f"{existing['artist']} – {existing['title']}")
            proceed = input("Download anyway? [y/N]: ").strip().lower()
            if proceed != "y":
                print("Skipped.")
                raise SystemExit

        mp3, thumbnail_url, info = download_audio(url)
        guess = parse_metadata(info)
        print(f"\n🔎 Guessed → artist: {guess['artist']!r}, title: {guess['title']!r}")
        artist = input(f"Artist [{guess['artist']}]: ").strip() or guess["artist"]
        title = input(f"Title [{guess['title']}]: ").strip() or guess["title"]

        # Fuzzy check now that we have clean metadata
        dupe = find_duplicate(url, title, artist)
        if dupe and dupe["match_type"] == "fuzzy":
            print(f"\n⚠️  Possible duplicate ({int(dupe['score']*100)}% match): "
                  f"{dupe['artist']} – {dupe['title']}")
            keep = input("Keep this one anyway? [y/N]: ").strip().lower()
            if keep != "y":
                # Remove the file we just downloaded
                if mp3.exists():
                    mp3.unlink()
                print("Discarded the download.")
                raise SystemExit

        album = input("Album (blank to skip): ").strip() or None
        write_tags(mp3, title=title, artist=artist, album=album)
        embed_cover_art(mp3, thumbnail_url)
        add_to_library(mp3, title, artist, url, album)
        