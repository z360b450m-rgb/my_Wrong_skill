"""Deterministic objective grading independent of document orchestration."""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Callable


DecimalParser = Callable[[Any], Decimal | None]
TextNormalizer = Callable[[Any], str]
SymbolicAdapter = Callable[[Any, Any], tuple[bool | None, float, str | None]]
MAX_SYMBOLIC_LENGTH = 512
MAX_SYMBOLIC_OPERATORS = 128
MAX_SYMBOLIC_DEPTH = 20
MAX_SYMBOLIC_INTEGER_DIGITS = 64
MAX_SYMBOLIC_EXPONENT = 1_000


def _safe_symbolic_text(value: Any) -> str | None:
    text = str(value)
    allowed = re.compile(
        rf"^[0-9A-Za-z+\-*/^().,=\s]{{1,{MAX_SYMBOLIC_LENGTH}}}$"
    )
    if not allowed.fullmatch(text):
        return None
    if any(
        len(token) > MAX_SYMBOLIC_INTEGER_DIGITS
        for token in re.findall(r"\d+", text)
    ):
        return None
    if len(re.findall(r"[+\-*/^=]", text)) > MAX_SYMBOLIC_OPERATORS:
        return None
    depth = 0
    for character in text:
        if character == "(":
            depth += 1
            if depth > MAX_SYMBOLIC_DEPTH:
                return None
        elif character == ")":
            depth -= 1
            if depth < 0:
                return None
    if depth != 0:
        return None
    for exponent in re.findall(r"\^\s*(\d+)", text):
        if int(exponent) > MAX_SYMBOLIC_EXPONENT:
            return None
    return text


@dataclass(frozen=True)
class ObjectiveGradingResult:
    score: Decimal | None
    confidence: float | None
    first_error_step: int | None
    reasons: tuple[str, ...]

    def as_tuple(self) -> tuple[Decimal | None, float | None, int | None, list[str]]:
        return self.score, self.confidence, self.first_error_step, list(self.reasons)


@dataclass(frozen=True)
class ObjectiveGradingService:
    normalize_text: TextNormalizer
    to_decimal: DecimalParser

    def _answer_set(self, value: Any) -> set[str]:
        if isinstance(value, list):
            return {self.normalize_text(item) for item in value if self.normalize_text(item)}
        return {
            item
            for item in re.split(r"[,，;；\s]+", self.normalize_text(value))
            if item
        }

    def _parse_numeric(self, value: Any) -> tuple[Decimal | None, str]:
        text = self.normalize_text(value)
        match = re.fullmatch(r"([+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:e[+-]?\d+)?)\s*(.*)", text)
        if not match:
            return None, ""
        return self.to_decimal(match.group(1)), match.group(2).strip()

    def symbolic_equivalent(self, student: Any, reference: Any) -> tuple[bool | None, float, str | None]:
        student_text = _safe_symbolic_text(student)
        reference_text = _safe_symbolic_text(reference)
        if student_text is None or reference_text is None:
            return None, 0.0, "unsafe_symbolic_input"
        return (
            self.normalize_text(student) == self.normalize_text(reference),
            0.75,
            "symbolic_equivalence_unavailable",
        )

    def score(
        self,
        question: dict[str, Any],
        response: dict[str, Any],
        symbolic_adapter: SymbolicAdapter | None = None,
    ) -> ObjectiveGradingResult:
        question_type = question["question_type"]
        reference = question.get("reference_answer")
        answer = response.get("normalized_answer")
        maximum = self.to_decimal(question.get("max_score")) or Decimal("0")
        reasons: list[str] = []
        if reference is None:
            return ObjectiveGradingResult(None, None, None, ("reference_answer_missing",))
        if answer is None or self.normalize_text(answer) == "":
            return ObjectiveGradingResult(Decimal("0"), 1.0, 1, ("unanswered",))

        confidence = 1.0
        first_error_step = None
        if question_type in {"single_choice", "true_false", "fill_blank"}:
            if isinstance(reference, list) and question_type == "fill_blank":
                expected = [self.normalize_text(item) for item in reference]
                actual = (
                    [self.normalize_text(item) for item in answer]
                    if isinstance(answer, list)
                    else [self.normalize_text(answer)]
                )
                correct = actual == expected
            else:
                correct = self.normalize_text(answer) == self.normalize_text(reference)
        elif question_type == "multiple_choice":
            correct = self._answer_set(answer) == self._answer_set(reference)
        elif question_type == "numeric":
            actual_number, actual_unit = self._parse_numeric(answer)
            expected_number, expected_unit = self._parse_numeric(reference)
            if actual_number is None or expected_number is None:
                return ObjectiveGradingResult(None, 0.50, None, ("numeric_parse_failed",))
            config = question.get("scoring_config") or {}
            tolerance = self.to_decimal(config.get("tolerance")) or Decimal("0")
            unit_required = bool(config.get("unit_required", bool(expected_unit)))
            number_ok = abs(actual_number - expected_number) <= tolerance
            unit_ok = (not unit_required) or actual_unit == expected_unit
            correct = number_ok and unit_ok
            if number_ok and not unit_ok:
                reasons.append("unit_mismatch")
        elif question_type == "formula":
            adapter = symbolic_adapter or self.symbolic_equivalent
            correct, confidence, adapter_reason = adapter(answer, reference)
            if adapter_reason:
                reasons.append(adapter_reason)
            if correct is None:
                return ObjectiveGradingResult(None, confidence, None, tuple(reasons))
        elif question_type == "ordered_steps":
            expected_steps = reference if isinstance(reference, list) else [reference]
            actual_steps = answer if isinstance(answer, list) else [answer]
            matches = 0
            for index, expected in enumerate(expected_steps):
                if index < len(actual_steps) and self.normalize_text(actual_steps[index]) == self.normalize_text(expected):
                    matches += 1
                else:
                    first_error_step = index + 1
                    break
            if first_error_step is None and len(actual_steps) != len(expected_steps):
                first_error_step = min(len(actual_steps), len(expected_steps)) + 1
            score = maximum * Decimal(matches) / Decimal(len(expected_steps)) if expected_steps else maximum
            return ObjectiveGradingResult(
                score.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP),
                confidence,
                first_error_step,
                tuple(reasons),
            )
        else:
            return ObjectiveGradingResult(None, None, None, ("subjective_requires_rubric",))

        return ObjectiveGradingResult(
            maximum if correct else Decimal("0"),
            confidence,
            1 if not correct else None,
            tuple(reasons),
        )
