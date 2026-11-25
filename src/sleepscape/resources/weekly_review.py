"""Packaged contract for the scheduled rolling sleep review."""

import hashlib
import json
import re
from collections import Counter
from datetime import date, timedelta
from functools import lru_cache
from importlib.resources import files

from jsonschema import Draft202012Validator, FormatChecker
from jsonschema.exceptions import ValidationError

from sleepscape.domain.morning_feeling import recording_assertions, stated_ratings
from sleepscape.domain.sleep_features import (
    canonical_json,
    opens_with_collection_status,
    report_text_is_safe,
)
from sleepscape.domain.weekly_review import METRIC_UNITS, PAIR_LABELS, build_evidence
from sleepscape.models.base import ChatMessage, ModelError, ModelRequest
from sleepscape.models.openai_compatible import MAX_MESSAGE_BYTES, strict_json

MAX_EVIDENCE_BYTES = 24 * 1024
_COMPARATIVE = re.compile(r"相比|相较|高于|低于|长于|短于|更高|更低|更长|更短|组间|差值")
_UNSUPPORTED = re.compile(r"长期趋势|持续恶化|导致|造成了|吵醒了|引发了")
_ENVIRONMENT_TERMS = {
    "温度": "temperature_c",
    "湿度": "relative_humidity_pct",
    "照度": "illuminance_lux",
    "光照": "illuminance_lux",
    "亮度": "illuminance_lux",
    "声音": "sound_relative",
    "音量": "sound_relative",
    "噪声": "sound_relative",
}


@lru_cache(maxsize=1)
def _schemas():
    root = files("sleepscape.resources")
    schema = json.loads(root.joinpath("sleep-review.schema.json").read_text())
    prompt = root.joinpath("sleep-review-system.txt").read_text()
    for contract in schema.values():
        Draft202012Validator.check_schema(contract)
    return schema, prompt


@lru_cache(maxsize=2)
def _validator(kind):
    return Draft202012Validator(_schemas()[0][kind], format_checker=FormatChecker())


def resource_digest():
    schema, prompt = _schemas()
    return hashlib.sha256(canonical_json([schema, prompt]).encode()).hexdigest()


def validate_evidence(value, *, for_generation=False):
    try:
        _validator("input").validate(value)
        nights = value["nights"]
        span = value["window"]
        start = date.fromisoformat(span["start_date"])
        days = [(start + timedelta(days=i)).isoformat() for i in range(len(nights))]
        identities = {
            item["id"] for key in ("nights", "aggregates", "comparisons") for item in value[key]
        }
        expected_ids = sum(len(value[key]) for key in ("nights", "aggregates", "comparisons"))
        feeling = value["quality"]["feeling_state"]
        counts = value["quality"]["feeling_counts"]
        measured = Counter(item["feeling"] or "unfilled" for item in nights)
        if (
            span["visible_n"] != len(nights)
            or span["eligible_n"] != len(nights)
            or [item["night_date"] for item in nights] != days
            or span["end_date"] != days[-1]
            or len(identities) != expected_ids
            or (for_generation and "pair_checks" not in value)
            or ("pair_checks" in value and value != build_evidence(nights, feeling))
            or not set(value["examples"]) <= {item["id"] for item in nights}
            or any(set(night["metrics"]) - METRIC_UNITS.keys() for night in nights)
            or any(
                row["unit"] != METRIC_UNITS[row["metric"]]
                for key in ("aggregates", "comparisons")
                for row in value[key]
            )
            or (feeling == "available") != (counts is not None)
            or (counts is not None and any(n != measured[key] for key, n in counts.items()))
            or (counts is None and any(item["feeling"] is not None for item in nights))
            or any(row["valid_n"] > len(nights) for row in value["aggregates"])
            or any(row["poor_n"] + row["good_n"] > len(nights) for row in value["comparisons"])
        ):
            raise ValueError
        return value
    except (ValidationError, ValueError, TypeError, KeyError, OverflowError):
        raise ModelError("model_operation_failed") from None


def request_for(value, *, correction=False):
    encoded = canonical_json(validate_evidence(value, for_generation=True))
    schema, prompt = _schemas()
    output = schema["output"]
    instructions = prompt + "\n\n" + canonical_json(output)
    if correction:
        instructions += (
            "\n上次输出未通过校验。只保留主要发现和一条有直接证据的观察，hypotheses 填 []。"
            "actions 有依据时保留一条，写清试什么或记录什么、如何对照，否则填 []。"
            "每条仅引用一至三个直接有效的 ID，摘要须有睡眠证据。"
            "跨夜睡感关系只照录对应 P 卡并单引该卡。"
            "输出规定的四字段JSON对象，正文不要写数字或推断因果。"
        )
    if len(encoded.encode()) > MAX_EVIDENCE_BYTES:
        raise ValueError("review_input_too_large")
    if len(instructions.encode()) + len(encoded.encode()) > MAX_MESSAGE_BYTES:
        raise ValueError("review_input_too_large")
    return ModelRequest(
        [ChatMessage("system", instructions), ChatMessage("user", encoded)],
        output,
        schema_name="sleepscape_sleep_review",
    )


