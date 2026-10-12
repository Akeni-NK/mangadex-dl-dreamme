#!/usr/bin/env python3
# Copyright (c) 2019-2021 eli fessler
# Original project: https://github.com/frozenpandaman/mangadex-dl
# Modifications: cross-platform menu, saved settings, language/group selection,
# and CBZ-by-default workflow.
# License: GPL-3.0-or-later. See LICENSE.

import argparse
import html
import json
import os
import re
import shutil
import sys
import textwrap
import time
import zipfile
from pathlib import Path
from urllib.parse import urlparse, unquote

A_VERSION = "0.8"
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
    ("vi", "Tiếng Việt"),
    ("en", "English"),
    ("ja", "日本語 (Japanese)"),
    ("zh", "中文简体 (Chinese Simplified)"),
    ("zh-hk", "中文繁體 (Chinese Traditional)"),
    ("ko", "한국어 (Korean)"),
    ("id", "Bahasa Indonesia"),
    ("th", "ไทย (Thai)"),
    ("es", "Español"),
    ("pt-br", "Português do Brasil"),
    ("fr", "Français"),
    ("de", "Deutsch"),
    ("ru", "Русский"),
]

try:
    import requests
except ImportError:
    requests = None

SESSION = None
GROUP_NAME_CACHE = {}


def line(char="=", width=None):
    if width is None:
        try:
            width = shutil.get_terminal_size((78, 20)).columns
        except Exception:
            width = 78
    width = max(36, min(int(width), 100))
    print(char * width)


def banner(title):
    line("=")
    print(title.center(min(78, max(36, shutil.get_terminal_size((78, 20)).columns))))
    line("=")


def pause(message="Nhấn Enter để tiếp tục..."):
    try:
        input("\n" + message)
    except (EOFError, KeyboardInterrupt):
        pass


def safe_input(prompt):
    try:
        return input(prompt)
    except EOFError:
        print("\nKhông nhận được dữ liệu nhập. Đang quay lại menu.")
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
        print("Cảnh báo: không đọc được file cài đặt, dùng thiết lập mặc định.")

    lang = str(settings.get("lang", "vi")).strip().lower() or "vi"
    pack_format = str(settings.get("pack_format", "CBZ")).upper()
    if pack_format not in ("FOLDER", "CBZ"):
        pack_format = "CBZ"
    outdir = str(settings.get("outdir", "download")).strip() or "download"
    settings["lang"] = lang
    settings["pack_format"] = pack_format
    settings["outdir"] = outdir
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
        print("Không lưu được cài đặt: {}".format(exc))
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
    return "Mã ngôn ngữ: {}".format(code)


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
            "Thiếu thư viện requests.\n"
            "  Windows: py -m pip install requests\n"
            "  Termux:  pip install requests"
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
        raise RuntimeError("Lỗi kết nối MangaDex{}: {}".format(extra, exc)) from exc


def safe_filename(name, fallback="Unknown", max_length=120):
    name = html.unescape(str(name or "")).strip()
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "-", name)
    name = re.sub(r"\s+", " ", name).strip(" .")
    if not name:
        name = fallback
    # Avoid problematic Windows names and excessively long paths.
    if name.upper() in {"CON", "PRN", "AUX", "NUL"}:
        name = "_" + name
    return name[:max_length].rstrip(" .") or fallback


def find_manga_id(user_value):
    """Accept MangaDex title URL, UUID, or legacy numeric manga ID."""
    value = (user_value or "").strip()
    if not value:
        raise ValueError("Bạn chưa nhập URL hoặc Manga ID.")

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
            raise ValueError("Đây là URL chapter. Hãy nhập URL trang truyện (title), không phải URL chapter.")
        if "title" in path_parts:
            index = path_parts.index("title")
            if index + 1 < len(path_parts):
                candidate = path_parts[index + 1]
                if candidate.isdigit() or uuid_pattern.match(candidate):
                    return candidate
        # Also handle a URL containing a UUID outside the standard /title/ path.
        for part in path_parts:
            if uuid_pattern.match(part):
                return part
    else:
        # A bare UUID is normally caught above, but allow pasted text containing one.
        match = uuid_pattern.search(value)
        if match:
            return match.group(0)

    raise ValueError("Không tìm thấy Manga ID hợp lệ. Hãy nhập URL /title/... hoặc UUID truyện.")


def get_uuid(manga_id):
    if not manga_id.isnumeric():
        return manga_id
    response = api_json(
        API_BASE + "/legacy/mapping",
        method="POST",
        json_body={"type": "manga", "ids": [int(manga_id)]},
    )
    # Be tolerant of small differences in the legacy mapping response structure.
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
    raise RuntimeError("Không đổi được ID cũ sang UUID. Hãy thử dùng URL MangaDex hiện tại.")


