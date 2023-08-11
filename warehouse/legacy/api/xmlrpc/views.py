# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import datetime
import functools
import re
import xmlrpc.client
import xmlrpc.server

from collections.abc import Mapping

from packaging.utils import canonicalize_name
from pydantic import StrictBool, StrictInt, StrictStr, ValidationError
from pydantic.decorator import ValidatedFunction
from pyramid.httpexceptions import HTTPMethodNotAllowed, HTTPTooManyRequests
from pyramid.view import view_config
from pyramid_rpc.mapper import MapplyViewMapper
from pyramid_rpc.xmlrpc import (
    XmlRpcApplicationError,
    XmlRpcError,
    XmlRpcInvalidMethodParams,
    exception_view as _exception_view,
    xmlrpc_method as _xmlrpc_method,
)
from sqlalchemy import func, orm, select
from sqlalchemy.exc import NoResultFound

from warehouse.accounts.models import User
from warehouse.classifiers.models import Classifier
from warehouse.metrics import IMetricsService
from warehouse.packaging.models import (
    File,
    JournalEntry,
    Project,
    Release,
    Role,
    release_classifiers,
)
from warehouse.rate_limiting import IRateLimiter

# From https://stackoverflow.com/a/22273639
_illegal_ranges = [
    "\x00-\x08",
    "\x0b-\x0c",
    "\x0e-\x1f",
    "\x7f-\x84",
    "\x86-\x9f",
    "\ufdd0-\ufddf",
    "\ufffe-\uffff",
    "\U0001fffe-\U0001ffff",
    "\U0002fffe-\U0002ffff",
    "\U0003fffe-\U0003ffff",
    "\U0004fffe-\U0004ffff",
    "\U0005fffe-\U0005ffff",
    "\U0006fffe-\U0006ffff",
    "\U0007fffe-\U0007ffff",
    "\U0008fffe-\U0008ffff",
    "\U0009fffe-\U0009ffff",
    "\U000afffe-\U000affff",
    "\U000bfffe-\U000bffff",
    "\U000cfffe-\U000cffff",
    "\U000dfffe-\U000dffff",
    "\U000efffe-\U000effff",
    "\U000ffffe-\U000fffff",
    "\U0010fffe-\U0010ffff",
]
_illegal_xml_chars_re = re.compile("[%s]" % "".join(_illegal_ranges))

XMLRPC_DEPRECATION_URL = (
    "https://warehouse.pypa.io/api-reference/xml-rpc.html#deprecated-methods"
)


def _clean_for_xml(data):
    """Sanitize any user-submitted data to ensure that it can be used in XML"""

    # If data is None or an empty string, don't bother
    if data:
        # This turns a string like "Hello…" into "Hello&#8230;"
        data = data.encode("ascii", "xmlcharrefreplace").decode("ascii")
        # However it's still possible that there are invalid characters in the string,
        # so simply remove any of those characters
        return _illegal_xml_chars_re.sub("", data)
    return data


def submit_xmlrpc_metrics(method=None):
    """
    Submit metrics.
    """

    def decorator(f):
        def wrapped(context, request):
            metrics = request.find_service(IMetricsService, context=None)
            metrics.increment("warehouse.xmlrpc.call", tags=[f"rpc_method:{method}"])
            with metrics.timed(
                "warehouse.xmlrpc.timing", tags=[f"rpc_method:{method}"]
            ):
                return f(context, request)

        return wrapped

    return decorator


def ratelimit():
    def decorator(f):
        def wrapped(context, request):
            ratelimiter = request.find_service(
                IRateLimiter, name="xmlrpc.client", context=None
            )
            metrics = request.find_service(IMetricsService, context=None)
            ratelimiter.hit(request.remote_addr)
            if not ratelimiter.test(request.remote_addr):
                metrics.increment("warehouse.xmlrpc.ratelimiter.exceeded", tags=[])
                message = (
                    "The action could not be performed because there were too "
                    "many requests by the client."
                )
                _resets_in = ratelimiter.resets_in(request.remote_addr)
                if _resets_in is not None:
                    _resets_in = max(1, int(_resets_in.total_seconds()))
                    message += f" Limit may reset in {_resets_in} seconds."
                raise XMLRPCWrappedError(HTTPTooManyRequests(message))
            metrics.increment("warehouse.xmlrpc.ratelimiter.hit", tags=[])
            return f(context, request)

        return wrapped

    return decorator


