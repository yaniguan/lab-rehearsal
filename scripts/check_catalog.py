"""Validate `catalog/simulators.json` SPDX/license governance and README URL integrity."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import urllib.error
import urllib.request

ALLOWED_SPDX = {
    "MIT",
    "Apache-2.0",
    "BSD-3-Clause",
    "CC-BY-4.0",
    "CC-BY-SA-4.0",
    "CC-BY-NC-4.0",
    "LicenseRef-FineBio-NC",
    "NOASSERTION",
}
NON_COMMERCIAL_SPDX = {
    "CC-BY-NC-4.0",
    "LicenseRef-FineBio-NC",
    "NOASSERTION",
}
ALLOWED_STATUS = {
    "verified_open",
    "verified_non_commercial",
    "non_commercial_agreement",
    "no_license_file",
    "check_project_site",
}
RESTRICTED_REDIST_STATUS = {
    "non_commercial_agreement",
    "no_license_file",
    "check_project_site",
}
URL_RE = re.compile(r"https?://[^\s)>\]\"`]+")


def _probe_url(url: str, timeout: float = 10.0) -> str | None:
    headers = {"User-Agent": "lab-rehearsal-ci/1.0"}
    req = urllib.request.Request(url, method="HEAD", headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            if resp.status >= 400:
                return f"HTTP {resp.status} for {url}"
            return None
    except urllib.error.HTTPError as exc:
        if exc.code in (403, 405):
            get_req = urllib.request.Request(url, method="GET", headers=headers)
            try:
                with urllib.request.urlopen(get_req, timeout=timeout) as resp:
                    if resp.status >= 400:
                        return f"HTTP {resp.status} for {url}"
                    return None
            except urllib.error.URLError as get_exc:
                return f"unreachable URL {url}: {get_exc}"
        return f"HTTP {exc.code} for {url}"
    except urllib.error.URLError as exc:
        return f"unreachable URL {url}: {exc}"


def validate_catalog(repo_root: Path, *, online: bool = False) -> list[str]:
    errors: list[str] = []
    catalog_path = repo_root / "catalog" / "simulators.json"
    if not catalog_path.exists():
        return [f"missing catalog file: {catalog_path}"]

    data = json.loads(catalog_path.read_text(encoding="utf-8"))
    readme_text = (repo_root / "README.md").read_text(encoding="utf-8")

    seen_ids: set[str] = set()
    datasets = list(data.get("datasets", []))
    repositories = list(data.get("repositories", []))
    all_entries = datasets + repositories
    if not all_entries:
        errors.append("catalog contains zero entries")

    for entry in datasets:
        eid = entry.get("id")
        if not isinstance(entry.get("eval_only"), bool):
            errors.append(f"{eid}: dataset entry must declare boolean 'eval_only'")

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
        paper_url = entry.get("paper_url")

        if spdx not in ALLOWED_SPDX:
            errors.append(f"{eid}: unsupported spdx_license {spdx!r}")
        if status not in ALLOWED_STATUS:
            errors.append(f"{eid}: unsupported license_status {status!r}")
        if not isinstance(comm_ok, bool) or not isinstance(redist_ok, bool):
            errors.append(f"{eid}: commercial_use_ok and redistribution_ok must be booleans")
        if spdx in NON_COMMERCIAL_SPDX and comm_ok:
            errors.append(f"{eid}: non-commercial or NOASSERTION entry cannot set commercial_use_ok to true")
        if spdx == "NOASSERTION" and (comm_ok or redist_ok):
            errors.append(
                f"{eid}: unlicensed/NOASSERTION entry cannot set commercial_use_ok or redistribution_ok to true"
            )
        if status in RESTRICTED_REDIST_STATUS and (comm_ok or redist_ok):
            errors.append(
                f"{eid}: status {status!r} cannot set commercial_use_ok or redistribution_ok to true"
            )
        if not str(url).startswith("https://"):
            errors.append(f"{eid}: URL must use https://, got {url!r}")
        if paper_url is not None and not str(paper_url).startswith("https://"):
            errors.append(f"{eid}: paper_url must use https://, got {paper_url!r}")
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

    for entry in all_entries:
        for key in ("url", "paper_url"):
            val = entry.get(key)
            if isinstance(val, str) and val.startswith("https://"):
                discovered_urls.add(val)

    if online:
        for url in sorted(discovered_urls):
            err = _probe_url(url)
            if err:
                errors.append(err)

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
