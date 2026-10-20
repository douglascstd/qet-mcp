"""qet-xml — pure-Python object model for QElectroTech .qet project files.

Grounded in docs/qet-xml-schema.md (M0). Key format facts relied upon:
- <element>/<conductor> must live inside <elements>/<conductors> wrappers.
- Conductors link via dual key: element instance uuid + definition-level
  terminal uuid. <segment> children are omitted -> QET autoroutes on load.
- Element definitions are embedded in the project <collection> under
  "import/", making projects self-contained (type="embed://import/...").
"""
from __future__ import annotations

import copy
import json
import uuid as _uuid
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path

QET_VERSION = "0.100.0"

# Folio presets at the toolchain scale of 4 px/mm (matches
# data/titleblocks/*). A3 landscape 420x297mm: inner drawing area
# 16 cols x 100px = 1600px (=400mm), 9 rows x 100px = 900px; with the
# 200px (50mm) title block the folio proportions print true on A3.
FOLIO_A3_LANDSCAPE = {
    "cols": "16", "colsize": "100",
    "rows": "10", "rowsize": "101",
}


def _new_uuid() -> str:
    return "{" + str(_uuid.uuid4()) + "}"


# --------------------------------------------------------------------------- #
# Definitions (element library side)
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class Terminal:
    """Definition-level terminal (shared by every instance of the element)."""
    uuid: str
    name: str
    x: float
    y: float
    orientation: str  # n / s / e / w


class ElementDefinition:
    """An .elmt definition, kept as raw XML plus parsed terminals."""

    def __init__(self, embed_path: str, dom: ET.Element):
        if dom.tag != "definition":
            raise ValueError(f"not an element definition: <{dom.tag}>")
        self.embed_path = embed_path          # e.g. "import/cpi.elmt"
        self.dom = dom
        # master (coil), slave (contact) or simple — drives cross-referencing
        self.link_type = dom.get("link_type", "simple")
        self.terminals: list[Terminal] = [
            Terminal(
                uuid=t.get("uuid", ""),
                name=t.get("name", ""),
                x=float(t.get("x", "0")),
                y=float(t.get("y", "0")),
                orientation=t.get("orientation", "n"),
            )
            for t in dom.iter("terminal")
            if t.get("uuid")  # definition-level terminals carry uuids
        ]
        # dynamic text templates (e.g. the label placeholder); QET only
        # renders instance-level <dynamic_texts>, so instances must
        # materialize these (see ElementInstance.to_xml)
        self.dynamic_texts: list[ET.Element] = list(dom.iter("dynamic_text"))

    @classmethod
    def load(cls, path: str | Path, embed_path: str | None = None) -> "ElementDefinition":
        path = Path(path)
        dom = ET.fromstring(path.read_text(encoding="utf-8"))
        return cls(embed_path or f"import/{path.name}", dom)

    def terminal(self, key: str | int) -> Terminal:
        if isinstance(key, int):
            return self.terminals[key]
        for t in self.terminals:
            if t.name == key and t.name:
                return t
        # fallback for unnamed terminals: numeric string = index
        if key.isdigit() and int(key) < len(self.terminals):
            return self.terminals[int(key)]
        raise KeyError(f"no terminal named {key!r} in {self.embed_path} "
                       f"(have: {[t.name for t in self.terminals]})")


# --------------------------------------------------------------------------- #
# Diagram content
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class TerminalRef:
    """A connectable endpoint: instance uuid + definition terminal."""
    element_uuid: str
    terminal: Terminal


