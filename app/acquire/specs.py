"""Per-source AcquisitionSpecs. Real endpoints match architecture-v2.md
§5.1's "Per-source current best forms" table; demo profiles are small
enough to complete in well under 2 minutes even against a real, reachable
mirror. Tests point an equivalent HTTPS spec at a local mock server instead
(see tests/acquire/) — never at these real endpoints (non-goal: real origin
traffic in tests).
"""
from pathlib import Path

from ..plugins.base import LicenseClass
from .spec import AcquisitionSpec, ChecksumSource, DeltaSpec, Endpoint, Filter, Transport

GUTENBERG_SPEC = AcquisitionSpec(
    source_id="gutenberg",
    transport=Transport.RSYNC,
    endpoints=(
        Endpoint(transport=Transport.RSYNC,
                 url="rsync.mirrorservice.org::gutenberg.org/cache/epub"),
        Endpoint(transport=Transport.RSYNC, url="aleph.gutenberg.org::gutenberg-epub"),
    ),
    layout="one-dir-per-doc",
    filters=("*.txt*", "*.rdf", "!*mbt-*", "!*.mobi", "!*.kf8*"),
    checksums=ChecksumSource(kind="none"),   # PG publishes no sidecar; blake2 always applies
    delta=DeltaSpec(mode="full-resync"),
    profiles={
        "demo": Filter(include=("1/**", "2/**", "3/**"), max_files=3, max_bytes=5_000_000),
        "starter": Filter(max_files=500, max_bytes=2_000_000_000),
        "recent": Filter(max_files=5000),
        "full": Filter(),
    },
    estimated_bytes={
        "demo": 2_000_000, "starter": 2_000_000_000,
        "recent": 20_000_000_000, "full": 80_000_000_000,
    },
    license_class=LicenseClass.GREEN,
)

PUBMED_SPEC = AcquisitionSpec(
    source_id="pubmed",
    transport=Transport.FTP,
    endpoints=(
        Endpoint(transport=Transport.FTP, url="ftp.ncbi.nlm.nih.gov/pubmed/baseline"),
    ),
    layout="container-files",
    filters=("*.xml.gz", "*.xml.gz.md5"),
    checksums=ChecksumSource(kind="sidecar", sidecar_suffix=".md5", sidecar_algo="md5"),
    delta=DeltaSpec(mode="updatefiles-dir", updatefiles_subdir="updatefiles"),
    profiles={
        # one baseline container + its .md5 sidecar; real baseline files run
        # 9-30 MB (the original 10 MB cap couldn't admit a single one)
        "demo": Filter(max_files=2, max_bytes=40_000_000),
        "starter": Filter(max_files=50),
        "recent": Filter(max_files=200),
        "full": Filter(),
    },
    estimated_bytes={
        "demo": 40_000_000, "starter": 500_000_000,
        "recent": 5_000_000_000, "full": 150_000_000_000,
    },
    license_class=LicenseClass.YELLOW,   # publisher copyright on text; raw is pointer-only (§8)
    terms_text_path=Path(__file__).parent.parent / "plugins" / "pubmed" / "TERMS.txt",
)

ACQUIRE_REGISTRY: dict[str, AcquisitionSpec] = {
    "gutenberg": GUTENBERG_SPEC,
    "pubmed": PUBMED_SPEC,
}
