"""
PMP Mock Test PDF Parser
------------------------
Reads 7 question PDFs + 7 answer PDFs and produces a single questions.json
with the structure the bot consumes:

[
  {
    "id": "T1Q1",
    "test_number": 1,
    "question_number": 1,
    "question_text": "...",
    "options": {"A": "...", "B": "...", "C": "...", "D": "..."},
    "correct": ["A"],            # list -> supports single & multi-select
    "is_multi": false,
    "explanation": "..."
  },
  ...
]
"""

import json
import re
import subprocess
from pathlib import Path

UPLOADS = Path("/mnt/user-data/uploads")
OUT_FILE = Path("/home/claude/pmp_bot/questions.json")

# Pair files: (test_number, questions_pdf, answers_pdf)
PAIRS = [
    (1, "Mock_Test_1___questions___2026-Q1.pdf", "Mock_Test_1__answers___2026-Q1.pdf"),
    (2, "Mock_Test_2___questions__2026-Q1.pdf",  "Mock_Test_2___answers__2026-Q1.pdf"),
    (3, "Mock_Test_3___questions__2026-Q1.pdf",  "Mock_Test_3___answers__2026-Q1.pdf"),
    (4, "Mock_Test_4___questions___2026-Q1.pdf", "Mock_Test_4___answers___2026-Q1.pdf"),
    (5, "Mock_Test_5___questions___2026-Q1.pdf", "Mock_Test_5___answers___2026-Q1.pdf"),
    (6, "Mock_Test_6___questions__2026-Q1.pdf",  "Mock_Test_6___answers__2026-Q1.pdf"),
    (7, "Mock_Test_7___questions__2026-Q1.pdf",  "Mock_Test_7___answers__2026-Q1.pdf"),
]


def pdf_to_text(path: Path) -> str:
    """Use pdftotext -layout for clean column-aware extraction."""
    result = subprocess.run(
        ["pdftotext", "-layout", str(path), "-"],
        capture_output=True, text=True, check=True,
    )
    return result.stdout


def split_by_question(text: str) -> dict[int, str]:
    """
    Split a PDF's full text into a dict {question_number: chunk_of_text}.
    Each PDF page (slide) is separated by \f and starts with 'Question N'.
    We use 'Question N' as the splitter since the form-feed is reliable.
    """
    chunks: dict[int, str] = {}
    # Split on form-feed (page break)
    pages = text.split("\f")
    for page in pages:
        page = page.strip()
        if not page:
            continue
        m = re.match(r"^Question\s+(\d+)\b", page)
        if not m:
            continue
        qnum = int(m.group(1))
        # Strip the "Question N" header itself
        body = page[m.end():].strip()
        chunks[qnum] = body
    return chunks


# Match an option line: starts with a letter (A-G), then '.' or ')' then space+text.
# Allowing letters up to G is generous but safe; PMP rarely exceeds E.
OPTION_RE = re.compile(r"^([A-G])\.\s+(.*)", re.MULTILINE)


def parse_question_chunk(chunk: str) -> tuple[str, dict[str, str]]:
    """
    Given the body of a question slide (header already stripped), return:
      question_text, options_dict
    """
    # Find the first option marker — everything before it is the question text.
    matches = list(OPTION_RE.finditer(chunk))
    if not matches:
        return chunk.strip(), {}

    first = matches[0]
    question_text = chunk[:first.start()].strip()

    options: dict[str, str] = {}
    for i, m in enumerate(matches):
        letter = m.group(1)
        # Capture text from this match's start to next match's start (or end)
        end = matches[i + 1].start() if i + 1 < len(matches) else len(chunk)
        full_line = chunk[m.start():end]
        # Drop the leading "X." and clean
        opt_text = full_line[2:].strip()
        # Collapse internal whitespace/newlines
        opt_text = re.sub(r"\s+", " ", opt_text).strip()
        options[letter] = opt_text

    # Clean question text — collapse whitespace but preserve paragraph breaks somewhat
    question_text = re.sub(r"[ \t]+", " ", question_text)
    question_text = re.sub(r"\n{2,}", "\n\n", question_text).strip()
    return question_text, options


# Match "Correct Answer: A" or "Correct Answer: A,B" or "Correct Answer: A, B, C"
ANSWER_RE = re.compile(r"Correct\s+Answer\s*:\s*([A-G](?:\s*,\s*[A-G])*)", re.IGNORECASE)


# Alternate format: "Option A: ..." sections each ending with "Correct" or "Incorrect"
OPTION_BLOCK_RE = re.compile(
    r"Option\s+([A-G])\s*:\s*(.*?)(?=(?:Option\s+[A-G]\s*:)|\Z)",
    re.IGNORECASE | re.DOTALL,
)


