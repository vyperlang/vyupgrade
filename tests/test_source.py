import pytest

from vyupgrade import source as source_tools

from vyupgrade.source import (
    code_mask,
    find_matching,
    find_matching_open,
    line_number,
    line_offsets,
    replace_identifier,
    split_top_level_args,
    split_top_level_arg_spans,
)


def test_physical_line_offsets_and_numbers_share_all_newline_conventions():
    source = "a\rb\r\nc\nd"
    assert line_offsets(source) == [0, 2, 5, 7]
    assert [line_number(source, i) for i in range(len(source) + 1)] == [1, 1, 2, 2, 2, 3, 3, 4, 4]
    assert code_mask("# note\rname") == [False] * 6 + [True] * 5


@pytest.mark.parametrize(
    "literal",
    [r'"a\", b"', r"'a\', b'", '"""a, b\nc"""', r'"""a\""", b"""'],
)
def test_argument_splitting_preserves_quoted_commas(literal: str) -> None:
    text = f' {literal} , nested([1, 2], {{"x": 3}}), end, '
    expected = [literal, 'nested([1, 2], {"x": 3})', "end"]
    assert split_top_level_args(text) == expected
    spans = split_top_level_arg_spans(text)
    assert spans is not None
    assert [text[start:end] for start, end, _ in spans] == expected


@pytest.mark.parametrize("text", ["([)]", "[}", 'a, "unterminated', "a, (b", "a, b)"])
def test_argument_splitting_rejects_malformed_delimiters(text: str) -> None:
    assert split_top_level_args(text) is None


@pytest.mark.parametrize(
    "body",
    [
        "a, # misleading ), ] and ,\n b",
        r'"a\"), b", [1, 2]',
        '"""a),\nb""", {"x": [1]}',
        r'"""a\"""), b""", 1',
    ],
)
def test_forward_and_reverse_matching_share_lexical_rules(body: str) -> None:
    text = f"call({body}) + suffix"
    closing = text.rindex(")")
    assert find_matching(text, 4) == closing
    assert find_matching_open(text, closing) == 4


@pytest.mark.parametrize("text", ["([)]", "([})", "(unterminated", '"(inside)"'])
def test_matching_rejects_invalid_or_masked_delimiters(text: str) -> None:
    assert find_matching(text, text.index("(")) is None
    if ")" in text:
        assert find_matching_open(text, text.index(")")) is None


def test_argument_comments_do_not_affect_depth_or_separators() -> None:
    assert split_top_level_args("a, # ), hidden comma,\n b, c") == [
        "a",
        "# ), hidden comma,\n b",
        "c",
    ]


def test_escaped_triple_quote_does_not_expose_literal_to_rewrites() -> None:
    literal = r'"""start\""" old_name, end"""'
    text = f"{literal}\nold_name = 1 # old_name\n"
    result, edits = replace_identifier(text, "old_name", "new_name")
    assert result == f"{literal}\nnew_name = 1 # old_name\n"
    assert len(edits) == 1
    assert not any(code_mask(text)[: len(literal)])


def test_matching_only_requires_the_requested_expression_to_be_complete() -> None:
    assert find_matching('(ok) + "unfinished', 0) == 3
    assert find_matching("x", -1) is None
    assert find_matching("x", 5) is None


def test_matching_recovers_balanced_pairs_around_mismatches_and_incomplete_input():
    source = "]([ok]}[(a)](\"unclosed"
    first = source.index("[")
    last = source.rindex("[")
    for start, end, opening, closing in (
        (first, first + 3, "[", "]"),
        (last, last + 4, "[", "]"),
        (last + 1, last + 3, "(", ")"),
    ):
        assert find_matching(source, start, opening, closing) == end
        assert find_matching_open(source, end, opening, closing) == start
    assert find_matching(source, 1) is None
    assert find_matching_open(source, 0, "[", "]") is None


def test_matching_does_not_reuse_positions_after_a_same_length_edit():
    before = "([x])"
    after = "([x)]"
    assert find_matching(before, 0) == 4
    assert find_matching(after, 0) is None
    assert find_matching_open(after, 3) is None
    assert find_matching_open(before, 4) == 0


def test_forward_and_reverse_lookups_share_an_index(monkeypatch):
    text = "call(one(), two[0])"
    scanned = []

    def count_mask(source):
        scanned.append(source)
        return code_mask(source)

    monkeypatch.setattr(source_tools, "code_mask", count_mask)
    for start, end, opening, closing in ((4, 18, "(", ")"), (8, 9, "(", ")"), (15, 17, "[", "]")):
        assert find_matching(text, start, opening, closing) == end
        assert find_matching_open(text, end, opening, closing) == start
    assert scanned == [text]