def xmlrpc_method(**kwargs):
    """
    Support multiple endpoints serving the same views by chaining calls to
    xmlrpc_method
    """
    # Add some default arguments
    kwargs.update(
        require_csrf=False,
        require_methods=["POST"],
        decorator=(submit_xmlrpc_metrics(method=kwargs["method"]), ratelimit()),
        mapper=TypedMapplyViewMapper,
    )

    def decorator(f):
        rpc2 = _xmlrpc_method(endpoint="xmlrpc.RPC2", **kwargs)
        pypi = _xmlrpc_method(endpoint="xmlrpc.pypi", **kwargs)
        pypi_slash = _xmlrpc_method(endpoint="xmlrpc.pypi_slash", **kwargs)
        return rpc2(pypi_slash(pypi(f)))

    return decorator


xmlrpc_cache_by_project = functools.partial(
    xmlrpc_method,
    xmlrpc_cache=True,
    xmlrpc_cache_expires=48 * 60 * 60,  # 48 hours
    xmlrpc_cache_tag="project/%s",
    xmlrpc_cache_arg_index=0,
    xmlrpc_cache_tag_processor=canonicalize_name,
)


xmlrpc_cache_all_projects = functools.partial(
    xmlrpc_method,
    xmlrpc_cache=True,
    xmlrpc_cache_expires=1 * 60 * 60,  # 1 hours
    xmlrpc_cache_tag="all-projects",
)


class XMLRPCServiceUnavailable(XmlRpcError):
    # NOQA due to N815 'mixedCase variable in class scope',
    # This is the interface for specifying fault code and string for XmlRpcError
    faultCode = -32403  # NOQA: ignore=N815
    faultString = "server error; service unavailable"  # NOQA: ignore=N815


class XMLRPCInvalidParamTypes(XmlRpcInvalidMethodParams):
    def __init__(self, exc):
        self.exc = exc

    # NOQA due to N802 'function name should be lowercase'
    # This is the interface for specifying fault string for XmlRpcError
    @property
    def faultString(self):  # NOQA: ignore=N802
        return f"client error; {self.exc}"


class XMLRPCWrappedError(xmlrpc.client.Fault):
    def __init__(self, exc):
        # NOQA due to N815 'mixedCase variable in class scope',
        # This is the interface for specifying fault code and string for XmlRpcError
        self.faultCode = -32500  # NOQA: ignore=N815
        self.wrapped_exception = exc  # NOQA: ignore=N815

    # NOQA due to N802 'function name should be lowercase'
    # This is the interface for specifying fault string for XmlRpcError
    @property
    def faultString(self):  # NOQA: ignore=N802
        return "{exc.__class__.__name__}: {exc}".format(exc=self.wrapped_exception)


class TypedMapplyViewMapper(MapplyViewMapper):
    def mapply(self, fn, args, kwargs):
        try:
            validated = ValidatedFunction(fn, None)
            values = validated.build_values(args, kwargs)
            validated.model(**values)
        except ValidationError as exc:
            raise XMLRPCInvalidParamTypes(
                "; ".join([f"{e['loc']}: {e['msg']}" for e in exc.errors()])
            )

        return super().mapply(fn, args, kwargs)


@view_config(route_name="xmlrpc.pypi", context=Exception, renderer="xmlrpc")
def exception_view(exc, request):
    if isinstance(exc, HTTPMethodNotAllowed):
        return XmlRpcApplicationError()
    return _exception_view(exc, request)


@xmlrpc_method(method="search")
def search(
    request,
    spec: Mapping[StrictStr, StrictStr | list[StrictStr]],
    operator: StrictStr = "and",
):
    domain = request.registry.settings.get("warehouse.domain", request.domain)
    raise XMLRPCWrappedError(
        RuntimeError(
            "PyPI no longer supports 'pip search' (or XML-RPC search). "
            f"Please use https://{domain}/search (via a browser) instead. "
            f"See {XMLRPC_DEPRECATION_URL} for more information."
        )
    )