class ElementInstance:
    def __init__(self, definition: ElementDefinition, x: float, y: float,
                 label: str = "", orientation: int = 0, prefix: str = "",
                 uuid: str | None = None,
                 dynamic_texts_dom: ET.Element | None = None,
                 links: "list[str] | None" = None):
        self.definition = definition
        self.uuid = uuid or _new_uuid()
        self.x, self.y = x, y
        self.label = label
        self.orientation = orientation
        self.prefix = prefix
        # uuids of elements this one is cross-referenced to (a slave
        # contact points at its master coil) -> <links_uuids>
        self.links: list[str] = links or []
        # raw <dynamic_texts> read from an existing file (preserved verbatim
        # so user-moved texts survive a round-trip); None = generate from
        # the definition's templates
        self.dynamic_texts_dom = dynamic_texts_dom
        # the whole <element> node of a QET-saved file: attributes (z,
        # freezeLabel…) and children we don't model (<terminals>, <inputs>,
        # <properties>, <texts_groups>, extra <elementInformation>s) are
        # written back verbatim — a GUI-made project lost them all otherwise
        self.raw_dom: ET.Element | None = None

    def terminal(self, key: str | int) -> TerminalRef:
        return TerminalRef(self.uuid, self.definition.terminal(key))

    def set_label(self, label: str) -> None:
        """Change the designation.  Preserved label texts (dynamic texts,
        also those inside <texts_groups>) are rewritten in place so the
        user's layout survives; without preserved texts they are
        re-materialized from the definition."""
        self.label = label
        holders = [] if self.dynamic_texts_dom is None else [self.dynamic_texts_dom]
        if self.raw_dom is not None:
            holders += self.raw_dom.findall("texts_groups")
            # QET recomputes the label from an autonum formula on load
            # (e.g. "%id%prefix%sequ"); a literal formula keeps ours
            for info in self.raw_dom.iter("elementInformation"):
                if info.get("name") == "formula" and info.text:
                    info.text = label
        for holder in holders:
            for t in holder.iter("dynamic_elmt_text"):
                if t.findtext("info_name") == "label":
                    text = t.find("text")
                    if text is None:
                        text = ET.SubElement(t, "text")
                    text.text = label

    def _materialize_dynamic_texts(self) -> ET.Element:
        """Instantiate the definition's dynamic_text templates.

        QET only renders instance-level <dynamic_texts>; without this the
        label would be stored but never displayed.
        """
        wrapper = ET.Element("dynamic_texts")
        for template in self.definition.dynamic_texts:
            t = ET.SubElement(wrapper, "dynamic_elmt_text",
                              dict(template.attrib))
            t.set("uuid", _new_uuid())
            info_name = template.findtext("info_name", "")
            text = ET.SubElement(t, "text")
            text.text = self.label if info_name == "label" else \
                template.findtext("text", "")
            if info_name:
                ET.SubElement(t, "info_name").text = info_name
        return wrapper

    def to_xml(self) -> ET.Element:
        if self.raw_dom is not None:
            return self._to_xml_preserved()
        e = ET.Element("element", {
            "uuid": self.uuid,
            "type": f"embed://{self.definition.embed_path}",
            "x": _num(self.x), "y": _num(self.y),
            "orientation": str(self.orientation), "z": "10",
            "prefix": self.prefix, "freezeLabel": "false",
        })
        if self.links:
            links_dom = ET.SubElement(e, "links_uuids")
            for master_uuid in self.links:
                ET.SubElement(links_dom, "link_uuid", {"uuid": master_uuid})
        infos = ET.SubElement(e, "elementInformations")
        if self.label:
            info = ET.SubElement(infos, "elementInformation",
                                 {"show": "1", "name": "label"})
            info.text = self.label
        if self.dynamic_texts_dom is not None:
            e.append(copy.deepcopy(self.dynamic_texts_dom))
        else:
            e.append(self._materialize_dynamic_texts())
        return e

    # QET's child order inside <element>
    _CHILD_ORDER = ("terminals", "inputs", "links_uuids", "elementInformations",
                    "properties", "dynamic_texts", "texts_groups")

    def _to_xml_preserved(self) -> ET.Element:
        """Re-emit the original node, patching only what we model."""
        e = copy.deepcopy(self.raw_dom)
        e.attrib.update({
            "uuid": self.uuid,
            "type": f"embed://{self.definition.embed_path}",
            "x": _num(self.x), "y": _num(self.y),
            "orientation": str(self.orientation), "prefix": self.prefix,
        })

        def put(node: ET.Element) -> None:
            """Replace the same-tag child, or insert at QET's position."""
            old = e.find(node.tag)
            if old is not None:
                e.insert(list(e).index(old), node)
                e.remove(old)
                return
            rank = self._CHILD_ORDER.index(node.tag)
            pos = sum(1 for c in e if c.tag in self._CHILD_ORDER[:rank])
            e.insert(pos, node)

        if self.links:
            links_dom = ET.Element("links_uuids")
            for master_uuid in self.links:
                ET.SubElement(links_dom, "link_uuid", {"uuid": master_uuid})
            put(links_dom)
        elif e.find("links_uuids") is not None:
            e.remove(e.find("links_uuids"))

        infos = e.find("elementInformations")
        if infos is None and self.label:
            infos = ET.Element("elementInformations")
            put(infos)
        if infos is not None:
            info = next((i for i in infos.findall("elementInformation")
                         if i.get("name") == "label"), None)
            if self.label:
                if info is None:
                    info = ET.SubElement(infos, "elementInformation",
                                         {"show": "1", "name": "label"})
                info.text = self.label
            elif info is not None:
                infos.remove(info)

        put(copy.deepcopy(self.dynamic_texts_dom)
            if self.dynamic_texts_dom is not None
            else self._materialize_dynamic_texts())
        return e


