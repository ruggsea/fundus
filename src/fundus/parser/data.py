from abc import ABC, abstractmethod
from dataclasses import dataclass, fields
from itertools import chain
from typing import (
    Any,
    Collection,
    Dict,
    Iterable,
    Iterator,
    List,
    Optional,
    Sequence,
    Tuple,
    TypeVar,
    Union,
    overload,
)

import lxml.etree
import more_itertools
import xmltodict
from dict2xml import dict2xml
from lxml.etree import XPath, tostring
from typing_extensions import Self, TypeAlias

from fundus.utils.serialization import replace_keys_in_nested_dict

LDMappingValue: TypeAlias = Union[List[Dict[str, Any]], Dict[str, Any]]

_sentinel = object()

_T = TypeVar("_T")


class LinkedDataMapping:
    """
    This class is a @type -> LD mapping.
    Given LD:
        ld_1 = {
            @type: 'Article'
            ...
            ...
        }
    Article will be mapped to ld_1

    If there are multiple LDs with the same type, this type will map to a list of LDs.
    In this context an LD is represented as a python dict.
    """

    __UNKNOWN_TYPE__ = "UNKNOWN_TYPE"

    def __init__(self, lds: Iterable[Dict[str, Any]] = ()):
        for ld in lds:
            if graph := ld.get("@graph"):
                for nested in graph:
                    self.add_ld(nested)
            else:
                self.add_ld(ld)
        self.__xml: Optional[lxml.etree._Element] = None

    def serialize(self) -> Dict[str, Any]:
        return {attribute: value for attribute, value in self.__dict__.items() if "__" not in attribute}

    def add_ld(self, ld: Dict[str, Any], name: Optional[str] = None) -> None:
        if ld_type := ld.get("@type", name):
            if isinstance(ld_type, list):
                if len(ld_type) == 1:
                    ld_type = ld_type[0]
                else:
                    raise TypeError(f"Unable tp parse ld_type '{ld_type}' of type {list} with length != 1")
            if value := self.__dict__.get(ld_type):
                if not isinstance(value, list):
                    self.__dict__[ld_type] = [value]
                self.__dict__[ld_type].append(ld)
            else:
                self.__dict__[ld_type] = ld
        else:
            if not self.__dict__.get(self.__UNKNOWN_TYPE__):
                self.__dict__[self.__UNKNOWN_TYPE__] = []
            self.__dict__[self.__UNKNOWN_TYPE__].append(ld)

    def get_value_by_key_path(self, key_path: List[str], default: Any = None) -> Optional[Any]:
        """
        Works like get() except this one assumes a path is given as list of keys (str).
        I.e:
            key_path := ["mainEntity", "author"], default := {}
            results in self._ld_by_type.get("mainEntity").get("author")

        Whenever a key is missing or an empty value occurs down the path this funktion will immediately return
        <default>, but will not catch if not all values supports get()

        :param key_path: A list of keys in order forming a path to the desired value
        :param default: A default returned when either a key is missing or resulting in an empty/null value
        :return: The reached value or <default>
        """
        tmp = self.__dict__.copy()
        for key in key_path:
            if not (nxt := tmp.get(key)):
                return default
            tmp = nxt
        return tmp

    def __as_xml__(self) -> lxml.etree._Element:
        def to_unicode_characters(text: str) -> str:
            text = text.replace(":", "U003A")
            return text

        if self.__xml is None:
            xml = dict2xml(replace_keys_in_nested_dict({"linkedData": self.serialize()}, to_unicode_characters))
            self.__xml = lxml.etree.fromstring(xml)
        return self.__xml

    def xpath_search(self, query: XPath) -> List[Any]:
        """Search through LD using XPath expressions

        Internally, the content of the LinkedDataMapping is converted to XPath and then searched with <query>.
        To search for keys including invalid XML characters, use unicode representation instead:
        I.e. to search for the key "_16:9" write "//_16U003A9"

        Examples:
            LinkedDataMapping = {
                "b": {
                    "key": value1,
                }
                "c": {
                    "key": value2,
                }
            }

        LinkedDataMapping.xpath_search(XPath("//key"))
        >> [value1, value2]

        LinkedDataMapping.xpath_search(XPath("//b/key"))
        >> [value1]

        Args:
            query: An XPath expression

        Returns:
            A ordered list of search results
        """

        def node2string(n: lxml.etree._Element) -> str:
            return "".join(
                chunk
                for chunk in chain(
                    (n.text,),
                    chain(*((tostring(child, with_tail=False, encoding=str), child.tail) for child in n.getchildren())),
                    (n.tail,),
                )
                if chunk
            )

        def to_original_characters(text: str) -> str:
            text = text.replace("U003A", ":")
            return text

        nodes = query(self.__as_xml__())

        results = {}

        for i, node in enumerate(nodes):
            xml = f"<result{i}>" + node2string(node) + f"</result{i}>"
            results.update(replace_keys_in_nested_dict(xmltodict.parse(xml), to_original_characters))

        return list(results.values())

    def bf_search(self, key: str, depth: Optional[int] = None, default: Optional[_T] = None) -> Union[Any, _T]:
        """
        This is a classic BF search on the nested dicts representing the JSON-LD. <key> specifies the dict key to
        search, <depth> the depth level. If the depth level is set to None, this method will search through the whole
        LD. It is important to notice that this will only return the value of the first matched key.
        For more precise operations consider using get_by_key_path().

        I.e:

            considering the following LD:
                MainPage
                    @type
                    @content
                    BreadcrumbList
                        ...
                        ...
                    NewsArticle
                        datePublished: ...
                        authors: ...

            the contents of 'MainPage' count as depth 1.

            So
                breadth_first_search('authors') -> None,

            whereas

                breadth_first_search('@content') -> the value of key '@content'

            and

                breadth_first_search('authors', 2) -> the value of key 'authors'

        :param key: The dict key to search for
        :param depth: The searched depth, default None
        :return: The content of the first matched key or None
        """

        def search_recursive(nodes: Iterable[LDMappingValue], current_depth: int):
            if current_depth == depth:
                return _sentinel
            else:
                new: List[Dict[str, Any]] = []
                for node in nodes:
                    if isinstance(node, list):
                        new.extend(node)
                        continue
                    elif (value := node.get(key, _sentinel)) is not _sentinel:
                        return value

                    nested_dicts: Iterable[Dict[str, Any]] = filter(
                        lambda obj: isinstance(obj, dict), more_itertools.collapse(node.values(), base_type=dict)
                    )
                    new.extend(nested_dicts)

                if not new:
                    return _sentinel

                return search_recursive(new, current_depth + 1)

        result = search_recursive([self.__dict__], 0)

        if result == _sentinel:
            return default

        return result

    def __repr__(self):
        return f"LD containing {', '.join(content)!r}" if (content := self.__dict__.keys()) else "Empty LD"


