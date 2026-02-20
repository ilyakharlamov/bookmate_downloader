#!/usr/bin/env python3
import os
import re
import argparse
import shutil
import json
import array
import base64
import zipfile
import logging
from dataclasses import dataclass
from xml.etree import ElementTree as ET
from html.parser import HTMLParser
import requests
from Crypto.Cipher import AES

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def bytess(arr):
    assert type(arr) in [list]
    return array.array('B', arr).tobytes()


def zipdir(path, ziph):
    """Add all files in *path* to *ziph*, ensuring mimetype is stored first."""
    top = path
    for root, _, files in os.walk(path):
        for filename in files:
            if filename != "mimetype":
                continue
            src = os.path.join(root, filename)
            ziph.write(filename=src, arcname=os.path.relpath(src, top),
                       compress_type=zipfile.ZIP_STORED)
    for root, _, files in os.walk(path):
        for filename in files:
            if filename == "mimetype":
                continue
            src = os.path.join(root, filename)
            ziph.write(filename=src, arcname=os.path.relpath(src, top))


def sanitize_filename(name: str) -> str:
    """Replace characters that are invalid in filenames."""
    return re.sub(r'[\\/*?:"<>|]', '_', name).strip()


# ---------------------------------------------------------------------------
# HTML parser — extracts window.CLIENT_PARAMS from the reader page
# ---------------------------------------------------------------------------

class ScriptParser(HTMLParser):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.__data = None
        self.client_params = []

    def handle_starttag(self, tag, attrs):
        if tag == "script":
            self.__data = ""

    def handle_endtag(self, tag):
        if tag == "script":
            self.handle_script_data(self.__data)
            self.__data = None

    def handle_data(self, data):
        if self.__data is not None:
            self.__data += data

    def handle_script_data(self, script_data):
        logging.debug("script_data:%s ...", script_data[:40])
        S = "window.CLIENT_PARAMS"
        if S not in script_data:
            return
        after = script_data[script_data.find(S) + len(S):]
        logging.debug("after: %s", after)
        json_text = after[after.find("=") + 1:after.find(";")]
        self.client_params = json.loads(json_text.strip())
        logging.debug("client_params: %s", self.client_params)


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class BookInfo:
    """Structured representation of decrypted book metadata."""
    title: str
    author: str
    document_uuid: str
    container: bytes   # META-INF/container.xml
    opf: bytes         # OEBPS/content.opf
    ncx: bytes         # OEBPS/toc.ncx


# ---------------------------------------------------------------------------
# BookmateAPI — all HTTP communication with Bookmate
# ---------------------------------------------------------------------------

class BookmateAPI:
    """Handles every HTTP request to the Bookmate reader API."""

    BASE = "https://reader.bookmate.com"

    def __init__(self, cookies: dict):
        self.cookies = cookies

    def _get(self, url: str):
        logging.debug("GET %s", url)
        response = requests.get(url, cookies=self.cookies)
        logging.debug("response: %s", response)
        assert response.status_code == 200, response.status_code
        return response

    def get_secret(self, bookid: str) -> str:
        """Fetch the AES secret embedded in the reader page."""
        url = f"{self.BASE}/{bookid}"
        html = self._get(url).text
        logging.debug("html:%s ...", html[:20])
        parser = ScriptParser()
        parser.feed(html)
        secret = parser.client_params["secret"]
        logging.debug("secret: %s", secret)
        return secret

    def get_base_metadata(self, bookid: str) -> dict:
        """Return the public book record (title, author, episodes_count, …)."""
        url = f"{self.BASE}/p/api/v5/books/{bookid}"
        return self._get(url).json()

    def get_episodes(self, bookid: str) -> list:
        """Return the list of episode objects for a serialised book."""
        url = f"{self.BASE}/p/api/v5/books/{bookid}/episodes"
        return self._get(url).json().get("episodes", [])

    def get_encrypted_metadata(self, bookid: str) -> dict:
        """Return the raw (encrypted) metadata payload for *bookid*."""
        url = f"{self.BASE}/p/api/v5/books/{bookid}/metadata/v4"
        response = self._get(url)
        logging.debug("encrypted metadata: %s ...", response.text[:40])
        return response.json()

    def get_content_file(self, uuid: str, fname: str) -> bytes:
        """Download a single OEBPS content file and return its bytes."""
        url = f"{self.BASE}/p/a/4/d/{uuid}/contents/OEBPS/{fname}"
        logging.info("Downloading: %s", url)
        return self._get(url).content