def get_title(manga_uuid, lang_code):
    response = api_json(API_BASE + "/manga/{}".format(manga_uuid))
    attributes = response.get("data", {}).get("attributes", {})
    titles = attributes.get("title") or {}
    if lang_code in titles and titles[lang_code]:
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


def get_group_names(chapters):
    ids = []
    for chapter in chapters:
        for group_id in chapter_group_ids(chapter):
            if group_id not in ids:
                ids.append(group_id)

    names = {}
    # MangaDex supports ids[] filters. Batch requests to keep URLs manageable.
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
            # Fall back to individual lookup if batch lookup is unsupported.
            for group_id in batch:
                try:
                    response = api_json(API_BASE + "/group/{}".format(group_id))
                    group = response.get("data", {})
                    name = group.get("attributes", {}).get("name")
                    if name:
                        names[group_id] = name
                except RuntimeError:
                    names[group_id] = "Nhóm dịch không xác định"

    for group_id in ids:
        if group_id not in names:
            names[group_id] = "Nhóm dịch không xác định"
    return names


def format_wrapped_list(items, prefix=" "):
    width = shutil.get_terminal_size((78, 20)).columns
    width = max(36, min(width, 100))
    content = ", ".join(str(item) for item in items)
    if not content:
        return prefix + "(trống)"
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


def make_group_options(chapters, names):
    counts = {}
    for chapter in chapters:
        group_ids = chapter_group_ids(chapter)
        if not group_ids:
            counts[None] = counts.get(None, 0) + 1
        else:
            for group_id in group_ids:
                counts[group_id] = counts.get(group_id, 0) + 1
    options = [(group_id, names.get(group_id, "Nhóm dịch không xác định"), count)
               for group_id, count in counts.items() if group_id is not None]
    options.sort(key=lambda item: item[1].casefold())
    if None in counts:
        options.append((None, "Không ghi nhóm dịch", counts[None]))
    return options


def choose_group(chapters, names):
    options = make_group_options(chapters, names)
    print("\nCác nhóm dịch có trong danh sách:")
    for index, (_, name, count) in enumerate(options, 1):
        print("  {}. {} ({} chapter)".format(index, name, count))
    print("  0. Tất cả nhóm dịch")
    print("  Enter. Tất cả nhóm dịch")

    while True:
        answer = safe_input("Chọn nhóm dịch: ")
        if answer is None:
            return chapters, "Tất cả nhóm dịch"
        answer = answer.strip()
        if answer == "" or answer == "0":
            return chapters, "Tất cả nhóm dịch"
        if answer.isdigit() and 1 <= int(answer) <= len(options):
            group_id, group_name, _ = options[int(answer) - 1]
            if group_id is None:
                filtered = [ch for ch in chapters if not chapter_group_ids(ch)]
            else:
                filtered = [ch for ch in chapters if group_id in chapter_group_ids(ch)]
            return filtered, group_name
        print("Lựa chọn không hợp lệ. Hãy nhập số trong danh sách, hoặc 0 để lấy tất cả.")


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

        # A range is based on chapter-list order, preserving the original tool's behavior.
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
                print("Range {} bị đảo ngược. Bỏ qua.".format(token))
                continue
            selected.extend(chapters[lower_index:upper_index + 1])
            continue

        matches = [ch for ch in chapters if chapter_number(ch) == token]
        if matches:
            # Preserve original behavior: if a number exists in multiple releases,
            # selecting that number downloads each matching release in the active group filter.
            selected.extend(matches)
        else:
            print("Chapter {} does not exist. Skipping.".format(token))
    return selected


def unique_chapter_paths(root, title, chapnum, groupname, pack_format):
    title_dir = root / title
    title_dir.mkdir(parents=True, exist_ok=True)
    base = "{} [{}]".format(chapnum, groupname)
    counter = 0
    while True:
        suffix = "" if counter == 0 else "-{}".format(counter)
        folder_name = "{}{}".format(chapnum, suffix) + " [{}]".format(groupname)
        folder_path = title_dir / folder_name
        archive_name = "{} {} [{}].cbz".format(title, chapnum + suffix, groupname)
        archive_path = title_dir / archive_name
        folder_conflict = folder_path.exists()
        archive_conflict = pack_format == "CBZ" and archive_path.exists()
        if not folder_conflict and not archive_conflict:
            return folder_path, archive_path
        counter += 1


def get_group_label(chapter, names):
    group_ids = chapter_group_ids(chapter)
    if not group_ids:
        return "Không ghi nhóm dịch"
    labels = [names.get(group_id, "Nhóm dịch không xác định") for group_id in group_ids]
    return " & ".join(labels)


