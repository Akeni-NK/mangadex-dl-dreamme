#!/usr/bin/env python3
# Copyright (c) 2019-2021 eli fessler
# Original project: https://github.com/frozenpandaman/mangadex-dl
# Modifications: clean English interface, cross-platform menu, saved settings,
# multi-group selection, folder picker, cancellation handling, and CBZ workflow.
# License: GPL-3.0-or-later. See LICENSE.

import argparse
import html
import os
import subprocess
import sys
import json
import re
import shutil
import time
import textwrap
import zipfile
from pathlib import Path
from urllib.parse import urlparse, unquote

A_VERSION = "1.0"
API_BASE = "https://api.mangadex.org"
APP_DIR = Path(__file__).resolve().parent
SETTINGS_FILE = APP_DIR / "mangadex-dl-settings.json"
DEFAULT_SETTINGS = {
    "lang": "vi",
    "pack_format": "CBZ",
    "outdir": "download",
    "datasaver": False,
}
CONTENT_RATINGS = ("safe", "suggestive", "erotica", "pornographic")
LANGUAGES = [
    ("vi", "Vietnamese"),
    ("en", "English"),
    ("ja", "日本語 (Japanese)"),
    ("zh", "中文简体 (Chinese Simplified)"),
    ("zh-hk", "中文繁體 (Chinese Traditional)"),
    ("ko", "한국어 (Korean)"),
    ("id", "Bahasa Indonesia"),
    ("th", "ไทย (Thai)"),
    ("es", "Español"),
    ("pt-br", "Portuguese (Brazil)"),
    ("fr", "Français"),
    ("de", "Deutsch"),
    ("ru", "Русский"),
]

try:
    import requests
except ImportError:
    requests = None

SESSION = None


def line(char="=", width=None):
    if width is None:
        try:
            width = shutil.get_terminal_size((78, 20)).columns
        except Exception:
            width = 78
    width = max(36, min(int(width), 100))
    print(char * width)


def clear_screen():
    """Clear the terminal on Windows, Linux and Termux."""
    command = "cls" if os.name == "nt" else "clear"
    try:
        result = subprocess.run(
            [command], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False
        )
        if result.returncode == 0:
            return
    except (OSError, subprocess.SubprocessError):
        pass
    # ANSI fallback for minimal Termux/Linux environments.
    try:
        sys.stdout.write("\033[2J\033[H")
        sys.stdout.flush()
    except Exception:
        pass


def banner(title):
    clear_screen()
    line("=")
    try:
        width = shutil.get_terminal_size((78, 20)).columns
    except Exception:
        width = 78
    print(title.center(min(78, max(36, width))))
    line("=")


def pause(message="Press Enter to continue..."):
    try:
        input("\n" + message)
    except (EOFError, KeyboardInterrupt):
        pass


def safe_input(prompt):
    try:
        return input(prompt)
    except EOFError:
        print("\nNo input received. Returning to the menu.")
        return None


def load_settings():
    settings = DEFAULT_SETTINGS.copy()
    try:
        with SETTINGS_FILE.open("r", encoding="utf-8") as file:
            loaded = json.load(file)
        if isinstance(loaded, dict):
            settings.update(loaded)
    except FileNotFoundError:
        pass
    except (OSError, json.JSONDecodeError):
        print("Warning: settings file could not be read; using defaults.")

    settings["lang"] = str(settings.get("lang", "vi")).strip().lower() or "vi"
    pack_format = str(settings.get("pack_format", "CBZ")).upper()
    settings["pack_format"] = pack_format if pack_format in ("FOLDER", "CBZ") else "CBZ"
    settings["outdir"] = str(settings.get("outdir", "download")).strip() or "download"
    settings["datasaver"] = bool(settings.get("datasaver", False))
    return settings