# ---------------------------------------------------------------------------
# MetadataDecryptor — AES-CBC decryption of the raw API payload
# ---------------------------------------------------------------------------

class MetadataDecryptor:
    """Decrypts every list-valued field in the encrypted metadata dict."""

    def decrypt_metadata(self, encrypted: dict, secret: str) -> dict:
        result = {}
        for key, val in encrypted.items():
            if isinstance(val, list):
                result[key] = self._decrypt(secret, bytess(val))
            else:
                result[key] = val
        return result

    def _decrypt(self, secret: str, data: bytes) -> bytes:
        assert isinstance(secret, str), type(secret)
        key = base64.b64decode(secret)
        plaintext = self._raw_decrypt(data[16:], key, iv=data[:16])
        logging.debug("decrypted %d bytes, pad=%d", len(plaintext), plaintext[-1])
        pad_size = -1 * plaintext[-1]
        return plaintext[:pad_size]

    @staticmethod
    def _raw_decrypt(crypt_arr: bytes, key: bytes, iv: bytes) -> bytes:
        cipher = AES.new(key, AES.MODE_CBC, iv)
        return cipher.decrypt(crypt_arr)


# ---------------------------------------------------------------------------
# MetadataParser — builds a BookInfo from decrypted metadata + OPF XML
# ---------------------------------------------------------------------------

class MetadataParser:
    """Parses decrypted metadata and extracts a structured BookInfo."""

    _OPF_NS = {
        "opf": "http://www.idpf.org/2007/opf",
        "dc":  "http://purl.org/dc/elements/1.1/",
    }

    def parse(self, metadata: dict) -> BookInfo:
        opf_bytes = metadata["opf"]
        title, author = self._extract_title_author(opf_bytes)
        logging.info("Parsed metadata — title: %r, author: %r", title, author)
        return BookInfo(
            title=title,
            author=author,
            document_uuid=metadata["document_uuid"],
            container=metadata["container"],
            opf=opf_bytes,
            ncx=metadata["ncx"],
        )

    def _extract_title_author(self, opf_bytes: bytes) -> tuple:
        try:
            root = ET.fromstring(opf_bytes)
            ns = self._OPF_NS
            title_el  = root.find(".//dc:title",   ns)
            author_el = root.find(".//dc:creator", ns)
            title  = title_el.text  if title_el  is not None else "Unknown Title"
            author = author_el.text if author_el is not None else "Unknown Author"
            return title, author
        except Exception as exc:
            logging.warning("Failed to parse OPF metadata: %s", exc)
            return "Unknown Title", "Unknown Author"


# ---------------------------------------------------------------------------
# FileStore — isolated file-system operations for one book/episode directory
# ---------------------------------------------------------------------------

class FileStore:
    """Manages reading and writing files within a single content directory."""

    def __init__(self, outdir: str):
        self.outdir = outdir

    def save(self, data: bytes, name: str):
        fpath = os.path.join(self.outdir, name)
        os.makedirs(os.path.dirname(fpath), exist_ok=True)
        with open(fpath, "wb") as fout:
            fout.write(data)

    def path(self, sub: str) -> str:
        return os.path.join(self.outdir, sub)

    def clear_css(self):
        """Blank out every CSS file inside this content directory."""
        for root, _, files in os.walk(self.outdir):
            for name in files:
                if name.lower().endswith(".css"):
                    with open(os.path.join(root, name), "w", encoding="UTF-8") as f:
                        f.write("")

    def delete(self):
        shutil.rmtree(self.outdir)


