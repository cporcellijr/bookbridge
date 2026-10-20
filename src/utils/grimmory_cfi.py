import re
from pathlib import Path
from typing import Optional

from lxml import html


class GrimmoryCFIResolver:
    """Small EPUB CFI/xpointer bridge modeled on grimmory.koplugin.

    It supports the KOReader xpointer shape stored by the bridge and Grimmory's
    EPUB CFI range shape. Conversion is intentionally fail-closed: callers should
    retry later rather than mark an annotation synced when an anchor cannot be
    resolved.
    """

    _DOC_FRAGMENT_RE = re.compile(r"^/body/DocFragment\[(\d+)](.+)$")
    _CFI_RANGE_RE = re.compile(r"^epubcfi\(([^,)]*),([^,)]*),([^,)]*)\)$")

    def __init__(self, ebook_parser, book_path):
        self.ebook_parser = ebook_parser
        self.book_path = Path(book_path)
        self.full_text, self.spine_map = ebook_parser.extract_text_and_map(self.book_path)

    @staticmethod
    def _element_children(parent):
        return [child for child in list(parent) if isinstance(getattr(child, "tag", None), str)]

    @staticmethod
    def _local_name(element) -> str:
        tag = getattr(element, "tag", "") or ""
        return tag.split("}", 1)[-1].lower()

    def _spine_item(self, fragment_index: int) -> dict:
        for item in self.spine_map:
            if int(item.get("spine_index") or 0) == int(fragment_index):
                return item
        raise ValueError(f"DocFragment[{fragment_index}] not found in EPUB spine")

    def _parse_xpointer(self, xpointer: str):
        match = self._DOC_FRAGMENT_RE.match(str(xpointer or ""))
        if not match:
            raise ValueError(f"Unsupported KOReader xpointer: {xpointer!r}")
        fragment_index = int(match.group(1))
        relative_path = match.group(2)
        text_match = re.search(r"/text\(\)(?:\[(\d+)\])?(?:\.\d+)?$", relative_path)
        text_index = int(text_match.group(1) or 1) if text_match else None
        if text_index is not None and text_index < 1:
            raise ValueError(f"Invalid xpointer text index: {xpointer!r}")
        clean_xpath, offset = self.ebook_parser._split_xpath_char_offset(relative_path)
        if text_match:
            clean_xpath = re.sub(r"/text\(\)(?:\[\d+\])?$", "", clean_xpath)
        if clean_xpath.startswith("/"):
            clean_xpath = "." + clean_xpath
        return fragment_index, clean_xpath, int(offset or 0), text_index

    def _resolve_xpointer_element(self, xpointer: str):
        fragment_index, clean_xpath, offset, text_index = self._parse_xpointer(xpointer)
        item, _tree, element = self.ebook_parser._resolve_xpath_target_node(
            self.book_path.name,
            self.spine_map,
            fragment_index,
            clean_xpath,
        )
        if item is None or element is None:
            raise ValueError(f"Could not resolve xpointer: {xpointer!r}")
        return int(item["spine_index"]), element, offset, text_index

    def _element_to_cfi_steps(self, element) -> list[str]:
        chain = []
        current = element
        while current is not None and self._local_name(current) not in ("", "html"):
            chain.append(current)
            current = current.getparent()

        steps = []
        for node in reversed(chain):
            parent = node.getparent()
            if parent is None:
                continue
            siblings = self._element_children(parent)
            if node not in siblings:
                continue
            steps.append(str((siblings.index(node) + 1) * 2))
        if not steps:
            steps = ["4"]
        return steps

    def xpointer_to_cfi(self, xpointer: str) -> str:
        fragment_index, element, offset, text_index = self._resolve_xpointer_element(xpointer)
        local_steps = self._element_to_cfi_steps(element)
        text_step = 1
        if text_index is not None:
            nodes = element.xpath("text()")
            if text_index > len(nodes):
                raise ValueError(f"Text node {text_index} not found: {xpointer!r}")
            node = nodes[text_index - 1]
            if not str(node):
                raise ValueError(f"Empty text node: {xpointer!r}")
            if node.is_tail:
                children = self._element_children(element)
                parent = node.getparent()
                if parent not in children:
                    raise ValueError(f"Unsupported tail node: {xpointer!r}")
                text_step = 2 * children.index(parent) + 3
        local_steps.append(f"{text_step}:{max(0, int(offset or 0))}")
        spine_step = fragment_index * 2
        return f"epubcfi(/6/{spine_step}!/{'/'.join(local_steps)})"

    @staticmethod
    def as_cfi_range(cfi_start: str, cfi_end: str) -> str:
        bare_start = cfi_start.removeprefix("epubcfi(").removesuffix(")")
        bare_end = cfi_end.removeprefix("epubcfi(").removesuffix(")")
        prefix = ""
        start_parts = bare_start.split("/")
        end_parts = bare_end.split("/")
        shared = []
        for a, b in zip(start_parts, end_parts):
            if a != b:
                break
            shared.append(a)
        if shared:
            prefix = "/".join(shared)
        if not prefix:
            return f"epubcfi({bare_start},,{bare_end})"
        return f"epubcfi({prefix},{bare_start[len(prefix):]},{bare_end[len(prefix):]})"

    def xpointer_range_to_cfi(self, xpointer_start: str, xpointer_end: Optional[str]) -> str:
        cfi_start = self.xpointer_to_cfi(xpointer_start)
        cfi_end = self.xpointer_to_cfi(xpointer_end or xpointer_start)
        return self.as_cfi_range(cfi_start, cfi_end)

    @classmethod
    def split_cfi_range(cls, cfi: str):
        match = cls._CFI_RANGE_RE.match(str(cfi or ""))
        if not match:
            raise ValueError(f"Unsupported Grimmory CFI range: {cfi!r}")
        root, start, end = match.groups()
        return f"epubcfi({root}{start})", f"epubcfi({root}{end})"

    @staticmethod
    def _parse_cfi_point(cfi: str):
        bare = str(cfi or "").strip()
        bare = bare.removeprefix("epubcfi(").removesuffix(")")
        global_path, _, local_path = bare.partition("!")
        fragment_index = 1
        match = re.search(r"^/6/(\d+)", global_path)
        if match:
            fragment_index = int(match.group(1)) // 2
        local = local_path or global_path
        parts = [part for part in local.strip("/").split("/") if part]
        return fragment_index, parts

    def _cfi_point_to_xpointer(self, cfi: str) -> str:
        fragment_index, parts = self._parse_cfi_point(cfi)
        item = self._spine_item(fragment_index)
        tree = html.fromstring(item["content"])
        current = tree
        offset = 0
        text_step = None

        for part in parts:
            if ":" in part:
                step, offset_text = part.split(":", 1)
                try:
                    offset = int(offset_text or 0)
                except ValueError:
                    offset = 0
            else:
                step = part
            try:
                step_num = int(re.sub(r"\[.*?]", "", step))
            except ValueError:
                continue
            if step_num % 2 == 1:
                text_step = step_num
                continue
            children = self._element_children(current)
            index = (step_num // 2) - 1
            if index < 0 or index >= len(children):
                raise ValueError(f"CFI step {step_num} does not resolve in fragment {fragment_index}")
            current = children[index]

        anchor = current
        inline = []
        fragile = self.ebook_parser.CRENGINE_FRAGILE_INLINE_TAGS
        while self._local_name(anchor) in fragile:
            inline.append(anchor)
            anchor = anchor.getparent()
            if anchor is None:
                raise ValueError(f"Missing structural ancestor in CFI: {cfi!r}")
        xpath = self.ebook_parser._build_xpath(anchor)
        for child in reversed(inline):
            step = self.ebook_parser._build_inline_step(anchor, child)
            if not step:
                raise ValueError(f"Cannot reconstruct inline path: {cfi!r}")
            xpath += step
            anchor = child
        if text_step is None:
            text_step = 1
        children = self._element_children(current)
        if text_step == 1:
            expected_parent = current
            expected_tail = False
        else:
            index = (text_step - 3) // 2
            if index < 0 or index >= len(children):
                raise ValueError(f"CFI text step {text_step} does not resolve: {cfi!r}")
            expected_parent = children[index]
            expected_tail = True
        nodes = current.xpath("text()")
        matches = [i for i, node in enumerate(nodes, 1)
                   if node.is_tail == expected_tail and node.getparent() is expected_parent and str(node)]
        if len(matches) != 1:
            raise ValueError(f"CFI text node does not resolve: {cfi!r}")
        suffix = "/text()" if matches[0] == 1 else f"/text()[{matches[0]}]"
        return f"/body/DocFragment[{fragment_index}]/{xpath}{suffix}.{max(0, offset)}"

    def cfi_range_to_xpointers(self, cfi: str):
        cfi_start, cfi_end = self.split_cfi_range(cfi)
        return self._cfi_point_to_xpointer(cfi_start), self._cfi_point_to_xpointer(cfi_end)