class Conductor:
    #: attributes QET always writes; provided so our output opens cleanly
    DEFAULTS = {
        "type": "multi", "num": "", "color": "#000000", "condsize": "1",
        "displaytext": "1", "x": "0", "y": "0", "freezeLabel": "false",
    }

    def __init__(self, t1: TerminalRef, t2: TerminalRef,
                 path: "list[tuple[str, float]] | None" = None,
                 **props: str):
        self.element1, self.terminal1 = t1.element_uuid, t1.terminal.uuid
        self.element2, self.terminal2 = t2.element_uuid, t2.terminal.uuid
        # optional explicit manhattan route from t1 to t2:
        # [("v", 20), ("h", -130), ("v", 39)]  (must sum to the terminal
        # delta within 1px, else QET falls back to autorouting)
        self.path = path
        self.props = {**self.DEFAULTS, **{k: str(v) for k, v in props.items()}}
        # unmodelled children of a QET-saved conductor (<sequentialNumbers>
        # holds the autonum state), written back verbatim
        self.extra_children: list[ET.Element] = []

    def to_xml(self) -> ET.Element:
        # Without explicit path we emit NO <segment> children: QET
        # autoroutes on load (conductor.cpp:1147).
        e = ET.Element("conductor", {
            "element1": self.element1, "terminal1": self.terminal1,
            "element2": self.element2, "terminal2": self.terminal2,
            **self.props,
        })
        for orient, length in (self.path or []):
            ET.SubElement(e, "segment", {
                "orientation": ("horizontal" if orient == "h"
                                else "vertical"),
                "length": _num(length),
            })
        for extra in self.extra_children:
            e.append(copy.deepcopy(extra))
        return e


# --------------------------------------------------------------------------- #
# Free text and drawing table (QET fork qet-pdm additions)
# --------------------------------------------------------------------------- #

def font_string(family: str, size: int, bold: bool = False) -> str:
    """QFont::toString() 10-field legacy form — Qt6 fromString accepts it."""
    return f"{family},{size},-1,5,{75 if bold else 50},0,0,0,0,0"


class TextItem:
    """Independent diagram text (<input> under <inputs>). Styling per the
    fork: optional font override, badge_bg (solid rounded background) or
    badge_border (white pill with colored outline). Colors are any form
    QColor parses (#rrggbb / #aarrggbb / names)."""

    def __init__(self, x: float, y: float, text: str, rotation: float = 0,
                 font: str | None = None, badge_bg: str | None = None,
                 badge_border: str | None = None,
                 uuid: str | None = None):
        self.x, self.y, self.text, self.rotation = x, y, text, rotation
        self.font = font                     # None = QET default
        self.badge_bg = badge_bg
        self.badge_border = badge_border
        # 供群組(element_groups)識別;QET 端格式含大括號
        self.uuid = uuid or ("{%s}" % _uuid.uuid4())

    @classmethod
    def from_xml(cls, dom: ET.Element) -> "TextItem":
        return cls(float(dom.get("x", "0")), float(dom.get("y", "0")),
                   dom.get("text", ""),
                   rotation=float(dom.get("rotation", "0")),
                   font=dom.get("font"), badge_bg=dom.get("badge_bg"),
                   badge_border=dom.get("badge_border"),
                   uuid=dom.get("uuid"))

    def to_xml(self) -> ET.Element:
        attrs = {"x": _num(self.x), "y": _num(self.y), "text": self.text,
                 "rotation": _num(self.rotation), "uuid": self.uuid}
        if self.font:
            attrs["font"] = self.font
        if self.badge_bg:
            attrs["badge_bg"] = self.badge_bg
        if self.badge_border:
            attrs["badge_border"] = self.badge_border
        return ET.Element("input", attrs)