def validate_output(value, evidence):
    try:
        _validator("output").validate(value)
    except ValidationError:
        raise ModelError("output_schema_mismatch") from None
    references = {
        item["id"]: item
        for key in ("aggregates", "comparisons", "nights")
        for item in evidence[key]
    }
    references.update({row["id"]: row for row in evidence.get("pair_checks", [])})
    night_ids = {row["id"] for row in evidence["nights"]}
    statements = [value["summary"], *value["observations"], *value["hypotheses"], *value["actions"]]
    for item in statements:
        ids = set(item["evidence_ids"])
        if (
            not ids <= references.keys()
            or not (ids & night_ids) <= set(evidence["examples"])
            or any(
                not item[key].strip()
                or not report_text_is_safe(item[key])
                or _UNSUPPORTED.search(item[key])
                for key in ("text", "alternative", "observe")
                if key in item
            )
        ):
            raise ModelError("output_business_mismatch")
        if _COMPARATIVE.search(item["text"]) and not _comparable(
            [references[ref] for ref in ids], item["text"]
        ):
            raise ModelError("output_business_mismatch")
    # A comparative or explanatory assertion requires precomputed comparisons or
    # direct paired evidence, even when its other references exist.
    for item in value["hypotheses"]:
        ids = set(item["evidence_ids"])
        if (
            not any(ref.startswith("C") for ref in ids) and len(ids & night_ids) < 2
        ) or not _comparable([references[ref] for ref in ids], item["text"]):
            raise ModelError("output_business_mismatch")
    return value


def _comparable(rows, text):
    selected = {metric for term, metric in _ENVIRONMENT_TERMS.items() if term in text}
    if selected:
        return all(_metric_comparable(rows, metric) for metric in selected)
    return _metric_comparable(rows)


def _metric_comparable(rows, selected=None):
    if any(row["id"].startswith("P") and selected in (None, row["metric"]) for row in rows):
        return True
    if any(row["id"].startswith("C") and selected in (None, row["metric"]) for row in rows):
        return True
    nights = [row for row in rows if row["id"].startswith("N")]
    for aggregate in (row for row in rows if row["id"].startswith("A")):
        if selected in (None, aggregate["metric"]) and any(
            aggregate["metric"] in night["metrics"]
            and aggregate["environment_group"] in (None, night["environment_group"])
            and aggregate["valid_n"] > 1
            for night in nights
        ):
            return True
    if selected is None:
        return len(nights) > 1
    groups = Counter(night["environment_group"] for night in nights if selected in night["metrics"])
    return max(groups.values(), default=0) >= 2


_PAIR_SUBJECTS = {
    "sleep_seconds": r"时长|睡得[长短久]|睡得更多|睡了更久|多睡|少睡",
    "sleep_start_offset_seconds": r"入睡|睡眠开始|睡得[早晚]|就寝|上床|早睡|晚睡",
    "sleep_end_offset_seconds": r"起床|醒来时间|醒得|早醒|晚醒|早起|晚起|醒来.{0,5}[早晚]",
    "average_heart_rate": r"心率(?!变异)",
    "night_average_hrv": r"HRV|心率变异",
    "training_load": r"负荷",
    "temperature_c": r"温度|室温|温湿|冷热",
    "relative_humidity_pct": r"温湿|湿度",
    "illuminance_lux": r"光照|亮度|照度",
    "sound_relative": r"声音|噪声|音量",
}
_SPREAD_CLAIM = re.compile(r"平稳|稳定|起伏|波动(?:很|较|不)?[大小]")
_SPREAD_LIMIT = re.compile(
    r"(?:无法|难以|不能|不足以)(?:由此)?(?:判断|说明|确定|证明)[^，。；！？]{0,10}"
    r"(?:平稳|稳定|起伏|波动(?:很|较|不)?[大小])"
)
_NIGHT_SCOPE = re.compile(
    r"其[他余它](?:的)?(?:几)?[晚夜]|别的[晚夜]|另外的?[晚夜]|(?:这|那)?[两几多]晚|各晚|跨夜|整段|整周"
)
_NIGHT_CONTRAST = re.compile(
    r"不同|不一样|相同|一样|一致|相似|接近|有别|对照|比较|相差|差异|[更较][高低早晚长短]"
)
_CONTRAST_LIMIT = re.compile(
    r"(?:无法|难以|不能|不足以)(?:由此)?(?:判断|说明|确定|证明)[^，。；！？]{0,16}?"
    r"(?:不同|不一样|相同|一样|一致|有别|有差异)"
)
_DIFFERENT = re.compile(r"不同|不一样|不相同|不一致|并非相同|并非一致|有别")
_MEDIAN_RELATIONS = {
    "高于": 1,
    "长于": 1,
    "晚于": 1,
    "低于": -1,
    "短于": -1,
    "早于": -1,
    "等于": 0,
    "相同": 0,
    "一致": 0,
    "一样": 0,
}
_MEDIAN_RELATION = re.compile(
    r"(?P<negated>并非|不是|不)?(?P<operator>不同|有别|" + "|".join(_MEDIAN_RELATIONS) + ")"
)
_FEELING = re.compile(
    r"睡感|自评|自觉|感觉|感受|体验|精神更好|睡得[好差]|更舒服|更踏实|更清爽|醒后状态"
)


