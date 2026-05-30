from __future__ import annotations

import re

from ..analysis import unwrap_type


def is_unsigned_integer_type(type_name: str | None) -> bool:
    if type_name is None:
        return False
    type_name = unwrap_type(type_name)
    return bool(
        re.fullmatch(
            r"uint(?:8|16|24|32|40|48|56|64|72|80|88|96|104|112|120|128|136|144|152|160|168|176|184|192|200|208|216|224|232|240|248|256)?",
            type_name,
        )
    )


def is_signed_integer_type(type_name: str | None) -> bool:
    if type_name is None:
        return False
    type_name = unwrap_type(type_name)
    return bool(
        re.fullmatch(
            r"int(?:8|16|24|32|40|48|56|64|72|80|88|96|104|112|120|128|136|144|152|160|168|176|184|192|200|208|216|224|232|240|248|256)?",
            type_name,
        )
    )


def same_integer_signedness(left: str | None, right: str | None) -> bool:
    return (is_signed_integer_type(left) and is_signed_integer_type(right)) or (
        is_unsigned_integer_type(left) and is_unsigned_integer_type(right)
    )
