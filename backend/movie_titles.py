"""Remove explicit ticket presentation suffixes without shortening movie identity."""
import re
import unicodedata


_MARKER = r"(?:[23]D|(?:中文|英文|国语|英语|普通话)(?:配音)?(?:版)?)"
_PRESENTATION = re.compile(_MARKER + r"(?:[\s/,，、+·-]*" + _MARKER + r")*", re.IGNORECASE)
_BRACKET = re.compile(r"\s*(?:\((?P<plain>[^()]*)\)|（(?P<wide>[^（）]*)）)\s*$")
_YEAR = re.compile(r"(?:18|19|20)\d{2}(?:\s+film)?")
_SEPARATOR = re.compile(r"\s*[-—–/|·]\s*|\s+")


def clean_movie_title(value):
    """Only whole trailing metadata blocks are removable; keep years and sequels."""
    original = title = value.strip()
    years = []
    changed = False
    while title:
        bracket = _BRACKET.search(title)
        if bracket:
            body = unicodedata.normalize("NFKC", bracket["plain"] if bracket["plain"] is not None else bracket["wide"]).strip()
            if _YEAR.fullmatch(body):
                years.append(title[bracket.start():].rstrip())
                title = title[:bracket.start()].rstrip()
                continue
            if _PRESENTATION.fullmatch(body):
                title = title[:bracket.start()].rstrip()
                changed = True
                continue
        for separator in reversed(list(_SEPARATOR.finditer(title))):
            body = unicodedata.normalize("NFKC", title[separator.end():]).strip()
            if _PRESENTATION.fullmatch(body):
                title = title[:separator.start()].rstrip()
                changed = True
                break
        else:
            break
    return title + "".join(reversed(years)) if changed else original
