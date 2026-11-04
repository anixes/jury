"""
src/jury/deliberation/sarif.py - OASIS SARIF v2.1.0 Diagnostics Generator.

Generates standard OASIS SARIF v2.1.0 reports from jury deliberation findings,
enabling native inline problem markers and gutter diagnostics in Cursor, VS Code,
GitHub Code Scanning, and Antigravity IDE environments.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

from jury.deliberation.rubrics import Finding, SeverityLevel


SARIF_SCHEMA_URI = (
    "https://raw.githubusercontent.com/oasis-tcs/sarif-spec/master/Schemata/sarif-schema-2.1.0.json"
)
SARIF_VERSION = "2.1.0"


def severity_to_sarif_level(severity: SeverityLevel) -> str:
    """
    Map Jury severity to standard SARIF v2.1.0 notification levels:
    - CRITICAL, HIGH -> 'error'
    - MEDIUM -> 'warning'
    - LOW, INFO -> 'note'
    """
    sev = severity.upper().strip()
    if sev in ("CRITICAL", "HIGH"):
        return "error"
    elif sev == "MEDIUM":
        return "warning"
    else:
        return "note"


def generate_rule_id(category: str, index: int) -> str:
    """Generate structured SARIF rule ID, e.g. JURY-SEC-001."""
    cat = category.upper()
    prefix = "GEN"
    if "SEC" in cat:
        prefix = "SEC"
    elif "CONC" in cat or "RACE" in cat:
        prefix = "CONC"
    elif "PERF" in cat:
        prefix = "PERF"
    elif "ARCH" in cat:
        prefix = "ARCH"
    elif "BUG" in cat or "CORRECT" in cat:
        prefix = "BUG"

    return f"JURY-{prefix}-{index:03d}"


class SarifReport:
    """
    OASIS SARIF v2.1.0 Report Container.
    Transforms findings into standard static analysis results interchange format.
    """

    def __init__(
        self,
        findings: Optional[List[Finding]] = None,
        tool_name: str = "jury-mcp",
        tool_version: str = "3.0.0",
        information_uri: str = "https://github.com/jury-mcp/jury",
    ):
        self.tool_name = tool_name
        self.tool_version = tool_version
        self.information_uri = information_uri
        self.findings: List[Finding] = findings or []

    def add_finding(self, finding: Finding) -> None:
        """Add a finding to the report."""
        self.findings.append(finding)

    def to_dict(self) -> Dict[str, Any]:
        """
        Generate complete, schema-compliant OASIS SARIF v2.1.0 dictionary.
        """
        rules_dict: Dict[str, Dict[str, Any]] = {}
        results: List[Dict[str, Any]] = []

        for idx, finding in enumerate(self.findings, start=1):
            rule_id = generate_rule_id(finding.category, idx)
            sarif_level = severity_to_sarif_level(finding.severity)

            # Build SARIF rule definition
            if rule_id not in rules_dict:
                rule_desc = finding.issue[:150]
                rules_dict[rule_id] = {
                    "id": rule_id,
                    "name": f"{finding.category.title()}Defect",
                    "shortDescription": {"text": rule_desc},
                    "fullDescription": {"text": finding.issue},
                    "defaultConfiguration": {"level": sarif_level},
                    "properties": {
                        "category": finding.category,
                        "severity": finding.severity,
                    },
                }
                if finding.proof_or_refutation:
                    rules_dict[rule_id]["help"] = {"text": finding.proof_or_refutation}

            # Normalize file URI
            norm_path = finding.file_path.replace("\\", "/").lstrip("/")

            # Build physical location region
            region: Dict[str, int] = {
                "startLine": finding.line_start,
                "endLine": finding.line_end,
                "startColumn": 1,
            }

            result_entry: Dict[str, Any] = {
                "ruleId": rule_id,
                "level": sarif_level,
                "message": {"text": finding.issue},
                "locations": [
                    {
                        "physicalLocation": {
                            "artifactLocation": {
                                "uri": norm_path,
                                "uriBaseId": "%SRCROOT%",
                            },
                            "region": region,
                        }
                    }
                ],
            }

            # Optional automated fix replacement
            if finding.suggested_replacement:
                result_entry["fixes"] = [
                    {
                        "description": {"text": "Suggested replacement from Jury Mesh deliberation"},
                        "fileChanges": [
                            {
                                "artifactLocation": {"uri": norm_path},
                                "replacements": [
                                    {
                                        "deletedRegion": region,
                                        "insertedContent": {"text": finding.suggested_replacement},
                                    }
                                ],
                            }
                        ],
                    }
                ]

            results.append(result_entry)

        # Assemble full SARIF v2.1.0 document
        sarif_doc: Dict[str, Any] = {
            "$schema": SARIF_SCHEMA_URI,
            "version": SARIF_VERSION,
            "runs": [
                {
                    "tool": {
                        "driver": {
                            "name": self.tool_name,
                            "version": self.tool_version,
                            "informationUri": self.information_uri,
                            "rules": list(rules_dict.values()),
                        }
                    },
                    "results": results,
                }
            ],
        }

        return sarif_doc

    def to_json(self, indent: int = 2) -> str:
        """Serialize SARIF report to JSON string."""
        return json.dumps(self.to_dict(), indent=indent)

    def save_to_file(self, path: Union[str, Path]) -> Path:
        """Write SARIF report to target file path."""
        out_path = Path(path).resolve()
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(self.to_json(), encoding="utf-8")
        return out_path


def generate_sarif_report(
    findings: List[Finding],
    output_path: Optional[Union[str, Path]] = None,
    tool_name: str = "jury-mcp",
    tool_version: str = "3.0.0",
) -> Dict[str, Any]:
    """
    Generate and optionally persist an OASIS SARIF v2.1.0 report.
    """
    report = SarifReport(
        findings=findings,
        tool_name=tool_name,
        tool_version=tool_version,
    )
    sarif_dict = report.to_dict()

    if output_path:
        report.save_to_file(output_path)

    return sarif_dict
