"""Personal morning ratings, independent of device measurements."""

import re
from enum import StrEnum


class Feeling(StrEnum):
    POOR = "poor"
    FAIR = "fair"
    GOOD = "good"


_RATING_VALUES = {"较好": Feeling.GOOD, "一般": Feeling.FAIR, "较差": Feeling.POOR}
_RATING_CLAIM = re.compile(
    r"(?:自评|睡感|自觉|晨起感受|自我感觉|自我评价)(?P<modifier>[^。，；！？]{0,5}?)(?P<label>较好|一般|较差)"
)


def stated_ratings(text):
    """Positive recorded labels; a negated/uncertain label cannot attest a rating."""
    ratings = set()
    for match in _RATING_CLAIM.finditer(text):
        modifier = match["modifier"]
        if re.search(r"不|非|未|无|是否|可能|也许", modifier) or re.search(
            r"(并非|不是|不能说)$", text[: match.start()]
        ):
            ratings.add(None)
        else:
            ratings.add(_RATING_VALUES[match["label"]])
    return ratings


def recording_assertions(text):
    """Remove an explicitly prospective recording question, retaining neighbouring claims."""
    return re.sub(
        r"(?:^|(?<=[。，；！？,;]))(?:下次|以后|后续|次日|请)?(?:可以|可)?(?:记录|观察|留意)"
        r"[^。，；！？,;]{0,12}是否[^。，；！？,;]*",
        "",
        text,
    )
