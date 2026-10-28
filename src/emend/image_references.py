"""Bounded reference parsing shared by image conversion and inspection."""

import re

import tinycss2


def srcset_urls(value):
    """Conservative local srcset grammar; ambiguous/data URLs need manual review."""
    if len(value) > 65536 or len(value.split(",")) > 200:
        raise ValueError("Image source set exceeds the parsing limit.")
    result, offset, descriptors = [], 0, set()
    for part in value.split(","):
        match = re.fullmatch(r"\s*(\S+?)(?:\s+([1-9]\d*w|(?:\d+(?:\.\d+)?|\.\d+)x))?\s*", part)
        if not match or match[1].lower().startswith("data:"):
            raise ValueError("Image source set has an unsupported or ambiguous URL.")
        descriptor = match[2] or "1x"
        if descriptor.endswith("x") and float(descriptor[:-1]) <= 0:
            raise ValueError("Image source set requires a positive density.")
        if descriptor in descriptors:
            raise ValueError("Image source set repeats a descriptor.")
        descriptors.add(descriptor)
        result.append((offset + match.start(1), offset + match.end(1), match[1]))
        offset += len(part) + 1
    if any(d.endswith("w") for d in descriptors) and any(d.endswith("x") for d in descriptors):
        raise ValueError("Image source set mixes width and density descriptors.")
    return result


def css_nodes(text):
    if len(text.encode("utf-8")) > 2 * 1024**2:
        raise ValueError("Stylesheet exceeds the image-reference limit.")
    nodes = tinycss2.parse_component_value_list(text)

    def walk(tokens, depth=0):
        if depth > 64:
            raise ValueError("CSS nesting exceeds the image-reference limit.")
        for token in tokens:
            if token.type == "error":
                raise ValueError("CSS cannot be parsed reliably for image conversion.")
            if token.type == "url":
                yield token, token.value
            elif token.type == "function" and token.lower_name == "url":
                arguments = [a for a in token.arguments if a.type not in {"comment", "whitespace"}]
                if len(arguments) != 1 or arguments[0].type != "string":
                    raise ValueError("CSS url() cannot be rewritten reliably.")
                yield token, arguments[0].value
            else:
                children = getattr(token, "content", getattr(token, "arguments", None))
                if children is not None:
                    yield from walk(children, depth + 1)

    return nodes, list(walk(nodes))


def css_url_end(text, start):
    """Find one parsed url() token's exact source span, respecting quotes/escapes."""
    opening = text.find("(", start)
    if opening < 0 or text[start:opening].lower() != "url":
        raise ValueError("CSS URL location no longer matches its parsed token.")
    quote, escaped = None, False
    for index in range(opening + 1, len(text)):
        char = text[index]
        if escaped:
            escaped = False
        elif char == "\\":
            escaped = True
        elif quote:
            if char == quote:
                quote = None
        elif char in "\"'":
            quote = char
        elif char == ")":
            return index + 1
        elif char == "(":
            raise ValueError("Nested unquoted CSS URL cannot be rewritten reliably.")
    raise ValueError("Unclosed CSS URL.")


def css_rewrite(text, resolve):
    nodes, urls = css_nodes(text)

    def check(tokens):
        for token in tokens:
            if token.type == "url" or (token.type == "function" and token.lower_name == "url"):
                continue
            if token.type in {"string", "ident"} and resolve(token.value) != token.value:
                raise ValueError(
                    "CSS references an image outside url(); the whole conversion is refused."
                )
            children = getattr(token, "content", getattr(token, "arguments", None))
            if children is not None:
                check(children)

    check(nodes)
    starts = [0] + [i + 1 for i, char in enumerate(text) if char == "\n"]
    edits = []
    for token, value in urls:
        after = resolve(value)
        if after == value:
            continue
        start = starts[token.source_line - 1] + token.source_column - 1
        end = css_url_end(text, start)
        escaped = after.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\a ")
        edits.append((start, end, text[start:end], f'url("{escaped}")'))
    result = text
    for start, end, _, after in reversed(edits):
        result = result[:start] + after + result[end:]
    return result, edits


def css_preserved(before, after, resolve):
    """Reparse actual bytes; only URL-token values and their spelling may differ."""
    old, old_urls = css_nodes(before)
    new, new_urls = css_nodes(after)
    if len(old_urls) != len(new_urls):
        return False
    changed = set()
    for index, ((_, a), (_, b)) in enumerate(zip(old_urls, new_urls, strict=True)):
        if resolve(a) != b:
            return False
        if resolve(a) != a:
            changed.add(index)

    # Exact text outside URL spans must also agree (including comments/whitespace).
    def masked(text, urls):
        starts = [0] + [i + 1 for i, c in enumerate(text) if c == "\n"]
        for index in reversed(range(len(urls))):
            if index not in changed:
                continue
            token, _ = urls[index]
            start = starts[token.source_line - 1] + token.source_column - 1
            end = css_url_end(text, start)
            text = text[:start] + "url(EMEND_REFERENCE)" + text[end:]
        return text

    return masked(before, old_urls) == masked(after, new_urls)