def save_settings(settings):
    temp_path = SETTINGS_FILE.with_suffix(".json.tmp")
    try:
        with temp_path.open("w", encoding="utf-8") as file:
            json.dump(settings, file, ensure_ascii=False, indent=2)
        temp_path.replace(SETTINGS_FILE)
        return True
    except OSError as exc:
        print("Could not save settings: {}".format(exc))
        try:
            if temp_path.exists():
                temp_path.unlink()
        except OSError:
            pass
        return False


def language_label(code):
    for item_code, label in LANGUAGES:
        if code == item_code:
            return "{} ({})".format(label, code)
    return "Language code: {}".format(code)


def display_path(path_value):
    path = Path(path_value).expanduser()
    return str(path) if path.is_absolute() else path_value


def resolve_outdir(path_value):
    path = Path(str(path_value)).expanduser()
    if not path.is_absolute():
        path = APP_DIR / path
    return path.resolve()


def request_session():
    global SESSION
    if requests is None:
        raise RuntimeError(
            "The requests library is missing.\n"
            "  Windows: py -m pip install requests\n"
            "  Termux:  python -m pip install requests"
        )
    if SESSION is None:
        SESSION = requests.Session()
        SESSION.headers.update({
            "User-Agent": "mangadex-dl/{} (Python; cross-platform CLI)".format(A_VERSION),
            "Accept": "application/json",
        })
    return SESSION


def api_json(url, params=None, method="GET", json_body=None, timeout=30):
    session = request_session()
    try:
        response = session.request(
            method, url, params=params, json=json_body, timeout=timeout
        )
        response.raise_for_status()
        return response.json()
    except Exception as exc:
        status = getattr(getattr(exc, "response", None), "status_code", None)
        extra = " (HTTP {})".format(status) if status else ""
        raise RuntimeError("MangaDex connection error{}: {}".format(extra, exc)) from exc


def safe_filename(name, fallback="Unknown", max_length=120):
    name = html.unescape(str(name or "")).strip()
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "-", name)
    name = re.sub(r"\s+", " ", name).strip(" .")
    if not name:
        name = fallback
    if name.upper() in {"CON", "PRN", "AUX", "NUL"}:
        name = "_" + name
    return name[:max_length].rstrip(" .") or fallback


def find_manga_id(user_value):
    """Accept MangaDex title URL, UUID, or legacy numeric manga ID."""
    value = (user_value or "").strip()
    if not value:
        raise ValueError("No URL or manga ID was entered.")

    uuid_pattern = re.compile(
        r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
        r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
    )
    if value.isdigit() or uuid_pattern.match(value):
        return value
    if "://" in value:
        parsed = urlparse(value)
        path_parts = [unquote(part) for part in parsed.path.split("/") if part]
        if "chapter" in path_parts:
            raise ValueError("This is a chapter URL. Enter the manga title URL, not a chapter URL.")
        if "title" in path_parts:
            index = path_parts.index("title")
            if index + 1 < len(path_parts):
                candidate = path_parts[index + 1]
                if candidate.isdigit() or uuid_pattern.match(candidate):
                    return candidate
        for part in path_parts:
            if uuid_pattern.match(part):
                return part
    else:
        match = uuid_pattern.search(value)
        if match:
            return match.group(0)
    raise ValueError("Could not find a valid manga ID. Enter a /title/... URL or manga UUID.")


def get_uuid(manga_id):
    if not manga_id.isnumeric():
        return manga_id
    response = api_json(
        API_BASE + "/legacy/mapping",
        method="POST",
        json_body={"type": "manga", "ids": [int(manga_id)]},
    )
    if isinstance(response, list) and response:
        first = response[0]
        found = (
            first.get("data", {}).get("attributes", {}).get("newId")
            or first.get("attributes", {}).get("newId")
        ) if isinstance(first, dict) else None
        if found:
            return found
    if isinstance(response, dict):
        data = response.get("data", [])
        if isinstance(data, list) and data:
            found = data[0].get("attributes", {}).get("newId")
            if found:
                return found
    raise RuntimeError("Could not convert the legacy ID to a UUID. Try using a current MangaDex title URL.")