class DrawingTable:
    """Drawing table (<drawing_table> under <drawing_tables>); the whole
    table lives in the JSON `state` attribute (diagramtableitem.cpp).
    Cell arrays are row-major; cell_bg entries are HexArgb or "" (no
    fill); halign/valign are Qt::Alignment ints; cell_size 0 = default."""

    HALIGN = {"left": 0x1, "right": 0x2, "center": 0x4}
    VALIGN = {"top": 0x20, "bottom": 0x40, "middle": 0x80}

    def __init__(self, x: float, y: float, rows: int, cols: int,
                 data: "list[list[str]] | None" = None,
                 col_widths: "list[float] | None" = None,
                 row_height: float = 24.0, font: str | None = None,
                 z: float = 0, uuid: str | None = None):
        self.x, self.y, self.z = x, y, z
        self.uuid = uuid or ("{%s}" % _uuid.uuid4())
        # 合併儲存格:[列, 欄, 跨列, 跨欄](0-based,同 QET state JSON)
        self.spans: "list[list[int]]" = []
        self.rows, self.cols = rows, cols
        self.row_height = row_height
        self.font = font                     # None = QET default
        self.col_widths = list(col_widths or [])[:cols]
        self.col_widths += [80.0] * (cols - len(self.col_widths))
        n = rows * cols
        self.cells = [""] * n
        for r, row in enumerate(data or []):
            for c, value in enumerate(row):
                if r < rows and c < cols:
                    self.cells[r * cols + c] = str(value)
        self.cell_bg = [""] * n
        self.cell_halign = [self.HALIGN["left"]] * n
        self.cell_valign = [self.VALIGN["middle"]] * n
        self.cell_size = [0] * n

    def set_cell(self, row: int, col: int, text: str) -> None:
        if not (0 <= row < self.rows and 0 <= col < self.cols):
            raise IndexError(f"cell ({row},{col}) outside "
                             f"{self.rows}x{self.cols} table")
        self.cells[row * self.cols + col] = text

    def style_range(self, r0: int, c0: int, r1: int, c1: int,
                    bg: str | None = None, halign: str | None = None,
                    valign: str | None = None,
                    size: int | None = None) -> int:
        """Style every cell in the inclusive range; returns cell count.
        bg "" clears the fill; halign/valign take left/center/right and
        top/middle/bottom; size 0 restores the default font size."""
        touched = 0
        for r in range(max(0, r0), min(self.rows - 1, r1) + 1):
            for c in range(max(0, c0), min(self.cols - 1, c1) + 1):
                i = r * self.cols + c
                if bg is not None:
                    self.cell_bg[i] = bg
                if halign is not None:
                    self.cell_halign[i] = self.HALIGN[halign]
                if valign is not None:
                    self.cell_valign[i] = self.VALIGN[valign]
                if size is not None:
                    self.cell_size[i] = size
                touched += 1
        return touched

    def merge_cells(self, r0: int, c0: int, r1: int, c1: int) -> None:
        """Excel 式合併:範圍(含界)併成一格,錨點=左上;範圍內既有
        合併先移除。被覆蓋格資料保留(QET 取消合併會重現)。"""
        r0, r1 = sorted((max(0, r0), min(self.rows - 1, r1)))
        c0, c1 = sorted((max(0, c0), min(self.cols - 1, c1)))
        if r0 == r1 and c0 == c1:
            raise ValueError("merge needs at least two cells")
        region = [r0, c0, r1 - r0 + 1, c1 - c0 + 1]
        self.unmerge_cells(r0, c0, r1, c1)
        self.spans.append(region)

    def unmerge_cells(self, r0: int, c0: int, r1: int, c1: int) -> int:
        """拆開與範圍相交的合併;回傳拆掉的個數。"""
        def hits(sp):
            sr, sc, srn, scn = sp
            return not (sr + srn - 1 < r0 or sr > r1
                        or sc + scn - 1 < c0 or sc > c1)
        before = len(self.spans)
        self.spans = [sp for sp in self.spans if not hits(sp)]
        return before - len(self.spans)

    @classmethod
    def from_xml(cls, dom: ET.Element) -> "DrawingTable":
        state = json.loads(dom.get("state", "{}") or "{}")
        rows = int(state.get("rows", 1))
        cols = int(state.get("cols", 1))
        t = cls(float(dom.get("x", "0")), float(dom.get("y", "0")),
                rows, cols,
                col_widths=[float(w) for w in state.get("widths", [])],
                row_height=float(state.get("row_height", 24)),
                font=state.get("font"),
                z=float(dom.get("z", "0")),
                uuid=dom.get("uuid"))
        t.spans = [[int(v) for v in sp]
                   for sp in state.get("spans", []) if len(sp) == 4]
        n = rows * cols

        def _fill(target: list, values: list, cast) -> None:
            for i, v in enumerate(values[:n]):
                target[i] = cast(v)
        _fill(t.cells, state.get("cells", []), str)
        _fill(t.cell_bg, state.get("cell_bg", []), str)
        _fill(t.cell_halign, state.get("cell_halign", []), int)
        _fill(t.cell_valign, state.get("cell_valign", []), int)
        _fill(t.cell_size, state.get("cell_size", []), int)
        return t

    def to_xml(self) -> ET.Element:
        state = {"rows": self.rows, "cols": self.cols,
                 "row_height": self.row_height,
                 "widths": self.col_widths, "cells": self.cells,
                 "cell_bg": self.cell_bg,
                 "cell_halign": self.cell_halign,
                 "cell_valign": self.cell_valign,
                 "cell_size": self.cell_size}
        if self.font:
            state["font"] = self.font
        if self.spans:
            state["spans"] = self.spans
        return ET.Element("drawing_table", {
            "uuid": self.uuid,
            "x": _num(self.x), "y": _num(self.y), "z": _num(self.z),
            "state": json.dumps(state, ensure_ascii=False,
                                separators=(",", ":")),
        })


