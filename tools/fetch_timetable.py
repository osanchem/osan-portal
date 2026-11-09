#!/usr/bin/env python3
"""컴시간 주간 시간표를 받아 data/timetable.json으로 저장합니다.

컴시간이 조회 규약을 자주 바꾸기 때문에, 조회는 외부에서 유지보수되는
pycomcigan 라이브러리에 맡기고 이 파일은 포털이 읽을 모양으로 바꾸는 일만 합니다.
라이브러리가 막히면 `pip install -U` 한 번으로 복구할 수 있습니다.

출력 형식
---------
{
  "ok": true,
  "school": "오산고등학교",
  "generatedAt": "2026-11-10T07:30:12+09:00",
  "updatedAt": "07:30",
  "days": {
    "2026-11-10": {
      "label": "11월 10일(화)",
      "swapLabel": "금요일",          # 요일 교환일이 아니면 null
      "changes": [
        {"period": 3, "grade": 1, "cls": 4,
         "before": {"subject": "화학", "teacher": "정지O"},
         "after":  {"subject": "지학", "teacher": "김덕O"}}
      ]
    }
  }
}
"""

from __future__ import annotations

import json
import os
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from pycomcigan import TimeTable, get_school_code

KST = timezone(timedelta(hours=9))
WEEKDAY_NAMES = ["월요일", "화요일", "수요일", "목요일", "금요일", "토요일", "일요일"]

SCHOOL_NAME = os.environ.get("COMCIGAN_SCHOOL", "오산고")
# 동명 학교가 여러 곳이면 아래 두 값으로 하나를 지정합니다(0이면 이름으로만 찾습니다).
SCHOOL_CODE = int(os.environ.get("COMCIGAN_SCHOOL_CODE", "0"))
LOCAL_CODE = int(os.environ.get("COMCIGAN_LOCAL_CODE", "0"))

# 요일 교환일로 볼 최소 일치율입니다. 이 값보다 많이 겹치면 그 요일 시간표로 운영한다고 봅니다.
SWAP_THRESHOLD = 0.80

OUT_PATH = Path(__file__).resolve().parent.parent / "data" / "timetable.json"


def mask_name(name: str) -> str:
    """포털은 로그인 없이 열리므로 교사 이름의 마지막 글자를 가립니다."""
    chars = list(str(name or "").strip())
    if len(chars) < 2:
        return "".join(chars)
    chars[-1] = "O"
    return "".join(chars)


def lecture(obj) -> dict | None:
    """TimeTableData 또는 Lecture 객체를 {subject, teacher} 형태로 바꿉니다."""
    if obj is None:
        return None
    subject = str(getattr(obj, "subject", "") or "").strip()
    teacher = str(getattr(obj, "teacher", "") or "").strip()
    if not subject and not teacher:
        return None
    return {"subject": subject, "teacher": mask_name(teacher)}


def monday_of(day: date) -> date:
    return day - timedelta(days=day.weekday())


def grade_class_pairs(timetable) -> list[tuple[int, int]]:
    """시간표에 들어 있는 (학년, 반) 조합을 모두 찾습니다."""
    pairs: list[tuple[int, int]] = []
    for grade in range(1, len(timetable)):
        classes = timetable[grade]
        for cls in range(1, len(classes)):
            if classes[cls]:
                pairs.append((grade, cls))
    return pairs


def slot_maps(timetable, pairs, day_index: int) -> tuple[dict, dict]:
    """한 요일의 '원래 시간표'와 '실제 운영 시간표'를 각각 사전으로 만듭니다."""
    base: dict[tuple[int, int, int], str] = {}
    actual: dict[tuple[int, int, int], str] = {}
    for grade, cls in pairs:
        try:
            periods = timetable[grade][cls][day_index]
        except (IndexError, TypeError):
            continue
        for item in periods or []:
            period = int(getattr(item, "period", 0) or 0)
            if period <= 0:
                continue
            key = (grade, cls, period)
            subject = str(getattr(item, "subject", "") or "").strip()
            teacher = str(getattr(item, "teacher", "") or "").strip()
            actual[key] = f"{subject}|{teacher}"
            original = getattr(item, "original", None)
            if getattr(item, "replaced", False) and original is not None:
                o_subject = str(getattr(original, "subject", "") or "").strip()
                o_teacher = str(getattr(original, "teacher", "") or "").strip()
                base[key] = f"{o_subject}|{o_teacher}"
            else:
                base[key] = actual[key]
    return base, actual


def similarity(a: dict, b: dict) -> float:
    keys = set(a) & set(b)
    if not keys:
        return 0.0
    same = sum(1 for k in keys if a[k] == b[k])
    return same / len(keys)