@xmlrpc_cache_all_projects(method="list_packages")
def list_packages(request):
    names = request.db.query(Project.name).all()
    return [n[0] for n in names]


@xmlrpc_cache_all_projects(method="list_packages_with_serial")
def list_packages_with_serial(request):
    serials = request.db.query(Project.name, Project.last_serial).all()
    return {serial[0]: serial[1] for serial in serials}


@xmlrpc_method(method="package_hosting_mode")
def package_hosting_mode(request, package_name: StrictStr):
    return "pypi-only"


@xmlrpc_method(method="user_packages")
def user_packages(request, username: StrictStr):
    roles = (
        request.db.query(Role)
        .join(User)
        .join(Project)
        .filter(User.username == username)
        .order_by(Role.role_name.desc(), Project.name)
        .all()
    )
    return [(r.role_name, r.project.name) for r in roles]


@xmlrpc_method(method="top_packages")
def top_packages(request, num: StrictInt | None = None):
    raise XMLRPCWrappedError(
        RuntimeError(
            "This API has been removed. Use BigQuery instead. "
            f"See {XMLRPC_DEPRECATION_URL} for more information."
        )
    )


@xmlrpc_cache_by_project(method="package_releases")
def package_releases(request, package_name: StrictStr, show_hidden: StrictBool = False):
    try:
        project = (
            request.db.query(Project)
            .filter(Project.normalized_name == func.normalize_pep426_name(package_name))
            .one()
        )
    except NoResultFound:
        return []

    # This used to support the show_hidden parameter to determine if it should
    # show hidden releases or not. However, Warehouse doesn't support the
    # concept of hidden releases, so this parameter controls if the latest
    # version or all_versions are returned.
    if show_hidden:
        return [v.version for v in project.all_versions]
    else:
        latest_version = project.latest_version
        if latest_version is None:
            return []
        return [latest_version.version]


@xmlrpc_method(method="package_data")
def package_data(request, package_name, version):
    raise XMLRPCWrappedError(
        RuntimeError(
            "This API has been deprecated. "
            f"See {XMLRPC_DEPRECATION_URL} for more information."
        )
    )


@xmlrpc_cache_by_project(method="release_data")
def release_data(request, package_name: StrictStr, version: StrictStr):
    try:
        release = (
            request.db.query(Release)
            .options(orm.joinedload(Release.description))
            .join(Project)
            .filter(
                (Project.normalized_name == func.normalize_pep426_name(package_name))
                & (Release.version == version)
            )
            .one()
        )
    except NoResultFound:
        return {}

    return {
        "name": release.project.name,
        "version": release.version,
        "stable_version": None,
        "bugtrack_url": None,
        "package_url": request.route_url(
            "packaging.project", name=release.project.name
        ),
        "release_url": request.route_url(
            "packaging.release", name=release.project.name, version=release.version
        ),
        "docs_url": _clean_for_xml(release.project.documentation_url),
        "home_page": _clean_for_xml(release.home_page),
        "download_url": _clean_for_xml(release.download_url),
        "project_url": [
            _clean_for_xml(f"{label}, {url}")
            for label, url in release.project_urls.items()
        ],
        "author": _clean_for_xml(release.author),
        "author_email": _clean_for_xml(release.author_email),
        "maintainer": _clean_for_xml(release.maintainer),
        "maintainer_email": _clean_for_xml(release.maintainer_email),
        "summary": _clean_for_xml(release.summary),
        "description": _clean_for_xml(release.description.raw),
        "license": _clean_for_xml(release.license),
        "keywords": _clean_for_xml(release.keywords),
        "platform": release.platform,
        "classifiers": list(release.classifiers),
        "requires": list(release.requires),
        "requires_dist": list(release.requires_dist),
        "provides": list(release.provides),
        "provides_dist": list(release.provides_dist),
        "obsoletes": list(release.obsoletes),
        "obsoletes_dist": list(release.obsoletes_dist),
        "requires_python": release.requires_python,
        "requires_external": list(release.requires_external),
        "_pypi_ordering": release._pypi_ordering,
        "downloads": {"last_day": -1, "last_week": -1, "last_month": -1},
        "cheesecake_code_kwalitee_id": None,
        "cheesecake_documentation_id": None,
        "cheesecake_installability_id": None,
    }


