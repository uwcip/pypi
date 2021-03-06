import collections
import hashlib
import json
import logging
import os.path
import re
import sys
from datetime import datetime
from typing import Any, Dict, Iterator, List, NamedTuple, Optional, Set, Tuple

import distlib.wheel
import jinja2
import packaging.utils
import packaging.version
import requests
from atomicwrites import atomic_write
from github import Github

logger = logging.getLogger(__name__)


def remove_extension(name: str) -> str:
    if name.endswith(("gz", "bz2")):
        name, _ = name.rsplit(".", 1)
    name, _ = name.rsplit(".", 1)
    return name


def guess_name_version_from_filename(filename: str) -> Tuple[str, Optional[str]]:
    if filename.endswith(".whl"):
        m = distlib.wheel.FILENAME_RE.match(filename)
        if m is not None:
            return m.group("nm"), m.group("vn")
        else:
            raise ValueError(f"invalid package name: {filename}")
    else:
        # These don't have a well-defined format like wheels do, so they are
        # sort of "best effort", with lots of tests to back them up.
        # The most important thing is to correctly parse the name.
        name = remove_extension(filename)
        version = None

        if "-" in name:
            if name.count("-") == 1:
                name, version = name.split("-")
            else:
                parts = name.split("-")
                for i in range(len(parts) - 1, 0, -1):
                    part = parts[i]
                    if "." in part and re.search(r"[0-9]", part):
                        name, version = "-".join(parts[0:i]), "-".join(parts[i:])

        # possible with poorly-named files
        if len(name) <= 0:
            raise ValueError(f"invalid package name: {filename}")

        return name, version


class Repository(NamedTuple):
    owner: str
    name: str


class Release(NamedTuple):
    filename: str
    url: str
    sha256: str
    uploaded_at: datetime
    uploaded_by: str


class Package(NamedTuple):
    filename: str
    name: str
    url: str
    sha256: str
    version: Optional[str]
    parsed_version: packaging.version.Version
    uploaded_at: Optional[datetime]
    uploaded_by: Optional[str]

    def __lt__(self, other: Tuple[Any, ...]) -> bool:
        return self.sort_key < other.sort_key

    def __str__(self) -> str:
        info = self.version or "unknown version"
        if self.uploaded_at is not None:
            info += f", {self.uploaded_at.strftime('%Y-%m-%d %H:%M:%S')}"
        if self.uploaded_by is not None:
            info += f", {self.uploaded_by}"
        return info

    @property
    def sort_key(self) -> Tuple[str, packaging.version.Version, str]:
        """Sort key for a filename."""
        return (
            self.name,
            self.parsed_version,

            # This looks ridiculous, but it's so that like extensions sort
            # together when the name and version are the same (otherwise it
            # depends on how the filename is normalized, which means sometimes
            # wheels sort before tarballs, but not always).
            # Alternatively we could just grab the extension, but that's less
            # amusing, even though it took 6 lines of comments to explain this.
            self.filename[::-1],
        )

    @classmethod
    def create(
            cls,
            *,
            filename: str,
            url: str,
            sha256: str,
            uploaded_at: Optional[int] = None,
            uploaded_by: Optional[str] = None,
    ) -> "Package":
        if not re.match(r"[a-zA-Z0-9_\-\.\+]+$", filename) or ".." in filename:
            raise ValueError(f"unsafe package name: {filename}")

        name, version = guess_name_version_from_filename(filename)
        return cls(
            filename=filename,
            name=packaging.utils.canonicalize_name(name),
            url=url,
            sha256=sha256,
            version=version,
            parsed_version=packaging.version.parse(version or "0"),
            uploaded_at=uploaded_at,
            uploaded_by=uploaded_by,
        )


def get_package_json(files: List[Package]) -> Dict[str, Any]:
    # https://warehouse.pypa.io/api-reference/json.html
    # note: the full api contains much more, we only output the info we have
    by_version: Dict[str, List[Dict[str, Any]]] = collections.defaultdict(list)

    latest = files[-1]
    for file in files:
        if file.version is not None:
            by_version[file.version].append({
                "filename": file.filename,
                "url": file.url,
            })

    return {
        "info": {
            "name": latest.name,
            "version": latest.version,
        },
        "releases": by_version,
        "urls": by_version[latest.version] if latest.version else [],
    }