def build_day(timetable, pairs, day_index: int, bases: dict[int, dict]) -> dict:
    """하루치 결과(요일 교환 여부와 변동 목록)를 만듭니다."""
    base, actual = slot_maps(timetable, pairs, day_index)

    # 오늘 실제 운영되는 시간표가 다른 요일의 원래 시간표와 더 많이 겹치면 요일 교환일로 봅니다.
    swap_index = None
    best = similarity(actual, base)
    for other_index, other_base in bases.items():
        if other_index == day_index:
            continue
        score = similarity(actual, other_base)
        if score >= SWAP_THRESHOLD and score > best:
            best = score
            swap_index = other_index

    reference = bases[swap_index] if swap_index is not None else base

    changes = []
    for grade, cls in pairs:
        try:
            periods = timetable[grade][cls][day_index]
        except (IndexError, TypeError):
            continue
        for item in periods or []:
            period = int(getattr(item, "period", 0) or 0)
            if period <= 0:
                continue
            key = (grade, cls, period)
            after_sig = actual.get(key, "")
            before_sig = reference.get(key)
            if before_sig is not None and before_sig == after_sig:
                continue  # 기준 시간표와 같은 수업은 변동이 아닙니다.
            if swap_index is None:
                before = lecture(getattr(item, "original", None))
                if not getattr(item, "replaced", False):
                    continue
            else:
                subject, _, teacher = (before_sig or "|").partition("|")
                before = (
                    {"subject": subject, "teacher": mask_name(teacher)}
                    if (subject or teacher)
                    else None
                )
            changes.append(
                {
                    "period": period,
                    "grade": grade,
                    "cls": cls,
                    "before": before,
                    "after": lecture(item),
                }
            )

    changes.sort(key=lambda c: (c["period"], c["grade"], c["cls"]))
    return {
        "swapLabel": WEEKDAY_NAMES[swap_index - 1] if swap_index is not None else None,
        "changes": changes,
    }


def collect_week(school_args: dict, week_num: int, monday: date) -> dict[str, dict]:
    timetable_obj = TimeTable(week_num=week_num, **school_args)
    table = timetable_obj.timetable
    pairs = grade_class_pairs(table)
    if not pairs:
        raise RuntimeError("시간표에 학급 정보가 없습니다.")

    bases = {d: slot_maps(table, pairs, d)[0] for d in range(1, 6)}

    days: dict[str, dict] = {}
    for day_index in range(1, 6):
        day = monday + timedelta(days=day_index - 1)
        result = build_day(table, pairs, day_index, bases)
        result["label"] = f"{day.month}월 {day.day}일({WEEKDAY_NAMES[day_index - 1][0]})"
        days[day.isoformat()] = result
    return days


def resolve_school() -> dict:
    """학교 이름이 여러 곳과 겹칠 때를 대비해 코드까지 확정합니다."""
    if SCHOOL_CODE and LOCAL_CODE:
        return {
            "school_name": SCHOOL_NAME,
            "school_code": SCHOOL_CODE,
            "local_code": LOCAL_CODE,
        }
    found = get_school_code(SCHOOL_NAME)
    print(f"학교 검색 결과: {found}", file=sys.stderr)
    if not found:
        raise RuntimeError(f"‘{SCHOOL_NAME}’으로 검색된 학교가 없습니다.")
    if len(found) > 1:
        exact = [row for row in found if str(row[0]).strip() == SCHOOL_NAME]
        if len(exact) != 1:
            raise RuntimeError(
                f"‘{SCHOOL_NAME}’으로 {len(found)}곳이 검색되었습니다. "
                "COMCIGAN_SCHOOL_CODE와 COMCIGAN_LOCAL_CODE를 지정해 주세요."
            )
        found = exact
    name, _region, school_code, local_code = found[0]
    return {
        "school_name": name,
        "school_code": int(school_code),
        "local_code": int(local_code),
    }


def main() -> int:
    now = datetime.now(KST)
    school_args = resolve_school()

    this_monday = monday_of(now.date())
    days = collect_week(school_args, 0, this_monday)
    try:
        days.update(collect_week(school_args, 1, this_monday + timedelta(days=7)))
    except Exception as err:  # 다음 주 시간표는 아직 없을 수 있습니다.
        print(f"다음 주 시간표는 건너뜁니다: {err}", file=sys.stderr)

    payload = {
        "ok": True,
        "school": school_args["school_name"],
        "generatedAt": now.isoformat(timespec="seconds"),
        "updatedAt": now.strftime("%H:%M"),
        "days": dict(sorted(days.items())),
    }

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(
        json.dumps(payload, ensure_ascii=False, indent=1) + "\n", encoding="utf-8"
    )
    total = sum(len(d["changes"]) for d in payload["days"].values())
    print(f"{OUT_PATH} 저장 완료 · {len(payload['days'])}일치 · 변동 {total}건")
    return 0


if __name__ == "__main__":
    sys.exit(main())
