import re


def normalize_isbn(value: str) -> str | None:
    value = re.sub(r"^(?:urn:)?isbn(?:-1[03])?:?\s*", "", value.strip(), flags=re.I)
    value = re.sub(r"[\s-]", "", value).upper()
    if re.fullmatch(r"[0-9]{9}[0-9X]", value):
        digits = [10 if char == "X" else int(char) for char in value]
        if sum((10 - index) * digit for index, digit in enumerate(digits)) % 11:
            return None
        value = "978" + value[:9]
        check = (
            10 - sum(int(char) * (1 if i % 2 == 0 else 3) for i, char in enumerate(value)) % 10
        ) % 10
        return value + str(check)
    if re.fullmatch(r"97[89][0-9]{10}", value):
        if sum(int(char) * (1 if i % 2 == 0 else 3) for i, char in enumerate(value)) % 10 == 0:
            return value
    return None


def choose_isbn(identifiers: list[str], selected: str | None = None) -> str:
    candidates = {normalized for value in identifiers if (normalized := normalize_isbn(value))}
    if selected is not None:
        normalized = normalize_isbn(selected)
        if normalized is None:
            raise ValueError("The supplied ISBN has an invalid format or checksum.")
        if normalized not in candidates:
            raise ValueError(
                "Choose an ISBN already present in this EPUB; Emend will not silently substitute another edition."
            )
        return normalized
    if len(candidates) != 1:
        raise ValueError(
            "Exact-edition research needs one valid ISBN in the EPUB. If several are present, select one with --isbn. Title-only matching is not yet supported."
        )
    return candidates.pop()