def get_title(manga_uuid, lang_code):
    response = api_json(API_BASE + "/manga/{}".format(manga_uuid))
    attributes = response.get("data", {}).get("attributes", {})
    titles = attributes.get("title") or {}
    if titles.get(lang_code):
        return html.unescape(titles[lang_code])
    alt_titles = {}
    for item in attributes.get("altTitles", []) or []:
        if isinstance(item, dict):
            alt_titles.update(item)
    if alt_titles.get(lang_code):
        return html.unescape(alt_titles[lang_code])
    if titles.get("en"):
        return html.unescape(titles["en"])
    if titles:
        return html.unescape(next(iter(titles.values())))
    return manga_uuid


def get_chapters(manga_uuid, lang_code):
    chapters = []
    offset = 0
    page_limit = 500
    while True:
        params = [
            ("order[chapter]", "asc"),
            ("order[volume]", "asc"),
            ("limit", page_limit),
            ("offset", offset),
            ("translatedLanguage[]", lang_code),
        ]
        params.extend(("contentRating[]", rating) for rating in CONTENT_RATINGS)
        response = api_json(API_BASE + "/manga/{}/feed".format(manga_uuid), params=params)
        page = response.get("data", []) or []
        chapters.extend(page)
        total = int(response.get("total", len(chapters)))
        if not page or len(chapters) >= total:
            break
        offset += len(page)
    return chapters


def zpad(number):
    number = str(number)
    if "." in number:
        whole, decimal = number.split(".", 1)
        return "{}.{}".format(whole.zfill(3), decimal)
    return number.zfill(3)


def pack_format_label(pack_format):
    return "Folder" if str(pack_format).upper() == "FOLDER" else "CBZ"


def chapter_number(chapter):
    value = chapter.get("attributes", {}).get("chapter")
    return "Oneshot" if value is None else str(value)


def chapter_group_ids(chapter):
    ids = []
    for relation in chapter.get("relationships", []) or []:
        if relation.get("type") == "scanlation_group" and relation.get("id"):
            ids.append(relation["id"])
    return list(dict.fromkeys(ids))


def group_key(chapter):
    """Return a stable key for the complete release group combination."""
    ids = tuple(sorted(set(chapter_group_ids(chapter))))
    return ids if ids else None


def get_group_names(chapters):
    ids = []
    for chapter in chapters:
        for group_id in chapter_group_ids(chapter):
            if group_id not in ids:
                ids.append(group_id)
    names = {}
    for start in range(0, len(ids), 40):
        batch = ids[start:start + 40]
        params = [("ids[]", group_id) for group_id in batch]
        params.append(("limit", 100))
        try:
            response = api_json(API_BASE + "/group", params=params)
            for group in response.get("data", []) or []:
                group_id = group.get("id")
                name = group.get("attributes", {}).get("name")
                if group_id and name:
                    names[group_id] = name
        except RuntimeError:
            for group_id in batch:
                try:
                    response = api_json(API_BASE + "/group/{}".format(group_id))
                    group = response.get("data", {})
                    name = group.get("attributes", {}).get("name")
                    if name:
                        names[group_id] = name
                except RuntimeError:
                    names[group_id] = "Unknown scanlation group"
    for group_id in ids:
        if group_id not in names:
            names[group_id] = "Unknown scanlation group"
    return names


def format_wrapped_list(items, prefix=" "):
    try:
        width = shutil.get_terminal_size((78, 20)).columns
    except Exception:
        width = 78
    width = max(36, min(width, 100))
    content = ", ".join(str(item) for item in items)
    if not content:
        return prefix + "(empty)"
    return textwrap.fill(
        content,
        width=max(30, width - len(prefix)),
        initial_indent=prefix,
        subsequent_indent=prefix,
        break_long_words=False,
        break_on_hyphens=False,
    )


def display_chapters(chapters, heading="Available chapters:"):
    print(heading)
    numbers = [chapter_number(chapter) for chapter in chapters]
    print(format_wrapped_list(numbers))