def _check_night_median(claim, subjects, rows):
    """Check an explicit comparison of one night against its window median."""
    if (
        "中位数" not in claim
        or not subjects
        or not re.search(r"这晚|该晚|当晚|那晚|这一晚", claim)
        or re.search(r"其他|其余|两晚|几晚|每晚|各晚", claim)
    ):
        return False
    relations = list(_MEDIAN_RELATION.finditer(claim))
    if not relations:
        return False
    if len(relations) != 1:
        raise ModelError("output_business_mismatch")
    relation = relations[0]
    for metric in subjects:
        nights = [row for row in rows if metric in row.get("metrics", {})]
        medians = [row for row in rows if row.get("metric") == metric and "median" in row]
        if len(nights) != 1 or len(medians) != 1:
            raise ModelError("output_business_mismatch")
        night, median = nights[0], medians[0]
        if median["valid_n"] < 2 or median["environment_group"] not in (
            None,
            night["environment_group"],
        ):
            raise ModelError("output_business_mismatch")
        value, typical = night["metrics"][metric], median["median"]
        sign = (value > typical) - (value < typical)
        if claim.index("中位数") < relation.start():
            sign = -sign
        operator = relation["operator"]
        matches = sign != 0 if operator in ("不同", "有别") else sign == _MEDIAN_RELATIONS[operator]
        if matches == bool(relation["negated"]):
            raise ModelError("output_business_mismatch")
    return True


def _check_measurement_claims(prose, rows):
    cited_metrics = {metric for row in rows for metric in row.get("metrics", {})}
    cited_metrics.update(row["metric"] for row in rows if "metric" in row)
    subjects = set()
    for clause in re.split(r"[。，；！？,;\n]", prose):
        named = {metric for metric, term in _PAIR_SUBJECTS.items() if re.search(term, clause, re.I)}
        if not named <= cited_metrics:
            raise ModelError("output_business_mismatch")
        subjects = named or subjects
        claim = _CONTRAST_LIMIT.sub("", _SPREAD_LIMIT.sub("", clause))
        if _check_night_median(claim, subjects, rows) and not _SPREAD_CLAIM.search(claim):
            continue
        spread = _SPREAD_CLAIM.search(claim)
        if not spread and not (_NIGHT_SCOPE.search(claim) and _NIGHT_CONTRAST.search(claim)):
            continue
        if spread and not subjects:
            raise ModelError("output_business_mismatch")
        for metric in subjects:
            nights = [row for row in rows if metric in row.get("metrics", {})]
            if len(nights) < 2 or (
                metric in _ENVIRONMENT_TERMS.values()
                and len({row["environment_group"] for row in nights}) != 1
            ):
                raise ModelError("output_business_mismatch")
            same = len({night["metrics"][metric] for night in nights}) == 1
            if same and _DIFFERENT.search(claim):
                raise ModelError("output_business_mismatch")
            if not same and re.search(r"相同|一样|一致", _DIFFERENT.sub("", claim)):
                raise ModelError("output_business_mismatch")
            if spread and (
                not re.search(r"两晚|所引夜晚", clause)
                or not re.search(r"稳定|平稳", claim)
                or re.search(r"整夜|全程|一直", claim)
                or not same
            ):
                raise ModelError("output_business_mismatch")