class Diagram:
    ATTR_DEFAULTS = {
        "cols": "17", "colsize": "60", "rows": "8", "rowsize": "80",
        "author": "qet-mcp", "date": "null", "order": "1",
    }

    def __init__(self, project: "QetProject", title: str = "",
                 attrs: dict[str, str] | None = None):
        self.project = project
        self.attrs = {**self.ATTR_DEFAULTS, **(attrs or {}), "title": title}
        self.elements: list[ElementInstance] = []
        self.conductors: list[Conductor] = []
        self.texts: list[TextItem] = []
        self.tables: list[DrawingTable] = []
        # 群組(每組 = uuid 字串集合;可含元件/文字/形狀/圖片/表格)
        self.groups: "list[set[str]]" = []
        # custom titleblock variables (%{key} in templates), serialized as
        # the diagram's <properties> child (titleblockproperties.cpp:96)
        self.properties: dict[str, str] = {}
        # diagram children we do not model (shapes, images, legacy tables…)
        # — preserved verbatim on save, same policy as extra_root_children
        self.extra_children: list[ET.Element] = []

    # -- authoring API ------------------------------------------------------
    def place_element(self, elmt: str | Path | ElementDefinition, x: float,
                      y: float, label: str = "", orientation: int = 0,
                      prefix: str = "") -> ElementInstance:
        definition = (elmt if isinstance(elmt, ElementDefinition)
                      else ElementDefinition.load(elmt))
        definition = self.project.embed(definition)
        inst = ElementInstance(definition, x, y, label, orientation, prefix)
        self.elements.append(inst)
        return inst

    #: revision-table columns bound by the titleblock (rev{n}-<field>)
    REVISION_FIELDS = ("idx", "date", "desc", "zone", "by", "appd")

    def set_revisions(self, revisions: "list[dict]", rows: int = 6) -> None:
        """Fill the titleblock revision history (rev1..rows).

        Each dict may hold idx/date/desc/zone/by/appd. Unused rows are
        set to empty strings so QET renders blanks instead of the raw
        %{rev3-idx} placeholders.
        """
        for i in range(1, rows + 1):
            entry = revisions[i - 1] if i <= len(revisions) else {}
            for key in self.REVISION_FIELDS:
                self.properties[f"rev{i}-{key}"] = str(entry.get(key, ""))

    def add_text(self, x: float, y: float, text: str, rotation: float = 0,
                 font: str | None = None, badge_bg: str | None = None,
                 badge_border: str | None = None) -> TextItem:
        item = TextItem(x, y, text, rotation, font, badge_bg, badge_border)
        self.texts.append(item)
        return item

    def add_table(self, x: float, y: float, rows: int, cols: int,
                  data: "list[list[str]] | None" = None,
                  col_widths: "list[float] | None" = None,
                  row_height: float = 24.0,
                  font: str | None = None) -> DrawingTable:
        table = DrawingTable(x, y, rows, cols, data=data,
                             col_widths=col_widths, row_height=row_height,
                             font=font)
        self.tables.append(table)
        return table

    def group_items(self, uuids: "list[str]") -> "set[str]":
        """把一組 uuid 群組(QET 端:選一個=選整組、一起移動)。
        已屬其他群組者先移出;不足兩個丟 ValueError。"""
        ids = {u for u in uuids if u}
        if len(ids) < 2:
            raise ValueError("a group needs at least two members")
        for g in self.groups:
            g -= ids
        self.groups = [g for g in self.groups if len(g) >= 2]
        self.groups.append(ids)
        return ids

    def ungroup_items(self, uuids: "list[str]") -> int:
        """解散含任一指定 uuid 的群組;回傳解散數。"""
        ids = set(uuids)
        before = len(self.groups)
        self.groups = [g for g in self.groups if not (g & ids)]
        return before - len(self.groups)

    def connect(self, t1: TerminalRef, t2: TerminalRef,
                path: "list[tuple[str, float]] | None" = None,
                **props: str) -> Conductor:
        c = Conductor(t1, t2, path=path, **props)
        self.conductors.append(c)
        return c

    def find_conductor(self, ta: TerminalRef, tb: TerminalRef):
        """Find the conductor between two terminal refs (either direction)."""
        want = {(ta.element_uuid, ta.terminal.uuid),
                (tb.element_uuid, tb.terminal.uuid)}
        for c in self.conductors:
            if {(c.element1, c.terminal1),
                    (c.element2, c.terminal2)} == want:
                return c
        return None

    def remove_element(self, inst: ElementInstance) -> int:
        """Remove an element and any conductor attached to it.
        Returns the number of conductors also removed."""
        self.elements.remove(inst)
        before = len(self.conductors)
        self.conductors = [c for c in self.conductors
                           if inst.uuid not in (c.element1, c.element2)]
        # drop cross-reference links pointing at the removed element
        for e in self.elements:
            if inst.uuid in e.links:
                e.links.remove(inst.uuid)
        return before - len(self.conductors)

    def auto_xref(self) -> dict:
        """Cross-reference parts sharing a designation: link each slave
        (contact) to the master (coil) of the same label. QET then renders
        the coil's contact table and each contact's parent reference."""
        by_label: "dict[str, list[ElementInstance]]" = {}
        for e in self.elements:
            if e.label:
                by_label.setdefault(e.label, []).append(e)
        linked, groups = 0, 0
        for label, group in by_label.items():
            masters = [e for e in group
                       if e.definition.link_type == "master"]
            slaves = [e for e in group if e.definition.link_type == "slave"]
            if not (masters and slaves):
                continue
            groups += 1
            master = masters[0]
            for s in slaves:
                if master.uuid not in s.links:
                    s.links.append(master.uuid)
                    linked += 1
        return {"linked": linked, "groups": groups}

    # -- serialization ------------------------------------------------------
    def to_xml(self) -> ET.Element:
        d = ET.Element("diagram", self.attrs)
        if self.properties:
            props = ET.SubElement(d, "properties")
            for key, value in self.properties.items():
                p = ET.SubElement(props, "property",
                                  {"name": key, "show": "1"})
                p.text = value
        elements = ET.SubElement(d, "elements")
        for inst in self.elements:
            elements.append(inst.to_xml())
        conductors = ET.SubElement(d, "conductors")
        for c in self.conductors:
            conductors.append(c.to_xml())
        if self.texts:
            inputs = ET.SubElement(d, "inputs")
            for t in self.texts:
                inputs.append(t.to_xml())
        if self.tables:
            dtables = ET.SubElement(d, "drawing_tables")
            for t in self.tables:
                dtables.append(t.to_xml())
        real_groups = [g for g in self.groups if len(g) >= 2]
        if real_groups:
            groups = ET.SubElement(d, "element_groups")
            for g in real_groups:
                ET.SubElement(groups, "group",
                              {"uuids": ";".join(sorted(g))})
        for extra in self.extra_children:
            d.append(copy.deepcopy(extra))
        return d