def choose_group(chapters, names):
    """Choose real scanlation groups; collaborations count as all participating groups.

    This intentionally does NOT create a separate option for a combination such as
    ``Group A + Group B``. If a chapter is a collaboration, it appears under both
    participating groups. This keeps the menu count equal to the number of unique
    groups and supports input such as ``1,2,3``.
    """
    group_ids = []
    counts = {}
    has_ungrouped = False

    for chapter in chapters:
        ids = chapter_group_ids(chapter)
        if not ids:
            has_ungrouped = True
            continue
        # A group is counted once per chapter even if MangaDex repeats its relationship.
        for group_id in set(ids):
            if group_id not in group_ids:
                group_ids.append(group_id)
            counts[group_id] = counts.get(group_id, 0) + 1

    group_ids.sort(key=lambda gid: names.get(gid, "Unknown scanlation group").casefold())
    options = [(gid, names.get(gid, "Unknown scanlation group"), counts.get(gid, 0))
               for gid in group_ids]
    if has_ungrouped:
        options.append((None, "No scanlation group", sum(1 for ch in chapters if not chapter_group_ids(ch))))

    if not options:
        return chapters, "All groups"

    print("\nAvailable scanlation groups:")
    for index, (_, name, count) in enumerate(options, 1):
        print("  {}. {} ({} chapter)".format(index, name, count))
    print("  0. All groups")
    print("  Enter. All groups")
    print("  Example: select multiple groups with 1,2,3")

    while True:
        answer = safe_input("Choose scanlation groups: ")
        if answer is None:
            return chapters, "All groups"
        answer = answer.strip()
        if answer == "" or answer == "0":
            return chapters, "All groups"

        tokens = [token.strip() for token in answer.split(",")]
        if any(not token.isdigit() for token in tokens):
            print("Invalid choice. Enter numbers such as 1,2,3, or enter 0 for all groups.")
            continue
        indexes = list(dict.fromkeys(int(token) for token in tokens))
        if any(index == 0 for index in indexes):
            print("To select all groups, enter 0 by itself.")
            continue
        if any(index < 1 or index > len(options) for index in indexes):
            print("One or more numbers are outside the list. Try again.")
            continue

        selected = [options[index - 1] for index in indexes]
        selected_ids = {item[0] for item in selected if item[0] is not None}
        include_ungrouped = any(item[0] is None for item in selected)
        filtered = []
        for chapter in chapters:
            ids = set(chapter_group_ids(chapter))
            if ids & selected_ids or (include_ungrouped and not ids):
                filtered.append(chapter)
        if not filtered:
            print("No chapters match that selection. Try again.")
            continue
        return filtered, ", ".join(item[1] for item in selected)


