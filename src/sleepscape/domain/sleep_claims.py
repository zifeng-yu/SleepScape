"""Direct evidence and signed personal comparisons for new nightly prose."""

import re

from sleepscape.domain.morning_feeling import recording_assertions, stated_ratings

# Wire text for comparisons is derived solely from signed, validated delta facts.
_COMPARISONS = {
    "comparison.sleep_total_minutes_delta": ("sleep.total_minutes", "睡眠时长", "长于", "短于"),
    "comparison.average_heart_rate_delta": (
        "sleep.average_heart_rate",
        "夜间平均心率",
        "高于",
        "低于",
    ),
    "comparison.night_average_hrv_delta": ("sleep.night_average_hrv", "夜间HRV", "高于", "低于"),
    "comparison.training_load_percent": ("previous.training_load", "前日负荷", "高于", "低于"),
}
_SUBJECTS = {
    "sleep.total_minutes": r"总睡眠|睡眠时长|总时长|睡了|睡得[长短久]",
    "sleep.average_heart_rate": r"心率(?!变异)|心跳",
    "sleep.night_average_hrv": r"HRV|心率变异",
    "previous.training_load": r"负荷",
    "previous.average_stress": r"压力",
    "previous.steps": r"步数",
    "sleep.score": r"睡眠评?分|评分",
    "sleep.awake_minutes": r"累计清醒|清醒时长",
    "sleep.deep_minutes": r"深睡",
    "sleep.light_minutes": r"浅睡",
    "sleep.rem_minutes": r"快速眼动|REM",
}
_ENVIRONMENT = {
    "temperature": (r"温度|室温|温湿", "temperature_c"),
    "humidity": (r"湿度|温湿", "humidity_pct"),
    "illuminance": (r"照度|光照|光线", "illuminance_lux"),
    "sound": (r"声音|噪音|声响|噪声", "sound_dbfs"),
}
_DIRECTION = re.compile(
    r"[高低长短]于|(?:偏|略|较|稍|更)[高低长短]|偏离|接近(?:个人|平时|平常|均值|基线)|"
    r"(?:比(?!较)|相比|相较).{0,10}(?:平时|平常|基线|均值)|升高|降低|上升|下降|延长|缩短|"
    r"接近(?:平常|均值)|(?:平常|平时|均值|基线)(?:附近|相近)|[高低]过|不高|不低|持平|等于个人|(?:基线|均值|平常水平|平时水平)(?:之上|之下|以上|以下)|走[高低]|回落|抬升|同向|反向|差值.{0,4}(?:一致|相同)|方向.{0,4}(?:一致|相反|相同|不同)|一升一降"
)
_VARIABILITY = re.compile(r"稳定|平稳|波动|起伏|变化不大|保持不变")


def comparison_phrase(metric, delta):
    if metric not in _COMPARISONS or delta is None:
        return None
    _, name, up, down = _COMPARISONS[metric]
    return name + (up if delta > 0 else down if delta < 0 else "等于") + "个人均值"


_CLAUSE = re.compile(r"[。；，、！？,;！\n]")
_UNCERTAIN_VARIATION = re.compile(
    r"(无法判断|难以判断|不能判断|未能确认|无法确认|不能确定|未知)(是否|其|缺口中的)?"
    r"[^，。；]{0,8}(稳定|平稳|波动|起伏|变化)"
)