# ---------------------------------------------------------------------------
# ContentDownloader — writes EPUB structure and fetches all OPF items
# ---------------------------------------------------------------------------

class ContentDownloader:
    """Saves EPUB skeleton files and downloads every item listed in the OPF."""

    def __init__(self, api: BookmateAPI, store: FileStore):
        self.api = api
        self.store = store

    def download(self, book_info: BookInfo):
        """Write metadata files then fetch all content referenced by the OPF."""
        self.store.save(b"application/epub+zip", "mimetype")
        self.store.save(book_info.container, "META-INF/container.xml")
        self.store.save(book_info.opf,       "OEBPS/content.opf")
        self.store.save(book_info.ncx,       "OEBPS/toc.ncx")
        self._download_opf_items(book_info.document_uuid)

    def _download_opf_items(self, uuid: str):
        content_file = self.store.path("OEBPS/content.opf")
        for event, elem in ET.iterparse(content_file, events=["start"]):
            if event != "start":
                continue
            if not elem.tag.endswith("}item"):
                continue
            if "href" not in elem.attrib:
                continue
            fname = elem.attrib["href"]
            if fname == "toc.ncx":
                continue
            logging.debug("OPF item: %s", fname)
            try:
                data = self.api.get_content_file(uuid, fname)
                self.store.save(data, "OEBPS/" + fname)
            except Exception:
                logging.warning("Cannot download content file: %s", fname)


# ---------------------------------------------------------------------------
# EpubBuilder — packages a content directory into a named .epub file
# ---------------------------------------------------------------------------

class EpubBuilder:
    """Creates .epub archives from downloaded content directories."""

    def __init__(self, epub_outdir: str):
        """*epub_outdir* is the folder where finished .epub files are written."""
        self.epub_outdir = epub_outdir

    def build(self, content_dir: str, epub_name: str) -> str:
        """
        Zip *content_dir* into ``{epub_outdir}/{epub_name}.epub``.
        Returns the path of the created file.
        """
        assert os.path.exists(content_dir), content_dir
        safe_name = sanitize_filename(epub_name)
        epub_path = os.path.join(self.epub_outdir, safe_name + ".epub")
        with zipfile.ZipFile(epub_path, "w", zipfile.ZIP_DEFLATED) as zipf:
            zipdir(content_dir, zipf)
        logging.info("ebook saved as %s", epub_path)
        logging.info("We recommend https://calibre-ebook.com/ for book management and conversion")
        return epub_path


# ---------------------------------------------------------------------------
# EpubMerger — combines multiple episode content directories into one EPUB
# ---------------------------------------------------------------------------