def choose_download_folder(current_value):
    """Open a native folder picker when possible, with PC/Termux fallbacks."""
    # Windows/macOS/Linux desktop: Tkinter native directory chooser.
    if os.name == "nt" or sys.platform.startswith(("darwin", "linux")) and os.environ.get("TERMUX_VERSION") is None:
        try:
            import tkinter as tk
            from tkinter import filedialog
            root = tk.Tk()
            root.withdraw()
            root.attributes("-topmost", True)
            selected = filedialog.askdirectory(
                title="Choose download folder",
                initialdir=str(resolve_outdir(current_value)) if Path(current_value).expanduser().exists() else str(APP_DIR),
                mustexist=True,
            )
            root.destroy()
            if selected:
                return selected
            return None
        except Exception as exc:
            print("Could not open the folder picker: {}".format(exc))

    # Termux + Termux:API: Android system folder picker. The normal Python
    # downloader needs a real filesystem path, so convert primary-storage SAF
    # URIs (the usual /storage/emulated/0 tree) back to their filesystem path.
    if shutil.which("termux-saf-managedir"):
        print("\nOpening the Android folder picker...")
        try:
            result = subprocess.run(
                ["termux-saf-managedir"],
                capture_output=True, text=True, timeout=300, check=False
            )
            uri = (result.stdout or "").strip().splitlines()[-1] if (result.stdout or "").strip() else ""
            if uri:
                parsed = urlparse(uri)
                raw = unquote(parsed.path)
                if "/tree/" in raw:
                    tree_part = raw.split("/tree/", 1)[1]
                    tree_part = unquote(tree_part)
                    if tree_part.startswith("primary:"):
                        relative = tree_part[len("primary:"):].lstrip("/")
                        return str(Path("/storage/emulated/0") / relative)
                # Some Android providers return the storage ID in the query/path.
                match = re.search(r"primary%3A([^?&]+)", uri, flags=re.I)
                if match:
                    relative = unquote(match.group(1)).lstrip("/")
                    return str(Path("/storage/emulated/0") / relative)
                print("A folder was selected, but Android returned a URI that cannot be mapped to a local path.")
            else:
                print("Folder selection cancelled.")
        except (OSError, subprocess.SubprocessError) as exc:
            print("Could not launch the Termux folder picker: {}".format(exc))
        return None

    print("\nTermux folder picker (termux-saf-managedir) is unavailable.")
    print("Install the Termux:API app and the termux-api package to use the Android folder picker.")
    print("Or enter the folder path manually.")
    return None


def parse_chapter_selection(value, chapters):
    selected = []
    chapter_values = [None if chapter_number(ch) == "Oneshot" else chapter_number(ch)
                      for ch in chapters]
    tokens = [token.strip() for token in value.split(",") if token.strip()]
    for token in tokens:
        if token.lower() == "oneshot":
            matches = [ch for ch in chapters if chapter_number(ch) == "Oneshot"]
            if matches:
                selected.extend(matches)
            else:
                print("Chapter Oneshot does not exist. Skipping.")
            continue
        if "-" in token:
            lower, upper = [part.strip() for part in token.split("-", 1)]
            try:
                lower_index = chapter_values.index(lower)
            except ValueError:
                print("Chapter {} does not exist. Skipping range {}.".format(lower, token))
                continue
            try:
                upper_index = chapter_values.index(upper)
            except ValueError:
                print("Chapter {} does not exist. Skipping range {}.".format(upper, token))
                continue
            if lower_index > upper_index:
                print("Range {} is reversed. Skipping.".format(token))
                continue
            selected.extend(chapters[lower_index:upper_index + 1])
            continue
        matches = [ch for ch in chapters if chapter_number(ch) == token]
        if matches:
            selected.extend(matches)
        else:
            print("Chapter {} does not exist. Skipping.".format(token))
    # Don't download a record twice if selections overlap (e.g. '1-3,2').
    result = []
    seen = set()
    for chapter in selected:
        chapter_id = chapter.get("id")
        key = chapter_id if chapter_id else id(chapter)
        if key not in seen:
            seen.add(key)
            result.append(chapter)
    return result


def unique_chapter_paths(root, title, chapnum, groupname, pack_format):
    title_dir = root / title
    title_dir.mkdir(parents=True, exist_ok=True)
    counter = 0
    while True:
        suffix = "" if counter == 0 else "-{}".format(counter)
        folder_name = "{}{} [{}]".format(chapnum, suffix, groupname)
        folder_path = title_dir / folder_name
        archive_name = "{} {} [{}].cbz".format(title, chapnum + suffix, groupname)
        archive_path = title_dir / archive_name
        folder_conflict = folder_path.exists()
        archive_conflict = pack_format == "CBZ" and archive_path.exists()
        if not folder_conflict and not archive_conflict:
            return folder_path, archive_path
        counter += 1


def get_group_label(chapter, names):
    ids = chapter_group_ids(chapter)
    if not ids:
        return "No scanlation group"
    # Stable display regardless of how MangaDex orders the relationship records.
    labels = [names.get(group_id, "Unknown scanlation group") for group_id in set(ids)]
    labels.sort(key=str.casefold)
    return " + ".join(labels)