def parse_answer_chunk(chunk: str) -> tuple[list[str], str]:
    """Return (correct_letters, explanation)."""
    # --- Format 1: "Correct Answer: A" / "Correct Answer: A,B" ---
    am = ANSWER_RE.search(chunk)
    if am:
        letters_raw = am.group(1)
        correct = [c.strip().upper() for c in letters_raw.split(",")]
        expl_match = re.search(r"Explanation\s*:\s*(.*)", chunk, re.IGNORECASE | re.DOTALL)
        explanation = expl_match.group(1).strip() if expl_match else chunk[am.end():].strip()
        explanation = re.sub(r"[ \t]+", " ", explanation)
        explanation = re.sub(r"\n{3,}", "\n\n", explanation).strip()
        return correct, explanation

    # --- Format 2: "Option A: ... Correct/Incorrect" per option ---
    blocks = OPTION_BLOCK_RE.findall(chunk)
    if blocks:
        correct = []
        per_option_lines = []
        for letter, body in blocks:
            letter = letter.upper()
            body_clean = body.strip()
            # Look at the LAST "Correct" / "Incorrect" occurrence to avoid hits
            # inside the explanation prose.
            verdicts = list(re.finditer(r"\b(Correct|Incorrect)\b", body_clean))
            verdict = verdicts[-1].group(1).lower() if verdicts else ""
            if verdicts:
                body_clean = body_clean[:verdicts[-1].start()].rstrip()
            if verdict == "correct":
                correct.append(letter)
            per_option_lines.append(f"Option {letter}: {body_clean} [{verdict.capitalize() or '?'}]")

        explanation = "\n\n".join(per_option_lines)
        explanation = re.sub(r"[ \t]+", " ", explanation)
        explanation = re.sub(r"\n{3,}", "\n\n", explanation).strip()
        return correct, explanation

    return [], chunk.strip()


def process_pair(test_num: int, q_pdf: Path, a_pdf: Path) -> list[dict]:
    print(f"  Reading {q_pdf.name}")
    q_text = pdf_to_text(q_pdf)
    print(f"  Reading {a_pdf.name}")
    a_text = pdf_to_text(a_pdf)

    q_chunks = split_by_question(q_text)
    a_chunks = split_by_question(a_text)

    # Sanity check
    q_nums = set(q_chunks.keys())
    a_nums = set(a_chunks.keys())
    if q_nums != a_nums:
        missing_in_a = q_nums - a_nums
        missing_in_q = a_nums - q_nums
        print(f"  ⚠️  Mismatch! In questions but not answers: {sorted(missing_in_a)[:10]}")
        print(f"  ⚠️  In answers but not questions: {sorted(missing_in_q)[:10]}")

    common = sorted(q_nums & a_nums)
    questions: list[dict] = []
    issues = 0
    for qnum in common:
        q_text_clean, options = parse_question_chunk(q_chunks[qnum])
        correct, explanation = parse_answer_chunk(a_chunks[qnum])

        if not options:
            print(f"  ⚠️  T{test_num}Q{qnum}: no options parsed")
            issues += 1
            continue
        if not correct:
            print(f"  ⚠️  T{test_num}Q{qnum}: no correct answer parsed")
            issues += 1
            continue
        # Validate that every correct letter exists in the options
        unknown = [c for c in correct if c not in options]
        if unknown:
            print(f"  ⚠️  T{test_num}Q{qnum}: correct letters {unknown} not in options {list(options.keys())}")
            issues += 1
            continue

        questions.append({
            "id": f"T{test_num}Q{qnum}",
            "test_number": test_num,
            "question_number": qnum,
            "question_text": q_text_clean,
            "options": options,
            "correct": correct,
            "is_multi": len(correct) > 1,
            "explanation": explanation,
        })

    print(f"  ✓ Test {test_num}: {len(questions)} questions parsed, {issues} issues")
    return questions


def main():
    all_questions: list[dict] = []
    for test_num, q_name, a_name in PAIRS:
        print(f"\n=== Test {test_num} ===")
        q_pdf = UPLOADS / q_name
        a_pdf = UPLOADS / a_name
        if not q_pdf.exists():
            print(f"  ✗ Missing: {q_pdf}")
            continue
        if not a_pdf.exists():
            print(f"  ✗ Missing: {a_pdf}")
            continue
        all_questions.extend(process_pair(test_num, q_pdf, a_pdf))

    OUT_FILE.write_text(json.dumps(all_questions, indent=2, ensure_ascii=False))
    print(f"\n✓ Total questions saved: {len(all_questions)}")
    print(f"✓ Output: {OUT_FILE}")

    # Quick stats
    multi = sum(1 for q in all_questions if q["is_multi"])
    print(f"  Single-answer:   {len(all_questions) - multi}")
    print(f"  Multi-answer:    {multi}")
    by_test = {}
    for q in all_questions:
        by_test[q["test_number"]] = by_test.get(q["test_number"], 0) + 1
    for t in sorted(by_test):
        print(f"  Test {t}: {by_test[t]} questions")


if __name__ == "__main__":
    main()