def _validate_pair_statements(report, evidence):
    proofs = {row["id"]: row for row in evidence.get("pair_checks", [])}
    groups = {}
    for row in evidence["comparisons"]:
        label = PAIR_LABELS[row["metric"]][0]
        direction = "高于" if row["difference"] > 0 else "低于" if row["difference"] < 0 else "等于"
        groups[row["id"]] = f"较差睡感组的{label}中位数{direction}较好睡感组。"
    nights = {row["id"]: row for row in evidence["nights"]}
    measurements = {
        r["id"]: r for key in ("nights", "aggregates", "comparisons") for r in evidence[key]
    }
    window_ratings = {night["feeling"] for night in nights.values()}
    mixed = {p["metric"] for p in proofs.values() if p["counterexample"]}
    for group in ("summary", "observations", "hypotheses", "actions"):
        rows = [report[group]] if group == "summary" else report[group]
        for row in rows:
            ids = row["evidence_ids"]
            cited = [proofs[k] for k in ids if k in proofs]
            if cited:
                # A proof card supplies its own explanation and both sets of
                # nights. Copy the checked claim; do not let an appended clause
                # turn a counterexample into an instruction to change bedtime.
                if (
                    len(ids) != 1
                    or group not in ("summary", "observations")
                    or row["text"] != cited[0]["text"]
                ):
                    raise ModelError("output_business_mismatch")
                continue
            prose = "。".join(row[k] for k in ("text", "alternative", "observe") if k in row)
            if group == "actions":
                prose = recording_assertions(prose)
            _check_measurement_claims(prose, [measurements[k] for k in ids])
            topics = {
                metric
                for metric, pattern in _PAIR_SUBJECTS.items()
                if re.search(pattern, prose, re.I)
            }
            if (
                group == "actions"
                and topics & mixed
                and re.search(
                    r"提前|推迟|提早|延后|调高|调低|早起|晚起|早睡|晚睡|减少|增加",
                    recording_assertions(row["text"]),
                )
            ):
                raise ModelError("output_business_mismatch")
            if all(k.startswith("A") for k in ids) and _SPREAD_CLAIM.search(
                _SPREAD_LIMIT.sub("", prose)
            ):
                raise ModelError("output_business_mismatch")
            ratings = stated_ratings(prose)
            if ratings:
                selected = [nights[k]["feeling"] for k in ids if k in nights]
                if re.search(r"这晚|该晚|当晚|那晚|这一晚", prose) and len(selected) != 1:
                    raise ModelError("output_business_mismatch")
                if None in ratings or not ratings <= set(selected):
                    raise ModelError("output_business_mismatch")
                if re.search(
                    r"每晚|整周都|各晚都|全部|所有夜晚|全都|都(?:自评|是|为|较)", prose
                ) and (len(ratings) != 1 or window_ratings != ratings):
                    raise ModelError("output_business_mismatch")
            if not topics or not _FEELING.search(prose):
                continue
            if (
                group == "actions"
                and re.match(r"(?:下次|次日|晨起)?(?:请)?记录", row["text"])
                and not re.search(
                    r"改善|会更|能更|往往|总是|越.+越|更[好差早晚高低]|说明|提示", prose
                )
                and all(
                    re.match(r"(?:下次|次日|晨起)?(?:请)?(?:记录|观察|留意)", clause)
                    for clause in re.split(r"[。，；！？,;]", prose)
                    if _FEELING.search(clause)
                    or any(re.search(pattern, clause, re.I) for pattern in _PAIR_SUBJECTS.values())
                )
            ):
                continue
            if (
                group in ("summary", "observations")
                and len(ids) == 2
                and all(k in nights for k in ids)
            ):
                a, b = (nights[k] for k in ids)
                if (
                    prose == "这两晚的睡眠时长不同，晨起睡感相同。"
                    and a["feeling"] is not None
                    and a["feeling"] == b["feeling"]
                    and a["metrics"]["sleep_seconds"] != b["metrics"]["sleep_seconds"]
                ):
                    continue
            # A scoped group median remains a valid observation even when
            # individual pairs differ. It cannot be used as a personal rule.
            if (
                group in ("summary", "observations")
                and len(ids) == 1
                and groups.get(ids[0]) == prose
            ):
                continue
            # One night may be described as one night without asserting a
            # relationship. Comparisons of experiences use the checked card.
            if (
                group in ("summary", "observations")
                and len(ids) == 1
                and ids[0].startswith("N")
                and re.search(r"这晚|该晚|当晚", prose)
                and not re.search(
                    r"相比|更[早晚高低好差]|比其他|往往|通常|可能|解释|总是|越|倾向|对照|关联|有关|影响",
                    prose,
                )
            ):
                continue
            raise ModelError("output_business_mismatch")


def parse_output(raw, evidence):
    report = validate_output(strict_json(raw), validate_evidence(evidence, for_generation=True))
    _validate_pair_statements(report, evidence)
    summary = report["summary"]
    # Each comparison contrasts nights with poor vs good self-reported sleep.
    sleep_ids = set(evidence["examples"]) | {row["id"] for row in evidence["comparisons"]}
    sleep_ids.update(row["id"] for row in evidence.get("pair_checks", []))
    sleep_ids.update(
        row["id"] for row in evidence["aggregates"] if row["metric"].startswith("sleep_")
    )
    if not sleep_ids.intersection(summary["evidence_ids"]) or opens_with_collection_status(
        summary["text"]
    ):
        raise ModelError("output_business_mismatch")
    return report