def download_chapter(chapter, index, total, title, root, group_names,
                     pack_format, datasaver):
    number = chapter_number(chapter)
    group_label = get_group_label(chapter, group_names)
    group_safe = safe_filename(group_label, "No scanlation group", max_length=60)
    title_safe = safe_filename(title, "MangaDex", max_length=80)
    chapnum = "Oneshot" if number == "Oneshot" else "c" + zpad(number)
    print("Downloading chapter {} [{}/{}]".format(number, index, total))

    chapter_data = api_json(API_BASE + "/at-home/server/{}".format(chapter["id"]))
    base_url = chapter_data.get("baseUrl")
    data = chapter_data.get("chapter", {})
    chaphash = data.get("hash")
    mode = "dataSaver" if datasaver else "data"
    mode_url = "data-saver" if datasaver else "data"
    filenames = data.get(mode, []) or []
    if not base_url or not chaphash or not filenames:
        raise RuntimeError("MangaDex did not return an image list for this chapter.")

    dest_folder, archive_path = unique_chapter_paths(
        root, title_safe, chapnum, group_safe, pack_format
    )
    dest_folder.mkdir(parents=True, exist_ok=False)
    failed_pages = []
    session = request_session()

    for page_number, remote_name in enumerate(filenames, 1):
        ext = Path(unquote(urlparse(remote_name).path)).suffix or ".jpg"
        local_name = "{:03d}{}".format(page_number, ext.lower())
        outfile = dest_folder / local_name
        url = "{}/{}/{}/{}".format(base_url.rstrip("/"), mode_url, chaphash, remote_name)
        downloaded = False
        last_problem = "Unknown error"
        for attempt in range(2):
            try:
                response = session.get(url, timeout=45)
                response.raise_for_status()
                temp_file = outfile.with_suffix(outfile.suffix + ".part")
                with temp_file.open("wb") as file:
                    file.write(response.content)
                temp_file.replace(outfile)
                downloaded = True
                break
            except Exception as exc:
                last_problem = str(exc)
                if attempt == 0:
                    time.sleep(2)
        print("\r{} Downloading pages [{}/{}]".format(
            "|/–\\"[(page_number - 1) % 4], page_number, len(filenames)
        ), end="", flush=True)
        if not downloaded:
            failed_pages.append((page_number, last_problem))
        time.sleep(0.2)
    print()

    if failed_pages:
        print("WARNING: {} image(s) failed in chapter {}:".format(len(failed_pages), number))
        for page_number, problem in failed_pages:
            print("  - Trang {}: {}".format(page_number, problem))

    if pack_format == "CBZ":
        # ZIP_STORED keeps original image bytes unchanged; images are not recompressed.
        with zipfile.ZipFile(archive_path, "w", compression=zipfile.ZIP_STORED) as archive:
            for file_path in sorted(dest_folder.iterdir(), key=lambda path: path.name.lower()):
                if file_path.is_file() and not file_path.name.endswith(".part"):
                    archive.write(file_path, arcname=file_path.name)
        print("Created CBZ: {}".format(archive_path.name))
        if not failed_pages:
            shutil.rmtree(dest_folder)
        else:
            print("Keeping the temporary image folder because some pages failed: {}".format(dest_folder.name))
    else:
        print("Saved images to: {}".format(dest_folder))
    return len(failed_pages)