class EpubMerger:
    """
    Merges a list of episode content directories into a single combined directory
    suitable for packaging as one EPUB.

    Algorithm
    ---------
    1. Copy the first episode directory wholesale as the base.
    2. For each subsequent episode:
       a. Copy all manifest items (HTML, images, fonts, …) to combined/OEBPS/,
          renaming files that would conflict with existing ones.
       b. Append new ``<item>`` elements to the combined ``content.opf`` manifest.
       c. Append matching ``<itemref>`` elements to the combined ``content.opf`` spine
          (preserving the episode's original reading order).
       d. Append ``<navPoint>`` elements from the episode ``toc.ncx`` to the
          combined ``toc.ncx`` navMap, with playOrder renumbered sequentially.
    3. Update the ``<dc:title>`` in the combined OPF to the overall book title.
    """

    _OPF_NS = "http://www.idpf.org/2007/opf"
    _DC_NS  = "http://purl.org/dc/elements/1.1/"
    _NCX_NS = "http://www.daisy.org/z3986/2005/ncx/"

    def merge(self, episode_dirs: list, episode_titles: list,
              book_title: str, author: str, combined_dir: str) -> str:
        """
        Merge *episode_dirs* into *combined_dir*. Returns *combined_dir*.

        :param episode_dirs:    Ordered list of downloaded episode content dirs.
        :param episode_titles:  Human-readable title for each episode (same order).
        :param book_title:      Title to place in the combined OPF dc:title.
        :param author:          Book author (used for logging only).
        :param combined_dir:    Destination directory (must not exist yet).
        """
        assert episode_dirs, "No episodes to merge"
        self._register_namespaces()

        # Bootstrap: copy first episode wholesale
        shutil.copytree(episode_dirs[0], combined_dir)
        logging.info("Merge base: %s", episode_dirs[0])

        combined_opf = os.path.join(combined_dir, "OEBPS", "content.opf")
        combined_ncx = os.path.join(combined_dir, "OEBPS", "toc.ncx")

        # Renumber the base episode's navPoints starting at 1
        play_order = self._renumber_ncx(combined_ncx, start=1)

        # Merge each additional episode
        for ep_dir, ep_title in zip(episode_dirs[1:], episode_titles[1:]):
            logging.info("Merging episode: %s", ep_title)
            play_order = self._merge_episode(
                ep_dir, combined_dir, combined_opf, combined_ncx,
                ep_title, play_order
            )

        # Set the series title in the combined OPF
        self._set_opf_title(combined_opf, book_title)
        return combined_dir

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _register_namespaces(self):
        ET.register_namespace("",    self._OPF_NS)
        ET.register_namespace("dc",  self._DC_NS)
        ET.register_namespace("opf", self._OPF_NS)
        ET.register_namespace("",    self._NCX_NS)  # NCX also uses default ns

    def _merge_episode(self, ep_dir: str, combined_dir: str,
                       combined_opf: str, combined_ncx: str,
                       ep_title: str, play_order: int) -> int:
        ep_opf = os.path.join(ep_dir, "OEBPS", "content.opf")
        ep_ncx = os.path.join(ep_dir, "OEBPS", "toc.ncx")

        # ---- Merge OPF manifest + spine --------------------------------
        ET.register_namespace("",    self._OPF_NS)
        ET.register_namespace("dc",  self._DC_NS)
        ET.register_namespace("opf", self._OPF_NS)

        c_tree = ET.parse(combined_opf)
        ep_tree = ET.parse(ep_opf)
        c_root  = c_tree.getroot()
        ep_root = ep_tree.getroot()

        c_manifest  = c_root.find(f"{{{self._OPF_NS}}}manifest")
        c_spine     = c_root.find(f"{{{self._OPF_NS}}}spine")
        ep_manifest = ep_root.find(f"{{{self._OPF_NS}}}manifest")
        ep_spine    = ep_root.find(f"{{{self._OPF_NS}}}spine")

        existing_ids   = {item.get("id")   for item in c_manifest}
        existing_hrefs = {item.get("href") for item in c_manifest}

        # Map old episode item-id → new id (needed to fix spine idrefs)
        id_remap: dict = {}

        for item in ep_manifest:
            old_href = item.get("href", "")
            old_id   = item.get("id", "")
            media    = item.get("media-type", "")

            if old_href == "toc.ncx":
                continue  # handled via NCX merge

            # Skip CSS files — the first episode's stylesheet is sufficient
            if old_href.lower().endswith(".css") or media == "text/css":
                continue

            # Resolve filename conflicts
            new_href = old_href
            if new_href in existing_hrefs:
                base, ext = os.path.splitext(old_href)
                new_href = f"{base}_{sanitize_filename(ep_title)}{ext}"

            # Resolve id conflicts
            new_id = old_id
            suffix = 1
            while new_id in existing_ids:
                new_id = f"{old_id}_{suffix}"
                suffix += 1

            id_remap[old_id] = new_id
            existing_ids.add(new_id)
            existing_hrefs.add(new_href)

            # Copy the physical file (skip if destination already exists)
            src = os.path.join(ep_dir,      "OEBPS", old_href)
            dst = os.path.join(combined_dir, "OEBPS", new_href)
            if os.path.exists(src) and not os.path.exists(dst):
                os.makedirs(os.path.dirname(dst), exist_ok=True)
                shutil.copy2(src, dst)

            # Add <item> to combined manifest
            new_item = ET.SubElement(c_manifest, f"{{{self._OPF_NS}}}item")
            new_item.set("id",         new_id)
            new_item.set("href",       new_href)
            new_item.set("media-type", media)

        # Add <itemref> entries to combined spine (in episode reading order)
        for itemref in ep_spine:
            old_idref = itemref.get("idref", "")
            new_idref = id_remap.get(old_idref, old_idref)
            new_ref = ET.SubElement(c_spine, f"{{{self._OPF_NS}}}itemref")
            new_ref.set("idref", new_idref)

        c_tree.write(combined_opf, xml_declaration=True, encoding="utf-8")

        # ---- Merge NCX navPoints ---------------------------------------
        ET.register_namespace("", self._NCX_NS)
        c_ncx_tree  = ET.parse(combined_ncx)
        ep_ncx_tree = ET.parse(ep_ncx)

        c_nav_map  = c_ncx_tree.find(f"{{{self._NCX_NS}}}navMap")
        ep_nav_map = ep_ncx_tree.find(f"{{{self._NCX_NS}}}navMap")

        for nav_point in list(ep_nav_map):
            nav_point.set("playOrder", str(play_order))
            play_order += 1
            # Replace navLabel text with the episode title
            nav_label = nav_point.find(f"{{{self._NCX_NS}}}navLabel")
            if nav_label is not None:
                text_el = nav_label.find(f"{{{self._NCX_NS}}}text")
                if text_el is not None:
                    text_el.text = ep_title
            c_nav_map.append(nav_point)

        c_ncx_tree.write(combined_ncx, xml_declaration=True, encoding="utf-8")
        return play_order

    def _renumber_ncx(self, ncx_path: str, start: int) -> int:
        """Renumber all navPoint playOrder values starting from *start*; returns next value."""
        ET.register_namespace("", self._NCX_NS)
        tree    = ET.parse(ncx_path)
        nav_map = tree.find(f"{{{self._NCX_NS}}}navMap")
        play_order = start
        for nav_point in nav_map:
            nav_point.set("playOrder", str(play_order))
            play_order += 1
        tree.write(ncx_path, xml_declaration=True, encoding="utf-8")
        return play_order

    def _set_opf_title(self, opf_path: str, title: str):
        ET.register_namespace("",    self._OPF_NS)
        ET.register_namespace("dc",  self._DC_NS)
        ET.register_namespace("opf", self._OPF_NS)
        tree     = ET.parse(opf_path)
        title_el = tree.getroot().find(f".//{{{self._DC_NS}}}title")
        if title_el is not None:
            title_el.text = title
        tree.write(opf_path, xml_declaration=True, encoding="utf-8")


