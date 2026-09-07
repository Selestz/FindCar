"""Small bounded HTML tree for semantic attributes; scripts are never executed."""

from collections.abc import Iterator
from dataclasses import dataclass, field
from html.parser import HTMLParser

from app.sources.base import SourceFailure


@dataclass
class Node:
    tag: str
    attrs: dict[str, str] = field(default_factory=dict)
    children: list["Node | str"] = field(default_factory=list)

    def walk(self) -> Iterator["Node"]:
        yield self
        for child in self.children:
            if isinstance(child, Node):
                yield from child.walk()

    def find(self, marker: str) -> list["Node"]:
        return [n for n in self.walk() if n.attrs.get("data-ftid") == marker]

    def text(self) -> str:
        return " ".join(
            child if isinstance(child, str) else child.text()
            for child in self.children
            if isinstance(child, str) or child.tag not in {"script", "style"}
        ).strip()


class Tree(HTMLParser):
    def __init__(self, html: str) -> None:
        super().__init__(convert_charrefs=True)
        self.root = Node("root")
        self.stack = [self.root]
        self.count = 0
        self.feed(html)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.count += 1
        if self.count > 50000 or len(self.stack) > 150:
            raise SourceFailure("PARSER_ERROR")
        node = Node(tag, {k: v or "" for k, v in attrs})
        self.stack[-1].children.append(node)
        if tag not in {
            "area",
            "base",
            "br",
            "col",
            "embed",
            "hr",
            "img",
            "input",
            "link",
            "meta",
            "param",
            "source",
            "track",
            "wbr",
        }:
            self.stack.append(node)

    def handle_endtag(self, tag: str) -> None:
        for i in range(len(self.stack) - 1, 0, -1):
            if self.stack[i].tag == tag:
                del self.stack[i:]
                break

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)
        self.handle_endtag(tag)

    def handle_data(self, data: str) -> None:
        self.stack[-1].children.append(data)