def start_download(settings, cli_overrides=None, direct=False):
    overrides = cli_overrides or {}
    lang_code = overrides.get("lang") or settings["lang"]
    pack_format = overrides.get("pack_format") or settings["pack_format"]
    outdir_value = overrides.get("outdir") or settings["outdir"]
    datasaver = settings.get("datasaver", False)
    if overrides.get("datasaver") is not None:
        datasaver = overrides["datasaver"]
    outdir = resolve_outdir(outdir_value)
    prompt = "Enter manga URL or ID: " if direct else "Enter manga URL or ID: "
    url = overrides.get("url")
    while not url:
        url = safe_input(prompt)
        if url is None:
            return
        url = url.strip()
        if not url:
            url = None

    try:
        manga_id = find_manga_id(url)
        manga_uuid = get_uuid(manga_id)
        title = get_title(manga_uuid, lang_code)
        print("\nTITLE: {}".format(html.unescape(title)))
        chapters = get_chapters(manga_uuid, lang_code)
        if not chapters:
            print("No chapters available to download! (language: {})".format(lang_code))
            return

        display_chapters(chapters)
        print("\nLoading scanlation groups...")
        group_names = get_group_names(chapters)
        selected_chapters, selected_group = choose_group(chapters, group_names)
        if not selected_chapters:
            print("No chapters are available for the selected groups.")
            return
        if selected_group != "All groups":
            print("\nSelected groups: {}".format(selected_group))
            display_chapters(selected_chapters, "Available chapters for selected group:")

        selection = safe_input("\nEnter chapter(s) to download: ")
        if selection is None or not selection.strip():
            print("No chapters selected. Download cancelled.")
            return
        requested = parse_chapter_selection(selection.strip(), selected_chapters)
        if not requested:
            print("There are no valid chapters to download.")
            return

        print("\nLanguage: {} | Format: {} | Output: {}".format(
            language_label(lang_code), pack_format_label(pack_format), outdir
        ))
        outdir.mkdir(parents=True, exist_ok=True)
        total_errors = 0
        for index, chapter in enumerate(requested, 1):
            try:
                total_errors += download_chapter(
                    chapter, index, len(requested), title, outdir,
                    group_names, pack_format, datasaver
                )
            except KeyboardInterrupt:
                print("\nCancellation received. Already downloaded files will be kept.")
                raise
            except Exception as exc:
                print("\nLỖI chapter {}: {}".format(chapter_number(chapter), exc))
        if total_errors:
            print("\nDone. {} image(s) failed; see warnings above.".format(total_errors))
        else:
            print("\nDone.")
    except KeyboardInterrupt:
        raise
    except Exception as exc:
        print("\nERROR: {}".format(exc))


def settings_menu(settings):
    while True:
        banner("SETTING")
        print("  1. Language       [{}]".format(language_label(settings["lang"])))
        print("  2. Package format [{}]".format(pack_format_label(settings["pack_format"])))
        print("  3. Download location [{}]".format(display_path(settings["outdir"])))
        print("\n  0. Back")
        line("-")
        choice = safe_input("Choose: ")
        if choice is None or choice.strip() == "0":
            return
        choice = choice.strip()
        if choice == "1":
            banner("LANGUAGE")
            for number, (code, label) in enumerate(LANGUAGES, 1):
                current = "  [current]" if code == settings["lang"] else ""
                print("  {}. {} ({}){}".format(number, label, code, current))
            print("  {}. Enter another language code".format(len(LANGUAGES) + 1))
            print("  0. Back")
            answer = safe_input("Choose language: ")
            if answer is None or answer.strip() == "0" or not answer.strip():
                continue
            answer = answer.strip()
            if answer.isdigit() and 1 <= int(answer) <= len(LANGUAGES):
                settings["lang"] = LANGUAGES[int(answer) - 1][0]
            elif answer == str(len(LANGUAGES) + 1):
                code = safe_input("Enter a MangaDex language code (e.g. it, pl, tr): ")
                if code and re.fullmatch(r"[a-z]{2,3}(?:-[a-z0-9]{2,8})?", code.strip().lower()):
                    settings["lang"] = code.strip().lower()
                else:
                    print("Invalid language code.")
                    pause()
                    continue
            else:
                print("Invalid choice.")
                pause()
                continue
            save_settings(settings)
        elif choice == "2":
            banner("CHAPTER OUTPUT FORMAT")
            print("  1. Folder - save page images in a chapter folder")
            print("  2. CBZ    - package each chapter as a .cbz file")
            print("  0. Back")
            answer = safe_input("Choose format: ")
            if answer is None or answer.strip() == "0" or not answer.strip():
                continue
            if answer.strip() == "1":
                settings["pack_format"] = "FOLDER"
            elif answer.strip() == "2":
                settings["pack_format"] = "CBZ"
            else:
                print("Invalid choice.")
                pause()
                continue
            save_settings(settings)
        elif choice == "3":
            banner("DOWNLOAD LOCATION")
            print("Current folder: {}".format(display_path(settings["outdir"])))
            print("  1. Choose folder using the picker")
            print("  2. Enter path manually")
            print("  0. Back")
            answer = safe_input("Choose: ")
            if answer is None or answer.strip() == "0" or not answer.strip():
                continue
            answer = answer.strip()
            selected_path = None
            if answer == "1":
                selected_path = choose_download_folder(settings["outdir"])
                if not selected_path:
                    pause("Download location unchanged. Press Enter...")
                    continue
            elif answer == "2":
                manual = safe_input("Enter path: ")
                if manual is None or not manual.strip():
                    continue
                selected_path = manual.strip()
            else:
                print("Invalid choice.")
                pause()
                continue
            settings["outdir"] = selected_path
            save_settings(settings)
            print("Saved download location: {}".format(display_path(settings["outdir"])))
            pause()
        else:
            print("Invalid choice.")
            pause()


