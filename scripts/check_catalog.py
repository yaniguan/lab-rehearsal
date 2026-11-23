"""Validate `catalog/simulators.json` SPDX/license governance and README URL integrity."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import urllib.error
import urllib.request

ALLOWED_SPDX = {"MIT", "Apache-2.0", "BSD-3-Clause", "NOASSERTION"}
ALLOWED_STATUS = {"verified_open", "no_license_file", "check_project_site"}
URL_RE = re.compile(r"https?://[^\s)>\]\"`]+")


def validate_catalog(repo_root: Path, *, online: bool = False) -> list[str]:
    errors: list[str] = []
    catalog_path = repo_root / "catalog" / "simulators.json"
    if not catalog_path.exists():
        return [f"missing catalog file: {catalog_path}"]

    data = json.loads(catalog_path.read_text(encoding="utf-8"))
    readme_text = (repo_root / "README.md").read_text(encoding="utf-8")

    seen_ids: set[str] = set()
    all_entries = list(data.get("datasets", [])) + list(data.get("repositories", []))
    if not all_entries:
        errors.append("catalog contains zero entries")

    for entry in all_entries:
        eid = entry.get("id")
        if not eid or eid in seen_ids:
            errors.append(f"duplicate or missing catalog id: {eid!r}")
        seen_ids.add(str(eid))

        spdx = entry.get("spdx_license")
        status = entry.get("license_status")
        comm_ok = entry.get("commercial_use_ok")
        redist_ok = entry.get("redistribution_ok")
        url = entry.get("url", "")

        if spdx not in ALLOWED_SPDX:
            errors.append(f"{eid}: unsupported spdx_license {spdx!r}")
        if status not in ALLOWED_STATUS:
            errors.append(f"{eid}: unsupported license_status {status!r}")
        if not isinstance(comm_ok, bool) or not isinstance(redist_ok, bool):
            errors.append(f"{eid}: commercial_use_ok and redistribution_ok must be booleans")
        if spdx == "NOASSERTION" and (comm_ok or redist_ok):
            errors.append(f"{eid}: unlicensed/NOASSERTION entry cannot set commercial_use_ok or redistribution_ok to true")
        if not str(url).startswith("https://"):
            errors.append(f"{eid}: URL must use https://, got {url!r}")
        if url not in readme_text:
            errors.append(f"{eid}: catalog URL {url} not found in README.md")

    # Extract all markdown URLs in README.md and docs/ and check well-formedness
    doc_files = [repo_root / "README.md"] + sorted((repo_root / "docs").glob("*.md"))
    discovered_urls: set[str] = set()
    for dpath in doc_files:
        for match in URL_RE.findall(dpath.read_text(encoding="utf-8")):
            clean = match.rstrip(".,;:")
            if not clean.startswith("https://"):
                errors.append(f"{dpath.name}: non-HTTPS URL {clean}")
            discovered_urls.add(clean)

    if online:
        for url in sorted(discovered_urls):
            req = urllib.request.Request(url, method="HEAD", headers={"User-Agent": "lab-rehearsal-ci/1.0"})
            try:
                with urllib.request.urlopen(req, timeout=10) as resp:
                    if resp.status >= 400:
                        errors.append(f"HTTP {resp.status} for {url}")
            except urllib.error.URLError as exc:
                errors.append(f"unreachable URL {url}: {exc}")

    return errors


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate simulator license catalog and markdown links.")
    parser.add_argument("--online", action="store_true", help="Probe remote URLs via HTTPS HEAD.")
    args = parser.parse_args()
    repo_root = Path(__file__).resolve().parents[1]
    errs = validate_catalog(repo_root, online=args.online)
    if errs:
        for err in errs:
            print(f"ERROR: {err}")
        return 1
    print("Catalog and markdown URLs validated cleanly.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