def _environment_supported(text, rows, bins, events):
    metrics = {r["metric"]: r["value"] for r in rows}
    for prefix, (terms, channel) in _ENVIRONMENT.items():
        if not re.search(terms, text):
            continue
        values = [r[channel] for r in bins if r.get(channel) is not None]
        if not (
            any(m.startswith(f"environment.{prefix}") for m in metrics)
            or values
            or (prefix == "sound" and events)
        ):
            return False
        # Carry an environment subject across a comma, but do not attribute a
        # different channel's variation to it. A full stop alone does not
        # change the subject of a following elliptical sentence.
        relevant = False
        for sentence in re.split(r"[。；！？;]", text):
            for clause in re.split(r"[，,]", sentence):
                if any(re.search(pattern, clause) for pattern, _ in _ENVIRONMENT.values()):
                    relevant = bool(re.search(terms, clause))
                elif any(re.search(pattern, clause, re.I) for pattern in _SUBJECTS.values()):
                    relevant = False
                asserted = _UNCERTAIN_VARIATION.sub("", clause)
                if not relevant or not _VARIABILITY.search(asserted):
                    continue
                low = metrics.get(f"environment.{prefix}_min")
                high = metrics.get(f"environment.{prefix}_max")
                has_range = low is not None and high is not None
                if not has_range and len(values) < 2:
                    return False
                if not has_range and re.search(r"整夜|全夜|一直", sentence):
                    return False
                if re.search(
                    r"稳定|平稳|保持不变|起伏不大|变化不大|波动(?:较小|很小|不大)", asserted
                ):
                    if has_range and low != high:
                        return False
                    if not has_range and (
                        len(set(values)) != 1 or not re.search(r"分段|所引时段", sentence)
                    ):
                        return False
    return True


def _comparison_supported(text, metrics, comparisons):
    rest = text
    for metric, (raw, phrase) in comparisons.items():
        if phrase not in text:
            continue
        if not {metric, raw} <= metrics:
            return False
        # No negation, adverb or second predicate may change a signed phrase.
        if any(
            not re.fullmatch(
                r"(?:但|而|且|同时)?(?:昨晚|本晚|当晚)?" + re.escape(phrase), part.strip()
            )
            for part in _CLAUSE.split(text)
            if phrase in part
        ):
            return False
        rest = rest.replace(phrase, "")
    if not _DIRECTION.search(rest):
        return True
    return not (
        any(re.search(_SUBJECTS[raw], text, re.I) for raw, *_ in _COMPARISONS.values())
        or "方向" in rest
        or "同向" in rest
        or "差值" in rest
        or "压力" in rest
    )


def nightly_claims_supported(candidate, source):
    facts = {row["id"]: row for row in source["facts"]}
    comparisons = {
        r["metric"]: (_COMPARISONS[r["metric"]][0], r["comparison_text"])
        for r in facts.values()
        if "comparison_text" in r
    }
    timeline = {r["id"]: r for r in source["timeline"]}
    events = {r["id"] for r in source["events"]}
    feeling = source.get("morning_feeling")
    observed_feeling = False
    for row in [
        candidate["summary"],
        *candidate["observations"],
        *candidate["hypotheses"],
        *candidate["actions"],
    ]:
        ids = set(row["evidence_ids"])
        rows = [facts[k] for k in ids & facts.keys()]
        metrics = {r["metric"] for r in rows}
        text = row["text"]
        bins = [timeline[k] for k in ids & timeline.keys()]
        for field in [text, *row.get("alternatives", []), row.get("observe", "")]:
            # A future recording question can name a factor with no measurement.
            # Keep every other clause, including assertions following the question.
            asserted = field
            if "observe" in row or field in row.get("alternatives", []):
                asserted = recording_assertions(field)
            labels = stated_ratings(asserted)
            if labels:
                if not feeling or feeling["id"] not in ids or labels != {feeling["value"]}:
                    return False
                if field == text and (
                    row is candidate["summary"] or row.get("category") == "sleep"
                ):
                    observed_feeling = True
            if any(
                re.search(pattern, asserted, re.I) and metric not in metrics
                for metric, pattern in _SUBJECTS.items()
            ):
                return False
            if not _comparison_supported(asserted, metrics, comparisons):
                return False
            if not _environment_supported(asserted, rows, bins, events & ids):
                if (
                    field == text
                    or _VARIABILITY.search(asserted)
                    or re.search(r"记录到|测得|整夜|当晚|昨晚", asserted)
                ):
                    return False
    return not feeling or observed_feeling