# --------------------------------------------------------------------------- #
# Project
# --------------------------------------------------------------------------- #

class QetProject:
    def __init__(self, title: str = "", version: str = QET_VERSION):
        self.title = title
        self.version = version
        self.diagrams: list[Diagram] = []
        self.collection: dict[str, ElementDefinition] = {}  # embed_path -> def
        # embedded titleblock templates: name -> <titleblocktemplate> element
        self.titleblock_templates: dict[str, ET.Element] = {}
        # project-level nodes we don't model (properties, newdiagrams, ...):
        # preserved verbatim across open/save, or QET loses user settings
        # (new-folio defaults, autonum rules live in <newdiagrams>)
        self.extra_root_children: list[ET.Element] = []
        self.extra_root_attrs: dict[str, str] = {}
        # <names> (display names) of collection categories, by category
        # path ("import/", "import/sub/"), kept so the GUI tree keeps them
        self.category_names: dict[str, ET.Element] = {}

    # -- construction -------------------------------------------------------
    @classmethod
    def new(cls, title: str = "") -> "QetProject":
        prj = cls(title)
        prj.add_diagram(title or "folio 1")
        return prj

    def add_diagram(self, title: str = "") -> Diagram:
        d = Diagram(self, title)
        d.attrs["order"] = str(len(self.diagrams) + 1)
        self.diagrams.append(d)
        return d

    def diagram(self, index: int = 0) -> Diagram:
        return self.diagrams[index]

    def embed(self, definition: ElementDefinition) -> ElementDefinition:
        """Register a definition in the embedded collection (dedup by path)."""
        existing = self.collection.get(definition.embed_path)
        if existing is not None:
            return existing
        self.collection[definition.embed_path] = definition
        return definition

    # -- open / save --------------------------------------------------------
    @classmethod
    def open(cls, path: str | Path) -> "QetProject":
        root = ET.fromstring(Path(path).read_text(encoding="utf-8"))
        if root.tag != "project":
            raise ValueError(f"not a QET project: <{root.tag}>")
        prj = cls(root.get("title", ""), root.get("version", QET_VERSION))
        prj.extra_root_attrs = {k: v for k, v in root.attrib.items()
                                if k not in ("title", "version")}
        prj.extra_root_children = [
            copy.deepcopy(c) for c in root
            if c.tag not in ("collection", "titleblocktemplates", "diagram")]

        collection = root.find("collection")
        if collection is not None:
            prj._read_collection(collection, prefix="")

        tbts = root.find("titleblocktemplates")
        if tbts is not None:
            for t in tbts.findall("titleblocktemplate"):
                prj.titleblock_templates[t.get("name", "")] = copy.deepcopy(t)

        for ddom in root.findall("diagram"):
            prj._read_diagram(ddom)
        return prj

    def embed_titleblock(self, template: ET.Element,
                         apply_to_diagrams: bool = True) -> str:
        """Embed a <titleblocktemplate> and reference it from diagrams."""
        name = template.get("name", "")
        self.titleblock_templates[name] = copy.deepcopy(template)
        if apply_to_diagrams:
            for d in self.diagrams:
                d.attrs["titleblocktemplate"] = name
        # keep the new-folio default (project properties dialog) in sync,
        # or QET falls back to "default template" for added pages
        for extra in self.extra_root_children:
            if extra.tag == "newdiagrams":
                inset = extra.find("inset")
                if inset is not None:
                    inset.set("titleblocktemplate", name)
                    inset.set("titleblocktemplateCollection", "embedded")
        return name

    def _read_collection(self, node: ET.Element, prefix: str) -> None:
        for cat in node.findall("category"):
            path = f"{prefix}{cat.get('name', '')}/"
            names = cat.find("names")
            if names is not None:
                self.category_names[path] = copy.deepcopy(names)
            self._read_collection(cat, path)
        for holder in node.findall("element"):
            ddom = holder.find("definition")
            if ddom is None:
                continue
            embed_path = f"{prefix}{holder.get('name', '')}"
            self.collection[embed_path] = ElementDefinition(
                embed_path, copy.deepcopy(ddom))

    def _read_diagram(self, ddom: ET.Element) -> None:
        d = Diagram(self, ddom.get("title", ""),
                    {k: v for k, v in ddom.attrib.items() if k != "title"})
        props = ddom.find("properties")
        if props is not None:
            d.properties = {p.get("name", ""): (p.text or "")
                            for p in props.findall("property")}
        self.diagrams.append(d)

        elements = ddom.find("elements")
        for edom in ([] if elements is None else elements.findall("element")):
            etype = edom.get("type", "")
            embed_path = etype.removeprefix("embed://")
            definition = self.collection.get(embed_path)
            if definition is None:
                continue  # non-embedded or missing definition: skip in M1
            label = ""
            for info in edom.iter("elementInformation"):
                if info.get("name") == "label":
                    label = info.text or ""
            dtexts = edom.find("dynamic_texts")
            links_dom = edom.find("links_uuids")
            links = ([lk.get("uuid", "") for lk in links_dom]
                     if links_dom is not None else [])
            d.elements.append(ElementInstance(
                definition,
                x=float(edom.get("x", "0")), y=float(edom.get("y", "0")),
                label=label,
                orientation=int(edom.get("orientation", "0")),
                prefix=edom.get("prefix", ""),
                uuid=edom.get("uuid"),
                dynamic_texts_dom=(copy.deepcopy(dtexts)
                                   if dtexts is not None else None),
                links=[u for u in links if u],
            ))
            d.elements[-1].raw_dom = copy.deepcopy(edom)

        inputs = ddom.find("inputs")
        for tdom in ([] if inputs is None else inputs.findall("input")):
            d.texts.append(TextItem.from_xml(tdom))

        dtables = ddom.find("drawing_tables")
        for tdom in ([] if dtables is None
                     else dtables.findall("drawing_table")):
            d.tables.append(DrawingTable.from_xml(tdom))

        egroups = ddom.find("element_groups")
        for gdom in ([] if egroups is None else egroups.findall("group")):
            ids = {u for u in gdom.get("uuids", "").split(";") if u}
            if len(ids) >= 2:
                d.groups.append(ids)

        # keep everything else (shapes, images, legacy tables…) verbatim —
        # dropping unmodelled nodes destroyed project data once (2026-07-08)
        modelled = {"properties", "elements", "conductors", "inputs",
                    "drawing_tables", "element_groups"}
        d.extra_children = [copy.deepcopy(child) for child in ddom
                            if child.tag not in modelled]

        conductors = ddom.find("conductors")
        for cdom in ([] if conductors is None else conductors.findall("conductor")):
            c = object.__new__(Conductor)
            c.element1 = cdom.get("element1", "")
            c.terminal1 = cdom.get("terminal1", "")
            c.element2 = cdom.get("element2", "")
            c.terminal2 = cdom.get("terminal2", "")
            segs = [("h" if s.get("orientation") == "horizontal" else "v",
                     float(s.get("length", "0")))
                    for s in cdom.findall("segment")]
            c.path = segs or None            # preserve explicit routing
            c.props = {k: v for k, v in cdom.attrib.items()
                       if k not in ("element1", "terminal1",
                                    "element2", "terminal2")}
            c.extra_children = [copy.deepcopy(x) for x in cdom
                                if x.tag != "segment"]
            d.conductors.append(c)

    def to_xml(self) -> ET.Element:
        attrs = {"title": self.title, "version": self.version}
        attrs.update(self.extra_root_attrs)
        root = ET.Element("project", attrs)
        for extra in self.extra_root_children:
            root.append(copy.deepcopy(extra))
        for d in self.diagrams:
            root.append(d.to_xml())

        if self.titleblock_templates:
            tbts = ET.SubElement(root, "titleblocktemplates")
            for name in sorted(self.titleblock_templates):
                tbts.append(copy.deepcopy(self.titleblock_templates[name]))

        collection = ET.SubElement(root, "collection")
        # QET requires the "import" root category even when empty:
        # without it, element integration (cross-project paste, drag &
        # drop into the project) fails on the QET side.
        imp = ET.SubElement(collection, "category", {"name": "import"})
        if "import/" in self.category_names:
            imp.append(copy.deepcopy(self.category_names["import/"]))
        for embed_path in sorted(self.collection):
            node = collection
            *dirs, filename = embed_path.split("/")
            cat_path = ""
            for name in dirs:  # build/reuse category chain
                cat_path += f"{name}/"
                nxt = next((c for c in node.findall("category")
                            if c.get("name") == name), None)
                if nxt is None:
                    nxt = ET.SubElement(node, "category", {"name": name})
                    if cat_path in self.category_names:
                        nxt.append(copy.deepcopy(self.category_names[cat_path]))
                node = nxt
            holder = ET.SubElement(node, "element", {"name": filename})
            holder.append(copy.deepcopy(self.collection[embed_path].dom))
        return root

    def save(self, path: str | Path) -> Path:
        path = Path(path)
        tree = ET.ElementTree(self.to_xml())
        ET.indent(tree)
        tree.write(path, encoding="utf-8", xml_declaration=True)
        return path


def _num(v: float) -> str:
    return str(int(v)) if float(v).is_integer() else str(v)