def build(packages: Dict[str, Set[Package]], output: str, title: str) -> None:
    simple = os.path.join(output, "simple")
    pypi = os.path.join(output, "pypi")

    jinja_env = jinja2.Environment(
        loader=jinja2.PackageLoader("ghpypi", "templates"),
        autoescape=True,
    )
    jinja_env.globals["title"] = title

    # Sorting package versions is actually pretty expensive, so we do it once
    # at the start.
    sorted_packages = {name: sorted(files) for name, files in packages.items()}

    for package_name, sorted_files in sorted_packages.items():
        logger.info(f"processing {package_name} with {len(sorted_files)} packages")
        # /simple/{package}/index.html
        simple_package_dir = os.path.join(simple, package_name)
        os.makedirs(simple_package_dir, exist_ok=True)
        with atomic_write(os.path.join(simple_package_dir, "index.html"), overwrite=True) as f:
            f.write(jinja_env.get_template("package.html").render(
                package_name=package_name,
                files=sorted_files,
            ))

        # /pypi/{package}/json
        pypi_package_dir = os.path.join(pypi, package_name)
        os.makedirs(pypi_package_dir, exist_ok=True)
        with atomic_write(os.path.join(pypi_package_dir, "json"), overwrite=True) as f:
            json.dump(get_package_json(sorted_files), f)

    # /simple/index.html
    os.makedirs(simple, exist_ok=True)
    with atomic_write(os.path.join(simple, "index.html"), overwrite=True) as f:
        f.write(jinja_env.get_template("simple.html").render(
            package_names=sorted(sorted_packages),
        ))

    # /index.html
    with atomic_write(os.path.join(output, "index.html"), overwrite=True) as f:
        f.write(jinja_env.get_template("index.html").render(
            packages=sorted(
                (
                    package,
                    sorted_versions[-1].version,
                )
                for package, sorted_versions in sorted_packages.items()
            ),
        ))


def create_packages(releases: Iterator[Release]) -> Dict[str, Set[Package]]:
    packages: Dict[str, Set[Package]] = collections.defaultdict(set)
    for release in releases:
        try:
            package = Package.create(**release._asdict())
        except ValueError as e:
            logger.warning(f"{e} (skipping package)", file=sys.stderr)
        else:
            packages[package.name].add(package)

    return packages


def load_repositories(path: str) -> Iterator[Repository]:
    with open(path, "rt") as f:
        for line in f.read().splitlines():
            parts = line.split("/")
            if len(parts) == 2:
                logger.info(f"found repository: {line}")
                yield Repository(owner=parts[0], name=parts[1])
            else:
                raise ValueError(f"invalid repository name: {line}")


def get_github_token(token: str, token_stdin: bool) -> str:
    # if provided then use it
    if token is not None:
        return token

    # if we were told to look to stdin then look there
    if token_stdin:
        tokens = sys.stdin.read().splitlines()
        if len(tokens) and len(tokens[0]):
            return tokens[0]

    # if we didn't find it anywhere else then look for an environment variable
    token = os.environ.get("GITHUB_TOKEN")
    if token:
        return token

    # no token found anywhere
    raise ValueError("No value for GITHUB_TOKEN.")


def get_releases(token: str, repository: Repository) -> Iterator[Release]:
    logger.info(f"fetching releases for {repository.owner}/{repository.name}")

    g = Github(token)
    r = g.get_repo(f"{repository.owner}/{repository.name}")

    releases = r.get_releases()
    for release in releases:
        assets = release.raw_data.get("assets", [])
        if not len(assets):
            continue

        for asset in assets:
            name = asset["name"]
            url = asset["browser_download_url"]

            # we only want wheels and shasums
            if not name.endswith(".whl"):
                continue

            # download the url and get the sha256 sum
            sha256 = hashlib.sha256()
            data = requests.get(url, stream=True)
            data.raise_for_status()  # we only expect 200 responses
            for chunk in data.iter_content(chunk_size=1024):
                if chunk:
                    sha256.update(chunk)

            yield Release(
                filename=name,
                url=url,
                sha256=sha256.hexdigest(),
                uploaded_at=datetime.fromisoformat(asset["updated_at"].rstrip("Z")),
                uploaded_by=asset["uploader"]["login"],
            )


def run(repositories: str, output: str, token: str, token_stdin: bool, title: Optional[str] = None) -> int:
    try:
        packages = {}
        token = get_github_token(token, token_stdin)
        for repository in load_repositories(repositories):
            packages.update(create_packages(get_releases(token, repository)))

        # this actually spits out HTML files
        build(packages, output, title)

        return 0
    except Exception as e:
        logger.exception(str(e))
        return 1
