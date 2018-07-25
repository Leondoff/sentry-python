from __future__ import absolute_import

from threading import Lock

from django.conf import settings
from django.core import signals

try:
    from django.urls import resolve
except ImportError:
    from django.core.urlresolvers import resolve

from sentry_sdk import get_current_hub, configure_scope, capture_exception, init
from .._wsgi import RequestExtractor
from .. import Integration


try:
    # Django >= 1.10
    from django.utils.deprecation import MiddlewareMixin
except ImportError:
    # Not required for Django <= 1.9, see:
    # https://docs.djangoproject.com/en/1.10/topics/http/middleware/#upgrading-pre-django-1-10-style-middleware
    MiddlewareMixin = object


def _get_transaction_from_request(request):
    return resolve(request.path).func.__name__


# request_started (or any other signal) cannot be used because the request is
# not yet available
class SentryMiddleware(MiddlewareMixin):
    def process_request(self, request):
        try:
            get_current_hub().push_scope()

            get_current_hub().add_event_processor(
                lambda: self.make_event_processor(request)
            )

            with configure_scope() as scope:
                scope.transaction = _get_transaction_from_request(request)
        except Exception:
            get_current_hub().capture_internal_exception()

    def make_event_processor(self, request):
        def processor(event):
            try:
                DjangoRequestExtractor(request).extract_into_event(event)
            except Exception:
                get_current_hub().capture_internal_exception()

            # TODO: user info

        return processor


class DjangoRequestExtractor(RequestExtractor):
    @property
    def url(self):
        return self.request.build_absolute_uri(self.request.path)

    @property
    def env(self):
        return self.request.META

    @property
    def cookies(self):
        return self.request.COOKIES

    @property
    def raw_data(self):
        return self.request.body

    @property
    def form(self):
        return self.request.POST

    @property
    def files(self):
        return self.request.FILES

    def size_of_file(self, file):
        return file.size


def _request_finished(*args, **kwargs):
    get_current_hub().pop_scope_unsafe()


def _got_request_exception(request=None, **kwargs):
    capture_exception()


MIDDLEWARE_NAME = "sentry_sdk.integrations.django.SentryMiddleware"

CONFLICTING_MIDDLEWARE = (
    "raven.contrib.django.middleware.SentryMiddleware",
    "raven.contrib.django.middleware.SentryLogMiddleware",
) + (MIDDLEWARE_NAME,)

_installer_lock = Lock()
_installed = False


def _install():
    global _installed
    with _installer_lock:
        if _installed:
            return

        client_options, integration_options = DjangoIntegration.parse_environment(
            dict((key, getattr(settings, key)) for key in dir(settings))
        )

        client_options.setdefault("integrations", []).append(
            DjangoIntegration(**integration_options)
        )

        init(**client_options)
        _installed = True


def _install_impl():
    # default settings.MIDDLEWARE is None
    if getattr(settings, "MIDDLEWARE", None):
        middleware_attr = "MIDDLEWARE"
    else:
        middleware_attr = "MIDDLEWARE_CLASSES"

    # make sure to get an empty tuple when attr is None
    middleware = list(getattr(settings, middleware_attr, ()) or ())
    conflicts = set(CONFLICTING_MIDDLEWARE).intersection(set(middleware))
    if conflicts:
        raise RuntimeError("Other sentry-middleware already registered: %s" % conflicts)

    setattr(settings, middleware_attr, [MIDDLEWARE_NAME] + middleware)

    signals.request_finished.connect(_request_finished)
    signals.got_request_exception.connect(_got_request_exception)


class DjangoIntegration(Integration):
    identifier = "django"

    def install(self, client):
        _install_impl()


try:
    # Django >= 1.7
    from django.apps import AppConfig
except ImportError:
    _install()
else:

    class SentryConfig(AppConfig):
        name = "sentry_sdk.integrations.django"
        label = "sentry_sdk_integrations_django"
        verbose_name = "Sentry"

        def ready(self):
            _install()

    default_app_config = "sentry_sdk.integrations.django.SentryConfig"