@xmlrpc_method(method="package_urls")
def package_urls(request, package_name, version):
    raise XMLRPCWrappedError(
        RuntimeError(
            "This API has been deprecated. "
            f"See {XMLRPC_DEPRECATION_URL} for more information."
        )
    )


@xmlrpc_cache_by_project(method="release_urls")
def release_urls(request, package_name: StrictStr, version: StrictStr):
    files = (
        request.db.query(File)
        .join(Release)
        .join(Project)
        .filter(
            (Project.normalized_name == func.normalize_pep426_name(package_name))
            & (Release.version == version)
        )
        .all()
    )

    return [
        {
            "filename": f.filename,
            "packagetype": f.packagetype,
            "python_version": f.python_version,
            "size": f.size,
            "md5_digest": f.md5_digest,
            "sha256_digest": f.sha256_digest,
            "digests": {"md5": f.md5_digest, "sha256": f.sha256_digest},
            # TODO: Remove this once we've had a long enough time with it
            #       here to consider it no longer in use.
            "has_sig": False,
            "upload_time": f.upload_time.isoformat() + "Z",
            "upload_time_iso_8601": f.upload_time.isoformat() + "Z",
            "comment_text": f.comment_text,
            # TODO: Remove this once we've had a long enough time with it
            #       here to consider it no longer in use.
            "downloads": -1,
            "path": f.path,
            "url": request.route_url("packaging.file", path=f.path),
        }
        for f in files
    ]


@xmlrpc_cache_by_project(method="package_roles")
def package_roles(request, package_name: StrictStr):
    roles = (
        request.db.query(Role)
        .join(User)
        .join(Project)
        .filter(Project.normalized_name == func.normalize_pep426_name(package_name))
        .order_by(Role.role_name.desc(), User.username)
        .all()
    )
    return [(r.role_name, r.user.username) for r in roles]


@xmlrpc_method(method="changelog_last_serial")
def changelog_last_serial(request):
    return request.db.query(func.max(JournalEntry.id)).scalar()


@xmlrpc_method(method="changelog_since_serial")
def changelog_since_serial(request, serial: StrictInt):
    entries = (
        request.db.query(JournalEntry)
        .filter(JournalEntry.id > serial)
        .order_by(JournalEntry.id)
        .limit(50000)
    )

    return [
        (
            e.name,
            e.version,
            int(e.submitted_date.replace(tzinfo=datetime.UTC).timestamp()),
            _clean_for_xml(e.action),
            e.id,
        )
        for e in entries
    ]


@xmlrpc_method(method="changelog")
def changelog(request, since: StrictInt, with_ids: StrictBool = False):
    since_dt = datetime.datetime.utcfromtimestamp(since)
    entries = (
        request.db.query(JournalEntry)
        .filter(JournalEntry.submitted_date > since_dt)
        .order_by(JournalEntry.id)
        .limit(50000)
    )

    results = (
        (
            e.name,
            e.version,
            int(e.submitted_date.replace(tzinfo=datetime.UTC).timestamp()),
            e.action,
            e.id,
        )
        for e in entries
    )

    if with_ids:
        return list(results)
    else:
        return [r[:-1] for r in results]


@xmlrpc_method(method="browse")
def browse(request, classifiers: list[StrictStr]):
    classifiers_q = (
        request.db.query(Classifier)
        .filter(Classifier.classifier.in_(classifiers))
        .subquery()
    )

    release_classifiers_q = (
        select(release_classifiers)
        .where(release_classifiers.c.trove_id == classifiers_q.c.id)
        .alias("rc")
    )

    releases = (
        request.db.query(Project.name, Release.version)
        .join(Release)
        .join(release_classifiers_q, Release.id == release_classifiers_q.c.release_id)
        .group_by(Project.name, Release.version)
        .having(func.count() == len(classifiers))
        .order_by(Project.name, Release.version)
        .all()
    )

    return [(r.name, r.version) for r in releases]


@xmlrpc_method(method="system.multicall")
def multicall(request, args):
    raise XMLRPCWrappedError(
        ValueError(
            "MultiCall requests have been deprecated, use individual "
            "requests instead."
        )
    )