class TextSequence(Sequence[str]):
    def __init__(self, texts: Iterable[str]):
        self._data: Tuple[str, ...] = tuple(texts)

    @overload
    def __getitem__(self, i: int) -> str:
        ...

    @overload
    def __getitem__(self, s: slice) -> "TextSequence":
        ...

    def __getitem__(self, i):
        return self._data[i] if isinstance(i, int) else type(self)(self._data[i])

    def __len__(self) -> int:
        return len(self._data)

    def __iter__(self) -> Iterator[str]:
        return iter(self._data)

    def __repr__(self) -> str:
        return repr(self._data)

    def __str__(self) -> str:
        return "\n".join(self)

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, TextSequence):
            return NotImplemented
        return self._data == other._data


@dataclass
class TextSequenceTree(ABC):
    """Base class to traverse and build trees of TextSequence."""

    def as_text_sequence(self) -> TextSequence:
        texts = [text for tl in self.df_traversal() for text in tl]
        return TextSequence(texts)

    def text(self, join_on: str = "\n\n") -> str:
        return join_on.join(self.as_text_sequence())

    def df_traversal(self) -> Iterable[TextSequence]:
        def recursion(o: object):
            if isinstance(o, TextSequence):
                yield o
            elif isinstance(o, Collection):
                for el in o:
                    yield from el
            else:
                yield o

        for value in self:
            yield from recursion(value)

    @abstractmethod
    def serialize(self) -> Dict[str, Any]:
        pass

    @classmethod
    @abstractmethod
    def deserialize(cls, serialized: Dict[str, Any]) -> Self:
        pass

    def __iter__(self) -> Iterator[Any]:
        field_values = [getattr(self, f.name) for f in fields(self)]
        yield from field_values

    def __str__(self):
        return self.text()

    def __bool__(self) -> bool:
        return bool(self.as_text_sequence())


@dataclass
class ArticleSection(TextSequenceTree):
    headline: TextSequence
    paragraphs: TextSequence

    def serialize(self) -> Dict[str, Any]:
        return {
            "headline": list(self.headline),
            "paragraphs": list(self.paragraphs),
        }

    @classmethod
    def deserialize(cls, serialized: Dict[str, Any]) -> Self:
        return cls(headline=TextSequence(serialized["headline"]), paragraphs=TextSequence(serialized["paragraphs"]))

    def __bool__(self):
        return bool(self.paragraphs)


@dataclass
class ArticleBody(TextSequenceTree):
    summary: TextSequence
    sections: List[ArticleSection]

    def serialize(self) -> Dict[str, Any]:
        return {
            "summary": list(self.summary),
            "sections": [section.serialize() for section in self.sections],
        }

    @classmethod
    def deserialize(cls, serialized: Dict[str, Any]) -> Self:
        return cls(
            summary=TextSequence(serialized["summary"]),
            sections=[ArticleSection.deserialize(section) for section in serialized["sections"]],
        )

    def __bool__(self):
        return any(bool(section) for section in self.sections)