def download_chapter(chapter, index, total, title, root, group_names,
                     pack_format, datasaver):
    number = chapter_number(chapter)
    group_label = get_group_label(chapter, group_names)
    group_safe = safe_filename(group_label, "Không ghi nhóm dịch", max_length=60)
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
        raise RuntimeError("MangaDex không trả về danh sách ảnh cho chapter này.")

    dest_folder, archive_path = unique_chapter_paths(
        root, title_safe, chapnum, group_safe, pack_format
    )
    dest_folder.mkdir(parents=True, exist_ok=False)
    failed_pages = []
    session = request_session()

    for page_number, remote_name in enumerate(filenames, 1):
        ext = Path(unquote(urlparse(remote_name).path)).suffix or ".jpg"
        # MangaDex page names are not used as local names: simple numeric names sort reliably.
        local_name = "{:03d}{}".format(page_number, ext.lower())
        outfile = dest_folder / local_name
        url = "{}/{}/{}/{}".format(base_url.rstrip("/"), mode_url, chaphash, remote_name)
        downloaded = False
        last_problem = "lỗi không xác định"
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
        time.sleep(0.2)  # Keep the original tool's conservative request pacing.

    print()
    if failed_pages:
        print("CẢNH BÁO: {} ảnh tải lỗi trong chapter {}:".format(len(failed_pages), number))
        for page_number, problem in failed_pages:
            print("  - Trang {}: {}".format(page_number, problem))

    if pack_format == "CBZ":
        # Store images without recompressing them. Image quality/bytes remain unchanged.
        with zipfile.ZipFile(archive_path, "w", compression=zipfile.ZIP_STORED) as archive:
            for file_path in sorted(dest_folder.iterdir(), key=lambda p: p.name.lower()):
                if file_path.is_file() and not file_path.name.endswith(".part"):
                    archive.write(file_path, arcname=file_path.name)
        print("Đã tạo CBZ: {}".format(archive_path.name))
        if not failed_pages:
            shutil.rmtree(dest_folder)
        else:
            print("Giữ thư mục ảnh tạm vì có ảnh lỗi: {}".format(dest_folder.name))
    else:
        print("Đã lưu ảnh vào: {}".format(dest_folder))

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

    prompt = "Enter manga URL or ID: " if direct else "Nhập URL truyện hoặc Manga ID: "
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
            print("No chapters available to download! (ngôn ngữ: {})".format(lang_code))
            return
        display_chapters(chapters)

        print("\nĐang lấy danh sách nhóm dịch...")
        group_names = get_group_names(chapters)
        selected_chapters, selected_group = choose_group(chapters, group_names)
        if not selected_chapters:
            print("Nhóm dịch này không có chapter để tải.")
            return
        if selected_group != "Tất cả nhóm dịch":
            print("\nNhóm dịch đã chọn: {}".format(selected_group))
            display_chapters(selected_chapters, "Available chapters for selected group:")

        chapter_prompt = "\nEnter chapter(s) to download: " if direct else "\nEnter chapter(s) to download: "
        selection = safe_input(chapter_prompt)
        if selection is None or not selection.strip():
            print("Chưa chọn chapter. Đã hủy thao tác tải.")
            return
        requested = parse_chapter_selection(selection.strip(), selected_chapters)
        if not requested:
            print("Không có chapter hợp lệ để tải.")
            return

        print("\nNgôn ngữ: {} | Định dạng: {} | Nơi lưu: {}".format(
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
                print("\nĐã nhận lệnh hủy. Các file đã tải sẽ được giữ lại.")
                raise
            except Exception as exc:
                print("\nLỖI chapter {}: {}".format(chapter_number(chapter), exc))
        if total_errors:
            print("\nDone. Có {} ảnh tải lỗi. Xem cảnh báo ở trên.".format(total_errors))
        else:
            print("\nDone.")
    except KeyboardInterrupt:
        raise
    except Exception as exc:
        print("\nLỖI: {}".format(exc))


def settings_menu(settings):
    while True:
        banner("SETTING")
        print("  1. Language       [{}]".format(language_label(settings["lang"])))
        print("  2. Đóng gói thành [{}]".format(pack_format_label(settings["pack_format"])))
        print("  3. Nơi tải xuống  [{}]".format(display_path(settings["outdir"])))
        print("\n  0. Quay lại")
        line("-")
        choice = safe_input("Chọn: ")
        if choice is None or choice.strip() == "0":
            return
        choice = choice.strip()

        if choice == "1":
            banner("LANGUAGE")
            for number, (code, label) in enumerate(LANGUAGES, 1):
                current = "  [đang dùng]" if code == settings["lang"] else ""
                print("  {}. {} ({}){}".format(number, label, code, current))
            print("  {}. Nhập mã ngôn ngữ khác".format(len(LANGUAGES) + 1))
            print("  0. Quay lại")
            answer = safe_input("Chọn ngôn ngữ: ")
            if answer is None or answer.strip() == "0" or not answer.strip():
                continue
            answer = answer.strip()
            if answer.isdigit() and 1 <= int(answer) <= len(LANGUAGES):
                settings["lang"] = LANGUAGES[int(answer) - 1][0]
            elif answer == str(len(LANGUAGES) + 1):
                code = safe_input("Nhập mã ngôn ngữ MangaDex (ví dụ: it, pl, tr): ")
                if code and re.fullmatch(r"[a-z]{2,3}(?:-[a-z0-9]{2,8})?", code.strip().lower()):
                    settings["lang"] = code.strip().lower()
                else:
                    print("Mã ngôn ngữ không hợp lệ.")
                    pause()
                    continue
            else:
                print("Lựa chọn không hợp lệ.")
                pause()
                continue
            save_settings(settings)

        elif choice == "2":
            banner("ĐÓNG GÓI CHAPTER")
            print("  1. Folder - lưu ảnh rời trong thư mục chapter")
            print("  2. CBZ    - đóng gói mỗi chapter thành một file .cbz")
            print("  0. Quay lại")
            answer = safe_input("Chọn định dạng: ")
            if answer is None or answer.strip() == "0" or not answer.strip():
                continue
            if answer.strip() == "1":
                settings["pack_format"] = "FOLDER"
            elif answer.strip() == "2":
                settings["pack_format"] = "CBZ"
            else:
                print("Lựa chọn không hợp lệ.")
                pause()
                continue
            save_settings(settings)

        elif choice == "3":
            banner("NƠI TẢI XUỐNG")
            print("Thư mục hiện tại: {}".format(display_path(settings["outdir"])))
            print("Nhập đường dẫn mới (đường dẫn tương đối tính từ thư mục chứa script).")
            print("Để trống để giữ nguyên; nhập 'download' để dùng thư mục mặc định.")
            answer = safe_input("Đường dẫn mới: ")
            if answer is None or not answer.strip():
                continue
            settings["outdir"] = answer.strip()
            save_settings(settings)
            print("Đã lưu nơi tải xuống: {}".format(display_path(settings["outdir"])))
            pause()
        else:
            print("Lựa chọn không hợp lệ.")
            pause()


def main_menu(settings):
    while True:
        banner("MANGADEX-DL v{}".format(A_VERSION))
        print("\nMENU")
        print("  1. Bắt đầu tải truyện")
        print("  2. Cài đặt")
        print("  0. Thoát")
        line("-")
        choice = safe_input("Chọn: ")
        if choice is None or choice.strip() == "0":
            print("Đã thoát MangaDex-DL.")
            return
        choice = choice.strip()
        if choice == "1":
            line("=")
            try:
                start_download(settings)
            except KeyboardInterrupt:
                print("\nĐã hủy thao tác.")
            pause("Nhấn Enter để quay lại menu chính...")
        elif choice == "2":
            settings_menu(settings)
        else:
            print("Lựa chọn không hợp lệ.")
            pause()


def parse_args():
    parser = argparse.ArgumentParser(
        description="Tải manga từ MangaDex (bản có menu PC/Termux)."
    )
    parser.add_argument("url", nargs="?", help="URL hoặc Manga ID (bỏ qua menu nếu cung cấp)")
    parser.add_argument("-l", dest="lang", default=None,
                        help="mã ngôn ngữ chapter, mặc định lấy từ cài đặt (ban đầu: vi)")
    parser.add_argument("-d", dest="datasaver", action="store_true", default=None,
                        help="tải ảnh data-saver, dung lượng thấp hơn")
    parser.add_argument("-a", dest="cbz", action="store_true", default=None,
                        help="đóng gói chapter thành CBZ")
    parser.add_argument("--no-cbz", dest="no_cbz", action="store_true", default=None,
                        help="lưu ảnh trong thư mục thay vì CBZ")
    parser.add_argument("-o", dest="outdir", default=None,
                        help="thư mục đầu ra")
    return parser.parse_args()


def cli_main(args, settings):
    # Preserve the original command-line switches. No arguments opens the new menu.
    direct = any(value is not None for value in (args.url, args.lang, args.datasaver,
                                                  args.cbz, args.no_cbz, args.outdir))
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
        print("\nĐã hủy thao tác.")
    pause("Nhấn Enter để kết thúc...")


def main():
    print("MangaDex-DL v{} | PC + Termux".format(A_VERSION))
    settings = load_settings()
    try:
        cli_main(parse_args(), settings)
    except KeyboardInterrupt:
        print("\nĐã thoát chương trình.")
    except Exception as exc:
        print("LỖI: {}".format(exc))
        pause("Nhấn Enter để đóng chương trình...")


if __name__ == "__main__":
    main()
er, args.outdir)