# ---------------------------------------------------------------------------
# BookDownloader — orchestrates the full download workflow
# ---------------------------------------------------------------------------

class BookDownloader:
    """
    Orchestrates the complete download process for one book (regular or serial).

    Workflow — regular book
    -----------------------
    download → clear CSS → build ``{title} - {author}.epub`` → clean up

    Workflow — serial book, merge_episodes=True (default)
    ------------------------------------------------------
    download all episodes → merge into combined dir → clear CSS →
    build ``{title} - {author}.epub`` → clean up

    Workflow — serial book, merge_episodes=False
    ---------------------------------------------
    for each episode: download → clear CSS →
    build ``{title} - {episode_title} - {author}.epub`` → clean up
    """

    def __init__(self, bookid: str, api: BookmateAPI, secret: str,
                 base_outdir: str, merge_episodes: bool = True):
        self.bookid         = bookid
        self.api            = api
        self.secret         = secret
        self.base_outdir    = base_outdir
        self.merge_episodes = merge_episodes
        self._decryptor     = MetadataDecryptor()
        self._parser        = MetadataParser()
        self._epub          = EpubBuilder(base_outdir)

    # ------------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------------

    def download(self, delete_downloaded: bool = True, make_epub: bool = True, delete_css: bool = True):
        base_meta = self.api.get_base_metadata(self.bookid)
        book_rec  = base_meta.get("book", {})
        if book_rec.get("episodes_count", 0) == 0:
            self._download_regular_book(book_rec, delete_downloaded, make_epub, delete_css)
        else:
            self._download_serial_book(book_rec, delete_downloaded, make_epub, delete_css)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _load_book_info(self, bookid: str) -> BookInfo:
        """Fetch, decrypt and parse metadata for *bookid* into a BookInfo."""
        encrypted = self.api.get_encrypted_metadata(bookid)
        decrypted = self._decryptor.decrypt_metadata(encrypted, self.secret)
        return self._parser.parse(decrypted)

    def _download_regular_book(self, book_rec: dict, delete_downloaded: bool, make_epub: bool, delete_css: bool):
        logging.info("Downloading regular book: %s", self.bookid)
        book_info = self._load_book_info(self.bookid)

        content_dir = os.path.join(self.base_outdir, self.bookid)
        store = FileStore(content_dir)
        ContentDownloader(self.api, store).download(book_info)

        if delete_css:
            store.clear_css()

        if make_epub:
            epub_name = f"{book_info.title} - {book_info.author}"
            self._epub.build(content_dir, epub_name)

        if delete_downloaded:
            store.delete()

    def _download_serial_book(self, book_rec: dict, delete_downloaded: bool, make_epub: bool, delete_css: bool):
        logging.info("Downloading serial book: %s (%s episodes, merge=%s)",
                     self.bookid, book_rec.get("episodes_count", "?"), self.merge_episodes)
        episodes = self.api.get_episodes(self.bookid)

        # --- Download all episodes first (no CSS clear / epub build yet) ---
        episode_dirs:   list = []
        episode_titles: list = []
        episode_infos:  list = []

        for episode in episodes:
            episode_id    = episode["uuid"]
            episode_title = episode.get("title", episode_id)
            logging.info("Downloading episode: %s — %s", episode_id, episode_title)

            content_dir = os.path.join(self.base_outdir, self.bookid, episode_id)
            store = FileStore(content_dir)
            episode_info = self._load_book_info(episode_id)
            ContentDownloader(self.api, store).download(episode_info)

            if delete_css:
                store.clear_css()

            episode_dirs.append(content_dir)
            episode_titles.append(episode_title)
            episode_infos.append(episode_info)

        # --- Build output ---
        book_title = book_rec.get("title") or (episode_infos[0].title if episode_infos else self.bookid)
        author     = episode_infos[0].author if episode_infos else "Unknown Author"

        if self.merge_episodes:
            self._build_merged_epub(episode_dirs, episode_titles, book_title, author, delete_downloaded, make_epub, delete_css)
        else:
            self._build_individual_epubs(episode_dirs, episode_titles, episode_infos, book_title, delete_downloaded, make_epub, delete_css)

    def _build_merged_epub(self, episode_dirs: list, episode_titles: list,
                           book_title: str, author: str, delete_downloaded: bool, make_epub: bool, delete_css: bool):
        """Merge all episodes into one combined directory and package as a single EPUB."""
        combined_dir = os.path.join(self.base_outdir, self.bookid, "combined")
        merger = EpubMerger()
        merger.merge(episode_dirs, episode_titles, book_title, author, combined_dir)

        combined_store = FileStore(combined_dir)

        if make_epub:
            epub_name = f"{book_title} - {author}"
            self._epub.build(combined_dir, epub_name)

        if delete_downloaded:
            combined_store.delete()
            # Clean up individual episode dirs
            for ep_dir in episode_dirs:
                FileStore(ep_dir).delete()

            # Remove the now-empty book staging dir if possible
            staging = os.path.join(self.base_outdir, self.bookid)
            try:
                os.rmdir(staging)
            except OSError:
                pass  # not empty — leave it

    def _build_individual_epubs(self, episode_dirs: list, episode_titles: list,
                                episode_infos: list, book_title: str, delete_downloaded: bool, make_epub: bool, delete_css: bool):
        """Build one EPUB per episode and clean up each staging directory."""
        for content_dir, ep_title, ep_info in zip(episode_dirs, episode_titles, episode_infos):
            store = FileStore(content_dir)
            if delete_css:
                store.clear_css()
            if make_epub:
                epub_name = f"{book_title} - {ep_title} - {ep_info.author}"
                self._epub.build(content_dir, epub_name)
            if delete_downloaded:
                store.delete()


