"""
QA Report Generator — produces human-readable and machine-readable QA reports.

Takes the QAVerdict objects from the reviewer and writes:
1. qa_report.json — full structured results for every filing
2. qa_summary.txt — human-readable pass/fail summary
3. failures/ — individual JSON files for each failed filing with details
"""

import os
import json
import logging
from typing import List

from qa_reviewer.reviewer import QAVerdict

logger = logging.getLogger(__name__)


def generate_report(verdicts: List[QAVerdict], output_dir: str = "output"):
    """
    Generate all QA report files from a list of verdicts.

    Args:
        verdicts: List of QAVerdict objects, one per date sub-section reviewed
        output_dir: Directory to write reports into
    """
    os.makedirs(output_dir, exist_ok=True)
    failures_dir = os.path.join(output_dir, "failures")
    os.makedirs(failures_dir, exist_ok=True)

    # 1) qa_report.json — full structured data
    report_data = [v.to_dict() for v in verdicts]
    report_path = os.path.join(output_dir, "qa_report.json")
    with open(report_path, "w") as f:
        json.dump(report_data, f, indent=2)
    logger.info(f"QA report written to {report_path}")

    # 2) qa_summary.txt — human-readable summary
    total = len(verdicts)
    passed = sum(1 for v in verdicts if v.overall_pass)
    failed = total - passed
    summary_lines = [
        "=" * 60,
        "QA REVIEW SUMMARY",
        "=" * 60,
        f"Total sections reviewed: {total}",
        f"  PASSED: {passed}",
        f"  FAILED: {failed}",
        "",
    ]

    if failed > 0:
        summary_lines.append("FAILED SECTIONS:")
        summary_lines.append("-" * 40)
        for v in verdicts:
            if not v.overall_pass:
                layers = []
                if not v.section_check.passed:
                    layers.append("SECTION")
                if not v.completeness_check.passed:
                    layers.append("COMPLETENESS")
                if not v.accuracy_check.passed:
                    layers.append("ACCURACY")
                summary_lines.append(
                    f"  CIK={v.cik} acc={v.file_code} date={v.as_at_date} "
                    f"failed=[{', '.join(layers)}]"
                )
        summary_lines.append("")

    summary_lines.append("=" * 60)
    summary_text = "\n".join(summary_lines)

    summary_path = os.path.join(output_dir, "qa_summary.txt")
    with open(summary_path, "w") as f:
        f.write(summary_text)
    logger.info(f"QA summary written to {summary_path}")

    # Print summary to console too
    print(summary_text)

    # 3) Individual failure files
    for v in verdicts:
        if not v.overall_pass:
            fail_filename = f"{v.cik}_{v.file_code}_{v.as_at_date.replace(' ', '_').replace(',', '')}.json"
            fail_path = os.path.join(failures_dir, fail_filename)
            with open(fail_path, "w") as f:
                json.dump(v.to_dict(), f, indent=2)

    if failed > 0:
        logger.info(f"Failure details written to {failures_dir}/ ({failed} files)")