def main_menu(settings):
    while True:
        banner("MANGADEX-DL v{}".format(A_VERSION))
        print("\nMENU")
        print("  1. Start downloading")
        print("  2. Settings")
        print("  0. Exit")
        line("-")
        choice = safe_input("Choose: ")
        if choice is None or choice.strip() == "0":
            print("MangaDex-DL exited.")
            return
        choice = choice.strip()
        if choice == "1":
            line("=")
            try:
                start_download(settings)
            except KeyboardInterrupt:
                print("\nOperation cancelled.")
            pause("Press Enter to return to the main menu...")
        elif choice == "2":
            settings_menu(settings)
        else:
            print("Invalid choice.")
            pause()


def parse_args():
    parser = argparse.ArgumentParser(
        description="Download manga from MangaDex (PC/Termux menu version)."
    )
    parser.add_argument("url", nargs="?", help="Manga URL or ID (skip the menu when provided)")
    parser.add_argument("-l", dest="lang", default=None,
                        help="chapter language code (defaults to saved settings; initial default: vi)")
    parser.add_argument("-d", dest="datasaver", action="store_true", default=None,
                        help="download data-saver images at lower quality")
    parser.add_argument("-a", dest="cbz", action="store_true", default=None,
                        help="package each chapter as a CBZ archive")
    parser.add_argument("--no-cbz", dest="no_cbz", action="store_true", default=None,
                        help="save page images in a folder instead of a CBZ archive")
    parser.add_argument("-o", dest="outdir", default=None,
                        help="output directory")
    return parser.parse_args()


def cli_main(args, settings):
    direct = any(value is not None for value in (
        args.url, args.lang, args.datasaver, args.cbz, args.no_cbz, args.outdir
    ))
    if not direct:
        main_menu(settings)
        return
    pack_format = None
    if args.no_cbz:
        pack_format = "FOLDER"
    elif args.cbz:
        pack_format = "CBZ"
    overrides = {
        "url": args.url,
        "lang": args.lang,
        "datasaver": args.datasaver,
        "pack_format": pack_format,
        "outdir": args.outdir,
    }
    try:
        start_download(settings, overrides, direct=True)
    except KeyboardInterrupt:
        print("\nOperation cancelled.")
    pause("Press Enter to finish...")


def main():
    print("MangaDex-DL v{} | PC + Termux | English".format(A_VERSION))
    settings = load_settings()
    try:
        cli_main(parse_args(), settings)
    except KeyboardInterrupt:
        print("\nProgram exited.")
    except Exception as exc:
        print("ERROR: {}".format(exc))
        pause("Press Enter to close the program...")


if __name__ == "__main__":
    main()