# ---------------------------------------------------------------------------
# Bookmate — top-level entry point
# ---------------------------------------------------------------------------

class Bookmate:
    def __init__(self, outdir: str, cookies: dict):
        assert os.path.exists(outdir), f"path {outdir} does not exist"
        assert cookies
        self.outdir  = outdir
        self.cookies = cookies
        self.api     = BookmateAPI(cookies)

    def get_book(self, bookid: str, merge_episodes: bool = True) -> BookDownloader:
        secret = self.api.get_secret(bookid)
        return BookDownloader(
            bookid=bookid,
            api=self.api,
            secret=secret,
            base_outdir=self.outdir,
            merge_episodes=merge_episodes,
        )


# ---------------------------------------------------------------------------
# Cookie helper
# ---------------------------------------------------------------------------

def get_cookies() -> dict:
    if os.environ.get("BMS") is not None:
        bms = os.environ.get("BMS")
    else:
        try:
            from pycookiecheat import chrome_cookies
            bms = chrome_cookies("https://reader.bookmate.com")["bms"]
        except Exception:
            bms = input(
                "Enter bms cookie\n"
                "(browser → DevTools → Application → bookmate.com → bms → Value): "
            )
    return {"bms": bms}


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    argparser = argparse.ArgumentParser()
    argparser.add_argument("--bookid",   help="bookid, taken from the book URL", required=True)
    argparser.add_argument("--outdir",   help="Output directory", default="out")
    argparser.add_argument("--log",      help="Log level", type=str, default="INFO",
                           choices=logging._nameToLevel.keys())
    argparser.add_argument("--no-merge", help="For serial books: produce one EPUB per episode "
                                              "instead of one combined EPUB (default: merge).",
                           dest="no_merge", action="store_true", default=False)
    argparser.add_argument("--no-delete", dest="no_delete", action="store_true", default=False)
    argparser.add_argument("--no-epub", dest="no_epub", action="store_true", default=False)
    argparser.add_argument("--keep-css", dest="keep_css", action="store_true", default=False)
    arg = argparser.parse_args()

    logformat = "%(asctime)s (%(name)s) %(levelname)s %(module)s.%(funcName)s():%(lineno)d  %(message)s"
    logging.basicConfig(level=arg.log, format=logformat)

    if not os.path.exists(arg.outdir):
        logging.info("Creating folder %s ...", arg.outdir)
        os.makedirs(arg.outdir)

    bookmate = Bookmate(outdir=arg.outdir, cookies=get_cookies())
    book = bookmate.get_book(bookid=arg.bookid, merge_episodes=not arg.no_merge)
    book.download(not arg.no_delete, not arg.no_epub, not arg.keep_css)
